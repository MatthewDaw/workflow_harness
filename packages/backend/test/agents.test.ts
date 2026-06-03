import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Agent, Project, ScopeRef } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  changeScope,
  createAgent,
  deleteAgent,
  getAgent,
  resolveAgents,
} from '../src/rest/agents.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U9 REST: agents. Scoped create; effective-set resolution (narrowest wins on
 * collision); elevate/demote rewrites the scope key; org-scope writes require
 * admin.
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
const PROJ = 'weekly-compass';

function agent(name: string, scope: ScopeRef): Agent {
  return { name, scope, model: 'opus', prompt: '', skills: [], tools: [] };
}

function project(id: string, owner: string): Project {
  return { id, name: id, repo: `gh/acme/${id}`, ownerUserId: owner, liveSessionCount: 0 };
}

describe('POST /agents', () => {
  it('creates an agent at project scope', async () => {
    await repo.putProject(project(PROJ, MATT));
    const res = await createAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        body: agent('builder', { tier: 'project', id: PROJ }),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    expect(await repo.getAgent({ tier: 'project', id: PROJ }, 'builder')).toBeDefined();
  });

  it('forbids an org-scope write without admin', async () => {
    const res = await createAgent(
      httpEvent({ method: 'POST', userId: MATT, body: agent('builder', { tier: 'org', id: ORG }) }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('allows an org-scope write for an admin', async () => {
    const res = await createAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        admin: true,
        body: agent('builder', { tier: 'org', id: ORG }),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
  });

  it("forbids writing another user's user scope", async () => {
    const res = await createAgent(
      httpEvent({ method: 'POST', userId: MATT, body: agent('x', { tier: 'user', id: 'alice' }) }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('forbids a project-scope write to a project the caller does not own (cross-tenant)', async () => {
    await repo.putProject(project('alices-proj', 'alice'));
    const res = await createAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        body: agent('x', { tier: 'project', id: 'alices-proj' }),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect(await repo.getAgent({ tier: 'project', id: 'alices-proj' }, 'x')).toBeUndefined();
  });

  it('forbids a project-scope write to a non-existent project', async () => {
    const res = await createAgent(
      httpEvent({
        method: 'POST',
        userId: MATT,
        body: agent('x', { tier: 'project', id: 'ghost-proj' }),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('GET /agents/:name (explicit-scope read, IDOR)', () => {
  it('reads an agent at a scope the caller owns', async () => {
    await repo.putAgent(agent('mine', { tier: 'user', id: MATT }));
    const res = await getAgent(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { name: 'mine' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it("404s (not 403) a read of another user's user-scope agent", async () => {
    await repo.putAgent(agent('alices', { tier: 'user', id: 'alice' }));
    const res = await getAgent(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { name: 'alices' },
        query: { tier: 'user', id: 'alice' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it("404s a read of another tenant's project-scope agent", async () => {
    await repo.putProject(project('alices-proj', 'alice'));
    await repo.putAgent(agent('builder', { tier: 'project', id: 'alices-proj' }));
    const res = await getAgent(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { name: 'builder' },
        query: { tier: 'project', id: 'alices-proj' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('GET /agents (effective set)', () => {
  it('composes org + user + project tiers', async () => {
    await repo.putAgent(agent('orgwide', { tier: 'org', id: ORG }));
    await repo.putAgent(agent('mine', { tier: 'user', id: MATT }));
    await repo.putAgent(agent('proj-only', { tier: 'project', id: PROJ }));

    const res = await resolveAgents(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, query: { project: PROJ } }),
      deps,
    );
    const { agents } = bodyOf<{ agents: Agent[] }>(res as { body: string });
    expect(agents.map((a) => a.name).sort()).toEqual(['mine', 'orgwide', 'proj-only']);
  });

  it('narrowest scope wins on a name collision (project shadows org)', async () => {
    await repo.putAgent({ ...agent('builder', { tier: 'org', id: ORG }), model: 'org-model' });
    await repo.putAgent({
      ...agent('builder', { tier: 'project', id: PROJ }),
      model: 'proj-model',
    });

    const res = await resolveAgents(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, query: { project: PROJ } }),
      deps,
    );
    const { agents } = bodyOf<{ agents: Agent[] }>(res as { body: string });
    const builder = agents.filter((a) => a.name === 'builder');
    expect(builder).toHaveLength(1);
    expect(builder[0]!.model).toBe('proj-model'); // project wins
    expect(builder[0]!.scope.tier).toBe('project');
  });

  it('excludes another user/project scopes from the effective set', async () => {
    await repo.putAgent(agent('alices', { tier: 'user', id: 'alice' }));
    await repo.putAgent(agent('other-proj', { tier: 'project', id: 'other' }));
    const res = await resolveAgents(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, query: { project: PROJ } }),
      deps,
    );
    const { agents } = bodyOf<{ agents: Agent[] }>(res as { body: string });
    expect(agents.map((a) => a.name)).not.toContain('alices');
    expect(agents.map((a) => a.name)).not.toContain('other-proj');
  });
});

describe('POST /agents/:name/scope (elevate/demote)', () => {
  it('elevates a project agent to user scope, rewriting the key', async () => {
    await repo.putProject(project(PROJ, MATT));
    await repo.putAgent(agent('builder', { tier: 'project', id: PROJ }));
    const res = await changeScope(
      httpEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/agents/builder/scope',
        path: { name: 'builder' },
        query: { tier: 'project', id: PROJ },
        body: { scope: { tier: 'user', id: MATT } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getAgent({ tier: 'project', id: PROJ }, 'builder')).toBeUndefined();
    expect(await repo.getAgent({ tier: 'user', id: MATT }, 'builder')).toBeDefined();
  });

  it('forbids elevating to org without admin', async () => {
    await repo.putAgent(agent('builder', { tier: 'user', id: MATT }));
    const res = await changeScope(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'builder' },
        query: { tier: 'user', id: MATT },
        body: { scope: { tier: 'org', id: ORG } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    // unchanged
    expect(await repo.getAgent({ tier: 'user', id: MATT }, 'builder')).toBeDefined();
  });
});

describe('DELETE /agents/:name', () => {
  it('deletes an owned agent', async () => {
    await repo.putAgent(agent('builder', { tier: 'user', id: MATT }));
    const res = await deleteAgent(
      httpEvent({
        method: 'DELETE',
        userId: MATT,
        path: { name: 'builder' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getAgent({ tier: 'user', id: MATT }, 'builder')).toBeUndefined();
  });
});
