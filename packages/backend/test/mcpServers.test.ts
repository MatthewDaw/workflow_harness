import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Agent, type McpServer } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  createMcpServer,
  deleteMcpServer,
  getMcpServer,
  getUsage,
  resolveMcpServers,
} from '../src/rest/mcpServers.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Org-catalog MCP servers — modeled on skills minus bundles. The catalog is
 * org-only: GET lists the org catalog, writes are admin-gated and stamp
 * createdBy, the record is a structured discriminated union on `transport`
 * (stdio / http / sse). There is no bundle / members / scope machinery.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const MATT = 'matt';
const ORG = 'acme';
const SCOPE = orgScope(ORG);

function stdioServer(name: string): McpServer {
  return { name, scope: SCOPE, transport: 'stdio', command: 'node', args: ['server.js'], env: {} };
}
function httpServer(name: string, url = 'https://mcp.example.com'): McpServer {
  return { name, scope: SCOPE, transport: 'http', url, headers: {} };
}
function agent(name: string, mcpServers: string[]): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills: [], tools: [], mcpServers };
}

/** An admin event for the caller's own org (org-catalog writes require admin). */
function adminEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ org: ORG, admin: true, ...opts });
}

describe('GET /mcp-servers (org catalog)', () => {
  it('returns the org catalog for the caller', async () => {
    await repo.putMcpServer(stdioServer('filesystem'));
    await repo.putMcpServer(httpServer('weather'));
    const res = await resolveMcpServers(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { mcpServers } = bodyOf<{ mcpServers: McpServer[] }>(res as { body: string });
    expect(mcpServers.map((s) => s.name).sort()).toEqual(['filesystem', 'weather']);
  });

  it('is empty when the org has no servers', async () => {
    const res = await resolveMcpServers(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { mcpServers } = bodyOf<{ mcpServers: McpServer[] }>(res as { body: string });
    expect(mcpServers).toEqual([]);
  });
});

describe('POST /mcp-servers (admin-gated org write + createdBy)', () => {
  it('forbids a non-admin create', async () => {
    const res = await createMcpServer(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: stdioServer('filesystem') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('forces org scope, ignores a client scope, and stamps createdBy from the principal', async () => {
    const res = await createMcpServer(
      adminEvent({
        method: 'POST',
        userId: MATT,
        // client tries to sneak a user scope; server must override to org.
        body: { ...stdioServer('filesystem'), scope: { tier: 'user', id: 'someone' } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getMcpServer(SCOPE, 'filesystem');
    expect(stored?.scope).toEqual({ tier: 'org', id: ORG });
    expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
  });

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

describe('PUT /mcp-servers/:name (preserves createdBy)', () => {
  it('updates fields but keeps the original createdBy', async () => {
    await repo.putMcpServer({
      ...stdioServer('filesystem'),
      createdBy: { userId: 'alice', name: 'Alice' },
    });
    const res = await createMcpServer(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'filesystem' },
        body: { ...stdioServer('filesystem'), command: 'bun' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const stored = await repo.getMcpServer(SCOPE, 'filesystem');
    expect(stored?.transport === 'stdio' && stored.command).toBe('bun');
    expect(stored?.createdBy).toEqual({ userId: 'alice', name: 'Alice' });
  });
});

describe('DELETE /mcp-servers/:name', () => {
  it('deletes for an admin', async () => {
    await repo.putMcpServer(stdioServer('filesystem'));
    const res = await deleteMcpServer(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'filesystem' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getMcpServer(SCOPE, 'filesystem')).toBeUndefined();
  });

  it('forbids a non-admin delete', async () => {
    await repo.putMcpServer(stdioServer('filesystem'));
    const res = await deleteMcpServer(
      httpEvent({ method: 'DELETE', userId: MATT, org: ORG, path: { name: 'filesystem' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
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
    const { mcpServer } = bodyOf<{ mcpServer: McpServer }>(read as { body: string });
    expect(mcpServer).toMatchObject({
      name: 'filesystem',
      transport: 'stdio',
      command: 'node',
      args: ['server.js'],
    });
  });

  it('404s an unknown server name', async () => {
    const res = await getMcpServer(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('usage / blast radius', () => {
  it('counts agents using an mcp server', async () => {
    await repo.putMcpServer(stdioServer('filesystem'));
    await repo.putAgent(agent('builder', ['filesystem']));
    await repo.putAgent(agent('analyst', ['filesystem', 'other']));
    await repo.putAgent(agent('idle', []));
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
    expect(bodyOf<{ count: number }>(res as { body: string }).count).toBe(2);
  });
});
