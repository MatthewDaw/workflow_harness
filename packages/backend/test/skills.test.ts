import { beforeEach, describe, expect, it } from 'vitest';
import {
  addMember,
  createSkill,
  deleteSkill,
  dissolveBundle,
  foldIdea,
  foldTargetVariant,
  getSkill,
  getUsage,
  promoteSkill,
  removeMember,
  resolveSkills,
} from '../src/rest/skills.js';
import { flattenBundle } from '../src/rest/bundles.js';
import type { Idea, Skill } from '@harness/shared';
import { orgScope } from '@harness/shared';
import { handler as skillsHandler } from '../src/rest/skills.js';
import { memRepoHarness } from './helpers/memtable.js';
import { adminEvent, bodyOf, deviceTokenEvent, httpEvent } from './helpers/httpevent.js';
import {
  MATT,
  ORG,
  SCOPE,
  makeAgent as agent,
  makeBundle as bundle,
  makeSkill as skill,
} from './helpers/factories.js';
import { describeOrgCatalogContract } from './helpers/catalog-contract.js';

/**
 * Org-catalog skills + bundles. Skills are org-only (3-tier scope retired).
 * The generic REST contract (list, admin-gated writes, createdBy, versioning +
 * promote, built-ins fork-only) runs via describeOrgCatalogContract; this file
 * keeps the skill-specific behavior: bundles, the idea cascade on delete, the
 * device-token write path, fold (U16/U19), and golden replay (U18).
 */

const { repo } = memRepoHarness();

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
  deletedVectors.length = 0;
  goldenCalls.length = 0;
  goldenVerdict = { satisfied: true, reason: 'ok' };
});

const builtinSkill = (name: string, body = ''): Skill => ({
  ...skill(name, body),
  source: 'built-in',
});

describeOrgCatalogContract<Skill>({
  noun: 'skill',
  plural: 'skills',
  entity: 'SKILL',
  repo,
  sampleNames: ['reconcile', 'forecast'],
  builtinName: 'hq-add-skill',
  make: (name) => skill(name),
  makeBuiltin: (name) => builtinSkill(name, 'canonical'),
  mutate: (s) => ({ ...s, body: 'new body' }),
  assertMutated: (stored) => expect(stored?.body).toBe('new body'),
  create: (e) => createSkill(e, deps),
  remove: (e) => deleteSkill(e, deps),
  get: (e) => getSkill(e, deps),
  list: (e) => resolveSkills(e, deps),
  promote: (e) => promoteSkill(e, deps),
  put: (s) => repo.putSkill(s),
  read: (name) => repo.getSkill(SCOPE, name),
  nonAdminPromote: 'forbidden',
  assertBuiltinUntouched: (stored) => {
    expect(stored?.source).toBe('built-in');
    expect(stored?.body).toBe('canonical');
  },
});

describe('GET /skills (org catalog, author resolution + bundle annotation)', () => {
  it('resolves a UUID createdBy.name to the author profile name (email)', async () => {
    // claude+ device-token writes stamp createdBy.name with the Cognito sub; the
    // list should display the author's real PROFILE name instead.
    const sub = 'c4a8c4a8-30b1-70ef-792f-61824f6ca129';
    await repo.putUser({ userId: sub, name: 'mattdaw7@gmail.com', org: ORG });
    await repo.putSkill({ ...skill('gstack'), createdBy: { userId: sub, name: sub } });
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Skill[] }>(res);
    const s = skills.find((x) => x.name === 'gstack')!;
    expect(s.createdBy).toEqual({ userId: sub, name: 'mattdaw7@gmail.com' });
  });

  it('leaves system-seeded and unresolvable authors untouched', async () => {
    await repo.putSkill({ ...skill('seeded'), createdBy: { userId: 'system', name: 'system' } });
    await repo.putSkill({ ...skill('orphan'), createdBy: { userId: 'ghost', name: 'ghost' } });
    const res = await resolveSkills(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { skills } = bodyOf<{ skills: Skill[] }>(res);
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
      res,
    );
    const outer = skills.find((s) => s.name === 'outer')!;
    expect(outer.resolvedMembers!.sort()).toEqual(['a', 'b']);
  });
});

describe('DELETE /skills/:name (idea cascade, U21)', () => {
  // U21 — skill delete/rename → idea orphan policy.
  it("cascades the skill's ideas to the bin and removes its vector", async () => {
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
    expect(bodyOf<{ members: string[] }>(res).members.sort()).toEqual([
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
    expect(bodyOf<{ count: number }>(res).count).toBe(2);
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
    expect(bodyOf<{ skill: Skill }>(read).skill.body).toBe('# Reconcile\nmd');
  });
});

/**
 * VERSIONING (KTD6) at the REST layer beyond the shared contract: an update
 * snapshots the NEXT revision of the same variant rather than clobbering.
 */
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
    expect(bodyOf<{ skill: Skill }>(put).skill.version).toBe(2);

    const revs = await repo.listRevisions(SCOPE, 'SKILL', 'reconcile');
    expect(revs.map((r) => r.version).sort()).toEqual([1, 2]);
    // The rev-1 snapshot is immutable — still the old body.
    expect((await repo.getRevision(SCOPE, 'SKILL', 'reconcile', 1))?.body).toBe('v1');
  });
});

describe('POST /skills/:name/promote (skill-edit gated, U16)', () => {
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

/**
 * The claude+ wrapper writes the catalog with its HS256 DEVICE TOKEN (no Cognito
 * gateway, no `custom:admin` claim). The write routes are HttpNoneAuthorizer, so
 * the token arrives as a raw `Authorization: Bearer` header and the handler must
 * (a) verify it via resolvePrincipal and (b) decide admin SERVER-SIDE from the
 * caller's PROFILE (`adminOrgs`) — that combination is what makes /hq-add-skill
 * able to register skills directly instead of routing through the git seed.
 */
describe('device-token catalog writes (claude+ wrapper, server-side admin)', () => {
  const deviceEvent = (
    userId: string,
    opts: { method: string; path?: Record<string, string>; body?: unknown; rawPath?: string },
  ) => deviceTokenEvent({ userId, org: ORG, ...opts });

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
    expect(bodyOf<{ skill: Skill }>(res).skill.body).toBe('body');
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
describe('built-ins are fork-only via REST (skill-specific guards)', () => {
  it('rejects a POST that would clobber a built-in base (409)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill'));
    const res = await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('hq-add-skill', 'new') }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('rejects mutating a built-in bundle membership (409)', async () => {
    await repo.putSkill(skill('extra'));
    await repo.putSkill({ ...bundle('command-hq-starter', ['a']), source: 'built-in' });
    const res = await addMember(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'command-hq-starter' },
        body: { member: 'extra' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('still allows editing a non-built-in skill in place', async () => {
    await repo.putSkill(skill('reconcile', 'v1'));
    const res = await createSkill(
      adminEvent({
        method: 'PUT',
        userId: MATT,
        path: { name: 'reconcile' },
        body: skill('reconcile', 'v2'),
      }),
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
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res);
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
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res);
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
    await repo.putUser({ userId: 'bob', org: ORG, orgs: [ORG], adminOrgs: [], admin: false });
    await repo.putSkill(skill('reconcile', 'v1'));
    await repo.putIdea(idea());
    const res = await foldIdea(
      await deviceTokenEvent({
        method: 'POST',
        userId: 'bob',
        org: ORG,
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
    expect(new Set(after?.sources.map((s) => s.sessionId))).toEqual(new Set(['s-1', 's-2', 's-3']));
  });

  // U19 — VARIANT-SCOPED FOLDING. The fold targets the variant implied by the
  // idea's PROVENANCE: when the contributing sources all carry the same `repoId`,
  // the lesson is repo-specific and the fold forks that variant line even though
  // the request body names no repo. Cross-repo / no-repo provenance targets base.
  it('folds into the repo variant implied by the idea provenance (no body repoId)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    // Every source agrees on repo "repoX" — a repo-scoped lesson.
    await repo.putIdea(
      idea({
        skillBaseName: 'hq-add-skill',
        sources: [
          { sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1', repoId: 'repoX' },
          { sessionId: 's-2', segmentId: 'seg-1', seq: 2, snippet: 'ev2', repoId: 'repoX' },
        ],
      }),
    );
    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'hq-add-skill', ideaId: 'i-1' },
        rawPath: '/skills/hq-add-skill/ideas/i-1/fold',
        // No repoId in the body — it must come from provenance.
        body: { body: 'canonical + repo-scoped fold' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res);
    // Forked the repoX variant (no 409 against the built-in), authored by the caller.
    expect(stamped.repoId).toBe('repoX');
    expect(stamped.authorUserId).toBe(MATT);
    expect(stamped.variantId).toBe('hq-add-skill#R#repoX#U#matt');
    // The canonical base is untouched; a new variant revision exists.
    expect((await repo.getSkill(SCOPE, 'hq-add-skill'))?.body).toBe('canonical + repo-scoped fold');
    const variantRevs = await repo.listRevisions(SCOPE, 'SKILL', 'hq-add-skill', {
      repoId: 'repoX',
      userId: MATT,
    });
    expect(variantRevs).toHaveLength(1);
  });

  it('an explicit body repoId overrides the idea provenance', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    await repo.putIdea(
      idea({
        skillBaseName: 'hq-add-skill',
        sources: [
          { sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1', repoId: 'repoX' },
        ],
      }),
    );
    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'hq-add-skill', ideaId: 'i-1' },
        rawPath: '/skills/hq-add-skill/ideas/i-1/fold',
        body: { body: 'fork override', repoId: 'repoChosen', authorUserId: 'bob' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res);
    expect(stamped.repoId).toBe('repoChosen');
    expect(stamped.authorUserId).toBe('bob');
  });

  it('rejects an in-place built-in fold when provenance is empty or conflicting (targets base)', async () => {
    await repo.putSkill(builtinSkill('hq-add-skill', 'canonical'));
    // Sources disagree on repo → no single repoId → base target → built-in guard 409.
    await repo.putIdea(
      idea({
        skillBaseName: 'hq-add-skill',
        sources: [
          { sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1', repoId: 'repoA' },
          { sessionId: 's-2', segmentId: 'seg-1', seq: 2, snippet: 'ev2', repoId: 'repoB' },
        ],
      }),
    );
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
    expect((await repo.getSkill(SCOPE, 'hq-add-skill'))?.body).toBe('canonical');
  });
});

/**
 * U19 — the pure provenance→variant selector, isolated from the handler.
 */
describe('U19: foldTargetVariant (provenance → variant)', () => {
  const baseIdea = (sources: Idea['sources']): Idea => ({
    ideaId: 'i-1',
    skillBaseName: 'reconcile',
    org: ORG,
    text: '',
    sources,
    status: 'open',
    corroborationVersion: 0,
    createdAt: 0,
    updatedAt: 0,
  });

  it('infers the single repoId shared by all sources, authored by the caller', () => {
    const target = foldTargetVariant(
      baseIdea([
        { sessionId: 's-1', segmentId: 'a', seq: 1, snippet: '', repoId: 'r1' },
        { sessionId: 's-2', segmentId: 'b', seq: 2, snippet: '', repoId: 'r1' },
      ]),
      {},
      'matt',
    );
    expect(target).toEqual({ repoId: 'r1', authorUserId: 'matt' });
  });

  it('targets the base when sources disagree on repoId', () => {
    const target = foldTargetVariant(
      baseIdea([
        { sessionId: 's-1', segmentId: 'a', seq: 1, snippet: '', repoId: 'r1' },
        { sessionId: 's-2', segmentId: 'b', seq: 2, snippet: '', repoId: 'r2' },
      ]),
      {},
      'matt',
    );
    expect(target).toEqual({});
  });

  it('targets the base when a source is missing a repoId (partial provenance)', () => {
    const target = foldTargetVariant(
      baseIdea([
        { sessionId: 's-1', segmentId: 'a', seq: 1, snippet: '', repoId: 'r1' },
        { sessionId: 's-2', segmentId: 'b', seq: 2, snippet: '' },
      ]),
      {},
      'matt',
    );
    expect(target).toEqual({});
  });

  it('targets the base when no source carries a repoId', () => {
    const target = foldTargetVariant(
      baseIdea([{ sessionId: 's-1', segmentId: 'a', seq: 1, snippet: '' }]),
      {},
      'matt',
    );
    expect(target).toEqual({});
  });

  it('an explicit body repoId/author overrides provenance', () => {
    const target = foldTargetVariant(
      baseIdea([{ sessionId: 's-1', segmentId: 'a', seq: 1, snippet: '', repoId: 'r1' }]),
      { repoId: 'chosen', authorUserId: 'bob' },
      'matt',
    );
    expect(target).toEqual({ repoId: 'chosen', authorUserId: 'bob' });
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
      res,
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
    }>(res);
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
    const out = bodyOf<{ goldenReplay: unknown[] }>(res);
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
    const out = bodyOf<{ goldenReplay: unknown[] }>(res);
    expect(out.goldenReplay).toHaveLength(0);
    // Org A's lone case still belongs to org A.
    expect(await repo.listGoldenCasesForSkill(ORG, 'reconcile')).toHaveLength(1);
    expect(await repo.listGoldenCasesForSkill(ORG_B, 'reconcile')).toHaveLength(0);
  });
});
