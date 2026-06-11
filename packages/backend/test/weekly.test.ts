import { beforeEach, describe, expect, it } from 'vitest';
import type { Project, WeeklyCommit, WeeklyPlan } from '@harness/shared';
import {
  createCommit,
  deleteCommit,
  getWeekly,
  listWeekly,
  updateCommit,
} from '../src/rest/weekly.js';
import { putObjective as pgPutObjective } from '../src/db/pg/objectivesRepo.js';
import { upsertPlan } from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { bodyOf, deviceTokenEvent, httpEvent } from './helpers/httpevent.js';

/**
 * U3 REST: itemized weekly commit CRUD + week read (Postgres-backed). A commit
 * carries EITHER a Supporting Outcome OR a typed orphan reason (KTD10); the
 * chess-layer `category`/`priorityNumeric` are DERIVED server-side (KTD4) and
 * client-sent values are ignored. Planned fields freeze at LOCK — create/update/
 * delete on a non-DRAFT week is a 409. Weekly is scoped to the project owner.
 */

const { repo } = memRepoHarness();

// Objectives + weekly relations live in Postgres (KTD7): a fresh pglite DB per
// test, injected into the weekly deps.
let db: PgDb;
beforeEach(async () => {
  db = await makePgliteDb();
});
const deps = () => ({ repo, db });

const MATT = 'matt';
const ORG = 'acme';
const PROJ = 'weekly-compass';
const WEEK = '2026-W23';

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

async function seedSo(id: string): Promise<void> {
  await pgPutObjective(db, { id, org: ORG, level: 'supporting_outcome', title: id });
}

function createEvent(userId: string, body: unknown) {
  return httpEvent({
    method: 'POST',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits`,
    path: { pid: PROJ, week: WEEK },
    body,
  });
}

function updateEvent(userId: string, cid: string, body: unknown) {
  return httpEvent({
    method: 'PUT',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits/${cid}`,
    path: { pid: PROJ, week: WEEK, cid },
    body,
  });
}

function deleteEvent(userId: string, cid: string) {
  return httpEvent({
    method: 'DELETE',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits/${cid}`,
    path: { pid: PROJ, week: WEEK, cid },
  });
}

function getEvent(userId: string) {
  return httpEvent({
    method: 'GET',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}`,
    path: { pid: PROJ, week: WEEK },
  });
}

function listEvent(userId: string) {
  return httpEvent({
    method: 'GET',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly`,
    path: { pid: PROJ },
  });
}

describe('create + serve itemized commits', () => {
  it('creates an SO commit and an orphan commit; the week auto-DRAFTs; GET returns both with derived chess fields', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');

    const r1 = await createCommit(
      createEvent(MATT, { title: 'Ship reconciliation', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    expect(r1).toMatchObject({ statusCode: 201 });
    const r2 = await createCommit(
      createEvent(MATT, { title: 'Patch the incident', orphanReason: 'Incident' }),
      deps(),
    );
    expect(r2).toMatchObject({ statusCode: 201 });

    const served = await getWeekly(getEvent(MATT), deps());
    const { week } = bodyOf<{ week: { plan: WeeklyPlan; commits: WeeklyCommit[] } }>(served);
    // The week was auto-created as DRAFT on first commit write.
    expect(week.plan.status).toBe('DRAFT');
    expect(week.commits).toHaveLength(2);

    const linked = week.commits.find((c) => c.supportingOutcomeId === 'so-a')!;
    expect(linked.category).toBe('Delivery'); // derived (U6 refines)
    expect(typeof linked.priorityNumeric).toBe('number');

    const orphan = week.commits.find((c) => c.orphanReason === 'Incident')!;
    // An orphan commit's reason IS its category (KTD10/KTD4).
    expect(orphan.category).toBe('Incident');
  });

  it('lists every week with its commits', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await createCommit(createEvent(MATT, { title: 'A', supportingOutcomeId: 'so-a' }), deps());

    const res = await listWeekly(listEvent(MATT), deps());
    const { weeks } = bodyOf<{ weeks: { plan: WeeklyPlan; commits: WeeklyCommit[] }[] }>(res);
    expect(weeks).toHaveLength(1);
    expect(weeks[0]!.plan.isoWeek).toBe(WEEK);
    expect(weeks[0]!.commits).toHaveLength(1);
  });

  it('ignores client-sent category/priority in favour of the derived values', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const res = await createCommit(
      createEvent(MATT, {
        title: 'Forge the fields',
        supportingOutcomeId: 'so-a',
        category: 'Strategic',
        priorityNumeric: 9999,
      }),
      deps(),
    );
    const { commit } = bodyOf<{ commit: WeeklyCommit }>(res);
    expect(commit.category).toBe('Delivery'); // not the client's 'Strategic'
    expect(commit.priorityNumeric).not.toBe(9999); // not the client's number
  });
});

describe('SO-or-orphan + dangling-SO enforcement', () => {
  it('rejects a commit with neither an SO nor an orphan reason (400)', async () => {
    await repo.putProject(project(MATT));
    const res = await createCommit(createEvent(MATT, { title: 'Floating work' }), deps());
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('rejects a dangling supportingOutcomeId (400)', async () => {
    await repo.putProject(project(MATT));
    const res = await createCommit(
      createEvent(MATT, { title: 'Link to nothing', supportingOutcomeId: 'so-missing' }),
      deps(),
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('planned fields freeze at LOCK', () => {
  it('409s a create when the week is LOCKED', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const locked: WeeklyPlan = { projectId: PROJ, isoWeek: WEEK, status: 'LOCKED', posture: 'focus' };
    await upsertPlan(db, locked);

    const res = await createCommit(
      createEvent(MATT, { title: 'Too late', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    expect(res).toMatchObject({ statusCode: 409 });
  });

  it('409s an update when the week is LOCKED', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    // Create while DRAFT, then lock.
    const created = await createCommit(
      createEvent(MATT, { title: 'Editable', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    const { commit } = bodyOf<{ commit: WeeklyCommit }>(created);
    await upsertPlan(db, { projectId: PROJ, isoWeek: WEEK, status: 'LOCKED', posture: 'focus' });

    const res = await updateCommit(updateEvent(MATT, commit.id, { title: 'Edited' }), deps());
    expect(res).toMatchObject({ statusCode: 409 });
  });
});

describe('update + delete', () => {
  it('updates a commit title during DRAFT', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const created = await createCommit(
      createEvent(MATT, { title: 'Original', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    const { commit } = bodyOf<{ commit: WeeklyCommit }>(created);

    const res = await updateCommit(updateEvent(MATT, commit.id, { title: 'Renamed' }), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const { commit: updated } = bodyOf<{ commit: WeeklyCommit }>(res);
    expect(updated.title).toBe('Renamed');
  });

  it('re-derives the category when an SO commit becomes an orphan', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const created = await createCommit(
      createEvent(MATT, { title: 'Switcheroo', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    const { commit } = bodyOf<{ commit: WeeklyCommit }>(created);
    expect(commit.category).toBe('Delivery');

    const res = await updateCommit(
      updateEvent(MATT, commit.id, { supportingOutcomeId: null, orphanReason: 'Exploration' }),
      deps(),
    );
    const { commit: updated } = bodyOf<{ commit: WeeklyCommit }>(res);
    expect(updated.supportingOutcomeId).toBeUndefined();
    expect(updated.orphanReason).toBe('Exploration');
    expect(updated.category).toBe('Exploration');
  });

  it('404s a DELETE for a missing commit', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    // Make the DRAFT plan exist so the freeze check passes through to the lookup.
    await createCommit(createEvent(MATT, { title: 'A', supportingOutcomeId: 'so-a' }), deps());

    const res = await deleteCommit(deleteEvent(MATT, 'does-not-exist'), deps());
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('deletes a commit during DRAFT', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const created = await createCommit(
      createEvent(MATT, { title: 'Doomed', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    const { commit } = bodyOf<{ commit: WeeklyCommit }>(created);

    const res = await deleteCommit(deleteEvent(MATT, commit.id), deps());
    expect(res).toMatchObject({ statusCode: 200 });

    const served = await getWeekly(getEvent(MATT), deps());
    const { week } = bodyOf<{ week: { commits: WeeklyCommit[] } }>(served);
    expect(week.commits).toHaveLength(0);
  });
});

describe('week read edge cases', () => {
  it('404s GET for a week that has never been written', async () => {
    await repo.putProject(project(MATT));
    const res = await getWeekly(getEvent(MATT), deps());
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('auth', () => {
  it('404s a non-owner (no enumeration)', async () => {
    await repo.putProject(project('alice'));
    await seedSo('so-a');
    const res = await createCommit(
      createEvent(MATT, { title: 'Sneaky', supportingOutcomeId: 'so-a' }),
      deps(),
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('401s when there is neither jwt claims nor a bearer token', async () => {
    await repo.putProject(project(MATT));
    const res = await createCommit(
      httpEvent({
        method: 'POST',
        userId: null,
        rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits`,
        path: { pid: PROJ, week: WEEK },
        body: { title: 'x', orphanReason: 'KTLO' },
      }),
      deps(),
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('creates a commit authenticated by a device token', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const event = await deviceTokenEvent({
      method: 'POST',
      userId: MATT,
      org: ORG,
      rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits`,
      path: { pid: PROJ, week: WEEK },
      body: { title: 'via device token', supportingOutcomeId: 'so-a' },
    });
    const res = await createCommit(event, deps());
    expect(res).toMatchObject({ statusCode: 201 });
  });

  it('404s a device token whose user does not own the project', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    const event = await deviceTokenEvent({
      method: 'POST',
      userId: 'someone-else',
      org: ORG,
      rawPath: `/projects/${PROJ}/weekly/${WEEK}/commits`,
      path: { pid: PROJ, week: WEEK },
      body: { title: 'x', supportingOutcomeId: 'so-a' },
    });
    const res = await createCommit(event, deps());
    expect(res).toMatchObject({ statusCode: 404 });
  });
});
