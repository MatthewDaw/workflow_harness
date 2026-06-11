import { beforeEach, describe, expect, it } from 'vitest';
import type { Project, WeeklyCommit, WeeklyPlan } from '@harness/shared';
import {
  completeReconcile,
  lockWeek,
  nextIsoWeek,
  startReconcile,
} from '../src/rest/weeklyTransitions.js';
import { putObjective as pgPutObjective } from '../src/db/pg/objectivesRepo.js';
import {
  createCommit as repoCreateCommit,
  getCommit,
  getPlan,
  listWeekCommits,
  updateCommit as repoUpdateCommit,
  upsertPlan,
} from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';
import { randomUUID } from 'node:crypto';

/**
 * U4 REST: lifecycle transitions (lock / reconcile-start / reconcile-complete).
 * `canTransition` (KTD2) is the single source of truth for legality (illegal ->
 * 409). LOCK re-checks the SO-or-orphan invariant + the non-empty guard and
 * returns a structured `blockers[]`. reconcile-complete refuses while any commit
 * is still `planned`, then carries the incomplete (`partial`) items into next
 * week's DRAFT in one transaction (KTD3).
 */

const { repo } = memRepoHarness();

let db: PgDb;
beforeEach(async () => {
  db = await makePgliteDb();
});
const deps = () => ({ repo, db });

const MATT = 'matt';
const ORG = 'acme';
const PROJ = 'weekly-compass';
const WEEK = '2026-W23';
const NEXT_WEEK = '2026-W24';

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

async function seedSo(id: string): Promise<void> {
  await pgPutObjective(db, { id, org: ORG, level: 'supporting_outcome', title: id });
}

function transitionEvent(userId: string | null, segment: string, week = WEEK) {
  const rawPath = `/projects/${PROJ}/weekly/${week}/${segment}`;
  return httpEvent({
    method: 'POST',
    userId,
    org: ORG,
    rawPath,
    path: { pid: PROJ, week },
  });
}

/** Insert a commit directly via the repo (bypassing the REST derive path). */
async function seedCommit(c: Partial<WeeklyCommit> & { isoWeek?: string }): Promise<WeeklyCommit> {
  const commit: WeeklyCommit = {
    id: randomUUID(),
    projectId: PROJ,
    isoWeek: c.isoWeek ?? WEEK,
    title: c.title ?? 'commit',
    supportingOutcomeId: c.supportingOutcomeId,
    orphanReason: c.orphanReason,
    alsoAdvances: c.alsoAdvances ?? [],
    category: c.category ?? 'Delivery',
    priorityNumeric: c.priorityNumeric ?? 1,
    status: c.status ?? 'planned',
    actualOutcome: c.actualOutcome,
    carriedFromWeek: c.carriedFromWeek,
    carriedToWeek: c.carriedToWeek,
    carryDepth: c.carryDepth ?? 0,
  };
  await repoCreateCommit(db, commit);
  return commit;
}

async function draftPlan(status: WeeklyPlan['status'] = 'DRAFT', week = WEEK): Promise<void> {
  await upsertPlan(db, { projectId: PROJ, isoWeek: week, status, posture: 'focus' });
}

describe('nextIsoWeek', () => {
  it('advances within the year', () => {
    expect(nextIsoWeek('2026-W23')).toBe('2026-W24');
  });
  it('rolls the final week of a 52-week year into the next year W01', () => {
    // 2025 is an ISO-52-week year (Jan 1 2025 is a Wednesday, not leap).
    expect(nextIsoWeek('2025-W52')).toBe('2026-W01');
  });
  it('respects 53-week years (2026 has W53; Jan 1 2026 is a Thursday)', () => {
    // 2026 is an ISO-53-week year: W52 -> W53, W53 -> 2027-W01.
    expect(nextIsoWeek('2026-W52')).toBe('2026-W53');
    expect(nextIsoWeek('2026-W53')).toBe('2027-W01');
  });
});

describe('lock', () => {
  it('DRAFT -> LOCKED with all commits SO-or-orphan stamps lockedAt', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('DRAFT');
    await seedCommit({ supportingOutcomeId: 'so-a' });

    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const { plan } = bodyOf<{ plan: WeeklyPlan }>(res);
    expect(plan.status).toBe('LOCKED');
    expect(typeof plan.lockedAt).toBe('number');

    const persisted = await getPlan(db, PROJ, WEEK);
    expect(persisted!.status).toBe('LOCKED');
  });

  it('409s an empty week with a blocker', async () => {
    await repo.putProject(project(MATT));
    await draftPlan('DRAFT');

    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 409 });
    const body = bodyOf<{ blockers: { reason: string }[] }>(res);
    expect(body.blockers.length).toBeGreaterThanOrEqual(1);
  });

  it('locks a week mixing an SO commit and a typed-orphan commit', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('DRAFT');
    await seedCommit({ supportingOutcomeId: 'so-a', title: 'linked' });
    // An orphan commit (typed non-link) is a valid lock participant (KTD10).
    await seedCommit({ orphanReason: 'KTLO', title: 'ktlo' });

    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 200 });
  });

  it('re-locking an already-LOCKED week is an illegal transition (409)', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('LOCKED');
    await seedCommit({ supportingOutcomeId: 'so-a' });

    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 409 });
    const body = bodyOf<{ error: string }>(res);
    expect(body.error).toContain('LOCKED -> LOCKED');
  });

  it('404s a week that has never been written', async () => {
    await repo.putProject(project(MATT));
    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('401s without auth', async () => {
    await repo.putProject(project(MATT));
    await draftPlan('DRAFT');
    const res = await lockWeek(transitionEvent(null, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('404s a non-owner', async () => {
    await repo.putProject(project('alice'));
    await draftPlan('DRAFT');
    const res = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('reconcile/start', () => {
  it('LOCKED -> RECONCILING', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('LOCKED');
    await seedCommit({ supportingOutcomeId: 'so-a' });

    const res = await startReconcile(transitionEvent(MATT, 'reconcile/start'), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const { plan } = bodyOf<{ plan: WeeklyPlan }>(res);
    expect(plan.status).toBe('RECONCILING');
  });

  it('409s starting reconcile from DRAFT (illegal)', async () => {
    await repo.putProject(project(MATT));
    await draftPlan('DRAFT');
    const res = await startReconcile(transitionEvent(MATT, 'reconcile/start'), deps());
    expect(res).toMatchObject({ statusCode: 409 });
  });
});

describe('reconcile/complete', () => {
  it('refuses while a commit is still planned (409 listing it)', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('RECONCILING');
    await seedCommit({ supportingOutcomeId: 'so-a', status: 'done' });
    const planned = await seedCommit({ supportingOutcomeId: 'so-a', status: 'planned' });

    const res = await completeReconcile(transitionEvent(MATT, 'reconcile/complete'), deps());
    expect(res).toMatchObject({ statusCode: 409 });
    const body = bodyOf<{ blockers: { commitId?: string }[] }>(res);
    expect(body.blockers.some((b) => b.commitId === planned.id)).toBe(true);
  });

  it('completes a terminal week, stamps reconciledAt, and carries partials into next week DRAFT', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('RECONCILING');
    const done1 = await seedCommit({ supportingOutcomeId: 'so-a', status: 'done', title: 'd1' });
    const done2 = await seedCommit({ supportingOutcomeId: 'so-a', status: 'done', title: 'd2' });
    const partial = await seedCommit({
      supportingOutcomeId: 'so-a',
      status: 'partial',
      title: 'p1',
    });
    const dropped = await seedCommit({
      supportingOutcomeId: 'so-a',
      status: 'dropped',
      title: 'x1',
    });

    const res = await completeReconcile(transitionEvent(MATT, 'reconcile/complete'), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ plan: WeeklyPlan; carriedTo: string; carriedCount: number }>(res);
    expect(body.plan.status).toBe('RECONCILED');
    expect(typeof body.plan.reconciledAt).toBe('number');
    expect(body.carriedTo).toBe(NEXT_WEEK);
    expect(body.carriedCount).toBe(1); // only the partial carries

    // The source week is RECONCILED.
    const src = await getPlan(db, PROJ, WEEK);
    expect(src!.status).toBe('RECONCILED');

    // Next week was created as DRAFT and holds exactly the carried clone.
    const nextPlan = await getPlan(db, PROJ, NEXT_WEEK);
    expect(nextPlan!.status).toBe('DRAFT');
    const nextCommits = await listWeekCommits(db, PROJ, NEXT_WEEK);
    expect(nextCommits).toHaveLength(1);
    const clone = nextCommits[0]!;
    expect(clone.title).toBe('p1');
    expect(clone.status).toBe('planned'); // reset
    expect(clone.carriedFromWeek).toBe(WEEK);
    expect(clone.carryDepth).toBe(1); // incremented
    expect(clone.id).not.toBe(partial.id); // fresh id

    // The source partial is stamped with the week it carried TO.
    const srcPartial = await getCommit(db, partial.id);
    expect(srcPartial!.carriedToWeek).toBe(NEXT_WEEK);
    // done/dropped sources do not carry and are not stamped.
    expect((await getCommit(db, done1.id))!.carriedToWeek).toBeUndefined();
    expect((await getCommit(db, done2.id))!.carriedToWeek).toBeUndefined();
    expect((await getCommit(db, dropped.id))!.carriedToWeek).toBeUndefined();
  });

  it('a fully-done week carries nothing and creates no next-week plan', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('RECONCILING');
    await seedCommit({ supportingOutcomeId: 'so-a', status: 'done' });

    const res = await completeReconcile(transitionEvent(MATT, 'reconcile/complete'), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ carriedCount: number }>(res);
    expect(body.carriedCount).toBe(0);
    expect(await getPlan(db, PROJ, NEXT_WEEK)).toBeUndefined();
  });

  it('a partial at depth 2 carries to depth 3 and raises the decompose/kill nudge', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('RECONCILING');
    await seedCommit({ supportingOutcomeId: 'so-a', status: 'partial', carryDepth: 2 });

    const res = await completeReconcile(transitionEvent(MATT, 'reconcile/complete'), deps());
    const body = bodyOf<{ deepCarryNudge?: string[] }>(res);
    expect(body.deepCarryNudge).toBeDefined();
    expect(body.deepCarryNudge!).toHaveLength(1);

    const nextCommits = await listWeekCommits(db, PROJ, NEXT_WEEK);
    expect(nextCommits[0]!.carryDepth).toBe(3);
  });

  it('409s completing from LOCKED (illegal)', async () => {
    await repo.putProject(project(MATT));
    await draftPlan('LOCKED');
    const res = await completeReconcile(transitionEvent(MATT, 'reconcile/complete'), deps());
    expect(res).toMatchObject({ statusCode: 409 });
  });
});

describe('full lifecycle path', () => {
  it('create -> lock -> reconcile/start -> reconcile/complete', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await draftPlan('DRAFT');
    const c = await seedCommit({ supportingOutcomeId: 'so-a' });

    const locked = await lockWeek(transitionEvent(MATT, 'lock'), deps());
    expect(locked).toMatchObject({ statusCode: 200 });

    const started = await startReconcile(transitionEvent(MATT, 'reconcile/start'), deps());
    expect(started).toMatchObject({ statusCode: 200 });

    // Record the actual outcome on the single commit so the week is terminal.
    await repoUpdateCommit(db, c.id, { status: 'done' });

    const completed = await completeReconcile(
      transitionEvent(MATT, 'reconcile/complete'),
      deps(),
    );
    expect(completed).toMatchObject({ statusCode: 200 });
    const { plan } = bodyOf<{ plan: WeeklyPlan }>(completed);
    expect(plan.status).toBe('RECONCILED');
  });
});
