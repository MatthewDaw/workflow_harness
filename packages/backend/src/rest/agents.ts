import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { agentSchema, type Agent } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { defaultRepo, gone, pathParam } from './runtime.js';
import { makeBundleMemberHandlers } from './bundles.js';
import { makeCatalogHandlers } from './catalogResource.js';

/**
 * REST: agents + agent bundles — collapsed to a single ORG catalog (mirrors
 * skills.ts).
 *
 *   GET    /agents                        — the caller's org catalog
 *   GET    /agents/:name                  — one agent from the org catalog
 *   POST   /agents                        — create (server forces org scope + createdBy; admin)
 *   PUT    /agents/:name                  — update (scope/createdBy immutable; admin)
 *   DELETE /agents/:name                  — delete (admin)
 *   POST   /agents/:name/members          — add a member ref to an agent bundle
 *   DELETE /agents/:name/members/:member  — eject a member (it stays standalone)
 *   POST   /agents/:name/dissolve         — flatten a bundle: members standalone, bundle removed
 *   POST   /agents/:name/scope            — RETIRED (410 Gone)
 *
 * Agents keep `skills[]`. There is no tier elevation/demotion in the org catalog.
 * An agent bundle (`kind:'bundle'`) holds member *refs* (agent names); a member
 * may itself be a bundle (nesting); resolution is transitive.
 *
 * Agents also carry a `description` (delegation trigger) alongside `model`/`tools`.
 * POST/PUT parse the body through `agentSchema` (so `description` flows through
 * with a '' default for back-compat), and GET returns the full record. The
 * wrapper materializes `model`, `tools`, and `description` into the subagent
 * file's frontmatter; this REST layer stores/serves them unchanged.
 */

export interface AgentsDeps {
  repo: Repo;
}

const handlers = makeCatalogHandlers({
  kind: 'AGENT',
  schema: agentSchema,
  label: 'agent',
  responseKey: 'agent',
  listKey: 'agents',
  seedHint: '.claude/agents',
  repoOps: {
    list: (repo, org, userId) => repo.listAgents(org, userId),
    get: (repo, scope, name) => repo.getAgent(scope, name),
    del: (repo, scope, name) => repo.deleteAgent(scope, name),
  },
  bundles: { noun: 'an agent bundle', push: 'an agent push', localName: 'agent' },
});

export const resolveAgents = handlers.list;
export const createAgent = handlers.create;
export const getAgent = handlers.get;
export const deleteAgent = handlers.remove;
/** Promote is NOT admin-gated (any authed member may repoint TRUE). */
export const promoteAgent = handlers.promote;

const memberHandlers = makeBundleMemberHandlers<Agent>({
  get: (repo, scope, name) => repo.getAgent(scope, name),
  put: (repo, bundle) => repo.putAgent(bundle),
  del: (repo, scope, name) => repo.deleteAgent(scope, name),
  responseKey: 'agent',
  builtinLabel: 'agent bundle',
  seedPath: 'catalog/agents/bundles.json',
});

export const addMember = memberHandlers.addMember;
export const removeMember = memberHandlers.removeMember;
export const dissolveAgentBundle = memberHandlers.dissolve;

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: AgentsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;

  // The scope-change endpoint is retired in the org-only catalog.
  if (method === 'POST' && path.endsWith('/scope')) return gone('scope changes are retired');
  if (method === 'POST' && path.endsWith('/members')) return addMember(event, deps);
  if (method === 'DELETE' && pathParam(event, 'member')) return removeMember(event, deps);
  if (method === 'POST' && path.endsWith('/dissolve')) return dissolveAgentBundle(event, deps);
  return handlers.dispatch(event, deps);
}
