import type { CommitOutcomeStatus, ObjectiveNode } from '@harness/shared';

/**
 * Objective roll-up projection (U10, re-pointed off tickets in U3; the roll-up
 * SOURCE rewritten in U6).
 *
 * RCDO nodes form a tree (Rally Cry -> Defining Objective -> Outcome ->
 * Supporting Outcome) linked by `parentId`. Completion is computed bottom-up:
 *
 *  - A LEAF node's % is the mean reconciled completion of the weekly COMMITS
 *    hard-linked to it (KTD5): each commit contributes `done = 1.0`,
 *    `partial = 0.5`, `planned`/`dropped = 0.0`; the leaf % is `100 ×` the mean
 *    over its commits. A leaf with no reconciled commits is **0%** — there is no
 *    GitHub `progressPct` fallback any more (removed in U6); an SO reads 0% until
 *    its first reconciliation (intended).
 *  - An INTERNAL node's % is the mean of its children's rolled-up % (unchanged).
 *
 * Only a commit's PRIMARY `supportingOutcomeId` earns leaf credit (KTD9): orphan
 * commits (a typed `orphanReason`, no SO) and the informational `alsoAdvances`
 * secondaries contribute NOTHING to any leaf.
 *
 * `recomputeRollup` is pure: it takes the full node set plus the reconciled
 * commit credits and returns the same nodes with `pct` filled in. The transition
 * path (`/reconcile/complete`) and the Streams trigger gather the inputs from the
 * Repo and persist the result, so the read path (`GET /objectives`) just serves
 * the cached tree.
 */

/**
 * A single reconciled commit's leaf-credit input: the PRIMARY Supporting Outcome
 * it advances (KTD9) and its reconciled outcome `status`. Orphan commits and
 * `alsoAdvances` secondaries are never represented here — only a primary SO link
 * earns credit, so the gather (`rollupRepo`) filters them out before this point.
 */
export interface CommitCredit {
  /** The PRIMARY Supporting Outcome this commit advances (KTD9). */
  supportingOutcomeId: string;
  /** The reconciled outcome — drives the credit weight (KTD5). */
  status: CommitOutcomeStatus;
}

export interface RollupInput {
  nodes: ObjectiveNode[];
  /** The reconciled commits whose primary SO links earn leaf credit (KTD5/KTD9). */
  commits: CommitCredit[];
}

/** The roll-up credit weight a single reconciled commit contributes (KTD5). */
function creditFor(status: CommitOutcomeStatus): number {
  if (status === 'done') return 1;
  if (status === 'partial') return 0.5;
  return 0; // planned / dropped earn nothing
}

/**
 * Completion (0..100) for a single leaf node — the mean reconciled completion of
 * the weekly commits hard-linked to it (KTD5). `done = 1.0`, `partial = 0.5`,
 * `planned`/`dropped = 0.0`; the leaf % is `100 ×` the mean over its commits.
 * A leaf with no reconciled commits is **0%** (no `progressPct` fallback — the
 * removal U6 regression-guards). Only the PRIMARY SO link counts (KTD9).
 */
export function leafPct(nodeId: string, commits: CommitCredit[]): number {
  const own = commits.filter((c) => c.supportingOutcomeId === nodeId);
  if (own.length === 0) return 0;
  const total = own.reduce((sum, c) => sum + creditFor(c.status), 0);
  return (total / own.length) * 100;
}

/**
 * Recompute every node's cached `pct` bottom-up. Returns a new array of nodes
 * (inputs are not mutated). Orphaned links (an objectiveId with no node) are
 * simply ignored — they contribute to no node and are never fatal.
 */
export function recomputeRollup(input: RollupInput): ObjectiveNode[] {
  const { nodes, commits } = input;

  const childrenOf = new Map<string, ObjectiveNode[]>();
  for (const n of nodes) {
    if (n.parentId) {
      const arr = childrenOf.get(n.parentId) ?? [];
      arr.push(n);
      childrenOf.set(n.parentId, arr);
    }
  }

  const cache = new Map<string, number>();
  const visiting = new Set<string>();

  const pctFor = (node: ObjectiveNode): number => {
    const cached = cache.get(node.id);
    if (cached !== undefined) return cached;
    // Cycle guard: treat a node currently being computed as 0 to break the loop.
    if (visiting.has(node.id)) return 0;
    visiting.add(node.id);

    const children = childrenOf.get(node.id) ?? [];
    let pct: number;
    if (children.length === 0) {
      pct = leafPct(node.id, commits);
    } else {
      pct = children.reduce((sum, c) => sum + pctFor(c), 0) / children.length;
    }

    visiting.delete(node.id);
    const rounded = Math.round(pct * 100) / 100;
    cache.set(node.id, rounded);
    return rounded;
  };

  return nodes.map((n) => ({ ...n, pct: pctFor(n) }));
}

/**
 * Assemble the node list into a nested tree for `GET /objectives`. Each node is
 * returned with its `children` array; roots are nodes with no (resolvable)
 * parent.
 */
export interface ObjectiveTreeNode extends ObjectiveNode {
  children: ObjectiveTreeNode[];
}

export function buildTree(nodes: ObjectiveNode[]): ObjectiveTreeNode[] {
  const byId = new Map<string, ObjectiveTreeNode>();
  for (const n of nodes) byId.set(n.id, { ...n, children: [] });

  const roots: ObjectiveTreeNode[] = [];
  for (const node of byId.values()) {
    const parent = node.parentId ? byId.get(node.parentId) : undefined;
    if (parent) parent.children.push(node);
    else roots.push(node);
  }
  return roots;
}
