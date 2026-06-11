import { randomUUID } from 'node:crypto';
import type { CommitCategory, ObjectiveLevel, WeeklyCommit, WeeklyStatus } from '@harness/shared';

/**
 * Weekly-commit lifecycle pure logic (weekly-commit-lifecycle, U1; the chess
 * layer FILLED IN U6).
 *
 * The lifecycle state machine (KTD2) lives on the plan: each transition is its
 * own server endpoint, and `canTransition` is the SINGLE SOURCE OF TRUTH for
 * legality (an illegal transition is a 409). This module also hosts the DERIVED
 * chess-layer helpers (`deriveCategory`, `wsjfPriority` — KTD4) and carry-forward
 * (U5). The chess-layer helpers read the linked SO's RCDO position / weight and
 * its behind-ness from the roll-up (KTD4); they stay PURE (the caller resolves
 * the RCDO node + plan-unit estimate and passes them in) so they unit-test
 * without a database.
 */

/**
 * The ONLY legal lifecycle transitions (KTD2). Read as: from a key state, the
 * set of states it may move to. "Carry Forward" is the OUTPUT of
 * `RECONCILING -> RECONCILED`, not a fifth state, so it is absent here.
 *   DRAFT -> LOCKED -> RECONCILING -> RECONCILED
 */
const LEGAL_TRANSITIONS: Record<WeeklyStatus, readonly WeeklyStatus[]> = {
  DRAFT: ['LOCKED'],
  LOCKED: ['RECONCILING'],
  RECONCILING: ['RECONCILED'],
  RECONCILED: [],
};

/**
 * Whether the lifecycle may move from `from` to `to`. Pure and total — the
 * single source of truth the transition endpoints consult before applying side
 * effects. A self-loop (e.g. `DRAFT -> DRAFT`, which is an in-place edit, not a
 * transition) is NOT a legal transition and returns `false`.
 */
export function canTransition(from: WeeklyStatus, to: WeeklyStatus): boolean {
  return (LEGAL_TRANSITIONS[from] ?? []).includes(to);
}

/**
 * Inputs to the DERIVED `category` (KTD4): the plan implementation-unit's intent
 * and the linked SO's RCDO position. An orphan commit's reason IS its category;
 * a linked commit's category is a read-through projection of where its SO sits in
 * the RCDO tree plus the plan unit's intent (U6).
 */
export interface DeriveCategoryInput {
  /** True when the commit carries no SO link (it is an orphan — KTD10). */
  orphan?: boolean;
  /** The orphan reason, which IS the category for an orphan commit (KTD10). */
  orphanReason?: CommitCategory;
  /**
   * The RCDO level of the linked Supporting Outcome's nearest non-leaf ancestor
   * (or the SO's own level) — the "position" the category reads. A commit whose
   * SO ladders up to a top-of-tree intent (a `rally_cry` / `defining_objective`)
   * is `Strategic`; one anchored at the `outcome`/`supporting_outcome` delivery
   * tier is `Delivery`.
   */
  rcdoLevel?: ObjectiveLevel;
  /**
   * The plan implementation-unit's declared intent, when the agent/plan tagged
   * the unit as strategic/exploratory. Lets the plan-unit half of the projection
   * (KTD4) override the position-only default; an explicit `Strategic` intent on
   * a delivery-tier SO still reads `Strategic`.
   */
  planIntent?: CommitCategory;
}

/** RCDO levels that ladder a commit up to the STRATEGIC tier (KTD4). */
const STRATEGIC_LEVELS: ReadonlySet<ObjectiveLevel> = new Set([
  'rally_cry',
  'defining_objective',
]);

/**
 * Derive a commit's chess-layer `category` from its plan unit + linked SO
 * position (KTD4). An orphan commit's reason IS its category. A linked commit
 * reads `Strategic` when its SO ladders up to a top-of-tree intent (`rally_cry` /
 * `defining_objective`) OR the plan unit declared a `Strategic` intent; otherwise
 * the delivery-tier default `Delivery`. Pure: the caller resolves the RCDO level
 * and the plan intent and passes them in.
 */
export function deriveCategory(input: DeriveCategoryInput): CommitCategory {
  if (input.orphan && input.orphanReason) return input.orphanReason;
  if (input.planIntent === 'Strategic') return 'Strategic';
  if (input.rcdoLevel && STRATEGIC_LEVELS.has(input.rcdoLevel)) return 'Strategic';
  return 'Delivery';
}

/**
 * Inputs to the DERIVED WSJF priority (KTD4): Cost of Delay (inherited from the
 * commit's RCDO position — its branch `weight` × how far BEHIND that branch is,
 * read from the roll-up's cached `pct`) over Job Size (the plan unit's estimate).
 * Either `costOfDelay` is supplied directly OR it is computed from `branchWeight`
 * × `behindPct`; the caller (U3/U10) resolves the linked SO's weight + cached pct
 * and the plan-unit estimate and passes them in.
 */
export interface WsjfPriorityInput {
  /** Cost of Delay numerator, supplied directly (overrides the weight × behind-ness compute). */
  costOfDelay?: number;
  /** The linked SO branch's weight (objectives `weight`, default 1). */
  branchWeight?: number;
  /** The linked SO branch's cached roll-up completion (0..100); behind-ness is `100 - pct`. */
  branchPct?: number;
  /** Job Size denominator (plan-unit estimate). */
  jobSize?: number;
}

/**
 * Derive a commit's WSJF-from-the-tree priority (KTD4): `CoD / JobSize`, where
 * `CoD = branchWeight × behindFraction` (behind-ness = `(100 - branchPct) / 100`,
 * clamped to `[0, 1]`) unless an explicit `costOfDelay` is given. The commit list
 * self-sorts by this derived leverage (descending). Pure, and guards a
 * zero/absent Job Size so the sort key is always finite (size ≤ 0 ⇒ just the CoD).
 */
export function wsjfPriority(input: WsjfPriorityInput): number {
  const cod = input.costOfDelay ?? costOfDelayFromBranch(input.branchWeight, input.branchPct);
  const size = input.jobSize ?? 0;
  if (size <= 0) return cod;
  return cod / size;
}

/** Cost of Delay inherited from the SO branch: `weight × behind-fraction` (KTD4). */
function costOfDelayFromBranch(branchWeight?: number, branchPct?: number): number {
  const weight = branchWeight ?? 1;
  const pct = branchPct ?? 0; // no roll-up data yet ⇒ fully behind (most leverage)
  const behind = Math.min(1, Math.max(0, (100 - pct) / 100));
  return weight * behind;
}

/* -------------------------------------------------------------------------- */
/* Carry-forward (U5)                                                          */
/* -------------------------------------------------------------------------- */

/**
 * `carry_depth >= 3` raises a "decompose or kill" nudge (KTD3) — a SURFACED
 * signal, never a block. A line that has carried this many times is a smell
 * (chronic over-commitment, or a unit that needs decomposing).
 */
export const CARRY_DEPTH_NUDGE_THRESHOLD = 3;

/**
 * The outcome statuses whose commits CARRY FORWARD into next week's DRAFT (KTD3):
 * the incomplete ones. `done` is finished and `dropped` is a deliberate kill —
 * neither carries.
 */
const CARRYING_STATUSES: ReadonlySet<WeeklyCommit['status']> = new Set(['planned', 'partial']);

/**
 * Advance the next ISO week from `YYYY-Www`, rolling W52/W53 into the next year's
 * W01 (KTD3). Carry-forward clones land in this returned week's DRAFT. A year has
 * 53 ISO weeks only when Jan 1 is a Thursday, or it is a leap year and Jan 1 is a
 * Wednesday (then Dec 31 is a Thursday); otherwise 52.
 */
export function nextIsoWeek(isoWeek: string): string {
  const m = /^(\d{4})-W(\d{2})$/.exec(isoWeek);
  if (!m) throw new Error(`malformed isoWeek: ${isoWeek}`);
  const year = Number(m[1]);
  const week = Number(m[2]);
  if (week >= isoWeeksInYear(year)) return `${year + 1}-W01`;
  return `${year}-W${String(week + 1).padStart(2, '0')}`;
}

/** The number of ISO weeks (52 or 53) in a given ISO-week-numbering year. */
function isoWeeksInYear(year: number): number {
  const jan1 = new Date(Date.UTC(year, 0, 1)).getUTCDay(); // Sun=0..Sat=6
  const isLeap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  if (jan1 === 4 || (isLeap && jan1 === 3)) return 53;
  return 52;
}

/** The pure result of carrying a reconciled week's incomplete commits forward. */
export interface CarryForwardResult {
  /** The fresh clones to insert into next week's DRAFT. */
  clones: WeeklyCommit[];
  /** The ids of the SOURCE commits to stamp with `carriedToWeek = nextWeek`. */
  sourceIds: string[];
  /** The ids of clones that reached `carryDepth >= 3` — the decompose/kill nudge. */
  deepCarryNudge: string[];
}

/**
 * Clone one source commit into next week's DRAFT (KTD3): a fresh id, `isoWeek`
 * set to `nextWeek`, `status` reset to `planned`, `actualOutcome` cleared,
 * `carriedFromWeek` set to the source's week, `carryDepth` incremented. The
 * source's own `carriedToWeek` is stamped by the caller (it lives on the source,
 * not the clone). Pure.
 */
function carryClone(source: WeeklyCommit, nextWeek: string): WeeklyCommit {
  return {
    id: randomUUID(),
    projectId: source.projectId,
    isoWeek: nextWeek,
    title: source.title,
    ...(source.supportingOutcomeId !== undefined
      ? { supportingOutcomeId: source.supportingOutcomeId }
      : {}),
    ...(source.orphanReason !== undefined ? { orphanReason: source.orphanReason } : {}),
    alsoAdvances: source.alsoAdvances,
    category: source.category,
    priorityNumeric: source.priorityNumeric,
    status: 'planned',
    carriedFromWeek: source.isoWeek,
    carryDepth: source.carryDepth + 1,
  };
}

/**
 * Compute the carry-forward of a reconciled week's commits into `nextWeek`'s
 * DRAFT (KTD3). Pure — the caller (`completeReconcile`, U4) writes the result
 * inside the reconcile transaction.
 *
 * Only INCOMPLETE commits carry (`status ∈ {planned, partial}`): `done` is
 * finished and `dropped` is a deliberate kill, so neither carries. Each carried
 * line is cloned with a fresh id, its week advanced, its outcome reset, and its
 * `carryDepth` incremented; the matching source ids come back so the caller can
 * stamp `carriedToWeek` on them. A clone that reaches `carryDepth >= 3` surfaces
 * in `deepCarryNudge` (a decompose/kill signal, never a block).
 */
export function carryForwardCommits(
  commits: readonly WeeklyCommit[],
  nextWeek: string,
): CarryForwardResult {
  const toCarry = commits.filter((c) => CARRYING_STATUSES.has(c.status));
  const clones = toCarry.map((source) => carryClone(source, nextWeek));
  return {
    clones,
    sourceIds: toCarry.map((c) => c.id),
    deepCarryNudge: clones
      .filter((c) => c.carryDepth >= CARRY_DEPTH_NUDGE_THRESHOLD)
      .map((c) => c.id),
  };
}
