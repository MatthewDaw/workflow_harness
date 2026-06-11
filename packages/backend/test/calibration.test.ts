import { beforeEach, describe, expect, it } from 'vitest';
import type { Project, WeeklyCommit } from '@harness/shared';
import {
  getCalibration,
  recordCalibration,
  tally,
} from '../src/projections/calibration.js';
import { completeReconcile } from '../src/rest/weeklyTransitions.js';
import { listWeekly } from '../src/rest/weekly.js';
import { putObjective as pgPutObjective } from '../src/db/pg/objectivesRepo.js';
import { createCommit as repoCreateCommit, upsertPlan } from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';
import { randomUUID } from 'node:crypto';

/**
 * U19 — reconciliation calibration feedback. On `completeReconcile`, the
 * just-reconciled week's terminal commits accumulate into the owner's running
 * locked-vs-done rate (keyed by user), which the plan-anchored `/hq-weekly-update`
 * agent reads to right-size next week's proposal. Advisory — never blocks.
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

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

async function seedSo(id: string): Promise<void> {
  await pgPutObjective(db, { id, org: ORG, level: 'supporting_outcome', title: id });
}

function commit(over: Partial<WeeklyCommit> = {}): WeeklyCommit {
  return {
    id: over.id ?? randomUUID(),
    projectId: PROJ,
    isoWeek: WEEK,
    title: over.title ?? 'commit',
    supportingOutcomeId: over.supportingOutcomeId ?? 'so-a',
    orphanReason: over.orphanReason,
    alsoAdvances: over.alsoAdvances ?? [],
    category: over.category ?? 'Delivery',
    priorityNumeric: over.priorityNumeric ?? 1,
    status: over.status ?? 'planned',
    actualOutcome: over.actualOutcome,
    carriedFromWeek: over.carriedFromWeek,
    carriedToWeek: over.carriedToWeek,
    carryDepth: over.carryDepth ?? 0,
  };
}

/** Seed a week's worth of N commits with `done` of them done, the rest dropped. */
async function seedTerminalWeek(week: string, total: number, done: number): Promise<void> {
  await upsertPlan(db, { projectId: PROJ, isoWeek: week, status: 'RECONCILING', posture: 'focus' });
  for (let i = 0; i < total; i++) {
    await repoCreateCommit(
      db,
      commit({
        id: `${week}-c${i}`,
        isoWeek: week,
        status: i < done ? 'done' : 'dropped',
      }),
    );
  }
}

function completeEvent(userId: string | null, week = WEEK) {
  const rawPath = `/projects/${PROJ}/weekly/${week}/reconcile/complete`;
  return httpEvent({ method: 'POST', userId, org: ORG, rawPath, path: { pid: PROJ, week } });
}

describe('tally (pure)', () => {
  it('6 done of 10 terminal yields a 0.6-shaped delta', () => {
    const commits = Array.from({ length: 10 }, (_, i) =>
      commit({ id: `c${i}`, status: i < 6 ? 'done' : 'dropped' }),
    );
    const d = tally(commits);
    expect(d.terminal).toBe(10);
    expect(d.done).toBe(6);
    expect(d.done / d.terminal).toBeCloseTo(0.6, 5);
  });

  it('excludes still-planned commits from the terminal denominator', () => {
    const commits = [
      commit({ id: 'a', status: 'done' }),
      commit({ id: 'b', status: 'planned' }), // not terminal — ignored
    ];
    const d = tally(commits);
    expect(d.terminal).toBe(1);
    expect(d.done).toBe(1);
  });

  it('the higher-priority half is tracked for the leverage-first signal', () => {
    // Two high-priority (shipped) + two low-priority (slipped): the high half is
    // fully done, the low half is not.
    const commits = [
      commit({ id: 'h1', priorityNumeric: 9, status: 'done' }),
      commit({ id: 'h2', priorityNumeric: 8, status: 'done' }),
      commit({ id: 'l1', priorityNumeric: 2, status: 'dropped' }),
      commit({ id: 'l2', priorityNumeric: 1, status: 'dropped' }),
    ];
    const d = tally(commits);
    expect(d.highPriorityTotal).toBe(2);
    expect(d.highPriorityFirst).toBe(2);
  });
});

describe('recordCalibration store', () => {
  it('happy path: 6/10 done over the window stores rate ~= 0.6', async () => {
    const commits = Array.from({ length: 10 }, (_, i) =>
      commit({ id: `c${i}`, status: i < 6 ? 'done' : 'dropped' }),
    );
    const stored = await recordCalibration(db, MATT, commits, 1000);
    expect(stored).toBeDefined();
    expect(stored!.lockedCount).toBe(10);
    expect(stored!.doneCount).toBe(6);
    expect(stored!.rate).toBeCloseTo(0.6, 5);
    expect(stored!.updatedAt).toBe(1000);

    const read = await getCalibration(db, MATT);
    expect(read!.rate).toBeCloseTo(0.6, 5);
  });

  it('edge: a first-ever week (no history) reads undefined before any reconcile', async () => {
    expect(await getCalibration(db, MATT)).toBeUndefined();
  });

  it('accumulates across windows (the trailing rate)', async () => {
    // Window 1: 1 done of 2. Window 2: 3 done of 4. Trailing: 4 of 6 = 0.667.
    await recordCalibration(
      db,
      MATT,
      [commit({ id: 'a', status: 'done' }), commit({ id: 'b', status: 'dropped' })],
      1,
    );
    const after = await recordCalibration(
      db,
      MATT,
      [
        commit({ id: 'c', status: 'done' }),
        commit({ id: 'd', status: 'done' }),
        commit({ id: 'e', status: 'done' }),
        commit({ id: 'f', status: 'dropped' }),
      ],
      2,
    );
    expect(after!.lockedCount).toBe(6);
    expect(after!.doneCount).toBe(4);
    expect(after!.rate).toBeCloseTo(4 / 6, 5);
  });

  it('a week with no terminal commits is a no-op', async () => {
    const stored = await recordCalibration(db, MATT, [commit({ status: 'planned' })], 5);
    expect(stored).toBeUndefined();
    expect(await getCalibration(db, MATT)).toBeUndefined();
  });
});

describe('completeReconcile integration', () => {
  it('writes the owner calibration row on reconcile-complete', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await seedTerminalWeek(WEEK, 10, 6); // 6 done, 4 dropped

    const res = await completeReconcile(completeEvent(MATT), deps());
    expect(res).toMatchObject({ statusCode: 200 });

    const cal = await getCalibration(db, MATT);
    expect(cal).toBeDefined();
    expect(cal!.lockedCount).toBe(10);
    expect(cal!.doneCount).toBe(6);
    expect(cal!.rate).toBeCloseTo(0.6, 5);
  });

  it('surfaces the calibration on the GET /weekly read after a reconcile', async () => {
    await repo.putProject(project(MATT));
    await seedSo('so-a');
    await seedTerminalWeek(WEEK, 4, 3);
    await completeReconcile(completeEvent(MATT), deps());

    const readEvent = httpEvent({
      method: 'GET',
      userId: MATT,
      org: ORG,
      rawPath: `/projects/${PROJ}/weekly`,
      path: { pid: PROJ },
    });
    const res = await listWeekly(readEvent, deps());
    expect(res).toMatchObject({ statusCode: 200 });
    const body = bodyOf<{ calibration?: { rate?: number } }>(res);
    expect(body.calibration).toBeDefined();
    expect(body.calibration!.rate).toBeCloseTo(0.75, 5);
  });

  it('the GET /weekly read omits calibration for a first-ever (no-history) caller', async () => {
    await repo.putProject(project(MATT));
    const readEvent = httpEvent({
      method: 'GET',
      userId: MATT,
      org: ORG,
      rawPath: `/projects/${PROJ}/weekly`,
      path: { pid: PROJ },
    });
    const res = await listWeekly(readEvent, deps());
    const body = bodyOf<{ calibration?: unknown }>(res);
    expect(body.calibration).toBeUndefined();
  });
});
