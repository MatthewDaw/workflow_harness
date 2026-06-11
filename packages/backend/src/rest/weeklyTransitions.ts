import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { WeeklyPlan, WeeklyStatus } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { getPlan, listWeekCommits, upsertPlan } from '../db/pg/weeklyRepo.js';
import { weeklyCommits, weeklyPlans } from '../db/pg/schema.js';
import { canTransition, carryForwardCommits, nextIsoWeek } from '../projections/weeklyLifecycle.js';
import { recomputeOrgRollup } from '../projections/rollupRepo.js';
import { recordCalibration } from '../projections/calibration.js';
// Re-export the now-pure `nextIsoWeek` (moved to `weeklyLifecycle` in U5) so the
// U4 transitions suite, which imports it from here, keeps resolving it.
export { nextIsoWeek } from '../projections/weeklyLifecycle.js';
import { conflict, defaultDb, defaultRepo, json, ok } from './runtime.js';
import { ownedProject } from './ownership.js';
import type { PgDb } from '../db/pg/migrate.js';
import { and, eq, inArray } from 'drizzle-orm';

/**
 * REST: weekly lifecycle transitions (U4). Each transition is its own endpoint;
 * `canTransition` (KTD2) is the SINGLE SOURCE OF TRUTH for legality — an illegal
 * transition is a 409. The side effects run inside ONE Postgres transaction so a
 * reconcile-complete (which stamps sources and clones carry-forward items) is
 * atomic (KTD3).
 *
 *   POST /projects/:pid/weekly/:week/lock              — DRAFT       -> LOCKED
 *   POST /projects/:pid/weekly/:week/reconcile/start   — LOCKED      -> RECONCILING
 *   POST /projects/:pid/weekly/:week/reconcile/complete — RECONCILING -> RECONCILED
 *
 * `lock` (the new "publish", replacing the removed `validated`/`POST /publish`)
 * re-checks the SO-or-orphan invariant server-side and refuses an empty week,
 * returning a structured `blockers[]` (commitId + reason) so the UI/agent can
 * self-correct. `reconcile/complete` refuses while any commit is still `planned`,
 * then carries the incomplete items into next week's DRAFT.
 *
 * NOTE: the transactional side effects are written against the `PgDb` because the
 * Neon HTTP driver does not support interactive transactions (pglite does, so the
 * suite exercises the real transaction). The prod WebSocket-driver swap is
 * deferred per the plan.
 */

export interface WeeklyTransitionsDeps {
  repo: Repo;
  /** Postgres client — the weekly relations live here (KTD7). */
  db: PgDb;
}

/** A 409 with the structured `blockers[]` the UI/agent reads to self-correct. */
interface Blocker {
  commitId?: string;
  reason: string;
}

const blocked = (message: string, blockers: Blocker[]): APIGatewayProxyResultV2 =>
  json(409, { error: message, blockers });

/** Resolve the owned project + week path param, or the error response to return. */
async function resolveWeek(
  event: APIGatewayProxyEventV2,
  deps: WeeklyTransitionsDeps,
): Promise<
  | { projectId: string; week: string; plan: WeeklyPlan; org: string; ownerUserId: string }
  | { error: APIGatewayProxyResultV2 }
> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved;
  const week = event.pathParameters?.week;
  if (!week) return { error: json(400, { error: 'missing project or week' }) };
  const plan = await getPlan(deps.db, resolved.project.id, week);
  if (!plan) return { error: json(404, { error: 'not found' }) };
  return {
    projectId: resolved.project.id,
    week,
    plan,
    org: resolved.principal.org,
    ownerUserId: resolved.principal.userId,
  };
}

/** Whether the transition `from -> to` is legal; a 409 otherwise (KTD2). */
function assertTransition(
  from: WeeklyStatus,
  to: WeeklyStatus,
): APIGatewayProxyResultV2 | undefined {
  if (!canTransition(from, to)) {
    return conflict(`illegal transition ${from} -> ${to}`);
  }
  return undefined;
}

/**
 * DRAFT -> LOCKED (the new "publish"). The guard re-checks server-side that the
 * week has >= 1 commit AND every commit carries an SO-or-orphan; a violation is a
 * 409 with `blockers[]`. The final WSJF `priorityNumeric` is already derived on
 * each commit (U3/U6); LOCK stamps `lockedAt`.
 */
export async function lockWeek(
  event: APIGatewayProxyEventV2,
  deps: WeeklyTransitionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await resolveWeek(event, deps);
  if ('error' in resolved) return resolved.error;
  const { projectId, week, plan } = resolved;

  const illegal = assertTransition(plan.status, 'LOCKED');
  if (illegal) return illegal;

  const commits = await listWeekCommits(deps.db, projectId, week);
  const blockers: Blocker[] = [];
  if (commits.length === 0) {
    blockers.push({ reason: 'a week needs at least one commit to lock' });
  }
  for (const c of commits) {
    if (!c.supportingOutcomeId && !c.orphanReason) {
      blockers.push({
        commitId: c.id,
        reason: 'a commit must have a supportingOutcomeId or an orphanReason',
      });
    }
  }
  if (blockers.length > 0) return blocked('cannot lock the week', blockers);

  const locked: WeeklyPlan = { ...plan, status: 'LOCKED', lockedAt: Date.now() };
  await upsertPlan(deps.db, locked);
  return ok({ plan: locked });
}

/** LOCKED -> RECONCILING. Opens per-commit actual recording (U3 update path). */
export async function startReconcile(
  event: APIGatewayProxyEventV2,
  deps: WeeklyTransitionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await resolveWeek(event, deps);
  if ('error' in resolved) return resolved.error;
  const { plan } = resolved;

  const illegal = assertTransition(plan.status, 'RECONCILING');
  if (illegal) return illegal;

  const next: WeeklyPlan = { ...plan, status: 'RECONCILING' };
  await upsertPlan(deps.db, next);
  return ok({ plan: next });
}

/**
 * RECONCILING -> RECONCILED. Refuses while any commit is still `planned` (a 409
 * listing them). On success, in ONE transaction: stamps `RECONCILED` +
 * `reconciledAt`, clones every `planned`/`partial` commit into next week's DRAFT
 * (created if absent) with `carriedFromWeek`/`carryDepth+1`, stamps
 * `carriedToWeek` on the sources (KTD3), and folds the week's terminal commits
 * into the owner's reconciliation calibration (U19). A clone reaching
 * `carryDepth >= 3` surfaces a decompose/kill nudge (not a block). The
 * single-source roll-up recompute (U6) runs after the transaction commits.
 */
export async function completeReconcile(
  event: APIGatewayProxyEventV2,
  deps: WeeklyTransitionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await resolveWeek(event, deps);
  if ('error' in resolved) return resolved.error;
  const { projectId, week, plan, org, ownerUserId } = resolved;

  const illegal = assertTransition(plan.status, 'RECONCILED');
  if (illegal) return illegal;

  const commits = await listWeekCommits(deps.db, projectId, week);
  const stillPlanned = commits.filter((c) => c.status === 'planned');
  if (stillPlanned.length > 0) {
    return blocked(
      'cannot complete reconciliation while commits are still planned',
      stillPlanned.map((c) => ({ commitId: c.id, reason: 'commit is still planned' })),
    );
  }

  const nextWeek = nextIsoWeek(week);
  // Carry-forward seeds next week's DRAFT (KTD3) — pure computation in U5's
  // `carryForwardCommits`: only incomplete items (`planned`/`partial`) clone,
  // each with `carryDepth + 1`; `done`/`dropped` never carry. (At this point the
  // still-`planned` guard above has already passed, so in practice only `partial`
  // commits remain to carry; the pure helper stays general.)
  const { clones, sourceIds, deepCarryNudge } = carryForwardCommits(commits, nextWeek);
  const reconciledAt = Date.now();

  // ONE transaction: stamp the source plan + sources, ensure next week's DRAFT,
  // insert the clones, and record the owner's calibration. pglite runs this as a
  // real transaction; the Neon HTTP driver swap to the WebSocket driver is
  // deferred per the plan.
  await deps.db.transaction(async (tx) => {
    // Stamp the source week RECONCILED.
    await tx
      .update(weeklyPlans)
      .set({ status: 'RECONCILED', reconciledAt })
      .where(and(eq(weeklyPlans.projectId, projectId), eq(weeklyPlans.isoWeek, week)));

    if (clones.length > 0) {
      // Ensure next week's DRAFT plan exists (created if absent), never demoting
      // an already-advanced next week.
      const existingNext = await tx
        .select()
        .from(weeklyPlans)
        .where(and(eq(weeklyPlans.projectId, projectId), eq(weeklyPlans.isoWeek, nextWeek)))
        .limit(1);
      if (existingNext.length === 0) {
        await tx
          .insert(weeklyPlans)
          .values({ projectId, isoWeek: nextWeek, status: 'DRAFT', posture: 'focus' });
      }

      // Stamp each source with the week it carried TO, then insert the clones.
      await tx
        .update(weeklyCommits)
        .set({ carriedToWeek: nextWeek })
        .where(inArray(weeklyCommits.id, sourceIds));
      await tx.insert(weeklyCommits).values(
        clones.map((c) => ({
          id: c.id,
          projectId: c.projectId,
          isoWeek: c.isoWeek,
          title: c.title,
          supportingOutcomeId: c.supportingOutcomeId ?? null,
          orphanReason: c.orphanReason ?? null,
          alsoAdvances: c.alsoAdvances,
          category: c.category,
          priorityNumeric: c.priorityNumeric,
          status: c.status,
          actualOutcome: c.actualOutcome ?? null,
          carriedFromWeek: c.carriedFromWeek ?? null,
          carriedToWeek: c.carriedToWeek ?? null,
          carryDepth: c.carryDepth,
        })),
      );
    }

    // Reconciliation calibration (U19): accumulate this week's terminal commits
    // into the owner's running locked-vs-done rate so the plan-anchored agent can
    // right-size next week's proposal. Advisory — it never blocks. Runs in the
    // same transaction as the reconcile (`tx` is the same PgDb shape).
    await recordCalibration(tx as unknown as PgDb, ownerUserId, commits, reconciledAt);
  });

  // Single-source roll-up recompute (KTD5/U6): now that this week is RECONCILED,
  // its reconciled commits are the SOURCE of every linked SO's completion. Run it
  // AFTER the transaction commits so the just-stamped commits are visible to the
  // gather (the Neon HTTP driver has no interactive transaction anyway; the
  // recompute is idempotent, so a post-commit run is safe). An SO with no
  // reconciled data stays 0% — the `progressPct` feed is gone.
  await recomputeOrgRollup(deps.db, deps.repo, org, [projectId]);

  const nextPlan: WeeklyPlan = { ...plan, status: 'RECONCILED', reconciledAt };
  return ok({
    plan: nextPlan,
    carriedTo: nextWeek,
    carriedCount: clones.length,
    ...(deepCarryNudge.length > 0 ? { deepCarryNudge } : {}),
  });
}

/**
 * Dispatch a transition by the trailing path segment. The route layer pins one
 * path per transition (see `infra/lib/api-stack.ts`); this mirror keeps the
 * handler self-contained for tests.
 */
export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: WeeklyTransitionsDeps = { repo: defaultRepo(), db: defaultDb() };
  const path = event.requestContext.http.path;
  if (path.endsWith('/reconcile/complete')) return completeReconcile(event, deps);
  if (path.endsWith('/reconcile/start')) return startReconcile(event, deps);
  if (path.endsWith('/lock')) return lockWeek(event, deps);
  return json(404, { error: 'not found' });
}
