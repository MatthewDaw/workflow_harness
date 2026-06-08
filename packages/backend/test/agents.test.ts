import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { agentSchema, orgScope, type Agent } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  createAgent,
  deleteAgent,
  getAgent,
  handler as agentsHandler,
  promoteAgent,
  resolveAgents,
} from '../src/rest/agents.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Org-catalog agents (3-tier scope retired). GET lists the org catalog; writes
 * are admin-gated, force org scope, and stamp createdBy; the scope-change route
 * is retired (410).
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

function agent(name: string, skills: string[] = []): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills, tools: [] };
}

function adminEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ org: ORG, admin: true, ...opts });
}

describe('agentSchema description default', () => {
  it("fills description '' when omitted and preserves a provided description", () => {
    // The agent() helper omits description; it must still validate via default ''.
    const omitted = agentSchema.parse(agent('builder'));
    expect(omitted.description).toBe('');

    const provided = agentSchema.parse({
      ...agent('builder'),
      description: 'use when refactoring legacy code',
    });
    expect(provided.description).toBe('use when refactoring legacy code');
  });
});

describe('POST /agents (admin-gated org write + createdBy)', () => {
  it('forbids a non-admin create', async () => {
    const res = await createAgent(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: agent('builder') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('creates at org scope for an admin, ignoring a client scope, and stamps createdBy', async () => {
    const res = await createAgent(
      adminEvent({
        method: 'POST',
        userId: MATT,
        body: { ...agent('builder'), scope: { tier: 'user', id: 'someone' } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getAgent(SCOPE, 'builder');
    expect(stored?.scope).toEqual({ tier: 'org', id: ORG });
    expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
  });
});

describe('PUT /agents/:name (preserves createdBy)', () => {
  it('updates but keeps the original createdBy', async () => {
    await repo.putAgent({ ...agent('builder'), createdBy: { userId: 'alice', name: 'Alice' } });
    const res = await createAgent(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'builder' },
        body: { ...agent('builder'), model: 'sonnet' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const stored = await repo.getAgent(SCOPE, 'builder');
    expect(stored?.model).toBe('sonnet');
    expect(stored?.createdBy).toEqual({ userId: 'alice', name: 'Alice' });
  });
});

describe('GET /agents (org catalog)', () => {
  it('returns the org catalog', async () => {
    await repo.putAgent(agent('builder'));
    await repo.putAgent(agent('analyst'));
    const res = await resolveAgents(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { agents } = bodyOf<{ agents: Agent[] }>(res as { body: string });
    expect(agents.map((a) => a.name).sort()).toEqual(['analyst', 'builder']);
  });
});

describe('GET /agents/:name', () => {
  it('reads an agent from the org catalog', async () => {
    await repo.putAgent(agent('builder'));
    const res = await getAgent(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'builder' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it('404s a missing agent', async () => {
    const res = await getAgent(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('DELETE /agents/:name', () => {
  it('deletes for an admin', async () => {
    await repo.putAgent(agent('builder'));
    const res = await deleteAgent(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'builder' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getAgent(SCOPE, 'builder')).toBeUndefined();
  });

  it('forbids a non-admin delete', async () => {
    await repo.putAgent(agent('builder'));
    const res = await deleteAgent(
      httpEvent({ method: 'DELETE', userId: MATT, org: ORG, path: { name: 'builder' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('versioning: create snapshots rev 1 + promote repoints TRUE', () => {
  it('stamps version fields on create and initializes the TRUE pointer', async () => {
    const res = await createAgent(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, admin: true, body: agent('builder') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const created = bodyOf<{ agent: Agent }>(res as { body: string }).agent;
    expect(created.variantId).toBe('builder');
    expect(created.version).toBe(1);
    const truth = await repo.getTrueVariant(SCOPE, 'AGENT', 'builder');
    expect(truth).toMatchObject({ baseName: 'builder', variantId: 'builder', rev: 1 });
  });

  it('a non-admin member may promote (not admin-gated)', async () => {
    await createAgent(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, admin: true, body: agent('builder') }),
      deps,
    );
    const res = await promoteAgent(
      httpEvent({
        method: 'POST',
        userId: 'bob',
        org: ORG,
        path: { name: 'builder' },
        rawPath: '/agents/builder/promote',
        body: { variantId: 'builder#R#r#U#bob', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getTrueVariant(SCOPE, 'AGENT', 'builder'))?.variantId).toBe(
      'builder#R#r#U#bob',
    );
  });
});

describe('POST /agents/:name/scope (retired)', () => {
  it('responds 410 Gone', async () => {
    const res = await agentsHandler(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        rawPath: '/agents/builder/scope',
        path: { name: 'builder' },
        body: { scope: { tier: 'org', id: ORG } },
      }),
    );
    expect(res).toMatchObject({ statusCode: 410 });
  });
});

/**
 * Canonical built-in agents (seed-owned, `createdBy.userId === 'system'`) are
 * fork-only via REST: the base is updated only by the git seed; edits must fork.
 */
describe('built-in agents are fork-only via REST (git-seed owned)', () => {
  const builtinAgent = (name: string): Agent => ({
    ...agent(name),
    createdBy: { userId: 'system', name: 'system' },
  });

  it('rejects an in-place PUT to a built-in agent (409)', async () => {
    await repo.putAgent(builtinAgent('forge'));
    const res = await createAgent(
      adminEvent({ method: 'PUT', userId: MATT, path: { name: 'forge' }, body: agent('forge') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('rejects deleting a built-in agent (409)', async () => {
    await repo.putAgent(builtinAgent('forge'));
    const res = await deleteAgent(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'forge' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('ALLOWS forking a built-in agent (repoId + authorUserId set)', async () => {
    await repo.putAgent(builtinAgent('forge'));
    const res = await createAgent(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'forge' },
        body: { ...agent('forge'), repoId: 'repo1', authorUserId: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });
});
