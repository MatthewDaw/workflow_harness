import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Agent, type Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  addMember,
  createSkill,
  deleteSkill,
  dissolveBundle,
  flattenBundle,
  getSkill,
  getUsage,
  removeMember,
  resolveSkills,
} from '../src/rest/skills.js';
import { handler as skillsHandler } from '../src/rest/skills.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Org-catalog skills + bundles. Skills are org-only (3-tier scope retired):
 * GET lists the org catalog, writes are admin-gated and stamp createdBy, the
 * scope-change route is retired (410). Bundle add/remove/dissolve + transitive
 * flatten are unchanged behaviorally.
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

function skill(name: string, body = ''): Skill {
  return { name, scope: SCOPE, kind: 'skill', description: '', source: 'local', members: [], body };
}
function bundle(name: string, members: string[]): Skill {
  return { name, scope: SCOPE, kind: 'bundle', description: '', source: 'local', members };
}
function agent(name: string, skills: string[]): Agent {
  return { name, scope: SCOPE, model: 'opus', prompt: '', skills, tools: [] };
}

/** An admin event for the caller's own org (org-catalog writes require admin). */
function adminEvent(opts: Parameters<typeof httpEvent>[0]) {
  return httpEvent({ org: ORG, admin: true, ...opts });
}

describe('GET /skills (org catalog)', () => {
  it('returns the org catalog for the caller', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putSkill(skill('forecast'));
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Skill[] }>(res as { body: string });
    expect(skills.map((s) => s.name).sort()).toEqual(['forecast', 'reconcile']);
  });

  it('annotates a bundle with its transitively-resolved members', async () => {
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putSkill(bundle('inner', ['a', 'b']));
    await repo.putSkill(bundle('outer', ['inner']));
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Array<Skill & { resolvedMembers?: string[] }> }>(
      res as { body: string },
    );
    const outer = skills.find((s) => s.name === 'outer')!;
    expect(outer.resolvedMembers!.sort()).toEqual(['a', 'b']);
  });
});

describe('POST /skills (admin-gated org write + createdBy)', () => {
  it('forbids a non-admin create', async () => {
    const res = await createSkill(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: skill('reconcile') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('forces org scope, ignores a client scope, and stamps createdBy from the principal', async () => {
    const res = await createSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        // client tries to sneak a user scope; server must override to org.
        body: { ...skill('reconcile'), scope: { tier: 'user', id: 'someone' } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getSkill(SCOPE, 'reconcile');
    expect(stored?.scope).toEqual({ tier: 'org', id: ORG });
    expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
  });
});

describe('PUT /skills/:name (preserves createdBy)', () => {
  it('updates fields but keeps the original createdBy', async () => {
    await repo.putSkill({ ...skill('reconcile', 'old'), createdBy: { userId: 'alice', name: 'Alice' } });
    const res = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'reconcile' },
        body: skill('reconcile', 'new body'),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const stored = await repo.getSkill(SCOPE, 'reconcile');
    expect(stored?.body).toBe('new body');
    expect(stored?.createdBy).toEqual({ userId: 'alice', name: 'Alice' });
  });
});

describe('DELETE /skills/:name', () => {
  it('deletes for an admin', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await deleteSkill(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getSkill(SCOPE, 'reconcile')).toBeUndefined();
  });

  it('forbids a non-admin delete', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await deleteSkill(
      httpEvent({ method: 'DELETE', userId: MATT, org: ORG, path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });
});

describe('bundle membership (admin)', () => {
  it('adds a standalone skill into a bundle, leaving it standalone', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putSkill(bundle('finance-pack', []));
    const res = await addMember(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'finance-pack' },
        body: { member: 'reconcile' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getSkill(SCOPE, 'finance-pack'))?.members).toEqual(['reconcile']);
    expect(await repo.getSkill(SCOPE, 'reconcile')).toBeDefined();
  });

  it('ejects a member, leaving it standalone', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putSkill(bundle('finance-pack', ['reconcile']));
    const res = await removeMember(
      adminEvent({
        method: 'DELETE',
        userId: MATT,
        path: { name: 'finance-pack', member: 'reconcile' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getSkill(SCOPE, 'finance-pack'))?.members).toEqual([]);
    expect(await repo.getSkill(SCOPE, 'reconcile')).toBeDefined();
  });
});

describe('nested bundles (pure flatten)', () => {
  it('flattens transitively through a nested bundle', () => {
    const inner = bundle('inner', ['a', 'b']);
    const outer = bundle('outer', ['inner', 'c']);
    const byName = new Map<string, Skill>([
      ['inner', inner],
      ['outer', outer],
      ['a', skill('a')],
      ['b', skill('b')],
      ['c', skill('c')],
    ]);
    expect(flattenBundle(outer, byName).sort()).toEqual(['a', 'b', 'c']);
  });
});

describe('dissolve', () => {
  it('removes the bundle but leaves members standalone', async () => {
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putSkill(bundle('pack', ['a', 'b']));
    const res = await dissolveBundle(
      adminEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/skills/pack/dissolve',
        path: { name: 'pack' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ members: string[] }>(res as { body: string }).members.sort()).toEqual([
      'a',
      'b',
    ]);
    expect(await repo.getSkill(SCOPE, 'pack')).toBeUndefined();
    expect(await repo.getSkill(SCOPE, 'a')).toBeDefined();
  });
});

describe('usage / blast radius', () => {
  it('counts agents using a skill', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putAgent(agent('builder', ['reconcile']));
    await repo.putAgent(agent('analyst', ['reconcile', 'other']));
    await repo.putAgent(agent('idle', []));
    const res = await getUsage(
      httpEvent({
        method: 'GET',
        userId: MATT,
        org: ORG,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/usage',
      }),
      deps,
    );
    expect(bodyOf<{ count: number }>(res as { body: string }).count).toBe(2);
  });
});

describe('skill body round-trip', () => {
  it('persists + returns the SKILL.md body on create and read', async () => {
    const create = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', '# Reconcile\nmd') }),
      deps,
    );
    expect(create).toMatchObject({ statusCode: 201 });
    const read = await getSkill(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'reconcile' } }),
      deps,
    );
    expect(bodyOf<{ skill: Skill }>(read as { body: string }).skill.body).toBe('# Reconcile\nmd');
  });
});

describe('POST /skills/:name/scope (retired)', () => {
  it('responds 410 Gone', async () => {
    const res = await skillsHandler(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        rawPath: '/skills/reconcile/scope',
        path: { name: 'reconcile' },
        body: { scope: { tier: 'org', id: ORG } },
      }),
    );
    expect(res).toMatchObject({ statusCode: 410 });
  });
});
