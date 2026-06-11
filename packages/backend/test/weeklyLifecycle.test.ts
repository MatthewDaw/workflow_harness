import { describe, expect, it } from 'vitest';
import {
  WEEKLY_STATUSES,
  weeklyCommitSchema,
  weeklyPlanSchema,
  type WeeklyCommit,
  type WeeklyStatus,
} from '@harness/shared';
import {
  canTransition,
  carryForwardCommits,
  nextIsoWeek,
} from '../src/projections/weeklyLifecycle.js';

/**
 * U1: weekly domain model — schema refinements (SO-or-orphan, defaults, regexes)
 * and the pure lifecycle `canTransition` table (DRAFT -> LOCKED -> RECONCILING ->
 * RECONCILED, and nothing else). Covers R1 (CRUD shape), R2 (derived chess
 * fields), R3 (the state machine).
 */

describe('weeklyCommitSchema', () => {
  it('parses a commit carrying a supporting outcome (happy path)', () => {
    const parsed = weeklyCommitSchema.parse({
      id: 'c1',
      projectId: 'p1',
      isoWeek: '2026-W23',
      title: 'Ship the lock guard',
      supportingOutcomeId: 'so-1',
      category: 'Delivery',
      priorityNumeric: 4.2,
    });
    expect(parsed.supportingOutcomeId).toBe('so-1');
    // Defaults apply: alsoAdvances -> [], status -> planned, carryDepth -> 0.
    expect(parsed.alsoAdvances).toEqual([]);
    expect(parsed.status).toBe('planned');
    expect(parsed.carryDepth).toBe(0);
  });

  it('parses a commit carrying only an orphanReason (happy path)', () => {
    const parsed = weeklyCommitSchema.parse({
      id: 'c2',
      projectId: 'p1',
      isoWeek: '2026-W23',
      title: 'Page-out incident triage',
      orphanReason: 'Incident',
      category: 'Incident',
      priorityNumeric: 1,
    });
    expect(parsed.orphanReason).toBe('Incident');
    expect(parsed.supportingOutcomeId).toBeUndefined();
  });

  it('rejects a commit with NEITHER an SO nor an orphanReason (refinement)', () => {
    const result = weeklyCommitSchema.safeParse({
      id: 'c3',
      projectId: 'p1',
      isoWeek: '2026-W23',
      title: 'Floating work',
      category: 'Delivery',
      priorityNumeric: 0,
    });
    expect(result.success).toBe(false);
  });

  it('rejects a bad isoWeek', () => {
    const result = weeklyCommitSchema.safeParse({
      id: 'c4',
      projectId: 'p1',
      isoWeek: '2026-23', // missing the W
      title: 'x',
      supportingOutcomeId: 'so-1',
      category: 'Delivery',
      priorityNumeric: 0,
    });
    expect(result.success).toBe(false);
  });

  it('rejects an unknown category', () => {
    const result = weeklyCommitSchema.safeParse({
      id: 'c5',
      projectId: 'p1',
      isoWeek: '2026-W23',
      title: 'x',
      supportingOutcomeId: 'so-1',
      category: 'Bogus',
      priorityNumeric: 0,
    });
    expect(result.success).toBe(false);
  });

  it('defaults alsoAdvances to [] and carryDepth to 0 when omitted (edge)', () => {
    const parsed = weeklyCommitSchema.parse({
      id: 'c6',
      projectId: 'p1',
      isoWeek: '2026-W23',
      title: 'x',
      supportingOutcomeId: 'so-1',
      category: 'Strategic',
      priorityNumeric: 9,
    });
    expect(parsed.alsoAdvances).toEqual([]);
    expect(parsed.carryDepth).toBe(0);
  });
});

describe('weeklyPlanSchema', () => {
  it('defaults status to DRAFT and posture to focus', () => {
    const parsed = weeklyPlanSchema.parse({ projectId: 'p1', isoWeek: '2026-W23' });
    expect(parsed.status).toBe('DRAFT');
    expect(parsed.posture).toBe('focus');
  });

  it('rejects a bad isoWeek', () => {
    expect(weeklyPlanSchema.safeParse({ projectId: 'p1', isoWeek: 'nope' }).success).toBe(false);
  });
});

describe('canTransition', () => {
  it('allows every legal pair', () => {
    expect(canTransition('DRAFT', 'LOCKED')).toBe(true);
    expect(canTransition('LOCKED', 'RECONCILING')).toBe(true);
    expect(canTransition('RECONCILING', 'RECONCILED')).toBe(true);
  });

  it('rejects skip-ahead, backward, and terminal transitions (Covers R3)', () => {
    expect(canTransition('DRAFT', 'RECONCILED')).toBe(false);
    expect(canTransition('DRAFT', 'RECONCILING')).toBe(false);
    expect(canTransition('LOCKED', 'DRAFT')).toBe(false);
    expect(canTransition('RECONCILING', 'LOCKED')).toBe(false);
    // RECONCILED is terminal — nothing follows it.
    for (const to of WEEKLY_STATUSES) {
      expect(canTransition('RECONCILED', to as WeeklyStatus)).toBe(false);
    }
  });

  it('rejects a self-loop (an in-place edit is not a transition)', () => {
    for (const s of WEEKLY_STATUSES) {
      expect(canTransition(s as WeeklyStatus, s as WeeklyStatus)).toBe(false);
    }
  });
});

/**
 * U5: carry-forward (the OUTPUT action of `reconcile/complete`, KTD3) + carry-aging.
 * Only incomplete commits (`status ∈ {planned, partial}`) carry into next week's
 * DRAFT, cloned with a fresh week, reset outcome, and `carryDepth + 1`; `done`/
 * `dropped` never carry. `nextIsoWeek` rolls W52/W53 into the next year's W01. A
 * clone reaching `carryDepth >= 3` surfaces a decompose/kill nudge. Covers R5.
 */

const ORG_PROJ = 'p1';

/** Build a reconciled-week source commit fixture. An orphan fixture (one with an
 * `orphanReason`) carries no SO link, mirroring the SO-XOR-orphan invariant. */
function commitFixture(over: Partial<WeeklyCommit>): WeeklyCommit {
  return {
    id: over.id ?? `c-${Math.random().toString(36).slice(2)}`,
    projectId: ORG_PROJ,
    isoWeek: over.isoWeek ?? '2026-W23',
    title: over.title ?? 'a commit',
    supportingOutcomeId:
      'supportingOutcomeId' in over || over.orphanReason
        ? over.supportingOutcomeId
        : 'so-1',
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

describe('nextIsoWeek', () => {
  it('advances within the year (happy path)', () => {
    expect(nextIsoWeek('2026-W23')).toBe('2026-W24');
  });

  it('rolls the final week of a 52-week year into the next year W01 (edge)', () => {
    // 2025 is an ISO-52-week year (Jan 1 2025 is a Wednesday, not leap).
    expect(nextIsoWeek('2025-W52')).toBe('2026-W01');
  });

  it("rolls 2026-W52 -> 2027-W01 only via W53 (2026 is a 53-week year)", () => {
    // 2026 is an ISO-53-week year: Jan 1 2026 is a Thursday.
    expect(nextIsoWeek('2026-W52')).toBe('2026-W53');
    expect(nextIsoWeek('2026-W53')).toBe('2027-W01');
  });

  it('throws on a malformed isoWeek', () => {
    expect(() => nextIsoWeek('2026-23')).toThrow();
  });
});

describe('carryForwardCommits', () => {
  it('carries only the incomplete items: 4 commits (2 done, 1 partial, 1 planned) -> 2 clones (happy path)', () => {
    const commits: WeeklyCommit[] = [
      commitFixture({ id: 'd1', status: 'done', actualOutcome: 'shipped' }),
      commitFixture({ id: 'd2', status: 'done' }),
      commitFixture({ id: 'pa', status: 'partial', actualOutcome: 'half', carryDepth: 0 }),
      commitFixture({ id: 'pl', status: 'planned' }),
    ];

    const { clones, sourceIds, deepCarryNudge } = carryForwardCommits(commits, '2026-W24');

    expect(clones).toHaveLength(2);
    expect(sourceIds.sort()).toEqual(['pa', 'pl']);
    expect(deepCarryNudge).toEqual([]);
    for (const clone of clones) {
      // Each clone: fresh week, status reset to planned, outcome cleared,
      // carriedFromWeek set, carryDepth incremented from its source's 0.
      expect(clone.isoWeek).toBe('2026-W24');
      expect(clone.status).toBe('planned');
      expect(clone.actualOutcome).toBeUndefined();
      expect(clone.carriedFromWeek).toBe('2026-W23');
      expect(clone.carryDepth).toBe(1);
      // A fresh id, not the source's.
      expect(clone.id).not.toBe('pa');
      expect(clone.id).not.toBe('pl');
    }
  });

  it('never carries a dropped commit (edge)', () => {
    const commits = [
      commitFixture({ id: 'dr', status: 'dropped' }),
      commitFixture({ id: 'dn', status: 'done' }),
    ];
    const { clones, sourceIds } = carryForwardCommits(commits, '2026-W24');
    expect(clones).toEqual([]);
    expect(sourceIds).toEqual([]);
  });

  it('preserves SO link / orphan reason / category / priority on the clone', () => {
    const linked = commitFixture({
      id: 'l',
      status: 'partial',
      supportingOutcomeId: 'so-9',
      category: 'Strategic',
      priorityNumeric: 4.2,
      alsoAdvances: ['so-x'],
    });
    const orphan = commitFixture({
      id: 'o',
      status: 'planned',
      supportingOutcomeId: undefined,
      orphanReason: 'Incident',
      category: 'Incident',
    });
    const { clones } = carryForwardCommits([linked, orphan], '2026-W24');
    const linkedClone = clones.find((c) => c.supportingOutcomeId === 'so-9');
    const orphanClone = clones.find((c) => c.orphanReason === 'Incident');
    expect(linkedClone?.category).toBe('Strategic');
    expect(linkedClone?.priorityNumeric).toBe(4.2);
    expect(linkedClone?.alsoAdvances).toEqual(['so-x']);
    expect(linkedClone?.orphanReason).toBeUndefined();
    expect(orphanClone?.supportingOutcomeId).toBeUndefined();
  });

  it('raises the decompose/kill nudge when a clone reaches carryDepth 3 (edge)', () => {
    // A commit already at carryDepth 2 carrying again reaches 3.
    const aged = commitFixture({ id: 'aged', status: 'partial', carryDepth: 2 });
    const fresh = commitFixture({ id: 'fresh', status: 'partial', carryDepth: 0 });
    const { clones, deepCarryNudge } = carryForwardCommits([aged, fresh], '2026-W24');
    const agedClone = clones.find((c) => c.carryDepth === 3);
    expect(agedClone).toBeDefined();
    expect(deepCarryNudge).toEqual([agedClone?.id]);
  });
});
