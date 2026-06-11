import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { WeeklyCommit, WeeklyPlan } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { getPlan, listPlansForProject, listWeekCommits } from '../db/pg/weeklyRepo.js';
import {
  gatherWeeklyMetrics,
  type ConcentrationSignal,
  type StarvedOutcome,
} from '../projections/weeklyMetrics.js';
import { resolvePrincipal } from './bearerAuth.js';
import { badRequest, defaultDb, defaultRepo, ok, queryParam, unauthorized } from './runtime.js';
import type { PgDb } from '../db/pg/migrate.js';

/**
 * REST: the reports-scoped manager exception/divergence brief (U8, KTD6).
 *
 *   GET /weekly/manager[?limit=&cursor=]
 *
 * The manager view is NOT an all-org admin grid: it is scoped by the
 * `managerUserId` edge (KTD6) — the caller's team is exactly the users whose
 * `managerUserId` is the caller (`repo.listReports`). For each report's latest
 * week the brief assembles the things that actually need a manager's attention —
 * the highest-leverage commit not started, the oldest carry, the longest-starved
 * Supporting Outcome, and any week that failed to lock — plus the report's
 * strategic concentration (U17). The default is "nothing needs you": a report
 * with no exceptions surfaces an empty `exceptions[]`, and a caller with no
 * reports gets an empty brief (no 403 — scoping is by EDGE, not a role gate).
 *
 * Pagination is keyset over the reports (sorted by `userId`) so the 2000-record
 * team target (R10) holds: `?limit=` + `?cursor=<last userId>` round-trips a page
 * at a time.
 *
 * The route is reached behind the gateway JWT authorizer (the HQ web manager view)
 * OR via a device token, so the caller is resolved through `resolvePrincipal`.
 */

export interface WeeklyManagerDeps {
  /** Dynamo repo — the manager edge (`listReports`) + the report→project map live here. */
  repo: Repo;
  /** Postgres client — the weekly relations + metrics read from here (KTD7). */
  db: PgDb;
}

/** The default page size; bounded so a single request can never sweep the team. */
const DEFAULT_LIMIT = 100;
const MAX_LIMIT = 500;

/** One exception surfaced on a report's brief — the thing that needs the manager. */
export interface BriefException {
  /** A stable kind so the UI can group/icon them. */
  kind:
    | 'highest_leverage_not_started'
    | 'oldest_carry'
    | 'longest_starved_outcome'
    | 'lock_failure';
  /** A human-readable one-liner. */
  detail: string;
  /** The commit this exception is about, when applicable. */
  commitId?: string;
  /** The Supporting Outcome this exception is about, when applicable. */
  supportingOutcomeId?: string;
}

/** The brief node for one report. */
export interface ReportBrief {
  userId: string;
  name?: string;
  /** The report's most-recent week across their projects, or null when they have none. */
  latestWeek: { projectId: string; isoWeek: string; status: WeeklyStatusLite } | null;
  /** The exceptions needing the manager's attention; empty ⇒ "nothing needs you". */
  exceptions: BriefException[];
  /** The report's strategic concentration over their latest reconciled window (U17). */
  concentration: ConcentrationSignal;
}

/** Narrowing alias so callers don't import the shared enum just for the field type. */
type WeeklyStatusLite = WeeklyPlan['status'];

/** The whole brief: the reports page + the keyset cursor for the next page. */
export interface ManagerBrief {
  reports: ReportBrief[];
  /** The userId to pass as `cursor` for the next page; absent on the last page. */
  nextCursor?: string;
}

/** The latest week (by ISO-week string, lexicographically sortable) across a set of plans. */
function latestPlan(plans: WeeklyPlan[]): WeeklyPlan | undefined {
  let latest: WeeklyPlan | undefined;
  for (const p of plans) {
    if (!latest || p.isoWeek > latest.isoWeek) latest = p;
  }
  return latest;
}

/**
 * Assemble one report's exceptions from their latest week's commits + their
 * metrics. The set mirrors the plan's Approach (KTD6): the highest-leverage
 * commit not started, the oldest carry, the longest-starved SO, and any
 * lock-blocking commit (one with neither an SO nor an orphan reason). A report
 * with a clean week surfaces no exceptions ("nothing needs you").
 */
function assembleExceptions(
  commits: WeeklyCommit[],
  starvation: StarvedOutcome[],
): BriefException[] {
  const exceptions: BriefException[] = [];

  // Highest-leverage commit not started: the still-`planned` commit with the
  // greatest derived WSJF priority (ties by id for determinism).
  const notStarted = commits
    .filter((c) => c.status === 'planned')
    .sort((a, b) => b.priorityNumeric - a.priorityNumeric || a.id.localeCompare(b.id));
  if (notStarted[0]) {
    const top = notStarted[0];
    exceptions.push({
      kind: 'highest_leverage_not_started',
      detail: `highest-leverage commit not started: "${top.title}"`,
      commitId: top.id,
    });
  }

  // Oldest carry: the commit that has carried the most (highest `carryDepth`),
  // surfaced only when it has actually carried (depth > 0).
  const carried = commits
    .filter((c) => c.carryDepth > 0)
    .sort((a, b) => b.carryDepth - a.carryDepth || a.id.localeCompare(b.id));
  if (carried[0]) {
    const oldest = carried[0];
    exceptions.push({
      kind: 'oldest_carry',
      detail: `oldest carry: "${oldest.title}" at carry depth ${oldest.carryDepth}`,
      commitId: oldest.id,
    });
  }

  // Longest-starved SO: the highest-weighted starved outcome (computed in U17,
  // already sorted by behind-ness).
  if (starvation[0]) {
    const starved = starvation[0];
    exceptions.push({
      kind: 'longest_starved_outcome',
      detail: `longest-starved supporting outcome: ${starved.id}`,
      supportingOutcomeId: starved.id,
    });
  }

  // Lock failures: a commit carrying neither an SO nor an orphan reason cannot
  // lock (the U4 guard). Surface each so the manager sees what blocks the week.
  for (const c of commits) {
    if (!c.supportingOutcomeId && !c.orphanReason) {
      exceptions.push({
        kind: 'lock_failure',
        detail: `commit "${c.title}" has neither a supporting outcome nor an orphan reason`,
        commitId: c.id,
      });
    }
  }

  return exceptions;
}

/**
 * Build one report's brief node: resolve their projects (via the owner index),
 * find their latest week, gather its commits + their metrics, and assemble the
 * exceptions. A report who owns no projects (or has no weeks) yields a null
 * `latestWeek` and an empty `exceptions[]`.
 */
async function buildReportBrief(
  deps: WeeklyManagerDeps,
  report: { userId: string; name?: string; org?: string },
): Promise<ReportBrief> {
  const projects = await deps.repo.listProjectsForUser(report.userId);
  const projectIds = projects.map((p) => p.id);

  // The report's concentration / starvation read off their own project set's
  // latest reconciled windows (U17), folded to one number for the person.
  const metrics =
    report.org && projectIds.length > 0
      ? await gatherWeeklyMetrics(deps.db, report.org, { projectIds })
      : { concentration: { herfindahl: 0, nodes: 0, postureDivergence: 0 }, starvation: [] };

  // The latest week across all the report's projects (any status — a week that
  // failed to lock is exactly what the manager wants to see).
  let latest: { projectId: string; plan: WeeklyPlan } | undefined;
  for (const id of projectIds) {
    const plans = await listPlansForProject(deps.db, id);
    const top = latestPlan(plans);
    if (top && (!latest || top.isoWeek > latest.plan.isoWeek)) {
      latest = { projectId: id, plan: top };
    }
  }

  if (!latest) {
    return {
      userId: report.userId,
      ...(report.name ? { name: report.name } : {}),
      latestWeek: null,
      exceptions: [],
      concentration: metrics.concentration,
    };
  }

  const commits = await listWeekCommits(deps.db, latest.projectId, latest.plan.isoWeek);
  const exceptions = assembleExceptions(commits, metrics.starvation);

  return {
    userId: report.userId,
    ...(report.name ? { name: report.name } : {}),
    latestWeek: {
      projectId: latest.projectId,
      isoWeek: latest.plan.isoWeek,
      status: latest.plan.status,
    },
    exceptions,
    concentration: metrics.concentration,
  };
}

/**
 * GET /weekly/manager — the reports-scoped exception/divergence brief (U8). The
 * caller's reports are paginated by keyset (sorted by `userId`); each report's
 * latest week is distilled to its exceptions + concentration. A caller with no
 * reports gets `{ reports: [] }` (the "nothing needs you" empty brief) — no 403,
 * since scoping is by the manager EDGE, not a role gate.
 */
export async function getManagerBrief(
  event: APIGatewayProxyEventV2,
  deps: WeeklyManagerDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();

  const limitRaw = queryParam(event, 'limit');
  let limit = DEFAULT_LIMIT;
  if (limitRaw !== undefined) {
    const parsed = Number(limitRaw);
    if (!Number.isInteger(parsed) || parsed <= 0) return badRequest('invalid limit');
    limit = Math.min(parsed, MAX_LIMIT);
  }
  const cursor = queryParam(event, 'cursor');

  // The team = users whose manager edge is the caller (KTD6). Keyset-paginate it
  // by `userId` so the 2000-record target (R10) holds.
  const allReports = await deps.repo.listReports(principal.userId);
  const sorted = allReports
    .slice()
    .sort((a, b) => a.userId.localeCompare(b.userId))
    .filter((r) => (cursor === undefined ? true : r.userId > cursor));
  const page = sorted.slice(0, limit);
  const hasMore = sorted.length > limit;

  const reports: ReportBrief[] = [];
  for (const r of page) {
    reports.push(
      await buildReportBrief(deps, {
        userId: r.userId,
        ...(r.name ? { name: r.name } : {}),
        ...(r.org ? { org: r.org } : {}),
      }),
    );
  }

  const brief: ManagerBrief = {
    reports,
    ...(hasMore && page.length > 0 ? { nextCursor: page[page.length - 1]!.userId } : {}),
  };
  return ok(brief);
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: WeeklyManagerDeps = { repo: defaultRepo(), db: defaultDb() };
  return getManagerBrief(event, deps);
}
