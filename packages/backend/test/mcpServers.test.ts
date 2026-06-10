import { describe, expect, it } from 'vitest';
import type { McpServer } from '@harness/shared';
import {
  createMcpServer,
  deleteMcpServer,
  getMcpServer,
  getUsage,
  promoteMcpServer,
  resolveMcpServers,
} from '../src/rest/mcpServers.js';
import { memRepoHarness } from './helpers/memtable.js';
import { adminEvent, bodyOf, httpEvent } from './helpers/httpevent.js';
import { MATT, ORG, SCOPE, makeAgent } from './helpers/factories.js';
import { describeOrgCatalogContract } from './helpers/catalog-contract.js';

/**
 * Org-catalog MCP servers — modeled on skills minus bundles. The generic REST
 * contract runs via describeOrgCatalogContract; this file keeps the
 * MCP-specific behavior: the structured discriminated union on `transport`
 * (stdio / http / sse), body validation, and usage / blast radius.
 */

const { repo } = memRepoHarness();
const deps = { repo };

function stdioServer(name: string): McpServer {
  return { name, scope: SCOPE, transport: 'stdio', command: 'node', args: ['server.js'], env: {} };
}
function httpServer(name: string, url = 'https://mcp.example.com'): McpServer {
  return { name, scope: SCOPE, transport: 'http', url, headers: {} };
}

describeOrgCatalogContract<McpServer>({
  noun: 'mcpServer',
  plural: 'mcpServers',
  pathPrefix: 'mcp-servers',
  entity: 'MCPSERVER',
  repo,
  sampleNames: ['filesystem', 'weather'],
  builtinName: 'filesystem',
  make: (name) => stdioServer(name),
  makeBuiltin: (name) => ({
    ...stdioServer(name),
    createdBy: { userId: 'system', name: 'system' },
  }),
  mutate: (s) => ({ ...s, command: 'bun' }) as McpServer,
  assertMutated: (stored) => expect(stored?.transport === 'stdio' && stored.command).toBe('bun'),
  create: (e) => createMcpServer(e, deps),
  remove: (e) => deleteMcpServer(e, deps),
  get: (e) => getMcpServer(e, deps),
  list: (e) => resolveMcpServers(e, deps),
  promote: (e) => promoteMcpServer(e, deps),
  put: (s) => repo.putMcpServer(s),
  read: (name) => repo.getMcpServer(SCOPE, name),
  nonAdminPromote: 'allowed',
});

describe('POST /mcp-servers body validation', () => {
  it('rejects an invalid body (unknown transport) with 400', async () => {
    const res = await createMcpServer(
      adminEvent({
        method: 'POST',
        userId: MATT,
        body: { name: 'bad', transport: 'carrier-pigeon' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('rejects an http server with a non-URL url with 400', async () => {
    const res = await createMcpServer(
      adminEvent({
        method: 'POST',
        userId: MATT,
        body: { ...httpServer('weather', 'not-a-url') },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('mcp server round-trip', () => {
  it('persists + returns a stdio server unchanged on create and read', async () => {
    const create = await createMcpServer(
      adminEvent({ method: 'POST', userId: MATT, body: stdioServer('filesystem') }),
      deps,
    );
    expect(create).toMatchObject({ statusCode: 201 });
    const read = await getMcpServer(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'filesystem' } }),
      deps,
    );
    const { mcpServer } = bodyOf<{ mcpServer: McpServer }>(read);
    expect(mcpServer).toMatchObject({
      name: 'filesystem',
      transport: 'stdio',
      command: 'node',
      args: ['server.js'],
    });
  });
});

describe('usage / blast radius', () => {
  it('counts agents using an mcp server', async () => {
    await repo.putMcpServer(stdioServer('filesystem'));
    await repo.putAgent(makeAgent('builder', [], { mcpServers: ['filesystem'] }));
    await repo.putAgent(makeAgent('analyst', [], { mcpServers: ['filesystem', 'other'] }));
    await repo.putAgent(makeAgent('idle', [], { mcpServers: [] }));
    const res = await getUsage(
      httpEvent({
        method: 'GET',
        userId: MATT,
        org: ORG,
        path: { name: 'filesystem' },
        rawPath: '/mcp-servers/filesystem/usage',
      }),
      deps,
    );
    expect(bodyOf<{ count: number }>(res).count).toBe(2);
  });
});
