import type { ObjectiveNode } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { recomputeRollup, type ProjectProgress } from './rollup.js';

/**
 * Repo-backed orchestration around the pure `recomputeRollup` (U10, re-pointed
 * in U3).
 *
 * The DynamoDB-Streams trigger fires when a project's stored progress changes.
 * It gathers the org's objective nodes and the progress of the given projects
 * (each owning zero or more Supporting Outcomes), recomputes every node's cached
 * `%` bottom-up, and persists the nodes whose `%` actually changed (so a roll-up
 * write does not itself fan out needless Stream events).
 */

/** A stored project may carry the Supporting Outcomes it owns (from framing). */
interface ProjectWithFraming {
  progressPct?: number;
  supportingOutcomeIds?: string[];
}

/** Gather each project's stored progress + the Supporting Outcomes it owns. */
async function gatherProjectProgress(
  repo: Repo,
  projectIds: string[],
): Promise<ProjectProgress[]> {
  const projects = await Promise.all(projectIds.map((pid) => repo.getProject(pid)));
  return projects
    .filter((p): p is NonNullable<typeof p> => Boolean(p))
    .map((p) => {
      const framing = p as ProjectWithFraming;
      return {
        progressPct: framing.progressPct,
        supportingOutcomeIds: framing.supportingOutcomeIds ?? [],
      };
    });
}

export async function recomputeOrgRollup(
  repo: Repo,
  org: string,
  projectIds: string[],
): Promise<ObjectiveNode[]> {
  const nodes = await repo.listObjectives(org);
  const projects = await gatherProjectProgress(repo, projectIds);

  const updated = recomputeRollup({ nodes, projects });

  // Persist only the nodes whose cached % changed. A node that has never had a
  // cached value (pct undefined) is always written so the first roll-up
  // establishes a concrete 0 rather than leaving it absent.
  const prevPct = new Map(nodes.map((n) => [n.id, n.pct]));
  await Promise.all(
    updated.filter((n) => prevPct.get(n.id) !== n.pct).map((n) => repo.putObjective(n)),
  );

  return updated;
}
