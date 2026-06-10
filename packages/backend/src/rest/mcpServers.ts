import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { mcpServerSchema } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { badRequest, defaultRepo, ok, pathParam } from './runtime.js';
import { requireOrgCatalogAuth } from './scopeauth.js';
import { makeCatalogHandlers } from './catalogResource.js';

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
 * markdown body.
 */

export interface McpServersDeps {
  repo: Repo;
}

const handlers = makeCatalogHandlers({
  kind: 'MCPSERVER',
  schema: mcpServerSchema,
  label: 'MCP server',
  responseKey: 'mcpServer',
  listKey: 'mcpServers',
  seedHint: 'the repo',
  repoOps: {
    list: (repo, org, userId) => repo.listMcpServers(org, userId),
    get: (repo, scope, name) => repo.getMcpServer(scope, name),
    del: (repo, scope, name) => repo.deleteMcpServer(scope, name),
  },
});

export const resolveMcpServers = handlers.list;
export const createMcpServer = handlers.create;
export const getMcpServer = handlers.get;
export const deleteMcpServer = handlers.remove;
/** Promote is NOT admin-gated (any authed member may repoint TRUE). */
export const promoteMcpServer = handlers.promote;

/** Count the agents (in the org catalog) whose `mcpServers[]` references a server. */
async function usageCount(repo: Repo, org: string, name: string): Promise<number> {
  const agents = await repo.listAgents(org);
  return agents.filter((a) => (a.mcpServers ?? []).includes(name)).length;
}

export async function getUsage(
  event: APIGatewayProxyEventV2,
  deps: McpServersDeps,
): Promise<APIGatewayProxyResultV2> {
  const gate = await requireOrgCatalogAuth(event, deps.repo);
  if ('error' in gate) return gate.error;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!gate.auth.org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, gate.auth.org, name);
  return ok({ name, count });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: McpServersDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;

  if (method === 'GET' && path.endsWith('/usage')) return getUsage(event, deps);
  return handlers.dispatch(event, deps);
}
