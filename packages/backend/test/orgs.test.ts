import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { createOrg, getMe, joinOrg, switchOrg } from '../src/rest/orgs.js';
import { STARTER_BUNDLE_NAME } from '../src/seed/skills.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Org onboarding REST: GET /me reflects REAL membership (the PROFILE), and
 * POST /orgs / /orgs/join drive create + join with a salted+hashed shared
 * secret. Join failures use a single generic message (no org enumeration).
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ALICE = 'alice';
const BOB = 'bob';

describe('GET /me', () => {
  it('returns org:null when the caller has no profile (forces onboarding)', async () => {
    // The token carries an auto-assigned claim org, but /me reads the PROFILE
    // ONLY — a user with no profile genuinely has no org.
    const res = await getMe(httpEvent({ method: 'GET', userId: ALICE, org: 'claim-org' }), deps);
    const body = bodyOf<{ userId: string; org: string | null; admin?: boolean }>(res);
    expect(res).toMatchObject({ statusCode: 200 });
    expect(body.userId).toBe(ALICE);
    expect(body.org).toBeNull();
  });

  it('401 when unauthenticated', async () => {
    const res = await getMe(httpEvent({ method: 'GET', userId: null }), deps);
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('reflects the profile org once a user has onboarded', async () => {
    await repo.setUserOrg(ALICE, 'acme', { name: 'Alice', admin: true });
    const res = await getMe(httpEvent({ method: 'GET', userId: ALICE, org: 'claim-org' }), deps);
    const body = bodyOf<{ org: string | null; admin?: boolean }>(res);
    expect(body.org).toBe('acme');
    expect(body.admin).toBe(true);
  });
});

describe('POST /orgs (create)', () => {
  it('creates an org, makes the caller admin, and sets their profile', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    expect(bodyOf<{ org: string; admin: boolean }>(res)).toEqual({ org: 'acme', admin: true });

    // Profile now reflects membership + admin.
    const profile = await repo.getUser(ALICE);
    expect(profile?.org).toBe('acme');
    expect(profile?.admin).toBe(true);

    // The stored org carries a salted hash, never the plaintext.
    const org = await repo.getOrg('acme');
    expect(org?.passwordSalt).toBeTruthy();
    expect(org?.passwordHash).toBeTruthy();
    expect(JSON.stringify(org)).not.toContain('secret1');
  });

  it('pre-loads the command-hq-starter bundle into the new org', async () => {
    // A template org carries the canonical starter set; creating a new org clones
    // it so the new org's Skills tab is populated immediately.
    const tmpl = orgScope('acme');
    const mk = (over: Partial<Skill> & Pick<Skill, 'name' | 'kind'>): Skill => ({
      scope: tmpl,
      description: '',
      source: 'built-in',
      members: [],
      body: '',
      ...over,
    });
    await repo.putSkill(mk({ name: 'hq-weekly-update', kind: 'skill', body: 'x' }));
    await repo.putSkill(mk({ name: STARTER_BUNDLE_NAME, kind: 'bundle', members: ['hq-weekly-update'] }));

    await createOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'newco', password: 'secret1' } }),
      deps,
    );

    const names = (await repo.listSkills('newco')).map((s) => s.name).sort();
    expect(names).toEqual([STARTER_BUNDLE_NAME, 'hq-weekly-update']);
  });

  it('409 when the org name is already taken', async () => {
    await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    const dup = await createOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'acme', password: 'other1' } }),
      deps,
    );
    expect(dup).toMatchObject({ statusCode: 409 });
    expect(bodyOf<{ error: string }>(dup).error).toBe('organization already exists');
    // Bob's profile is untouched — a failed create does not onboard him.
    expect((await repo.getUser(BOB))?.org).toBeUndefined();
  });

  it('accepts any password — there is no length/complexity restriction', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'x' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
  });

  it('400 carries a human-readable message, not raw zod JSON', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
    const msg = bodyOf<{ error: string }>(res).error;
    expect(msg).toBe('Please enter an organization name.');
    expect(msg).not.toContain('{'); // never the zod issue array
  });

  it('400 on a missing name', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('401 when unauthenticated', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: null, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });
});

describe('POST /orgs/join', () => {
  beforeEach(async () => {
    // Alice creates the org Bob will join.
    await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
  });

  it('joins with the exact name + correct password (not auto-admin)', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ org: string; admin: boolean }>(res)).toEqual({ org: 'acme', admin: false });
    expect((await repo.getUser(BOB))?.org).toBe('acme');
    expect((await repo.getUser(BOB))?.admin).toBeFalsy();
  });

  it('403 with the generic message on a wrong password', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'acme', password: 'wrongpw' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect(bodyOf<{ error: string }>(res).error).toBe('invalid organization name or password');
    expect((await repo.getUser(BOB))?.org).toBeUndefined();
  });

  it('403 with the SAME generic message on an unknown org (no enumeration)', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'ghost', password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect(bodyOf<{ error: string }>(res).error).toBe('invalid organization name or password');
  });

  it('400 on validation failure (missing name)', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('401 when unauthenticated', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: null, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });
});

describe('POST /me/org (switch active org)', () => {
  beforeEach(async () => {
    // Alice creates 'acme' (she is its admin), Bob creates 'beta', then Alice
    // joins 'beta' (as a plain member). Alice now belongs to both; 'beta' — her
    // most recent membership write — is her active org.
    await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'secret1' } }),
      deps,
    );
    await createOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'beta', password: 'secret2' } }),
      deps,
    );
    await joinOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'beta', password: 'secret2' } }),
      deps,
    );
  });

  it('GET /me lists every joined org and the active one', async () => {
    const res = await getMe(httpEvent({ method: 'GET', userId: ALICE }), deps);
    const body = bodyOf<{ org: string | null; orgs: string[] }>(res);
    expect(body.org).toBe('beta');
    expect([...body.orgs].sort()).toEqual(['acme', 'beta']);
  });

  it('switches the active org to another joined org (no password)', async () => {
    const res = await switchOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { org: 'acme' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getUser(ALICE))?.org).toBe('acme');
  });

  it('the admin flag follows the active org', async () => {
    // Active 'beta' (Alice only joined it) → not admin.
    const before = bodyOf<{ admin?: boolean }>(await getMe(httpEvent({ method: 'GET', userId: ALICE }), deps));
    expect(before.admin).toBeFalsy();
    // Switch to 'acme' (she created it) → admin.
    await switchOrg(httpEvent({ method: 'POST', userId: ALICE, body: { org: 'acme' } }), deps);
    const after = bodyOf<{ admin?: boolean }>(await getMe(httpEvent({ method: 'GET', userId: ALICE }), deps));
    expect(after.admin).toBe(true);
  });

  it('403 (and no change) when switching to an org you are not a member of', async () => {
    const res = await switchOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { org: 'ghost' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    expect((await repo.getUser(ALICE))?.org).toBe('beta'); // unchanged
  });

  it('401 when unauthenticated', async () => {
    const res = await switchOrg(
      httpEvent({ method: 'POST', userId: null, body: { org: 'acme' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });
});
