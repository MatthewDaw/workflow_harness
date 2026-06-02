import type { ObjectiveNode, WeeklyItem } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { recomputeRollup } from './rollup.js';

/**
 * Repo-backed orchestration around the pure `recomputeRollup` (U10).
 *
 * The DynamoDB-Streams trigger fires when a linked ticket or weekly update
 * changes. It gathers the org's objective nodes and all the linked work across
 * the given projects, recomputes every node's cached `%` bottom-up, and persists
 * the nodes whose `%` actually changed (so a roll-up write does not itself fan
 * out needless Stream events).
 */

/** Gather all weekly-update items across a set of projects. */
async function gatherWeeklyItems(repo: Repo, projectIds: string[]): Promise<WeeklyItem[]> {
  const perProject = await Promise.all(projectIds.map((pid) => repo.listWeekly(pid)));
  // Only published (validated) weeks feed the roll-up; drafts must not move %.
  return perProject.flat().flatMap((w) => (w.validated ? [...w.done, ...w.plan] : []));
}

export async function recomputeOrgRollup(
  repo: Repo,
  org: string,
  projectIds: string[],
): Promise<ObjectiveNode[]> {
  const nodes = await repo.listObjectives(org);
  const ticketsPerProject = await Promise.all(projectIds.map((pid) => repo.listTickets(pid)));
  const tickets = ticketsPerProject.flat();
  const weeklyItems = await gatherWeeklyItems(repo, projectIds);

  const updated = recomputeRollup({ nodes, tickets, weeklyItems });

  // Persist only the nodes whose cached % changed. A node that has never had a
  // cached value (pct undefined) is always written so the first roll-up
  // establishes a concrete 0 rather than leaving it absent.
  const prevPct = new Map(nodes.map((n) => [n.id, n.pct]));
  await Promise.all(
    updated.filter((n) => prevPct.get(n.id) !== n.pct).map((n) => repo.putObjective(n)),
  );

  return updated;
}
