import type { ObjectiveNode } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { recomputeRollup } from './rollup.js';
import { listObjectives, putObjective } from '../db/pg/objectivesRepo.js';
import { listReconciledCommitCredits } from '../db/pg/weeklyRepo.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * Repo-backed orchestration around the pure `recomputeRollup` (U10, re-pointed
 * in U3; objectives moved to Postgres in U16; the roll-up SOURCE rewritten in U6).
 *
 * The objective tree and the weekly relations both live in Postgres (read/written
 * via `db`). The roll-up's SINGLE SOURCE OF TRUTH is now the reconciled weekly
 * commits (KTD5): `leafPct(soId)` is the mean reconciled completion of the commits
 * hard-linked to that SO. The GitHub `progressPct` feed is REMOVED (U6) — there is
 * no Dynamo project read here any more, and an SO with no reconciled commits reads
 * 0% (intended).
 *
 * The `repo` parameter is retained for the call-site contract (the Streams trigger
 * and the transition path both pass it) but is no longer read for roll-up inputs;
 * everything the roll-up needs now lives in Postgres.
 */

export async function recomputeOrgRollup(
  db: PgDb,
  _repo: Repo,
  org: string,
  projectIds: string[],
): Promise<ObjectiveNode[]> {
  const nodes = await listObjectives(db, org);
  const commits = await listReconciledCommitCredits(db, projectIds);

  const updated = recomputeRollup({ nodes, commits });

  // Persist (to Postgres) only the nodes whose cached % changed. A node that has
  // never had a cached value (pct undefined) is always written so the first
  // roll-up establishes a concrete 0 rather than leaving it absent.
  const prevPct = new Map(nodes.map((n) => [n.id, n.pct]));
  await Promise.all(
    updated.filter((n) => prevPct.get(n.id) !== n.pct).map((n) => putObjective(db, n)),
  );

  return updated;
}
