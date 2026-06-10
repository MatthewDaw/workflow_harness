import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { weeklyUpdateSchema, type WeeklyUpdate } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { recomputeOrgRollup } from '../projections/rollupRepo.js';
import {
  badRequest,
  defaultRepo,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
} from './runtime.js';
import { ownedProject } from './ownership.js';

/**
 * REST: weekly updates (U11, store/serve in U4) — store + publish, scoped to
 * the project owner.
 *
 *   GET  /projects/:pid/weekly             — all weeks for the project
 *   GET  /projects/:pid/weekly/:week       — one week (e.g. 2026-W23)
 *   PUT  /projects/:pid/weekly/:week       — store a posted report (draft)
 *   POST /projects/:pid/weekly/:week/publish — mark validated + recompute roll-up
 *
 * The weekly report is generated client-side by the `/weekly-update` skill and
 * POSTed here (KTD4): a free-form `done` summary, a `plan` summary, and a
 * never-blocking `conformityScore`. HQ stores and serves it — it never generates
 * the content. Publishing flips `validated` true and re-runs the org roll-up
 * (which now derives objective completion from project progress, not the weekly
 * report itself). Conformity is a stored number, never a gate.
 */

export interface WeeklyDeps {
  repo: Repo;
}

export async function listWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const weeks = await deps.repo.listWeekly(resolved.project.id);
  return ok({ weeks });
}

export async function getWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  if (!week) return badRequest('missing project or week');
  const update = await deps.repo.getWeekly(resolved.project.id, week);
  if (!update) return notFound();
  return ok({ update });
}

/**
 * Store a posted report for the week (`done` summary, `plan`, optional
 * `conformityScore`). Re-storing overwrites it (idempotent per week). A stored
 * report is `validated: false`; only publish flips it.
 */
export async function putWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const week = pathParam(event, 'week');
  if (!week) return badRequest('missing project or week');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = weeklyUpdateSchema.safeParse({
    validated: false,
    ...(body as Record<string, unknown>),
    projectId: resolved.project.id,
    isoWeek: week,
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  const update: WeeklyUpdate = parsed.data;
  await deps.repo.putWeekly(update);
  return ok({ update });
}

/**
 * Publish the week: mark it validated and re-run the org roll-up so the
 * objectives' cached % reflect current project progress. Re-publishing
 * overwrites the week (idempotent).
 */
export async function publishWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps.repo, 'pid');
  if ('error' in resolved) return resolved.error;
  const { principal } = resolved;
  const week = pathParam(event, 'week');
  if (!week) return badRequest('missing project or week');

  const existing = await deps.repo.getWeekly(resolved.project.id, week);
  if (!existing) return notFound();

  const published: WeeklyUpdate = { ...existing, validated: true };
  await deps.repo.putWeekly(published);

  // Feed the roll-up (U10). The org is the caller's org; the project set is the
  // caller's projects so the recompute sees all their stored progress.
  const projects = await deps.repo.listProjectsForUser(principal.userId);
  await recomputeOrgRollup(
    deps.repo,
    principal.org,
    projects.map((p) => p.id),
  );

  return ok({ update: published });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: WeeklyDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const week = pathParam(event, 'week');

  if (method === 'POST' && path.endsWith('/publish')) return publishWeekly(event, deps);
  if (method === 'PUT') return putWeekly(event, deps);
  if (method === 'GET' && week) return getWeekly(event, deps);
  return listWeekly(event, deps);
}
