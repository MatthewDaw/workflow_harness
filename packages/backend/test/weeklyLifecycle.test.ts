import { describe, expect, it } from 'vitest';
import {
  WEEKLY_STATUSES,
  weeklyCommitSchema,
  weeklyPlanSchema,
  type WeeklyStatus,
} from '@harness/shared';
import { canTransition } from '../src/projections/weeklyLifecycle.js';

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
