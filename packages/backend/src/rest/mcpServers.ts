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
  principalOf,
  unauthorized,
} from './runtime.js';
import { canWriteOrgCatalog, isAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';

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
 * markdown body. Catalog writes are gated by `canWriteOrgCatalog` + `isAdmin`,
 * exactly like skills/agents.
 */

export interface McpServersDeps {
  repo: Repo;
}

export async function resolveMcpServers(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const org = await effectiveOrg(event, deps.repo);
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
  await deps.repo.setTrueVariant(orgScope(org), 'MCPSERVER', pointer);
  return ok({ true: pointer });
}

export async function getMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return notFound();
  const server = await deps.repo.getMcpServer(orgScope(org), name);
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
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, org, name);
  return ok({ name, count });
}

export async function deleteMcpServer(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  await deps.repo.deleteMcpServer(orgScope(org), name);
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
