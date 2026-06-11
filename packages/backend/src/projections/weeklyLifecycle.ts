import type { CommitCategory, WeeklyStatus } from '@harness/shared';

/**
 * Weekly-commit lifecycle pure logic (weekly-commit-lifecycle, U1).
 *
 * The lifecycle state machine (KTD2) lives on the plan: each transition is its
 * own server endpoint, and `canTransition` is the SINGLE SOURCE OF TRUTH for
 * legality (an illegal transition is a 409). This module also hosts the DERIVED
 * chess-layer helpers (`deriveCategory`, `wsjfPriority` — KTD4) and, later,
 * carry-forward (U5). The chess-layer helpers are STUBS here (U1) and are filled
 * in U6, where they read RCDO position / weight / behind-ness; their signatures
 * are pinned now so the REST + repo layers can wire to them.
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
 * and the linked SO's RCDO position. STUB shape for U1 — the real projection is
 * filled in U6.
 */
export interface DeriveCategoryInput {
  /** True when the commit carries no SO link (it is an orphan — KTD10). */
  orphan?: boolean;
  /** The orphan reason, which IS the category for an orphan commit (KTD10). */
  orphanReason?: CommitCategory;
}

/**
 * Derive a commit's chess-layer `category` from its plan unit + linked SO
 * position (KTD4). STUB (U1): an orphan commit's reason is its category; a linked
 * commit defaults to `Delivery` until the real RCDO-position projection lands in
 * U6.
 */
export function deriveCategory(input: DeriveCategoryInput): CommitCategory {
  if (input.orphan && input.orphanReason) return input.orphanReason;
  return 'Delivery';
}

/**
 * Inputs to the DERIVED WSJF priority (KTD4): Cost of Delay (inherited from the
 * commit's RCDO position — its branch weight × how far behind that branch is) over
 * Job Size (the plan unit's estimate). STUB shape for U1 — the real computation is
 * filled in U6.
 */
export interface WsjfPriorityInput {
  /** Cost of Delay numerator (branch weight × behind-ness). */
  costOfDelay?: number;
  /** Job Size denominator (plan-unit estimate). */
  jobSize?: number;
}

/**
 * Derive a commit's WSJF-from-the-tree priority (KTD4). STUB (U1): CoD / JobSize,
 * defaulting to a neutral 0 until U6 supplies real RCDO weight / behind-ness.
 * Guards a zero/absent Job Size so the list-sort key is always finite.
 */
export function wsjfPriority(input: WsjfPriorityInput): number {
  const cod = input.costOfDelay ?? 0;
  const size = input.jobSize ?? 0;
  if (size <= 0) return cod;
  return cod / size;
}
