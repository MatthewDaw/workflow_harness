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
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';

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

export async function resolveAgents(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer route).
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ agents: [] });
  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped agent shadows an org-scoped one of the same name).
  const agents = await deps.repo.listAgents(org, principal.userId);
  return ok({ agents });
}

export async function createAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();

  const name = pathParam(event, 'name');
  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = agentSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const agent: Agent = parsed.data;

  if (name) {
    // PUT /agents/:name — update; preserve the existing createdBy stamp.
    const existing = await deps.repo.getAgent(orgScope(org), name);
    agent.createdBy = existing?.createdBy ?? agent.createdBy;
    agent.baseName = existing?.baseName ?? agent.baseName ?? name;
  } else {
    agent.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    agent.baseName = agent.baseName ?? agent.name;
  }

  // VERSIONING (KTD6): snapshot a revision + fork/advance the variant instead of
  // clobbering; `putNewVersion` also upserts the live record under `agentKey`.
  const stamped = await deps.repo.putNewVersion('AGENT', agent, {
    repoId: agent.repoId,
    authorUserId: agent.authorUserId,
  });
  return name ? ok({ agent: stamped }) : created({ agent: stamped });
}

/**
 * POST /agents/:name/promote — repoint the org-wide TRUE variant for a baseName.
 * NOT admin-gated (any authed member). Body: `{ variantId, rev? }`. Mirrors
 * skills' promote: it ONLY repoints TRUE, never editing/deleting a variant.
 */
export async function promoteAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const variantId = (body as { variantId?: unknown })?.variantId;
  if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
  const revRaw = (body as { rev?: unknown })?.rev;
  const rev = typeof revRaw === 'number' ? revRaw : undefined;

  const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
  await deps.repo.setTrueVariant(orgScope(org), 'AGENT', pointer);
  return ok({ true: pointer });
}

export async function getAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return notFound();
  const agent = await deps.repo.getAgent(orgScope(org), name);
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
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  await deps.repo.deleteAgent(orgScope(org), name);
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
  if (method === 'POST' && path.endsWith('/promote')) return promoteAgent(event, deps);
  if (method === 'POST') return createAgent(event, deps);
  if (method === 'PUT') return createAgent(event, deps); // upsert
  if (method === 'DELETE') return deleteAgent(event, deps);
  if (method === 'GET' && name) return getAgent(event, deps);
  return resolveAgents(event, deps);
}
