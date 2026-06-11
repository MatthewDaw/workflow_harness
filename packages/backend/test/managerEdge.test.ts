import { describe, expect, it } from 'vitest';
import { getMe, setManager } from '../src/rest/orgs.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U18 — the lightweight manager↔report edge (KTD6). `managerUserId` on the
 * PROFILE is the single edge; `repo.listReports(mgr)` is a GSI1 query over the
 * `MANAGER#<id>` partition. `POST /me/manager` sets it (self, or another user for
 * an admin); `GET /me` surfaces it so the UI can offer the manager view.
 */

const { repo } = memRepoHarness();
const deps = { repo };

const MGR = 'manager';
const ALICE = 'alice';
const BOB = 'bob';

describe('repo.setManager / listReports', () => {
  it('lists every report once their manager edge is set (happy path)', async () => {
    await repo.setManager(ALICE, MGR);
    await repo.setManager(BOB, MGR);

    const reports = await repo.listReports(MGR);
    expect(reports.map((r) => r.userId).sort()).toEqual([ALICE, BOB]);
    // The edge round-trips on the profile.
    expect((await repo.getUser(ALICE))?.managerUserId).toBe(MGR);
  });

  it('a user with no manager appears in nobody\'s reports', async () => {
    await repo.setManager(ALICE, MGR);
    // Bob has a profile but no manager.
    await repo.setUserOrg(BOB, 'acme');

    const reports = await repo.listReports(MGR);
    expect(reports.map((r) => r.userId)).toEqual([ALICE]);
  });

  it('a manager with no reports gets an empty list', async () => {
    expect(await repo.listReports(MGR)).toEqual([]);
  });

  it('clearing the edge removes the user from the reports list', async () => {
    await repo.setManager(ALICE, MGR);
    expect((await repo.listReports(MGR)).map((r) => r.userId)).toEqual([ALICE]);

    await repo.setManager(ALICE, null);
    expect(await repo.listReports(MGR)).toEqual([]);
    expect((await repo.getUser(ALICE))?.managerUserId).toBeUndefined();
  });

  it('preserves the manager edge across onboarding (setUserOrg)', async () => {
    await repo.setManager(ALICE, MGR);
    await repo.setUserOrg(ALICE, 'acme', { name: 'Alice' });

    // Onboarding rebuilds the profile object; the edge survives.
    expect((await repo.getUser(ALICE))?.managerUserId).toBe(MGR);
    expect((await repo.getUser(ALICE))?.org).toBe('acme');
    expect((await repo.listReports(MGR)).map((r) => r.userId)).toEqual([ALICE]);
  });
});

describe('POST /me/manager', () => {
  it('a user sets their own manager edge', async () => {
    const res = await setManager(
      httpEvent({ method: 'POST', userId: ALICE, body: { managerUserId: MGR } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ userId: string; managerUserId: string | null }>(res)).toEqual({
      userId: ALICE,
      managerUserId: MGR,
    });
    expect((await repo.listReports(MGR)).map((r) => r.userId)).toEqual([ALICE]);
  });

  it('clears the edge when managerUserId is null', async () => {
    await repo.setManager(ALICE, MGR);
    const res = await setManager(
      httpEvent({ method: 'POST', userId: ALICE, body: { managerUserId: null } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ managerUserId: string | null }>(res).managerUserId).toBeNull();
    expect(await repo.listReports(MGR)).toEqual([]);
  });

  it('403 when a non-admin sets ANOTHER user\'s manager edge', async () => {
    const res = await setManager(
      httpEvent({ method: 'POST', userId: ALICE, body: { userId: BOB, managerUserId: MGR } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    // Bob's edge is untouched.
    expect((await repo.getUser(BOB))?.managerUserId).toBeUndefined();
  });

  it('an admin may set another user\'s manager edge on their behalf', async () => {
    const res = await setManager(
      httpEvent({
        method: 'POST',
        userId: ALICE,
        admin: true,
        body: { userId: BOB, managerUserId: MGR },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.listReports(MGR)).map((r) => r.userId)).toEqual([BOB]);
  });

  it('401 when unauthenticated', async () => {
    const res = await setManager(
      httpEvent({ method: 'POST', userId: null, body: { managerUserId: MGR } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('400 on a malformed body (empty managerUserId string)', async () => {
    const res = await setManager(
      httpEvent({ method: 'POST', userId: ALICE, body: { managerUserId: '' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('GET /me surfaces the manager edge', () => {
  it('returns managerUserId once set', async () => {
    await repo.setManager(ALICE, MGR);
    const res = await getMe(httpEvent({ method: 'GET', userId: ALICE }), deps);
    expect(bodyOf<{ managerUserId?: string }>(res).managerUserId).toBe(MGR);
  });

  it('omits managerUserId when the caller has no manager', async () => {
    await repo.setUserOrg(ALICE, 'acme');
    const res = await getMe(httpEvent({ method: 'GET', userId: ALICE }), deps);
    expect(bodyOf<{ managerUserId?: string }>(res).managerUserId).toBeUndefined();
  });
});
