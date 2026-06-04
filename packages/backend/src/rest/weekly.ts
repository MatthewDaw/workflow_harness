import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { weeklyUpdateSchema, type WeeklyUpdate } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';
import { recomputeOrgRollup } from '../projections/rollupRepo.js';
import {
  badRequest,
  defaultRepo,
  notFound,
  ok,
  parseBody,
  pathParam,
  unauthorized,
} from './runtime.js';
import { resolvePrincipal } from './bearerAuth.js';

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

async function ownedProject(repo: Repo, principal: Principal, projectId: string): Promise<boolean> {
  const project = await repo.getProject(projectId);
  return Boolean(project && project.ownerUserId === principal.userId);
}

export async function listWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  if (!pid) return badRequest('missing project id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();
  const weeks = await deps.repo.listWeekly(pid);
  return ok({ weeks });
}

export async function getWeekly(
  event: APIGatewayProxyEventV2,
  deps: WeeklyDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const week = pathParam(event, 'week');
  if (!pid || !week) return badRequest('missing project or week');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();
  const update = await deps.repo.getWeekly(pid, week);
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
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const week = pathParam(event, 'week');
  if (!pid || !week) return badRequest('missing project or week');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = weeklyUpdateSchema.safeParse({
    validated: false,
    ...(body as Record<string, unknown>),
    projectId: pid,
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
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const week = pathParam(event, 'week');
  if (!pid || !week) return badRequest('missing project or week');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  const existing = await deps.repo.getWeekly(pid, week);
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
