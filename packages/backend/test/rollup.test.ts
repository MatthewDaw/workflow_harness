import { beforeEach, describe, expect, it } from 'vitest';
import type { ObjectiveNode, Project, WeeklyCommit } from '@harness/shared';
import { leafPct, recomputeRollup, type CommitCredit } from '../src/projections/rollup.js';
import { deriveCategory, wsjfPriority } from '../src/projections/weeklyLifecycle.js';
import { recomputeOrgRollup } from '../src/projections/rollupRepo.js';
import { completeReconcile } from '../src/rest/weeklyTransitions.js';
import { putObjective as pgPutObjective, getObjective as pgGetObjective } from '../src/db/pg/objectivesRepo.js';
import {
  createCommit as repoCreateCommit,
  updateCommit as repoUpdateCommit,
  upsertPlan,
} from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { httpEvent } from './helpers/httpevent.js';
import { randomUUID } from 'node:crypto';

/**
 * U6: the single-source roll-up (reconciled weekly commits — KTD5) + the DERIVED
 * chess layer (`deriveCategory` / `wsjfPriority` — KTD4). The GitHub `progressPct`
 * feed is removed: an SO reads 0% until its first reconciliation, and only the
 * PRIMARY SO link earns credit (orphan commits + `alsoAdvances` contribute zero).
 */

const ORG = 'acme';
const PROJ = 'weekly-compass';
const WEEK = '2026-W23';
const MATT = 'matt';

const credit = (so: string, status: CommitCredit['status']): CommitCredit => ({
  supportingOutcomeId: so,
  status,
});

describe('leafPct — reconciled-commit single source (KTD5/U6)', () => {
  it('2 done + 1 partial → 83.33 (mean ×100)', () => {
    expect(leafPct('so-a', [credit('so-a', 'done'), credit('so-a', 'done'), credit('so-a', 'partial')])).toBeCloseTo(83.33, 1);
  });

  it('an SO with no reconciled data → 0% (progressPct removed — regression guard)', () => {
    // No commit references `so-a`: the OLD code would have fallen back to a
    // project `progressPct`; the new code has NO fallback → 0%.
    expect(leafPct('so-a', [credit('so-other', 'done')])).toBe(0);
    expect(leafPct('so-a', [])).toBe(0);
  });

  it('credit weights: done=1.0, partial=0.5, planned/dropped=0', () => {
    expect(leafPct('s', [credit('s', 'done')])).toBe(100);
    expect(leafPct('s', [credit('s', 'partial')])).toBe(50);
    expect(leafPct('s', [credit('s', 'planned')])).toBe(0);
    expect(leafPct('s', [credit('s', 'dropped')])).toBe(0);
  });
});

describe('recomputeRollup — parent rolls up the mean', () => {
  it('parent of a fully-done and an empty SO is 50%', () => {
    const nodes: ObjectiveNode[] = [
      { id: 'out', org: ORG, level: 'outcome', title: 'out' },
      { id: 'so-a', org: ORG, level: 'supporting_outcome', title: 'so-a', parentId: 'out' },
      { id: 'so-b', org: ORG, level: 'supporting_outcome', title: 'so-b', parentId: 'out' },
    ];
    const out = recomputeRollup({ nodes, commits: [credit('so-a', 'done')] });
    const byId = new Map(out.map((n) => [n.id, n.pct]));
    expect(byId.get('so-a')).toBe(100);
    expect(byId.get('so-b')).toBe(0); // no reconciled data
    expect(byId.get('out')).toBe(50);
  });

  it('an orphan-only set contributes 0 to its (absent) SO', () => {
    const nodes: ObjectiveNode[] = [
      { id: 'so-a', org: ORG, level: 'supporting_outcome', title: 'so-a' },
    ];
    // No CommitCredit ever carries an orphan reason (the gather drops orphans), so
    // an orphan-only week produces an empty credit list → the SO stays 0%.
    const out = recomputeRollup({ nodes, commits: [] });
    expect(out[0]!.pct).toBe(0);
  });
});

describe('deriveCategory — RCDO position + plan intent (KTD4)', () => {
  it("an orphan commit's reason IS its category", () => {
    expect(deriveCategory({ orphan: true, orphanReason: 'Incident' })).toBe('Incident');
    expect(deriveCategory({ orphan: true, orphanReason: 'KTLO' })).toBe('KTLO');
  });

  it('a delivery-tier SO defaults to Delivery', () => {
    expect(deriveCategory({ rcdoLevel: 'supporting_outcome' })).toBe('Delivery');
    expect(deriveCategory({ rcdoLevel: 'outcome' })).toBe('Delivery');
    expect(deriveCategory({})).toBe('Delivery');
  });

  it('an SO laddering to a top-of-tree intent reads Strategic', () => {
    expect(deriveCategory({ rcdoLevel: 'rally_cry' })).toBe('Strategic');
    expect(deriveCategory({ rcdoLevel: 'defining_objective' })).toBe('Strategic');
  });

  it('a Strategic plan intent overrides the delivery-tier default', () => {
    expect(deriveCategory({ rcdoLevel: 'supporting_outcome', planIntent: 'Strategic' })).toBe('Strategic');
  });
});

describe('wsjfPriority — WSJF-from-the-tree (KTD4)', () => {
  it('CoD = branchWeight × behind-fraction, over jobSize', () => {
    // weight 4, branch 25% done → behind 0.75 → CoD 3; jobSize 2 → 1.5.
    expect(wsjfPriority({ branchWeight: 4, branchPct: 25, jobSize: 2 })).toBeCloseTo(1.5, 5);
  });

  it('a more-behind branch outranks a less-behind one at equal weight + size', () => {
    const behind = wsjfPriority({ branchWeight: 1, branchPct: 10, jobSize: 1 });
    const ahead = wsjfPriority({ branchWeight: 1, branchPct: 90, jobSize: 1 });
    expect(behind).toBeGreaterThan(ahead);
  });

  it('an explicit costOfDelay overrides the weight × behind-ness compute', () => {
    expect(wsjfPriority({ costOfDelay: 5, jobSize: 2 })).toBeCloseTo(2.5, 5);
  });

  it('guards a zero/absent jobSize (sort key stays finite)', () => {
    expect(wsjfPriority({ branchWeight: 2, branchPct: 0 })).toBe(2); // size absent → just CoD
    expect(Number.isFinite(wsjfPriority({ costOfDelay: 3, jobSize: 0 }))).toBe(true);
  });
});

// --- Integration: completeReconcile moves the SO pct_cache (KTD5/U6) ---------

const { repo } = memRepoHarness();

describe('completeReconcile drives the single-source roll-up', () => {
  let db: PgDb;
  beforeEach(async () => {
    db = await makePgliteDb();
  });
  const deps = () => ({ repo, db });

  async function seedTree(): Promise<void> {
    await pgPutObjective(db, { id: 'rally', org: ORG, level: 'rally_cry', title: 'rally' });
    await pgPutObjective(db, { id: 'out', org: ORG, level: 'outcome', title: 'out', parentId: 'rally' });
    await pgPutObjective(db, { id: 'so-a', org: ORG, level: 'supporting_outcome', title: 'so-a', parentId: 'out' });
  }

  async function seedCommit(over: Partial<WeeklyCommit>): Promise<WeeklyCommit> {
    const commit: WeeklyCommit = {
      id: randomUUID(),
      projectId: PROJ,
      isoWeek: WEEK,
      title: 'commit',
      supportingOutcomeId: over.supportingOutcomeId,
      orphanReason: over.orphanReason,
      alsoAdvances: [],
      category: 'Delivery',
      priorityNumeric: 1,
      status: over.status ?? 'planned',
      carryDepth: 0,
      ...over,
    };
    await repoCreateCommit(db, commit);
    return commit;
  }

  function completeEvent() {
    return httpEvent({
      method: 'POST',
      userId: MATT,
      org: ORG,
      rawPath: `/projects/${PROJ}/weekly/${WEEK}/reconcile/complete`,
      path: { pid: PROJ, week: WEEK },
    });
  }

  it('reconciling a week (1 done, 1 partial) moves the SO pct_cache to 75%', async () => {
    await repo.putProject({
      id: PROJ,
      name: PROJ,
      repo: 'gh/acme/wc',
      ownerUserId: MATT,
      liveSessionCount: 0,
    } as Project);
    await seedTree();
    await upsertPlan(db, { projectId: PROJ, isoWeek: WEEK, status: 'RECONCILING', posture: 'focus' });
    await seedCommit({ supportingOutcomeId: 'so-a', status: 'done' });
    await seedCommit({ supportingOutcomeId: 'so-a', status: 'partial' });

    const res = await completeReconcile(completeEvent(), deps());
    expect(res).toMatchObject({ statusCode: 200 });

    // (1.0 + 0.5)/2 = 0.75 → 75%; parent rolls it up unchanged (single child).
    expect((await pgGetObjective(db, ORG, 'so-a'))?.pct).toBe(75);
    expect((await pgGetObjective(db, ORG, 'out'))?.pct).toBe(75);
    expect((await pgGetObjective(db, ORG, 'rally'))?.pct).toBe(75);
  });

  it('an SO with only an orphan commit stays 0% after reconcile', async () => {
    await repo.putProject({
      id: PROJ,
      name: PROJ,
      repo: 'gh/acme/wc',
      ownerUserId: MATT,
      liveSessionCount: 0,
    } as Project);
    await seedTree();
    await upsertPlan(db, { projectId: PROJ, isoWeek: WEEK, status: 'RECONCILING', posture: 'focus' });
    // An orphan commit must still be reconciled (terminal) so complete is allowed.
    const orphan = await seedCommit({ orphanReason: 'Incident', status: 'planned' });
    await repoUpdateCommit(db, orphan.id, { status: 'done' });

    const res = await completeReconcile(completeEvent(), deps());
    expect(res).toMatchObject({ statusCode: 200 });

    // Orphan credit never reaches a leaf (KTD9/KTD10) → so-a stays 0%.
    expect((await pgGetObjective(db, ORG, 'so-a'))?.pct).toBe(0);
  });
});
