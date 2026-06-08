import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { mcpServerSchema, orgScope, type McpServer } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  notFound,
  ok,
  parseBody,
  pathParam,
  unauthorized,
} from './runtime.js';
import { resolveOrgCatalogAuth } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';

/**
 * REST: MCP servers — a single ORG catalog, modeled on skills minus bundles.
 *
 *   GET    /mcp-servers              — the caller's org catalog
 *   GET    /mcp-servers/:name        — one server from the org catalog
 *   POST   /mcp-servers              — create (server forces org scope + createdBy; admin)
 *   PUT    /mcp-servers/:name        — update (scope/createdBy immutable; admin)
 *   DELETE /mcp-servers/:name        — delete (admin)
 *   GET    /mcp-servers/:name/usage  — count of agents depending on the server
 *
 * MCP servers are FLAT: there is no bundle concept (no members/dissolve/scope
 * verbs). The record is a structured discriminated union on `transport`, not a
 * markdown body. Catalog writes are gated by `resolveOrgCatalogAuth` (device
 * token OR Cognito JWT, with server-side admin), exactly like skills/agents.
 */

export interface McpServersDeps {
  repo: Repo;
}

export async function resolveMcpServers(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer route).
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ mcpServers: [] });

  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped server shadows an org-scoped one of the same name).
  const all = await deps.repo.listMcpServers(org, principal.userId);
  return ok({ mcpServers: all });
}

export async function createMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
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
  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = mcpServerSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const server: McpServer = parsed.data;

  if (name) {
    // PUT /mcp-servers/:name — update; preserve the existing createdBy stamp.
    const existing = await deps.repo.getMcpServer(orgScope(org), name);
    server.createdBy = existing?.createdBy ?? server.createdBy;
    server.baseName = existing?.baseName ?? server.baseName ?? name;
  } else {
    // POST — stamp authorship from the principal.
    server.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    server.baseName = server.baseName ?? server.name;
  }

  // VERSIONING (KTD6): snapshot a revision + fork/advance the variant instead of
  // clobbering; `putNewVersion` also upserts the live record under `mcpServerKey`.
  const stamped = await deps.repo.putNewVersion('MCPSERVER', server, {
    repoId: server.repoId,
    authorUserId: server.authorUserId,
  });
  return name ? ok({ mcpServer: stamped }) : created({ mcpServer: stamped });
}

/**
 * POST /mcp-servers/:name/promote — repoint the org-wide TRUE variant for a
 * baseName. NOT admin-gated (any authed member). Body: `{ variantId, rev? }`.
 * Mirrors skills/agents promote: it ONLY repoints TRUE.
 */
export async function promoteMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  // Promote is NOT admin-gated (any authed org member may repoint TRUE), but it
  // still accepts the device token via the shared resolver.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const org = auth.org;

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
  await deps.repo.setTrueVariant(orgScope(org), 'MCPSERVER', pointer);
  return ok({ true: pointer });
}

export async function getMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return notFound();
  const server = await deps.repo.getMcpServer(orgScope(auth.org), name);
  if (!server) return notFound();
  return ok({ mcpServer: server });
}

/** Count the agents (in the org catalog) whose `mcpServers[]` references a server. */
async function usageCount(repo: Repo, org: string, name: string): Promise<number> {
  const agents = await repo.listAgents(org);
  return agents.filter((a) => (a.mcpServers ?? []).includes(name)).length;
}

export async function getUsage(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, auth.org, name);
  return ok({ name, count });
}

export async function deleteMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  await deps.repo.deleteMcpServer(orgScope(auth.org), name);
  return ok({ deleted: true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: McpServersDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const name = pathParam(event, 'name');

  if (method === 'GET' && path.endsWith('/usage')) return getUsage(event, deps);
  if (method === 'POST' && path.endsWith('/promote')) return promoteMcpServer(event, deps);
  if (method === 'POST') return createMcpServer(event, deps);
  if (method === 'PUT') return createMcpServer(event, deps);
  if (method === 'DELETE') return deleteMcpServer(event, deps);
  if (method === 'GET' && name) return getMcpServer(event, deps);
  return resolveMcpServers(event, deps);
}
