import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Agent, type Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { signDeviceToken } from '../src/auth/verify.js';
import {
  addMember,
  createSkill,
  deleteSkill,
  dissolveBundle,
  flattenBundle,
  foldIdea,
  getSkill,
  getUsage,
  promoteSkill,
  removeMember,
  resolveSkills,
} from '../src/rest/skills.js';
import type { Idea } from '@harness/shared';
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

/**
 * U21 — the delete path removes the skill's vector. We inject a fake S3Vectors
 * that records `deleteVectors` calls so delete tests never touch AWS and we can
 * assert the right index + key were used.
 */
const deletedVectors: Array<{ index: string; keys: string[] }> = [];
const fakeVectors = {
  deleteVectors: async (index: string, keys: string[]) => {
    deletedVectors.push({ index, keys });
  },
} as unknown as import('../src/embeddings/s3vectors.js').S3Vectors;

/**
 * U18 — the promote path replays golden cases via a Bedrock judge. We inject a
 * fake judge so promote tests never touch AWS. By default it reports every case
 * SATISFIED (clean candidate); a test can flip `goldenVerdict` to model a
 * regression. Each `judge` call is recorded so we can assert the replay ran and
 * against which candidate body.
 */
let goldenVerdict: { satisfied: boolean; reason: string } = { satisfied: true, reason: 'ok' };
const goldenCalls: Array<{ caseId: string; candidateBody: string }> = [];
const fakeGolden = {
  judge: async (c: { caseId: string; lesson: string }, candidateBody: string) => {
    goldenCalls.push({ caseId: c.caseId, candidateBody });
    return { caseId: c.caseId, lesson: c.lesson, ...goldenVerdict };
  },
} as unknown as import('../src/rerank/golden.js').GoldenJudge;

const deps = { repo, vectors: fakeVectors, golden: fakeGolden };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
  deletedVectors.length = 0;
  goldenCalls.length = 0;
  goldenVerdict = { satisfied: true, reason: 'ok' };
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

  it('resolves a UUID createdBy.name to the author profile name (email)', async () => {
    // claude+ device-token writes stamp createdBy.name with the Cognito sub; the
    // list should display the author's real PROFILE name instead.
    const sub = 'c4a8c4a8-30b1-70ef-792f-61824f6ca129';
    await repo.putUser({ userId: sub, name: 'mattdaw7@gmail.com', org: ORG });
    await repo.putSkill({ ...skill('gstack'), createdBy: { userId: sub, name: sub } });
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Skill[] }>(res as { body: string });
    const s = skills.find((x) => x.name === 'gstack')!;
    expect(s.createdBy).toEqual({ userId: sub, name: 'mattdaw7@gmail.com' });
  });

  it('leaves system-seeded and unresolvable authors untouched', async () => {
    await repo.putSkill({ ...skill('seeded'), createdBy: { userId: 'system', name: 'system' } });
    await repo.putSkill({ ...skill('orphan'), createdBy: { userId: 'ghost', name: 'ghost' } });
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Skill[] }>(res as { body: string });
    expect(skills.find((x) => x.name === 'seeded')!.createdBy).toEqual({
      userId: 'system',
      name: 'system',
    });
    expect(skills.find((x) => x.name === 'orphan')!.createdBy).toEqual({
      userId: 'ghost',
      name: 'ghost',
    });
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
    await repo.putSkill({
      ...skill('reconcile', 'old'),
      createdBy: { userId: 'alice', name: 'Alice' },
    });
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

  // U21 — skill delete/rename → idea orphan policy.
  it('cascades the skill\'s ideas to the bin and removes its vector', async () => {
    await repo.putSkill(skill('reconcile'));
    // Two ideas attached to the skill family; one is corroborated by two
    // distinct sessions (corroboration must survive the cascade as signal).
    await repo.putIdea({
      ideaId: 'i-1',
      skillBaseName: 'reconcile',
      org: ORG,
      text: 'Always reconcile in the ledger currency, never the display currency.',
      sources: [
        { sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1' },
        { sessionId: 's-2', segmentId: 'seg-1', seq: 2, snippet: 'ev2' },
      ],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 10,
      updatedAt: 10,
    });
    await repo.putIdea({
      ideaId: 'i-2',
      skillBaseName: 'reconcile',
      org: ORG,
      text: 'Round half-to-even at the boundary.',
      sources: [{ sessionId: 's-3', segmentId: 'seg-1', seq: 1, snippet: 'ev3' }],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 11,
      updatedAt: 11,
    });

    const res = await deleteSkill(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });

    // Skill gone, idea rows gone.
    expect(await repo.getSkill(SCOPE, 'reconcile')).toBeUndefined();
    expect(await repo.listIdeasForSkill(ORG, 'reconcile')).toEqual([]);

    // Ideas landed in the bin with corroboration (distinct-session sources)
    // and provenance (text) preserved.
    const bin = await repo.listUnassignedForOrg(ORG);
    expect(bin).toHaveLength(2);
    const byText = new Map(bin.map((e) => [e.entryId, e]));
    const moved = byText.get('reconcile#i-1')!;
    expect(moved.text).toContain('ledger currency');
    expect(new Set(moved.sources.map((s) => s.sessionId))).toEqual(new Set(['s-1', 's-2']));

    // The skill's vector was deleted with the right index + `<org>#<baseName>` key.
    expect(deletedVectors).toEqual([{ index: 'skills', keys: [`${ORG}#reconcile`] }]);
  });

  it('cascades by baseName, not the display name, for a forked skill', async () => {
    // A skill whose row name differs from its family baseName.
    await repo.putSkill({ ...skill('reconcile#R#repo#U#matt'), baseName: 'reconcile' });
    await repo.putIdea({
      ideaId: 'i-1',
      skillBaseName: 'reconcile',
      org: ORG,
      text: 'lesson',
      sources: [{ sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: '' }],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 1,
      updatedAt: 1,
    });

    await deleteSkill(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'reconcile#R#repo#U#matt' } }),
      deps,
    );

    expect(await repo.listIdeasForSkill(ORG, 'reconcile')).toEqual([]);
    expect(await repo.listUnassignedForOrg(ORG)).toHaveLength(1);
    // Vector key uses the family baseName.
    expect(deletedVectors).toEqual([{ index: 'skills', keys: [`${ORG}#reconcile`] }]);
  });

  it('deletes a skill with no ideas (vector still removed, bin untouched)', async () => {
    await repo.putSkill(skill('reconcile'));
    const res = await deleteSkill(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.listUnassignedForOrg(ORG)).toEqual([]);
    expect(deletedVectors).toEqual([{ index: 'skills', keys: [`${ORG}#reconcile`] }]);
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

/**
 * VERSIONING (KTD6) at the REST layer: create mints rev 1 of the base variant
 * and initializes TRUE; an update snapshots the NEXT revision of the same variant
 * rather than clobbering; promote (any authed member) repoints TRUE.
 */
describe('versioning: create snapshots rev 1 + initializes TRUE', () => {
  it('stamps baseName/variantId/version on create and sets the TRUE pointer', async () => {
    const res = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const { skill: created } = bodyOf<{ skill: Skill }>(res as { body: string });
    expect(created.variantId).toBe('reconcile');
    expect(created.baseName).toBe('reconcile');
    expect(created.version).toBe(1);

    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth).toMatchObject({ baseName: 'reconcile', variantId: 'reconcile', rev: 1 });
  });
});

describe('versioning: update snapshots the next revision (no clobber)', () => {
  it('advances the base variant to rev 2 and keeps a rev-1 snapshot', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    const put = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'reconcile' },
        body: skill('reconcile', 'v2'),
      }),
      deps,
    );
    expect(put).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ skill: Skill }>(put as { body: string }).skill.version).toBe(2);

    const revs = await repo.listRevisions(SCOPE, 'SKILL', 'reconcile');
    expect(revs.map((r) => r.version).sort()).toEqual([1, 2]);
    // The rev-1 snapshot is immutable — still the old body.
    expect((await repo.getRevision(SCOPE, 'SKILL', 'reconcile', 1))?.body).toBe('v1');
  });
});

describe('POST /skills/:name/promote (skill-edit gated, U16)', () => {
  it('repoints TRUE to the given variant for an admin', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'base') }),
      deps,
    );
    // Promote now requires the same skill-edit (admin) authority as the revision
    // write it points at — an admin repoints TRUE to a (hypothetical) fork variant.
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile#R#r#U#matt', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth).toEqual({ baseName: 'reconcile', variantId: 'reconcile#R#r#U#matt', rev: 1 });
  });

  it('forbids a non-admin promote (reconciled with the revision-write gate)', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'base') }),
      deps,
    );
    const res = await promoteSkill(
      httpEvent({
        method: 'POST',
        userId: 'bob',
        org: ORG,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile#R#r#U#bob', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('400s when variantId is missing', async () => {
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: {},
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('401s an unauthenticated promote', async () => {
    const res = await promoteSkill(
      httpEvent({
        method: 'POST',
        userId: null,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
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

/**
 * The claude+ wrapper writes the catalog with its HS256 DEVICE TOKEN (no Cognito
 * gateway, no `custom:admin` claim). The write routes are HttpNoneAuthorizer, so
 * the token arrives as a raw `Authorization: Bearer` header and the handler must
 * (a) verify it via resolvePrincipal and (b) decide admin SERVER-SIDE from the
 * caller's PROFILE (`adminOrgs`) — that combination is what makes /hq-add-skill
 * able to register skills directly instead of routing through the git seed.
 */
describe('device-token catalog writes (claude+ wrapper, server-side admin)', () => {
  const SECRET = new TextEncoder().encode('test-device-secret');

  beforeEach(() => {
    process.env.DEVICE_TOKEN_SECRET = 'test-device-secret';
  });

  async function deviceEvent(
    userId: string,
    opts: { method: string; path?: Record<string, string>; body?: unknown; rawPath?: string },
  ) {
    const token = await signDeviceToken({ userId, org: ORG }, { secret: SECRET });
    return httpEvent({
      method: opts.method,
      userId: null, // no Cognito jwt claims — only the bearer header
      headers: { authorization: `Bearer ${token}` },
      path: opts.path,
      rawPath: opts.rawPath,
      body: opts.body,
    });
  }

  it('lets a device-token org admin (adminOrgs) create a skill', async () => {
    await repo.putUser({ userId: MATT, org: ORG, orgs: [ORG], adminOrgs: [ORG], admin: true });
    const res = await createSkill(
      await deviceEvent(MATT, { method: 'POST', body: skill('reconcile', '# md') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    const stored = await repo.getSkill(SCOPE, 'reconcile');
    expect(stored?.body).toBe('# md');
    // Authorship is stamped from the verified device principal.
    expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
  });

  it('forbids a device-token member who is NOT an org admin', async () => {
    // Profile exists and is in the org, but adminOrgs does not list it.
    await repo.putUser({ userId: 'bob', org: ORG, orgs: [ORG], adminOrgs: [], admin: false });
    const res = await createSkill(
      await deviceEvent('bob', { method: 'POST', body: skill('reconcile') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('forbids a device token with no profile (admin cannot be derived)', async () => {
    const res = await createSkill(
      await deviceEvent('ghost', { method: 'POST', body: skill('reconcile') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
  });

  it('lets a device-token admin delete a skill', async () => {
    await repo.putUser({ userId: MATT, org: ORG, orgs: [ORG], adminOrgs: [ORG], admin: true });
    await repo.putSkill(skill('reconcile'));
    const res = await deleteSkill(
      await deviceEvent(MATT, { method: 'DELETE', path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getSkill(SCOPE, 'reconcile')).toBeUndefined();
  });

  it('lets a device-token caller read a skill by name (public GET route)', async () => {
    await repo.putUser({ userId: MATT, org: ORG, orgs: [ORG] });
    await repo.putSkill(skill('reconcile', 'body'));
    const res = await getSkill(
      await deviceEvent(MATT, { method: 'GET', path: { name: 'reconcile' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ skill: Skill }>(res as { body: string }).skill.body).toBe('body');
  });

  it('401s when the bearer token is absent entirely', async () => {
    const res = await createSkill(
      httpEvent({ method: 'POST', userId: null, body: skill('reconcile') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });
});

/**
 * Canonical built-ins (`source:'built-in'`) are owned by the git seed: the DB is the
 * single runtime source of truth, but a built-in's BASE variant is updated ONLY by
 * the seed, never mutated in place through REST. Edits must FORK (set repoId +
 * authorUserId), which the variant model already supports; deletes and built-in
 * bundle-membership edits are rejected (manage via .claude/skills + re-seed).
 */
describe('built-ins are fork-only via REST (git-seed owned)', () => {
  const builtinSkill = (name: string, body = ''): Skill => ({ ...skill(name, body), source: 'built-in' });

  it('rejects an in-place PUT to a built-in base (409), leaving it untouched', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    const res = await createSkill(
      adminEvent({ method: 'PUT', userId: MATT, path: { name: 'hq-add-skill' }, body: skill('hq-add-skill', 'edited') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    const stored = await repo.getSkill(SCOPE, 'hq-add-skill');
    expect(stored?.source).toBe('built-in');
    expect(stored?.body).toBe('canonical');
  });

  it('rejects a POST that would clobber a built-in base (409)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill'));
    const res = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('hq-add-skill', 'new') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('ALLOWS forking a built-in (repoId + authorUserId set)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill'));
    const res = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'hq-add-skill' },
        body: { ...skill('hq-add-skill', 'my fork'), repoId: 'repo1', authorUserId: MATT },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it('rejects deleting a built-in (409)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill'));
    const res = await deleteSkill(
      adminEvent({ method: 'DELETE', userId: MATT, path: { name: 'hq-add-skill' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    expect(await repo.getSkill(SCOPE, 'hq-add-skill')).toBeDefined();
  });

  it('rejects mutating a built-in bundle membership (409)', async () => {
    await repo.putSkill(skill('extra'));
    await repo.putSkill({ ...bundle('command-hq-starter', ['a']), source: 'built-in' });
    const res = await addMember(
      adminEvent({ method: 'POST', userId: MATT, path: { name: 'command-hq-starter' }, body: { member: 'extra' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('still allows editing a non-built-in skill in place', async () => {
    await repo.putSkill(skill('reconcile', 'v1'));
    const res = await createSkill(
      adminEvent({ method: 'PUT', userId: MATT, path: { name: 'reconcile' }, body: skill('reconcile', 'v2') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
  });
});

/**
 * Bundle-overwrite guard: `claude+ sync` is bidirectional and upserts catalog
 * records BY NAME from local skills, so a local skill dir whose name equals a
 * bundle would be pushed over the bundle and wipe its members (this destroyed a
 * 35-member bundle once). A non-bundle skill write must NOT clobber an existing
 * `kind:bundle` of the same name — the server rejects it with 409.
 */
describe('bundle-overwrite guard (skill push must not clobber a bundle)', () => {
  it('rejects a POST skill that collides with an existing bundle name (409), leaving members intact', async () => {
    await repo.putSkill(bundle('finance-pack', ['a', 'b']));
    const res = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('finance-pack', 'local skill') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    const stored = await repo.getSkill(SCOPE, 'finance-pack');
    expect(stored?.kind).toBe('bundle');
    expect(stored?.members).toEqual(['a', 'b']);
  });

  it('rejects a PUT skill over an existing bundle name (409)', async () => {
    await repo.putSkill(bundle('finance-pack', ['a', 'b']));
    const res = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'finance-pack' },
        body: skill('finance-pack', 'local skill'),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    expect((await repo.getSkill(SCOPE, 'finance-pack'))?.members).toEqual(['a', 'b']);
  });

  it('still ALLOWS updating a bundle with a bundle (kind matches)', async () => {
    await repo.putSkill(bundle('finance-pack', ['a']));
    const res = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'finance-pack' },
        body: bundle('finance-pack', ['a', 'b']),
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getSkill(SCOPE, 'finance-pack'))?.members).toEqual(['a', 'b']);
  });

  it('still ALLOWS a normal skill create when no bundle of that name exists', async () => {
    const res = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'body') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
  });
});

/**
 * U16 — fold an idea into a NEW skill revision. Fold reuses `putNewVersion`, so a
 * fold into a built-in FORKS a variant (never an in-place overwrite of the
 * git-seeded base) and leaves the org-wide TRUE pointer where it was until a human
 * promotes. After the revision write the idea is marked `folded` with
 * `foldedIntoRev` via the optimistic-concurrency conditional, so a fold racing an
 * incoming corroboration is safe. Fold is admin-gated (skill-edit permission).
 */
describe('POST /skills/:name/ideas/:ideaId/fold (U16)', () => {
  const builtinSkill = (name: string, body = ''): Skill => ({
    ...skill(name, body),
    source: 'built-in',
  });

  function idea(over: Partial<Idea> = {}): Idea {
    return {
      ideaId: 'i-1',
      skillBaseName: 'reconcile',
      org: ORG,
      text: 'Always reconcile in the ledger currency.',
      sources: [
        { sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1' },
        { sessionId: 's-2', segmentId: 'seg-1', seq: 2, snippet: 'ev2' },
      ],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 10,
      updatedAt: 10,
      ...over,
    };
  }

  it('folds into a NON-built-in in place: new revision, idea flips to folded, TRUE unchanged', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    await repo.putIdea(idea());
    const truthBefore = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');

    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: 'v1\n\n## Folded\nUse the ledger currency.' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res as { body: string });
    // A new revision was snapshotted (rev 2 of the base variant), body merged.
    expect(stamped.version).toBe(2);
    expect(stamped.body).toContain('Folded');

    // TRUE is unchanged — a human promotes separately.
    expect(await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile')).toEqual(truthBefore);

    // The idea flipped to folded, stamped with the rev it folded into.
    const folded = await repo.getIdea(ORG, 'reconcile', 'i-1');
    expect(folded?.status).toBe('folded');
    expect(folded?.foldedIntoRev).toBe(2);
  });

  it('folds into a BUILT-IN by forking a variant (no 409)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    await repo.putIdea(idea({ skillBaseName: 'hq-add-skill' }));

    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'hq-add-skill', ideaId: 'i-1' },
        rawPath: '/skills/hq-add-skill/ideas/i-1/fold',
        body: { body: 'canonical + fold', repoId: 'repo1', authorUserId: MATT },
      }),
      deps,
    );
    // Forking is allowed: the built-in guard only rejects an in-place fold.
    expect(res).toMatchObject({ statusCode: 200 });
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res as { body: string });
    expect(stamped.repoId).toBe('repo1');
    expect(stamped.variantId).toBe('hq-add-skill#R#repo1#U#matt');
    expect((await repo.getIdea(ORG, 'hq-add-skill', 'i-1'))?.status).toBe('folded');
  });

  it('rejects an in-place fold of a BUILT-IN (409), leaving the idea open', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    await repo.putIdea(idea({ skillBaseName: 'hq-add-skill' }));
    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'hq-add-skill', ideaId: 'i-1' },
        rawPath: '/skills/hq-add-skill/ideas/i-1/fold',
        body: { body: 'edited base' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
    // Base untouched, idea still open.
    expect((await repo.getSkill(SCOPE, 'hq-add-skill'))?.body).toBe('canonical');
    expect((await repo.getIdea(ORG, 'hq-add-skill', 'i-1'))?.status).toBe('open');
  });

  it('a promote AFTER a fold repoints TRUE to the folded revision', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    await repo.putIdea(idea());
    await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: 'v2 with fold' },
      }),
      deps,
    );
    // The fold left TRUE at rev 1; a human promotes the new rev 2.
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile', rev: 2 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile')).toEqual({
      baseName: 'reconcile',
      variantId: 'reconcile',
      rev: 2,
    });
  });

  it('forbids a non-admin device token folding', async () => {
    process.env.DEVICE_TOKEN_SECRET = 'test-device-secret';
    const SECRET = new TextEncoder().encode('test-device-secret');
    await repo.putUser({ userId: 'bob', org: ORG, orgs: [ORG], adminOrgs: [], admin: false });
    await repo.putSkill(skill('reconcile', 'v1'));
    await repo.putIdea(idea());
    const token = await signDeviceToken({ userId: 'bob', org: ORG }, { secret: SECRET });
    const res = await foldIdea(
      httpEvent({
        method: 'POST',
        userId: null,
        headers: { authorization: `Bearer ${token}` },
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: 'v2' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    // Idea untouched.
    expect((await repo.getIdea(ORG, 'reconcile', 'i-1'))?.status).toBe('open');
  });

  it('404s when the idea does not exist', async () => {
    await repo.putSkill(skill('reconcile', 'v1'));
    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'ghost' },
        rawPath: '/skills/reconcile/ideas/ghost/fold',
        body: { body: 'v2' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('the conditional mark prevents a fold↔corroboration race (C3)', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    await repo.putIdea(idea({ corroborationVersion: 0 }));

    // Simulate the race precisely: a concurrent corroboration lands BETWEEN the
    // fold handler's `getIdea` (which observes v0) and its conditional mark. We
    // wrap the repo so the first `getIdea` returns the v0 snapshot the fold reads,
    // then immediately writes a corroboration that bumps v0 -> v1 underneath it.
    let raced = false;
    const racingRepo = new Proxy(repo, {
      get(target, prop, receiver) {
        if (prop === 'getIdea') {
          return async (...args: Parameters<typeof target.getIdea>) => {
            const snapshot = await target.getIdea(...args);
            if (!raced && snapshot) {
              raced = true;
              await target.corroborateIdeaConditional(
                {
                  ...snapshot,
                  sources: [
                    ...snapshot.sources,
                    { sessionId: 's-3', segmentId: 'seg-1', seq: 3, snippet: 'ev3' },
                  ],
                },
                snapshot.corroborationVersion,
              );
            }
            return snapshot; // the fold still holds the STALE v0 view
          };
        }
        return Reflect.get(target, prop, receiver);
      },
    });

    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: 'v2' },
      }),
      { repo: racingRepo, vectors: fakeVectors },
    );
    // The mark is guarded on the stale v0; the racing corroboration advanced it,
    // so the conditional fails → 409 (the revision was written, the mark was not).
    expect(res).toMatchObject({ statusCode: 409 });
    // The race-winning corroboration is intact: still OPEN, third session kept.
    const after = await repo.getIdea(ORG, 'reconcile', 'i-1');
    expect(after?.status).toBe('open');
    expect(new Set(after?.sources.map((s) => s.sessionId))).toEqual(
      new Set(['s-1', 's-2', 's-3']),
    );
  });
});

/**
 * U18 — golden-set regression at fold. A fold captures its before→after as a
 * golden case co-located with the skill; a later promote replays the skill's
 * golden cases against the candidate revision body via a (mocked) Bedrock judge
 * and SURFACES any regression. v1 is advisory — the promote still succeeds.
 */
describe('U18: golden-set regression (fold records, promote replays)', () => {
  function goldIdea(over: Partial<Idea> = {}): Idea {
    return {
      ideaId: 'i-1',
      skillBaseName: 'reconcile',
      org: ORG,
      text: 'Always reconcile in the ledger currency.',
      sources: [{ sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1' }],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 10,
      updatedAt: 10,
      ...over,
    };
  }

  async function seedFold(foldBody: string) {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'before-body') }),
      deps,
    );
    await repo.putIdea(goldIdea());
    return foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: foldBody },
      }),
      deps,
    );
  }

  it('folding writes a golden case (before→after + lesson)', async () => {
    await seedFold('before-body\n\n## Folded\nUse the ledger currency.');
    const cases = await repo.listGoldenCasesForSkill(ORG, 'reconcile');
    expect(cases).toHaveLength(1);
    const c = cases[0]!;
    expect(c.caseId).toBe('i-1');
    expect(c.ideaId).toBe('i-1');
    expect(c.lesson).toBe('Always reconcile in the ledger currency.');
    expect(c.before).toBe('before-body');
    expect(c.after).toContain('Use the ledger currency.');
    expect(c.foldedIntoRev).toBe(2);
  });

  it('a clean candidate passes replay (no regressions, promote succeeds)', async () => {
    await seedFold('before-body\n\n## Folded\nUse the ledger currency.');
    goldenVerdict = { satisfied: true, reason: 'still present' };
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile', rev: 2 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const out = bodyOf<{ goldenReplay: unknown[]; goldenRegressions: unknown[] }>(
      res as { body: string },
    );
    expect(out.goldenReplay).toHaveLength(1);
    expect(out.goldenRegressions).toHaveLength(0);
    // The replay judged the candidate body (the folded rev 2), not the base.
    expect(goldenCalls).toHaveLength(1);
    expect(goldenCalls[0]!.candidateBody).toContain('Use the ledger currency.');
  });

  it('a candidate that BREAKS a prior golden case is flagged before promote (advisory)', async () => {
    await seedFold('before-body\n\n## Folded\nUse the ledger currency.');
    // The judge reports the lesson is gone in the candidate → a regression.
    goldenVerdict = { satisfied: false, reason: 'the ledger-currency guidance was removed' };
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile', rev: 2 },
      }),
      deps,
    );
    // ADVISORY: the regression is surfaced but the promote still succeeds.
    expect(res).toMatchObject({ statusCode: 200 });
    const out = bodyOf<{
      true: { rev: number };
      goldenRegressions: Array<{ caseId: string; reason: string }>;
    }>(res as { body: string });
    expect(out.true.rev).toBe(2);
    expect(out.goldenRegressions).toHaveLength(1);
    expect(out.goldenRegressions[0]!.caseId).toBe('i-1');
    expect(out.goldenRegressions[0]!.reason).toMatch(/removed/);
  });

  it('a promote with NO golden cases skips replay entirely', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'v1') }),
      deps,
    );
    const res = await promoteSkill(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const out = bodyOf<{ goldenReplay: unknown[] }>(res as { body: string });
    expect(out.goldenReplay).toHaveLength(0);
    expect(goldenCalls).toHaveLength(0);
  });

  it('replay is org-scoped: org B never replays org A golden cases', async () => {
    // Org A folds an idea → a golden case under org A.
    await seedFold('before-body\n\n## Folded\nUse the ledger currency.');
    // Org B has its OWN reconcile skill, no golden cases. A promote in org B
    // must not see org A's case (the cases are partitioned by `SCOPE#org#`).
    const ORG_B = 'globex';
    await createSkill(
      httpEvent({
        org: ORG_B,
        admin: true,
        method: 'POST',
        userId: MATT,
        body: { ...skill('reconcile', 'b-body'), scope: orgScope(ORG_B) },
      }),
      deps,
    );
    const res = await promoteSkill(
      httpEvent({
        org: ORG_B,
        admin: true,
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile' },
        rawPath: '/skills/reconcile/promote',
        body: { variantId: 'reconcile', rev: 1 },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const out = bodyOf<{ goldenReplay: unknown[] }>(res as { body: string });
    expect(out.goldenReplay).toHaveLength(0);
    // Org A's lone case still belongs to org A.
    expect(await repo.listGoldenCasesForSkill(ORG, 'reconcile')).toHaveLength(1);
    expect(await repo.listGoldenCasesForSkill(ORG_B, 'reconcile')).toHaveLength(0);
  });
});
