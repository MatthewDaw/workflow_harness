import type { GitCommit, ObjectiveNode, Ticket, WeeklyItem } from '@harness/shared';

/**
 * Weekly alignment + completion (U28, feeds U10 roll-up).
 *
 * Pure functions that turn a validated weekly update into the alignment +
 * completion numbers the Weekly screen renders and the objective roll-up
 * consumes. No I/O — the agent (agent.ts) gathers the inputs and calls these.
 */

/** Per-objective alignment for a set of weekly items. */
export interface AlignmentEntry {
  objectiveId: string;
  /** Number of items in this set linked to the objective. */
  itemCount: number;
  /** Mean completion of those items (0..100). */
  completionPct: number;
}

export interface AlignmentSummary {
  /** Items linked to some owned Supporting Outcome, grouped by objective. */
  aligned: AlignmentEntry[];
  /** Items linked to no objective (the push-back set). */
  unaligned: WeeklyItem[];
  /** Fraction (0..100) of items that map to an owned outcome. */
  alignedPct: number;
}

/**
 * Summarize how a set of weekly items aligns to objectives. An item with an
 * `objectiveId` that is in `ownedOutcomeIds` is aligned; everything else is
 * unaligned (and would have been challenged at validation time).
 */
export function summarizeAlignment(
  items: WeeklyItem[],
  ownedOutcomeIds: readonly string[],
): AlignmentSummary {
  const owned = new Set(ownedOutcomeIds);
  const aligned: WeeklyItem[] = [];
  const unaligned: WeeklyItem[] = [];
  for (const item of items) {
    if (item.objectiveId && owned.has(item.objectiveId)) aligned.push(item);
    else unaligned.push(item);
  }

  const byObjective = new Map<string, WeeklyItem[]>();
  for (const item of aligned) {
    const id = item.objectiveId as string;
    const arr = byObjective.get(id) ?? [];
    arr.push(item);
    byObjective.set(id, arr);
  }

  const entries: AlignmentEntry[] = [...byObjective.entries()].map(([objectiveId, group]) => ({
    objectiveId,
    itemCount: group.length,
    completionPct: mean(group.map((i) => i.completionPct ?? 0)),
  }));
  entries.sort((a, b) => a.objectiveId.localeCompare(b.objectiveId));

  const total = items.length;
  const alignedPct = total === 0 ? 0 : round2((aligned.length / total) * 100);

  return { aligned: entries, unaligned, alignedPct };
}

/**
 * Compute the completion delta a published week contributes per objective: the
 * difference between the week's mean item completion and the objective's prior
 * cached `pct`. These deltas are the U10 roll-up inputs — applying them re-bases
 * the objective toward the week's reported progress.
 */
export interface AlignmentDelta {
  objectiveId: string;
  priorPct: number;
  reportedPct: number;
  deltaPct: number;
}

export function computeDeltas(
  alignment: AlignmentSummary,
  nodes: ObjectiveNode[],
): AlignmentDelta[] {
  const priorById = new Map(nodes.map((n) => [n.id, n.pct ?? 0]));
  return alignment.aligned.map((entry) => {
    const prior = priorById.get(entry.objectiveId) ?? 0;
    return {
      objectiveId: entry.objectiveId,
      priorPct: round2(prior),
      reportedPct: round2(entry.completionPct),
      deltaPct: round2(entry.completionPct - prior),
    };
  });
}

/**
 * Attribute a week's git commits to objectives via their ticket links. Each
 * commit's ticket ids are resolved to tickets; each ticket's `objectiveId`
 * groups the commit. Commits whose tickets resolve to no objective (or no
 * ticket) are returned under `unattributed` rather than guessed (plan R6).
 */
export interface DoneAttribution {
  /** objectiveId -> commits advancing it. */
  byObjective: Record<string, GitCommit[]>;
  /** Commits with no resolvable objective. */
  unattributed: GitCommit[];
}

export function attributeDone(commits: GitCommit[], tickets: Ticket[]): DoneAttribution {
  const objectiveByTicket = new Map<string, string | undefined>();
  for (const t of tickets) objectiveByTicket.set(t.id, t.objectiveId);

  const byObjective: Record<string, GitCommit[]> = {};
  const unattributed: GitCommit[] = [];

  for (const commit of commits) {
    const objectiveIds = new Set<string>();
    for (const ticketId of commit.ticketIds) {
      const objId = objectiveByTicket.get(ticketId);
      if (objId) objectiveIds.add(objId);
    }
    if (objectiveIds.size === 0) {
      unattributed.push(commit);
      continue;
    }
    for (const objId of objectiveIds) {
      (byObjective[objId] ??= []).push(commit);
    }
  }

  return { byObjective, unattributed };
}

function mean(xs: number[]): number {
  if (xs.length === 0) return 0;
  return round2(xs.reduce((a, b) => a + b, 0) / xs.length);
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
