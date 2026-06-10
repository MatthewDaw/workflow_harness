import { randomUUID } from 'node:crypto';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  workflowSchema,
  orgScope,
  workflowRunNodeStateSchema,
  type WorkflowRun,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  created,
  defaultRepo,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  queryParam,
  unauthorized,
} from './runtime.js';
import { requireOrgCatalogAuth } from './scopeauth.js';
import { makeCatalogHandlers } from './catalogResource.js';

/**
 * REST: workflows — collapsed to a single ORG catalog (mirrors agents.ts).
 *
 *   GET    /workflows              — the caller's org catalog
 *   GET    /workflows/:name        — one workflow from the org catalog
 *   POST   /workflows              — create (server forces org scope + createdBy; admin)
 *   PUT    /workflows/:name        — update (scope/createdBy immutable; admin)
 *   DELETE /workflows/:name        — delete (admin)
 *   POST   /workflows/:name/promote — repoint the org-wide TRUE variant (NOT admin-gated)
 *
 * A workflow is a DAG of catalog agents: each node references an agent by name and
 * carries its own `dependsOn` edges (encoded per node, not a separate edges array).
 * Unlike skills/agents there is no bundle concept in v1, so there is no
 * members/dissolve surface — `kind` is the single literal `'workflow'`.
 *
 * POST/PUT parse the body through `workflowSchema` (whose `superRefine` enforces
 * unique node ids, resolvable `dependsOn`/`declaredBy` refs, and acyclicity), and GET
 * returns the full record. The wrapper materializes the workflow spec as JSON; this
 * REST layer stores/serves it unchanged.
 */

export interface WorkflowsDeps {
  repo: Repo;
}

const handlers = makeCatalogHandlers({
  kind: 'WORKFLOW',
  schema: workflowSchema,
  label: 'workflow',
  responseKey: 'workflow',
  listKey: 'workflows',
  seedHint: 'catalog/workflows',
  repoOps: {
    list: (repo, org, userId) => repo.listWorkflows(org, userId),
    get: (repo, scope, name) => repo.getWorkflow(scope, name),
    del: (repo, scope, name) => repo.deleteWorkflow(scope, name),
  },
});

export const resolveWorkflows = handlers.list;
export const createWorkflow = handlers.create;
export const getWorkflow = handlers.get;
export const deleteWorkflow = handlers.remove;
/** Promote is NOT admin-gated (any authed member may repoint TRUE). */
export const promoteWorkflow = handlers.promote;

/**
 * WORKFLOW RUNS (M5) — the live execution-status surface the Go executor reports
 * to and the web Workflows tab polls. A run is NOT a versioned catalog item: it is
 * a transient per-project record under `WORKFLOWRUN#<runId>` (see repo). These
 * handlers are auth'd via the shared org resolver (the executor carries the same
 * device token), but they are NOT admin-gated — running a workflow is a member
 * action, like enabling one on a project.
 */

/**
 * POST /workflows/:name/runs — create a run. Body carries `projectId`; the runId
 * is minted server-side. Node states init to `pending` so the DAG view has an
 * entry per node immediately; the executor flips them as the run progresses.
 */
export async function createWorkflowRun(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  if (!gate.auth.org) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const projectId = (body as { projectId?: unknown })?.projectId;
  if (typeof projectId !== 'string' || !projectId) return badRequest('missing projectId');

  const workflow = await deps.repo.getWorkflow(orgScope(gate.auth.org), name);
  if (!workflow) return notFound();

  // Init one node entry per DAG node, all `pending`, so the web overlay has a
  // complete map before the executor reports its first transition.
  const nodes: WorkflowRun['nodes'] = {};
  for (const node of workflow.nodes) {
    nodes[node.id] = { state: 'pending', runs: 0, outputTail: '' };
  }
  const run: WorkflowRun = {
    runId: randomUUID(),
    workflowName: name,
    projectId,
    status: 'running',
    nodes,
    startedAt: Date.now(),
  };
  await deps.repo.putWorkflowRun(run);
  return created({ run });
}

/** GET /workflows/:name/runs/:runId — read one run's live status. */
export async function getWorkflowRun(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  const runId = pathParam(event, 'runId');
  const projectId = queryParam(event, 'projectId');
  if (!runId) return badRequest('missing runId');
  if (!projectId) return badRequest('missing projectId');
  const run = await deps.repo.getWorkflowRun(projectId, runId);
  if (!run) return notFound();
  return ok({ run });
}

/** GET /workflows/:name/runs?projectId=… — list a project's runs. */
export async function listWorkflowRuns(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  const projectId = queryParam(event, 'projectId');
  if (!projectId) return badRequest('missing projectId');
  const runs = await deps.repo.listWorkflowRuns(projectId);
  return ok({ runs });
}

/**
 * POST /workflows/:name/runs/:runId/nodes/:nodeId — the executor reports a node
 * transition. Body carries any of `state`/`runs`/`outputTail`; the repo merges
 * them onto the node's current slice (read-modify-write).
 */
export async function updateWorkflowRunNode(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  const runId = pathParam(event, 'runId');
  const nodeId = pathParam(event, 'nodeId');
  if (!runId || !nodeId) return badRequest('missing runId or nodeId');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const b = (body ?? {}) as Record<string, unknown>;
  const projectId = b.projectId;
  if (typeof projectId !== 'string' || !projectId) return badRequest('missing projectId');

  // Only forward the fields the body actually carries, validating `state`.
  const partial: Partial<{
    state: WorkflowRun['nodes'][string]['state'];
    runs: number;
    outputTail: string;
  }> = {};
  if (b.state !== undefined) {
    const parsed = workflowRunNodeStateSchema.safeParse(b.state);
    if (!parsed.success) return badRequest('invalid state');
    partial.state = parsed.data;
  }
  if (typeof b.runs === 'number') partial.runs = b.runs;
  if (typeof b.outputTail === 'string') partial.outputTail = b.outputTail;

  const run = await deps.repo.updateWorkflowRunNode(projectId, runId, nodeId, partial);
  if (!run) return notFound();
  return ok({ run });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: WorkflowsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;

  // Run sub-routes (M5). Matched BEFORE the generic CRUD branches because they
  // share the `/workflows/{name}` prefix; the run/node ids are path-tail segments
  // handled inside the handlers (the agents.ts promote/members precedent).
  if (method === 'POST' && pathParam(event, 'nodeId')) return updateWorkflowRunNode(event, deps);
  if (method === 'GET' && pathParam(event, 'runId')) return getWorkflowRun(event, deps);
  if (path.endsWith('/runs')) {
    if (method === 'POST') return createWorkflowRun(event, deps);
    if (method === 'GET') return listWorkflowRuns(event, deps);
  }

  return handlers.dispatch(event, deps);
}
