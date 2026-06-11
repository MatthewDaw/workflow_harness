import type { ObjectiveNode } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { recomputeRollup, type ProjectProgress } from './rollup.js';
import { listObjectives, putObjective } from '../db/pg/objectivesRepo.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * Repo-backed orchestration around the pure `recomputeRollup` (U10, re-pointed
 * in U3; objectives moved to Postgres in U16).
 *
 * This is the **cross-store seam** of the persistence split (KTD7): the objective
 * tree now lives in Postgres (read/written via `db`), while a project's stored
 * `progressPct` + owned Supporting Outcomes still live in DynamoDB (read via
 * `repo`). The DynamoDB-Streams trigger (a project's progress changed) and the
 * weekly publish path both call this; it gathers the org's objective nodes from
 * Postgres and the projects' progress from Dynamo, recomputes every node's cached
 * `%` bottom-up, and persists (to Postgres) the nodes whose `%` actually changed.
 *
 * NOTE: the `progressPct` input is removed in Phase 2 (U6) in favour of reconciled
 * weekly commits as the single source of truth; until then the gather still reads
 * it from the Dynamo project record.
 */

/** A stored project may carry the Supporting Outcomes it owns (from framing). */
interface ProjectWithFraming {
  progressPct?: number;
  supportingOutcomeIds?: string[];
}

/** Gather each project's stored progress + the Supporting Outcomes it owns (from Dynamo). */
async function gatherProjectProgress(repo: Repo, projectIds: string[]): Promise<ProjectProgress[]> {
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
  db: PgDb,
  repo: Repo,
  org: string,
  projectIds: string[],
): Promise<ObjectiveNode[]> {
  const nodes = await listObjectives(db, org);
  const projects = await gatherProjectProgress(repo, projectIds);

  const updated = recomputeRollup({ nodes, projects });

  // Persist (to Postgres) only the nodes whose cached % changed. A node that has
  // never had a cached value (pct undefined) is always written so the first
  // roll-up establishes a concrete 0 rather than leaving it absent.
  const prevPct = new Map(nodes.map((n) => [n.id, n.pct]));
  await Promise.all(
    updated.filter((n) => prevPct.get(n.id) !== n.pct).map((n) => putObjective(db, n)),
  );

  return updated;
}
