import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { Repo } from '../src/db/repo.js';
import { createOrg, getMe, joinOrg } from '../src/rest/orgs.js';
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

  it('400 on a too-short password', async () => {
    const res = await createOrg(
      httpEvent({ method: 'POST', userId: ALICE, body: { name: 'acme', password: 'short' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
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

  it('400 on validation failure', async () => {
    const res = await joinOrg(
      httpEvent({ method: 'POST', userId: BOB, body: { name: 'acme', password: 'x' } }),
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
