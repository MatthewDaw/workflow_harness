import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Agent, ScopeRef, Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import {
  addMember,
  deleteSkill,
  dissolveBundle,
  flattenBundle,
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
const PROJ = 'weekly-compass';
const USER: ScopeRef = { tier: 'user', id: MATT };

function skill(name: string, scope: ScopeRef = USER): Skill {
  return { name, scope, kind: 'skill', description: '', source: 'local', members: [] };
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
