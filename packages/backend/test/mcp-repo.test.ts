import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Agent, type McpServer, type Project, type Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * MCP server persistence + project/agent attachment (mirrors the skills repo
 * coverage). CRUD round-trips through the real Repo against the in-memory table;
 * listMcpServers returns only MCPSERVER# items; project opt-in is idempotent;
 * enabling an agent unions its declared `mcpServers` (PLAIN names — no bundles)
 * into enabledMcpServers; disabling an agent never prunes the servers.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
const MATT = 'matt';
const PROJ = 'weekly-compass';
const SCOPE = orgScope(ORG);

function stdioServer(name: string): McpServer {
  return {
    name,
    scope: SCOPE,
    transport: 'stdio',
    command: 'npx',
    args: ['-y', 'srv'],
    env: { TOKEN: 'sk-1' },
  };
}
function httpServer(name: string): McpServer {
  return {
    name,
    scope: SCOPE,
    transport: 'http',
    url: 'https://mcp.example.com',
    headers: { Authorization: 'Bearer x' },
  };
}
function project(id: string, owner: string): Project {
  return {
    id,
    name: id,
    repo: `gh/acme/${id}`,
    ownerUserId: owner,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledAgents: [],
    enabledMcpServers: [],
  };
}
function agent(name: string, mcpServers: string[]): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills: [], tools: [], mcpServers };
}

describe('mcpServerKey', () => {
  it('keys under the scope partition with an MCPSERVER# sort key', () => {
    expect(k.mcpServerKey(SCOPE, 'context7')).toEqual({
      PK: 'SCOPE#org#acme',
      SK: 'MCPSERVER#context7',
    });
  });
});

describe('putMcpServer / getMcpServer', () => {
  it('round-trips an stdio server unchanged', async () => {
    const srv = stdioServer('local-fs');
    await repo.putMcpServer(srv);
    const out = await repo.getMcpServer(SCOPE, 'local-fs');
    expect(out).toMatchObject(srv);
  });

  it('round-trips an http server unchanged', async () => {
    const srv = httpServer('remote-mcp');
    await repo.putMcpServer(srv);
    const out = await repo.getMcpServer(SCOPE, 'remote-mcp');
    expect(out).toMatchObject(srv);
  });

  it('returns undefined for a missing server (no throw)', async () => {
    expect(await repo.getMcpServer(SCOPE, 'ghost')).toBeUndefined();
  });
});

describe('deleteMcpServer', () => {
  it('removes the server so a subsequent get is undefined', async () => {
    await repo.putMcpServer(stdioServer('local-fs'));
    await repo.deleteMcpServer(SCOPE, 'local-fs');
    expect(await repo.getMcpServer(SCOPE, 'local-fs')).toBeUndefined();
  });
});

describe('listMcpServers', () => {
  it('returns only MCPSERVER# items in the org partition (not skills/agents)', async () => {
    await repo.putMcpServer(stdioServer('a'));
    await repo.putMcpServer(httpServer('b'));
    // A skill and an agent share the same scope partition and must NOT appear.
    const skill: Skill = {
      name: 'reconcile',
      scope: SCOPE,
      kind: 'skill',
      description: '',
      source: 'local',
      members: [],
      body: '',
    };
    await repo.putSkill(skill);
    await repo.putAgent(agent('builder', []));

    const out = await repo.listMcpServers(ORG);
    expect(out.map((s) => s.name).sort()).toEqual(['a', 'b']);
  });

  it('is empty when the org has no servers', async () => {
    expect(await repo.listMcpServers(ORG)).toEqual([]);
  });
});

describe('addMcpServerToProject / removeMcpServerFromProject', () => {
  it('idempotently enables a server (adding twice yields one entry)', async () => {
    await repo.putProject(project(PROJ, MATT));

    const first = await repo.addMcpServerToProject(PROJ, 'local-fs');
    expect(first?.enabledMcpServers).toEqual(['local-fs']);
    const second = await repo.addMcpServerToProject(PROJ, 'local-fs');
    expect(second?.enabledMcpServers).toEqual(['local-fs']);
  });

  it('removes a server from enabledMcpServers', async () => {
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledMcpServers: ['local-fs', 'remote-mcp'],
    });
    const out = await repo.removeMcpServerFromProject(PROJ, 'local-fs');
    expect(out?.enabledMcpServers).toEqual(['remote-mcp']);
  });

  it('returns undefined for a missing project (no throw)', async () => {
    expect(await repo.addMcpServerToProject('ghost', 'local-fs')).toBeUndefined();
    expect(await repo.removeMcpServerFromProject('ghost', 'local-fs')).toBeUndefined();
  });
});

describe('addAgentToProject (unions agent mcpServers)', () => {
  it('unions the agent declared mcpServers (de-duped against already-present)', async () => {
    await repo.putProject({ ...project(PROJ, MATT), enabledMcpServers: ['a'] });
    await repo.putAgent(agent('builder', ['a', 'b']));

    const out = await repo.addAgentToProject(PROJ, 'builder', ORG);
    expect(out?.enabledAgents).toEqual(['builder']);
    expect((out?.enabledMcpServers ?? []).sort()).toEqual(['a', 'b']);
  });

  it('leaves enabledMcpServers untouched for an agent with no servers', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putAgent(agent('builder', []));
    const out = await repo.addAgentToProject(PROJ, 'builder', ORG);
    expect(out?.enabledMcpServers).toEqual([]);
  });

  it('returns undefined when the agent is missing', async () => {
    await repo.putProject(project(PROJ, MATT));
    expect(await repo.addAgentToProject(PROJ, 'ghost', ORG)).toBeUndefined();
  });
});

describe('removeAgentFromProject (does NOT strip servers)', () => {
  it('prunes only enabledAgents, leaving enabledMcpServers intact', async () => {
    await repo.putProject({
      ...project(PROJ, MATT),
      enabledAgents: ['builder'],
      enabledMcpServers: ['a', 'b'],
    });
    const out = await repo.removeAgentFromProject(PROJ, 'builder');
    expect(out?.enabledAgents).toEqual([]);
    expect((out?.enabledMcpServers ?? []).sort()).toEqual(['a', 'b']);
  });
});
