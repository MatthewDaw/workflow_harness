import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Agent, Project, ScopeRef, Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  addMember,
  changeScope,
  createSkill,
  deleteSkill,
  dissolveBundle,
  flattenBundle,
  getSkill,
  getUsage,
  removeMember,
  resolveSkills,
} from '../src/rest/skills.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U9 REST: skills + bundles. add/remove/eject members, nested-bundle transitive
 * resolution, dissolve, and skill usage (blast radius) counts.
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
const USER: ScopeRef = { tier: 'user', id: MATT };

function skill(name: string, scope: ScopeRef = USER, body = ''): Skill {
  return { name, scope, kind: 'skill', description: '', source: 'local', members: [], body };
}

function project(id: string, owner: string): Project {
  return { id, name: id, repo: `gh/acme/${id}`, ownerUserId: owner, liveSessionCount: 0 };
}
function bundle(name: string, members: string[], scope: ScopeRef = USER): Skill {
  return { name, scope, kind: 'bundle', description: '', source: 'local', members };
}
function agent(name: string, skills: string[], scope: ScopeRef = USER): Agent {
  return { name, scope, model: 'opus', prompt: '', skills, tools: [] };
}

function memberEvent(name: string, member: string) {
  return httpEvent({
    method: 'POST',
    userId: MATT,
    path: { name },
    query: { tier: 'user', id: MATT },
    body: { member },
  });
}

describe('bundle membership', () => {
  it('adds a standalone skill into a bundle', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putSkill(bundle('finance-pack', []));

    const res = await addMember(memberEvent('finance-pack', 'reconcile'), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const stored = await repo.getSkill(USER, 'finance-pack');
    expect(stored?.members).toEqual(['reconcile']);
    // The standalone skill record is untouched.
    expect(await repo.getSkill(USER, 'reconcile')).toBeDefined();
  });

  it('ejects a member, leaving it standalone', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putSkill(bundle('finance-pack', ['reconcile']));

    const res = await removeMember(
      httpEvent({
        method: 'DELETE',
        userId: MATT,
        path: { name: 'finance-pack', member: 'reconcile' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getSkill(USER, 'finance-pack'))?.members).toEqual([]);
    expect(await repo.getSkill(USER, 'reconcile')).toBeDefined();
  });

  it('rejects adding a member to a non-bundle skill', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await addMember(memberEvent('reconcile', 'x'), deps);
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('nested bundles', () => {
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

  it('resolution endpoint annotates a bundle with transitive members', async () => {
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putSkill(bundle('inner', ['a', 'b']));
    await repo.putSkill(bundle('outer', ['inner']));

    const res = await resolveSkills(
      httpEvent({ method: 'GET', userId: MATT, query: { project: PROJ } }),
      deps,
    );
    const { skills } = bodyOf<{ skills: Array<Skill & { resolvedMembers?: string[] }> }>(
      res as { body: string },
    );
    const outer = skills.find((s) => s.name === 'outer')!;
    expect(outer.resolvedMembers!.sort()).toEqual(['a', 'b']);
  });
});

describe('dissolve', () => {
  it('removes the bundle but leaves members standalone', async () => {
    await repo.putSkill(skill('a'));
    await repo.putSkill(skill('b'));
    await repo.putSkill(bundle('pack', ['a', 'b']));

    const res = await dissolveBundle(
      httpEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/skills/pack/dissolve',
        path: { name: 'pack' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ members: string[] }>(res as { body: string }).members.sort()).toEqual([
      'a',
      'b',
    ]);
    expect(await repo.getSkill(USER, 'pack')).toBeUndefined();
    expect(await repo.getSkill(USER, 'a')).toBeDefined();
    expect(await repo.getSkill(USER, 'b')).toBeDefined();
  });
});

describe('usage / blast radius', () => {
  it('returns the count of agents using a skill on delete', async () => {
    await repo.putSkill(skill('reconcile'));
    await repo.putAgent(agent('builder', ['reconcile']));
    await repo.putAgent(agent('analyst', ['reconcile', 'other']));
    await repo.putAgent(agent('idle', []));

    const res = await deleteSkill(
      httpEvent({
        method: 'DELETE',
        userId: MATT,
        path: { name: 'reconcile' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ usageCount: number }>(res as { body: string }).usageCount).toBe(2);
  });
});

describe('skill body round-trip', () => {
  it('persists + returns the SKILL.md body on create and read', async () => {
    const create = await createSkill(
      httpEvent({
        method: 'POST',
        userId: MATT,
        body: skill('reconcile', USER, '# Reconcile\nfull markdown'),
      }),
      deps,
    );
    expect(create).toMatchObject({ statusCode: 201 });
    expect((await repo.getSkill(USER, 'reconcile'))?.body).toBe('# Reconcile\nfull markdown');

    const read = await getSkill(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { name: 'reconcile' },
        query: { tier: 'user', id: MATT },
      }),
      deps,
    );
    expect(bodyOf<{ skill: Skill }>(read as { body: string }).skill.body).toBe(
      '# Reconcile\nfull markdown',
    );
  });
});

describe('GET /skills/:name (explicit-scope read, IDOR)', () => {
  it("404s (not 403) a read of another user's user-scope skill", async () => {
    await repo.putSkill(skill('alices', { tier: 'user', id: 'alice' }));
    const res = await getSkill(
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

  it("404s a usage read scoped to another tenant's project", async () => {
    await repo.putProject(project('alices-proj', 'alice'));
    const res = await getUsage(
      httpEvent({
        method: 'GET',
        userId: MATT,
        path: { name: 'x' },
        query: { tier: 'project', id: 'alices-proj' },
        rawPath: '/skills/x/usage',
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('POST /skills/:name/scope (elevate/demote)', () => {
  it('elevates a user skill to org scope for an admin, rewriting the key', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await changeScope(
      httpEvent({
        method: 'POST',
        userId: MATT,
        admin: true,
        org: ORG,
        rawPath: '/skills/reconcile/scope',
        path: { name: 'reconcile' },
        query: { tier: 'user', id: MATT },
        body: { scope: { tier: 'org', id: ORG } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getSkill(USER, 'reconcile')).toBeUndefined();
    expect(await repo.getSkill({ tier: 'org', id: ORG }, 'reconcile')).toBeDefined();
  });

  it('forbids elevating to org without admin', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await changeScope(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        path: { name: 'reconcile' },
        query: { tier: 'user', id: MATT },
        body: { scope: { tier: 'org', id: ORG } },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect(await repo.getSkill(USER, 'reconcile')).toBeDefined();
  });
});
