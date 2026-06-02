import type { ObjectiveNode, Ticket, WeeklyItem } from '@harness/shared';

/**
 * Objective roll-up projection (U10).
 *
 * RCDO nodes form a tree (Rally Cry -> Defining Objective -> Outcome ->
 * Supporting Outcome) linked by `parentId`. Completion is computed bottom-up:
 *
 *  - A LEAF node's % comes from the work linked to it:
 *      · tickets whose `objectiveId` is the node -> fraction `done`
 *      · weekly-update items whose `objectiveId` is the node -> their
 *        `completionPct` (averaged in with the tickets)
 *    A leaf with no linked work is 0%.
 *  - An INTERNAL node's % is the mean of its children's rolled-up %.
 *
 * `recomputeRollup` is pure: it takes the full node set plus the linked work and
 * returns the same nodes with `pct` filled in. The Streams trigger gathers the
 * inputs from the Repo and persists the result, so the read path (`GET
 * /objectives`) just serves the cached tree.
 */

export interface RollupInput {
  nodes: ObjectiveNode[];
  /** Tickets across the org's projects, each optionally linked via objectiveId. */
  tickets: Ticket[];
  /** Weekly-update items across the org, each optionally linked via objectiveId. */
  weeklyItems: WeeklyItem[];
}

/** Completion (0..100) for the work linked directly to a single leaf node. */
export function leafPct(nodeId: string, tickets: Ticket[], weeklyItems: WeeklyItem[]): number {
  const linkedTickets = tickets.filter((t) => t.objectiveId === nodeId);
  const linkedWeekly = weeklyItems.filter((w) => w.objectiveId === nodeId);

  const samples: number[] = [];
  for (const t of linkedTickets) samples.push(t.status === 'done' ? 100 : 0);
  for (const w of linkedWeekly) samples.push(w.completionPct ?? 0);

  if (samples.length === 0) return 0;
  return samples.reduce((a, b) => a + b, 0) / samples.length;
}

/**
 * Recompute every node's cached `pct` bottom-up. Returns a new array of nodes
 * (inputs are not mutated). Orphaned links (an objectiveId with no node) are
 * simply ignored — they contribute to no node and are never fatal.
 */
export function recomputeRollup(input: RollupInput): ObjectiveNode[] {
  const { nodes, tickets, weeklyItems } = input;

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
      pct = leafPct(node.id, tickets, weeklyItems);
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
