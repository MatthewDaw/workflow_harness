import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { agentSchema, orgScope, type Agent } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  gone,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';
import { canWriteOrgCatalog, isAdmin } from './scopeauth.js';

/**
 * REST: agents — collapsed to a single ORG catalog (mirrors skills.ts).
 *
 *   GET    /agents              — the caller's org catalog
 *   GET    /agents/:name        — one agent from the org catalog
 *   POST   /agents              — create (server forces org scope + createdBy; admin)
 *   PUT    /agents/:name        — update (scope/createdBy immutable; admin)
 *   DELETE /agents/:name        — delete (admin)
 *   POST   /agents/:name/scope  — RETIRED (410 Gone)
 *
 * Agents keep `skills[]`. There is no tier elevation/demotion in the org catalog.
 */

export interface AgentsDeps {
  repo: Repo;
}

export async function resolveAgents(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const agents = await deps.repo.listAgents(principal.org);
  return ok({ agents });
}

export async function createAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();

  const name = pathParam(event, 'name');
  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(principal.org) };
  const parsed = agentSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const agent: Agent = parsed.data;

  if (name) {
    // PUT /agents/:name — update; preserve the existing createdBy stamp.
    const existing = await deps.repo.getAgent(orgScope(principal.org), name);
    agent.createdBy = existing?.createdBy ?? agent.createdBy;
  } else {
    agent.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
  }

  await deps.repo.putAgent(agent);
  return name ? ok({ agent }) : created({ agent });
}

export async function getAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const agent = await deps.repo.getAgent(orgScope(principal.org), name);
  if (!agent) return notFound();
  return ok({ agent });
}

export async function deleteAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  await deps.repo.deleteAgent(orgScope(principal.org), name);
  return ok({ deleted: true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: AgentsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const name = pathParam(event, 'name');
  const path = event.requestContext.http.path;
  const isScopeRoute = path.endsWith('/scope');

  // The scope-change endpoint is retired in the org-only catalog.
  if (method === 'POST' && isScopeRoute) return gone('scope changes are retired');
  if (method === 'POST') return createAgent(event, deps);
  if (method === 'PUT') return createAgent(event, deps); // upsert
  if (method === 'DELETE') return deleteAgent(event, deps);
  if (method === 'GET' && name) return getAgent(event, deps);
  return resolveAgents(event, deps);
}
