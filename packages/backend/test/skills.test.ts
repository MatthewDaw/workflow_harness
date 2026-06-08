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
  getSkill,
  getUsage,
  promoteSkill,
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

describe('POST /skills/:name/promote (any authed member)', () => {
  it('repoints TRUE to the given variant (not admin-gated)', async () => {
    await createSkill(
      adminEvent({ method: 'POST', userId: MATT, body: skill('reconcile', 'base') }),
      deps,
    );
    // A non-admin member promotes a (hypothetical) fork variant.
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
    expect(res).toMatchObject({ statusCode: 200 });
    const truth = await repo.getTrueVariant(SCOPE, 'SKILL', 'reconcile');
    expect(truth).toEqual({ baseName: 'reconcile', variantId: 'reconcile#R#r#U#bob', rev: 1 });
  });

  it('400s when variantId is missing', async () => {
    const res = await promoteSkill(
      httpEvent({
        method: 'POST',
        userId: 'bob',
        org: ORG,
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
