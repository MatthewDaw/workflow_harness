import { eq } from 'drizzle-orm';
import type { WeeklyCommit } from '@harness/shared';
import { calibrations, type CalibrationRow } from '../db/pg/schema.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * Reconciliation calibration feedback (U19). When a week reconciles, the agent
 * wants to know how much of what a person LOCKS they actually finish, so it can
 * right-size next week's proposal ("you complete ~60% — here are the 6 highest-
 * leverage units, not 10"). On every `completeReconcile`, the just-reconciled
 * week's terminal commits accumulate into the owner's running totals (a trailing
 * accumulation across that person's reconciled weeks), keyed by `userId`.
 *
 * This is ADVISORY — it never blocks a transition. The store is purely a feedback
 * signal the plan-anchored `/hq-weekly-update` skill reads in its propose step.
 *
 * Mirrors `rollupRepo`'s compute-on-transition shape: a pure aggregate (`tally`)
 * the transactional `completeReconcile` calls, plus a Postgres store keyed by user.
 */

/** The public per-person calibration the agent reads. */
export interface Calibration {
  userId: string;
  /** Terminal commits the person has reconciled (the rate denominator). */
  lockedCount: number;
  /** Of those, the ones reconciled `done` (the rate numerator). */
  doneCount: number;
  /**
   * `doneCount / lockedCount` — the headline completion rate. Undefined when the
   * person has no reconciled history yet (a first-ever week — the agent proposes
   * an unscaled set).
   */
  rate?: number;
  /** Of the higher-priority half across windows, how many shipped (`done`). */
  highPriorityFirstCount: number;
  /** The size of that higher-priority half across windows. */
  highPriorityTotal: number;
  /** Epoch-ms of the last `completeReconcile` that touched this row. */
  updatedAt?: number;
}

/** A single window's contribution to the running calibration (pure). */
export interface CalibrationDelta {
  /** Terminal (not `planned`) commits in the window. */
  terminal: number;
  /** Of those, the ones reconciled `done`. */
  done: number;
  /** The higher-priority half of the terminal commits that shipped (`done`). */
  highPriorityFirst: number;
  /** The size of that higher-priority half. */
  highPriorityTotal: number;
}

/**
 * Tally a reconciled week's terminal commits into a calibration delta (pure). A
 * commit is `terminal` once it is not `planned`; of those, `done` ones count
 * toward the completion rate. The "did high-leverage ship first" signal looks at
 * the higher-priority half (by derived `priorityNumeric`, KTD4) and counts how
 * many of those shipped — so a person who finishes the easy, low-leverage items
 * but slips the high-leverage ones is visible.
 */
export function tally(commits: WeeklyCommit[]): CalibrationDelta {
  const terminalCommits = commits.filter((c) => c.status !== 'planned');
  const done = terminalCommits.filter((c) => c.status === 'done').length;

  // The higher-priority half by derived leverage (ties broken by id, matching the
  // repo's deterministic sort). With an odd count, the median commit counts as
  // higher-priority (`ceil`), so a single-commit window's lone item is "high".
  const byPriority = [...terminalCommits].sort(
    (a, b) => b.priorityNumeric - a.priorityNumeric || a.id.localeCompare(b.id),
  );
  const halfSize = Math.ceil(byPriority.length / 2);
  const highHalf = byPriority.slice(0, halfSize);
  const highPriorityFirst = highHalf.filter((c) => c.status === 'done').length;

  return {
    terminal: terminalCommits.length,
    done,
    highPriorityFirst,
    highPriorityTotal: highHalf.length,
  };
}

/** Map a stored row to the public `Calibration`, omitting absent optionals. */
function toCalibration(row: CalibrationRow): Calibration {
  return {
    userId: row.userId,
    lockedCount: row.lockedCount,
    doneCount: row.doneCount,
    ...(row.rate != null ? { rate: row.rate } : {}),
    highPriorityFirstCount: row.highPriorityFirstCount,
    highPriorityTotal: row.highPriorityTotal,
    ...(row.updatedAt != null ? { updatedAt: row.updatedAt } : {}),
  };
}

/**
 * The person's current calibration, or undefined when they have no reconciled
 * history yet (a first-ever week — the agent proposes an unscaled set).
 */
export async function getCalibration(db: PgDb, userId: string): Promise<Calibration | undefined> {
  const rows = await db
    .select()
    .from(calibrations)
    .where(eq(calibrations.userId, userId))
    .limit(1);
  return rows[0] ? toCalibration(rows[0]) : undefined;
}

/**
 * Accumulate a reconciled week's commits into the person's running calibration
 * and persist it (U19). Called from inside `completeReconcile`'s transaction so
 * the calibration write is atomic with the reconcile. A week with no terminal
 * commits is a no-op (nothing to learn from). `now` is supplied by the caller so
 * the transactional path stays free of `Date.now()`.
 *
 * The recomputed `rate` is `doneCount / lockedCount` over the trailing totals.
 * Returns the updated calibration (or undefined when nothing accumulated).
 */
export async function recordCalibration(
  db: PgDb,
  userId: string,
  commits: WeeklyCommit[],
  now: number,
): Promise<Calibration | undefined> {
  const delta = tally(commits);
  if (delta.terminal === 0) return getCalibration(db, userId);

  const prev = await getCalibration(db, userId);
  const lockedCount = (prev?.lockedCount ?? 0) + delta.terminal;
  const doneCount = (prev?.doneCount ?? 0) + delta.done;
  const highPriorityFirstCount = (prev?.highPriorityFirstCount ?? 0) + delta.highPriorityFirst;
  const highPriorityTotal = (prev?.highPriorityTotal ?? 0) + delta.highPriorityTotal;
  const rate = lockedCount > 0 ? doneCount / lockedCount : null;

  await db
    .insert(calibrations)
    .values({
      userId,
      lockedCount,
      doneCount,
      rate,
      highPriorityFirstCount,
      highPriorityTotal,
      updatedAt: now,
    })
    .onConflictDoUpdate({
      target: calibrations.userId,
      set: {
        lockedCount,
        doneCount,
        rate,
        highPriorityFirstCount,
        highPriorityTotal,
        updatedAt: now,
      },
    });

  return {
    userId,
    lockedCount,
    doneCount,
    ...(rate != null ? { rate } : {}),
    highPriorityFirstCount,
    highPriorityTotal,
    updatedAt: now,
  };
}
