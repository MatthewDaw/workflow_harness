import { randomUUID } from 'node:crypto';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { weeklyCommitSchema, type WeeklyCommit, type WeeklyPlan } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  createCommit as repoCreateCommit,
  deleteCommit as repoDeleteCommit,
  getCommit as repoGetCommit,
  getPlan,
  listPlansForProject,
  listWeekCommits,
  updateCommit as repoUpdateCommit,
  upsertPlan,
} from '../db/pg/weeklyRepo.js';
import { getObjective } from '../db/pg/objectivesRepo.js';
import { deriveCategory, wsjfPriority } from '../projections/weeklyLifecycle.js';
import { getCalibration } from '../projections/calibration.js';
import {
  badRequest,
  conflict,
  created,
  defaultDb,
  defaultRepo,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
} from './runtime.js';
import { ownedProject } from './ownership.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * REST: weekly commit lifecycle (U3) — itemized commits, Postgres-backed,
 * scoped to the project owner. Replaces the legacy prose `WeeklyUpdate`
 * store/serve (`putWeekly`/`publishWeekly` are removed — LOCK is the new
 * "publish", U4).
 *
 *   GET    /projects/:pid/weekly                       — every week (plan + commits)
 *   GET    /projects/:pid/weekly/:week                 — one week (plan + commits)
 *   POST   /projects/:pid/weekly/:week/commits         — add a commit (DRAFT only)
 *   PUT    /projects/:pid/weekly/:week/commits/:cid    — edit a commit (DRAFT only)
 *   DELETE /projects/:pid/weekly/:week/commits/:cid    — remove a commit (DRAFT only)
 *
 * The "chess layer" (`category` + `priorityNumeric`) is DERIVED server-side
 * (KTD4) — client-sent values are ignored in favour of the derived ones. The
 * SO-or-orphan invariant (KTD10) is enforced three times over: the Zod
 * refinement, the explicit dangling-SO check here, and the DB CHECK. Planned
 * fields are frozen at LOCK: create/update/delete are rejected with a 409 once
 * the week leaves DRAFT (the reconciliation actual-fields path lives in U4).
 *
 * Lifecycle transitions (lock / reconcile-start / reconcile-complete) live in
 * `weeklyTransitions.ts` (U4).
 */

export interface WeeklyDeps {
  repo: Repo;
  /** Postgres client — the weekly relations + objectives FK target live here (KTD7). */
  db: PgDb;
}

/** A week shaped for the client: the plan plus its (priority-sorted) commits. */
interface WeekView {
  plan: WeeklyPlan;
  commits: WeeklyCommit[];
}

/** The DRAFT plan a commit write attaches to, auto-created on first write. */
async function ensureDraftPlan(db: PgDb, projectId: string, isoWeek: string): Promise<WeeklyPlan> {
  const existing = await getPlan(db, projectId, isoWeek);
  if (existing) return existing;
  const plan: WeeklyPlan = { projectId, isoWeek, status: 'DRAFT', posture: 'focus' };
  await upsertPlan(db, plan);
  return plan;
}

/** Derive the chess-layer fields the client is not trusted to author (KTD4). */
function deriveChessFields(input: {
  orphanReason?: WeeklyCommit['orphanReason'];
}): { category: WeeklyCommit['category']; priorityNumeric: number } {
  const orphan = input.orphanReason !== undefined;
  const category = deriveCategory({
    ...(orphan ? { orphan: true, orphanReason: input.orphanReason } : {}),
  });
  // U6 fills the real RCDO-position/weight/behind-ness inputs; the U1 stub yields
  // a neutral leverage for now so the list-sort key is finite.
  const priorityNumeric = wsjfPriority({});
  return { category, priorityNumeric };
}

/**
 * Every week for the project (plan + its commits), oldest ISO-week first. The
 * commits are priority-sorted by the repo (KTD4). The response also carries the
 * caller's reconciliation `calibration` (U19) — the plan-anchored agent reads it
 * in its propose step to right-size next week's set ("you complete ~60% — here are
 * the 6 highest-leverage units"). It is absent for a first-ever week (no history).
 */
export async function listWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const plans = await listPlansForProject(deps.db, resolved.project.id);
  const weeks: WeekView[] = [];
  for (const plan of plans) {
    const commits = await listWeekCommits(deps.db, plan.projectId, plan.isoWeek);
    weeks.push({ plan, commits });
  }
  const calibration = await getCalibration(deps.db, resolved.principal.userId);
  return ok({ weeks, ...(calibration ? { calibration } : {}) });
}

/**
 * One week: the plan and its commits. A week with no plan row is a 404 (it has
 * never been written to).
 */
export async function getWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  if (!week) return badRequest('missing project or week');
  const plan = await getPlan(deps.db, resolved.project.id, week);
  if (!plan) return notFound();
  const commits = await listWeekCommits(deps.db, resolved.project.id, week);
  return ok({ week: { plan, commits } satisfies WeekView });
}

/**
 * Add a commit to the week's DRAFT plan (auto-creating the plan on first write).
 * The server DERIVES `category` + `priorityNumeric` (KTD4) — any client-sent
 * values are ignored. A linked SO must exist in the caller's org (a dangling id
 * is a 400, surfaced clearly rather than left to the DB). The SO-or-orphan
 * invariant is enforced by the Zod refinement and the DB CHECK.
 */
export async function createCommit(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  if (!week) return badRequest('missing project or week');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const input = (body ?? {}) as Record<string, unknown>;

  // The week must be DRAFT (or absent — auto-created DRAFT) to take new commits.
  const plan = await getPlan(deps.db, resolved.project.id, week);
  if (plan && plan.status !== 'DRAFT') {
    return conflict('week is not DRAFT; planned commits are frozen');
  }

  const orphanReason = input.orphanReason as WeeklyCommit['orphanReason'] | undefined;
  const supportingOutcomeId =
    typeof input.supportingOutcomeId === 'string' ? input.supportingOutcomeId : undefined;

  // Derive the chess-layer fields server-side; ignore any client-sent ones.
  const { category, priorityNumeric } = deriveChessFields({
    ...(orphanReason !== undefined ? { orphanReason } : {}),
  });

  const parsed = weeklyCommitSchema.safeParse({
    ...input,
    id: randomUUID(),
    projectId: resolved.project.id,
    isoWeek: week,
    category,
    priorityNumeric,
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  const commit = parsed.data;

  // The SO link is validated at the app layer (the column is not a hard FK so a
  // dangling id would otherwise pass): a non-existent SO in the caller's org is a 400.
  if (supportingOutcomeId !== undefined) {
    const so = await getObjective(deps.db, resolved.principal.org, supportingOutcomeId);
    if (!so) return badRequest(`unknown supportingOutcomeId: ${supportingOutcomeId}`);
  }

  await ensureDraftPlan(deps.db, resolved.project.id, week);
  try {
    await repoCreateCommit(deps.db, commit);
  } catch {
    // The DB CHECK (SO-or-orphan) is the last line of defence; surface it as a 400.
    return badRequest('a commit must have a supportingOutcomeId or an orphanReason');
  }
  return created({ commit });
}

/**
 * Edit a commit's planned fields during DRAFT. The chess-layer fields are
 * re-derived when the SO link / orphan reason changes; client-sent
 * `category`/`priorityNumeric` are ignored. The reconciliation actual-fields
 * path (allowed in RECONCILING) lives in U4.
 */
export async function updateCommit(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  const cid = pathParam(event, 'cid');
  if (!week || !cid) return badRequest('missing project, week, or commit id');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const input = (body ?? {}) as Record<string, unknown>;

  const plan = await getPlan(deps.db, resolved.project.id, week);
  if (plan && plan.status !== 'DRAFT') {
    return conflict('week is not DRAFT; planned commits are frozen');
  }

  const existing = await repoGetCommit(deps.db, cid);
  if (!existing || existing.projectId !== resolved.project.id || existing.isoWeek !== week) {
    return notFound();
  }

  // The merged commit (existing + patch) must still parse + satisfy the
  // SO-or-orphan refinement; the chess-layer fields are re-derived.
  const supportingOutcomeId =
    'supportingOutcomeId' in input
      ? typeof input.supportingOutcomeId === 'string'
        ? input.supportingOutcomeId
        : undefined
      : existing.supportingOutcomeId;
  const orphanReason = (
    'orphanReason' in input ? input.orphanReason : existing.orphanReason
  ) as WeeklyCommit['orphanReason'] | undefined;
  const { category, priorityNumeric } = deriveChessFields({
    ...(orphanReason !== undefined ? { orphanReason } : {}),
  });

  const merged = {
    ...existing,
    ...input,
    id: cid,
    projectId: resolved.project.id,
    isoWeek: week,
    supportingOutcomeId,
    orphanReason,
    category,
    priorityNumeric,
  };
  const parsed = weeklyCommitSchema.safeParse(merged);
  if (!parsed.success) return badRequest(parsed.error.message);
  const next = parsed.data;

  if (next.supportingOutcomeId !== undefined) {
    const so = await getObjective(deps.db, resolved.principal.org, next.supportingOutcomeId);
    if (!so) return badRequest(`unknown supportingOutcomeId: ${next.supportingOutcomeId}`);
  }

  // PUT is a full overwrite of the editable fields: every nullable optional is
  // sent explicitly (as `null` when absent) so the repo CLEARS it — the repo
  // skips `undefined` keys, so omitting a now-absent SO would leave the stale one.
  const patch = {
    title: next.title,
    supportingOutcomeId: next.supportingOutcomeId ?? null,
    orphanReason: next.orphanReason ?? null,
    alsoAdvances: next.alsoAdvances,
    category: next.category,
    priorityNumeric: next.priorityNumeric,
    status: next.status,
    actualOutcome: next.actualOutcome ?? null,
  } as Partial<Omit<WeeklyCommit, 'id'>>;

  let updated: WeeklyCommit | undefined;
  try {
    updated = await repoUpdateCommit(deps.db, cid, patch);
  } catch {
    return badRequest('a commit must have a supportingOutcomeId or an orphanReason');
  }
  if (!updated) return notFound();
  return ok({ commit: updated });
}

/**
 * Remove a commit during DRAFT. A missing commit (or one not on this week /
 * project) is a 404.
 */
export async function deleteCommit(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  const cid = pathParam(event, 'cid');
  if (!week || !cid) return badRequest('missing project, week, or commit id');

  const plan = await getPlan(deps.db, resolved.project.id, week);
  if (plan && plan.status !== 'DRAFT') {
    return conflict('week is not DRAFT; planned commits are frozen');
  }

  const existing = await repoGetCommit(deps.db, cid);
  if (!existing || existing.projectId !== resolved.project.id || existing.isoWeek !== week) {
    return notFound();
  }
  await repoDeleteCommit(deps.db, cid);
  return ok({ deleted: cid });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: WeeklyDeps = { repo: defaultRepo(), db: defaultDb() };
  const method = event.requestContext.http.method;
  const cid = pathParam(event, 'cid');
  const week = pathParam(event, 'week');

  if (cid) {
    if (method === 'PUT') return updateCommit(event, deps);
    if (method === 'DELETE') return deleteCommit(event, deps);
    return notFound();
  }
  if (method === 'POST') return createCommit(event, deps);
  if (method === 'GET' && week) return getWeekly(event, deps);
  return listWeekly(event, deps);
}
