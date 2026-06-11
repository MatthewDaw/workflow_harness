import { beforeEach, describe, expect, it } from 'vitest';
import type { WeeklyCommit, WeeklyPlan } from '@harness/shared';
import {
  CARRY_AGING_DECOMPOSE_THRESHOLD,
  computeCarryAging,
  computeConcentration,
  computeStarvation,
  gatherWeeklyMetrics,
  type ConcentrationCommit,
  type StarvationCandidate,
} from '../src/projections/weeklyMetrics.js';
import { putObjective } from '../src/db/pg/objectivesRepo.js';
import { createCommit, upsertPlan } from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { makePgliteDb } from './helpers/pgharness.js';

/**
 * U17: the three KTD8 health signals — Strategic Concentration Index, strategic
 * starvation/coverage, and carry-aging. The compute halves are PURE and reproducible
 * from fixtures (the test contract); the `gather*` layer reads from pglite and
 * folds across an aggregation scope (user / team / org).
 */

const ORG = 'acme';

/* -------------------------------------------------------------------------- */
/* Concentration                                                              */
/* -------------------------------------------------------------------------- */

const cc = (so: string, priorityNumeric = 1): ConcentrationCommit => ({
  supportingOutcomeId: so,
  priorityNumeric,
});

describe('computeConcentration — Herfindahl over touched SO nodes (KTD8)', () => {
  it('8 commits on 2 SOs scores HIGHER than 8 commits on 8 SOs', () => {
    const two = computeConcentration(
      [cc('a'), cc('a'), cc('a'), cc('a'), cc('b'), cc('b'), cc('b'), cc('b')],
      'focus',
    );
    const eight = computeConcentration(
      ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h'].map((s) => cc(s)),
      'focus',
    );
    expect(two.herfindahl).toBeGreaterThan(eight.herfindahl);
    expect(two.nodes).toBe(2);
    expect(eight.nodes).toBe(8);
  });

  it('all weight on one SO is maximally concentrated (herfindahl 1)', () => {
    const c = computeConcentration([cc('a'), cc('a'), cc('a')], 'focus');
    expect(c.herfindahl).toBeCloseTo(1, 9);
    expect(c.nodes).toBe(1);
  });

  it('priority-weights the share: a high-priority SO dominates the index', () => {
    // SO `a` carries 9 units of priority, `b` carries 1 → shares 0.9 / 0.1 →
    // herfindahl 0.81 + 0.01 = 0.82, far above the even-split 0.5.
    const c = computeConcentration([cc('a', 9), cc('b', 1)], 'focus');
    expect(c.herfindahl).toBeCloseTo(0.82, 5);
  });

  it('the explore posture INVERTS the "good" direction', () => {
    // Concentrated effort: focus reads it as aligned (low divergence), explore as
    // divergent (high divergence) — the same input, opposite posture verdict.
    const commits = [cc('a'), cc('a'), cc('a'), cc('b')];
    const focus = computeConcentration(commits, 'focus');
    const explore = computeConcentration(commits, 'explore');
    expect(focus.postureDivergence).toBeCloseTo(1 - focus.herfindahl, 9);
    expect(explore.postureDivergence).toBeCloseTo(explore.herfindahl, 9);
    // For concentrated work, explore diverges MORE than focus.
    expect(explore.postureDivergence).toBeGreaterThan(focus.postureDivergence);
  });

  it('no commits → herfindahl 0, nodes 0', () => {
    const c = computeConcentration([], 'focus');
    expect(c).toMatchObject({ herfindahl: 0, nodes: 0 });
  });
});

/* -------------------------------------------------------------------------- */
/* Starvation                                                                 */
/* -------------------------------------------------------------------------- */

describe('computeStarvation — zero-commit SOs weighted by behind-ness (KTD8)', () => {
  it('an SO at 10% with no commits ranks ABOVE one at 90% with no commits', () => {
    const candidates: StarvationCandidate[] = [
      { id: 'behind', pctCache: 10 },
      { id: 'ahead', pctCache: 90 },
    ];
    const starved = computeStarvation(candidates, new Set());
    expect(starved.map((s) => s.id)).toEqual(['behind', 'ahead']);
    expect(starved[0]!.weight).toBeCloseTo(0.9, 9); // 1 - 10/100
    expect(starved[1]!.weight).toBeCloseTo(0.1, 9); // 1 - 90/100
  });

  it('a touched SO is NOT starved (excluded from the result)', () => {
    const candidates: StarvationCandidate[] = [
      { id: 'touched', pctCache: 10 },
      { id: 'starved', pctCache: 50 },
    ];
    const starved = computeStarvation(candidates, new Set(['touched']));
    expect(starved.map((s) => s.id)).toEqual(['starved']);
  });

  it('an SO with no cached pct is treated as fully behind (weight 1)', () => {
    const starved = computeStarvation([{ id: 'fresh' }], new Set());
    expect(starved[0]).toMatchObject({ id: 'fresh', weight: 1 });
  });
});

/* -------------------------------------------------------------------------- */
/* Carry-aging                                                                */
/* -------------------------------------------------------------------------- */

describe('computeCarryAging — carry_depth distribution + deep count (KTD8)', () => {
  it('three commits at depth >= 3 are counted; depth-1 items are not', () => {
    const aging = computeCarryAging([0, 1, 3, 4, 5]);
    expect(aging.deepCount).toBe(3); // depths 3, 4, 5
    expect(aging.distribution).toEqual({ 0: 1, 1: 1, 3: 1, 4: 1, 5: 1 });
  });

  it('the threshold is 3', () => {
    expect(CARRY_AGING_DECOMPOSE_THRESHOLD).toBe(3);
    expect(computeCarryAging([2, 2, 2]).deepCount).toBe(0);
    expect(computeCarryAging([3]).deepCount).toBe(1);
  });

  it('no commits → empty distribution, zero deep count', () => {
    expect(computeCarryAging([])).toEqual({ distribution: {}, deepCount: 0 });
  });
});

/* -------------------------------------------------------------------------- */
/* gather: foldable scope over pglite                                         */
/* -------------------------------------------------------------------------- */

describe('gatherWeeklyMetrics — reconciled-window read + foldable scope (R12)', () => {
  let db: PgDb;
  beforeEach(async () => {
    db = await makePgliteDb();
  });

  const plan = (over: Partial<WeeklyPlan> & Pick<WeeklyPlan, 'projectId' | 'isoWeek'>): WeeklyPlan => ({
    status: 'RECONCILED',
    posture: 'focus',
    ...over,
  });

  let seq = 0;
  const commit = (
    over: Partial<WeeklyCommit> & Pick<WeeklyCommit, 'projectId' | 'isoWeek'>,
  ): WeeklyCommit => ({
    id: over.id ?? `m-${++seq}`,
    title: 'commit',
    supportingOutcomeId: over.orphanReason ? undefined : (over.supportingOutcomeId ?? 'so-1'),
    alsoAdvances: [],
    category: 'Delivery',
    priorityNumeric: 1,
    status: 'done',
    carryDepth: 0,
    ...over,
  });

  async function seedSo(id: string, pct?: number): Promise<void> {
    await putObjective(db, {
      id,
      org: ORG,
      level: 'supporting_outcome',
      title: id,
      ...(pct != null ? { pct } : {}),
    });
  }

  it('a single project: concentration + starvation + carry-aging compute from the window', async () => {
    await seedSo('so-1', 80);
    await seedSo('so-2', 20); // never touched → starved, far behind
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W23' }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', supportingOutcomeId: 'so-1', carryDepth: 3 }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', supportingOutcomeId: 'so-1' }));

    const metrics = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1'] });
    expect(metrics.concentration.nodes).toBe(1);
    expect(metrics.concentration.herfindahl).toBeCloseTo(1, 9);
    expect(metrics.starvation.map((s) => s.id)).toEqual(['so-2']); // so-1 touched
    expect(metrics.carryAging.deepCount).toBe(1); // the depth-3 commit
  });

  it('only the LATEST reconciled week per project forms the window', async () => {
    await seedSo('so-1');
    await seedSo('so-2');
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W22' }));
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W23' }));
    // Older week touched so-1; latest week touched so-2 → only so-2 is "touched".
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W22', supportingOutcomeId: 'so-1' }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', supportingOutcomeId: 'so-2' }));

    const metrics = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1'] });
    // so-1 was only in the OLD window → starved now; so-2 touched in the latest.
    expect(metrics.starvation.map((s) => s.id)).toEqual(['so-1']);
  });

  it('orphan commits never contribute to concentration but do count toward carry-aging', async () => {
    await seedSo('so-1');
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W23' }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', orphanReason: 'Incident', carryDepth: 4 }));

    const metrics = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1'] });
    expect(metrics.concentration.nodes).toBe(0); // no primary SO link
    expect(metrics.carryAging.deepCount).toBe(1); // the orphan still ages
    expect(metrics.starvation.map((s) => s.id)).toEqual(['so-1']); // so-1 untouched
  });

  it('team scope = the UNION of members project sets folds to one team number', async () => {
    await seedSo('so-1');
    await seedSo('so-2');
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W23' }));
    await upsertPlan(db, plan({ projectId: 'p2', isoWeek: '2026-W23' }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', supportingOutcomeId: 'so-1' }));
    await createCommit(db, commit({ projectId: 'p2', isoWeek: '2026-W23', supportingOutcomeId: 'so-2' }));

    // The whole team (both projects) touched 2 distinct SOs.
    const team = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1', 'p2'] });
    expect(team.concentration.nodes).toBe(2);
    expect(team.starvation).toEqual([]); // both SOs touched somewhere on the team

    // A single member sees only their own SO touched; the other is starved.
    const member = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1'] });
    expect(member.concentration.nodes).toBe(1);
    expect(member.starvation.map((s) => s.id)).toEqual(['so-2']);
  });

  it('a subject with no reconciled week → empty metrics (no window)', async () => {
    await seedSo('so-1');
    const metrics = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p-none'] });
    expect(metrics.concentration).toMatchObject({ herfindahl: 0, nodes: 0 });
    expect(metrics.carryAging).toEqual({ distribution: {}, deepCount: 0 });
    // No window touched anything → every org SO is starved.
    expect(metrics.starvation.map((s) => s.id)).toEqual(['so-1']);
  });

  it('the window posture folds to explore if ANY project window declares it', async () => {
    await seedSo('so-1');
    await upsertPlan(db, plan({ projectId: 'p1', isoWeek: '2026-W23', posture: 'focus' }));
    await upsertPlan(db, plan({ projectId: 'p2', isoWeek: '2026-W23', posture: 'explore' }));
    await createCommit(db, commit({ projectId: 'p1', isoWeek: '2026-W23', supportingOutcomeId: 'so-1' }));
    await createCommit(db, commit({ projectId: 'p2', isoWeek: '2026-W23', supportingOutcomeId: 'so-1' }));

    const metrics = await gatherWeeklyMetrics(db, ORG, { projectIds: ['p1', 'p2'] });
    // explore divergence = herfindahl (the concentration itself).
    expect(metrics.concentration.postureDivergence).toBeCloseTo(metrics.concentration.herfindahl, 9);
  });
});
