import { randomUUID } from 'node:crypto';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  workflowSchema,
  orgScope,
  workflowRunNodeStateSchema,
  type Workflow,
  type WorkflowRun,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  conflict,
  created,
  defaultRepo,
  forbidden,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  queryParam,
  unauthorized,
} from './runtime.js';
import { isBuiltin, resolveOrgCatalogAuth } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';
import { withAuthorNames } from './authorNames.js';

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

export async function resolveWorkflows(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer route).
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ workflows: [] });
  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped workflow shadows an org-scoped one of the same name).
  const workflows = await deps.repo.listWorkflows(org, principal.userId);
  // Show the author's real name (their email) instead of the raw Cognito sub that
  // claude+ device-token writes stamp into createdBy.name.
  return ok({ workflows: await withAuthorNames(deps.repo, workflows) });
}

export async function createWorkflow(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer
  // route); admin is decided server-side from the profile so the device token
  // (no role claim) can write.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  if (!auth.org) return unauthorized();
  const { principal, org } = auth;

  const name = pathParam(event, 'name');
  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = workflowSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const workflow: Workflow = parsed.data;

  // Canonical built-ins are owned by the git seed: reject an in-place write to the
  // BASE variant (no repo/author). Forking (repoId + authorUserId) is still allowed.
  const targetName = name ?? workflow.name;
  const existing = await deps.repo.getWorkflow(orgScope(org), targetName);
  if (isBuiltin(existing) && !workflow.repoId && !workflow.authorUserId) {
    return conflict(
      `"${targetName}" is a canonical built-in workflow — fork it (set repoId + authorUserId) ` +
        `or change it in catalog/workflows and re-seed; in-place writes are rejected.`,
    );
  }

  if (name) {
    // PUT /workflows/:name — update; preserve the existing createdBy stamp.
    workflow.createdBy = existing?.createdBy ?? workflow.createdBy;
    workflow.baseName = existing?.baseName ?? workflow.baseName ?? name;
  } else {
    workflow.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    workflow.baseName = workflow.baseName ?? workflow.name;
  }

  // VERSIONING (KTD6): snapshot a revision + fork/advance the variant instead of
  // clobbering; `putNewVersion` also upserts the live record under `workflowKey`.
  const stamped = await deps.repo.putNewVersion('WORKFLOW', workflow, {
    repoId: workflow.repoId,
    authorUserId: workflow.authorUserId,
  });
  return name ? ok({ workflow: stamped }) : created({ workflow: stamped });
}

/**
 * POST /workflows/:name/promote — repoint the org-wide TRUE variant for a baseName.
 * NOT admin-gated (any authed member). Body: `{ variantId, rev? }`. Mirrors
 * agents' promote: it ONLY repoints TRUE, never editing/deleting a variant.
 */
export async function promoteWorkflow(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Promote is NOT admin-gated (any authed org member may repoint TRUE), but it
  // still accepts the device token via the shared resolver.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const org = auth.org;

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const variantId = (body as { variantId?: unknown })?.variantId;
  if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
  const revRaw = (body as { rev?: unknown })?.rev;
  const rev = typeof revRaw === 'number' ? revRaw : undefined;

  const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
  await deps.repo.setTrueVariant(orgScope(org), 'WORKFLOW', pointer);
  return ok({ true: pointer });
}

export async function getWorkflow(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return notFound();
  const workflow = await deps.repo.getWorkflow(orgScope(auth.org), name);
  if (!workflow) return notFound();
  return ok({ workflow });
}

export async function deleteWorkflow(
  event: APIGatewayProxyEventV2,
  deps: WorkflowsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const existing = await deps.repo.getWorkflow(orgScope(auth.org), name);
  if (isBuiltin(existing)) {
    return conflict(
      `"${name}" is a canonical built-in workflow — remove it from catalog/workflows and ` +
        `re-seed; it cannot be deleted via REST.`,
    );
  }
  await deps.repo.deleteWorkflow(orgScope(auth.org), name);
  return ok({ deleted: true });
}

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
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.org) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const projectId = (body as { projectId?: unknown })?.projectId;
  if (typeof projectId !== 'string' || !projectId) return badRequest('missing projectId');

  const workflow = await deps.repo.getWorkflow(orgScope(auth.org), name);
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
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
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
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
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
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
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
  const name = pathParam(event, 'name');
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

  if (method === 'POST' && path.endsWith('/promote')) return promoteWorkflow(event, deps);
  if (method === 'POST') return createWorkflow(event, deps);
  if (method === 'PUT') return createWorkflow(event, deps); // upsert
  if (method === 'DELETE') return deleteWorkflow(event, deps);
  if (method === 'GET' && name) return getWorkflow(event, deps);
  return resolveWorkflows(event, deps);
}
