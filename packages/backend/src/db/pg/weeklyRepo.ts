import { and, asc, eq, gt } from 'drizzle-orm';
import type { WeeklyCommit, WeeklyPlan } from '@harness/shared';
import {
  projectsMirror,
  weeklyCommits,
  weeklyPlans,
  type ProjectMirrorRow,
  type WeeklyCommitRow,
  type WeeklyPlanRow,
} from './schema.js';
import type { PgDb } from './migrate.js';

/**
 * Weekly persistence on Postgres (U2) — the relational half of the weekly-commit
 * lifecycle the REST layer (`rest/weekly.ts` U3, `rest/weeklyTransitions.ts` U4)
 * consumes. Two relations (KTD1): `weekly_plans` (the week, keyed
 * `(projectId, isoWeek)`) and `weekly_commits` (the items). Plus the slim
 * `projects` mirror (KTD7) and its org-scoped, keyset-paginated listing (R7/R10).
 *
 * Every function takes a `PgDb` so it runs against Neon in Lambda and pglite in
 * tests — driver-agnostic, mirroring `objectivesRepo`.
 */

// --- weekly_plans -----------------------------------------------------------

/** Map a plan row to the public `WeeklyPlan`, omitting absent optional fields. */
function toPlan(row: WeeklyPlanRow): WeeklyPlan {
  return {
    projectId: row.projectId,
    isoWeek: row.isoWeek,
    status: row.status as WeeklyPlan['status'],
    posture: row.posture as WeeklyPlan['posture'],
    ...(row.lockedAt != null ? { lockedAt: row.lockedAt } : {}),
    ...(row.reconciledAt != null ? { reconciledAt: row.reconciledAt } : {}),
  };
}

/** Upsert a week (full overwrite on the composite `(projectId, isoWeek)` key). */
export async function upsertPlan(db: PgDb, plan: WeeklyPlan): Promise<void> {
  const values = {
    projectId: plan.projectId,
    isoWeek: plan.isoWeek,
    status: plan.status,
    posture: plan.posture,
    lockedAt: plan.lockedAt ?? null,
    reconciledAt: plan.reconciledAt ?? null,
  };
  await db
    .insert(weeklyPlans)
    .values(values)
    .onConflictDoUpdate({
      target: [weeklyPlans.projectId, weeklyPlans.isoWeek],
      set: {
        status: values.status,
        posture: values.posture,
        lockedAt: values.lockedAt,
        reconciledAt: values.reconciledAt,
      },
    });
}

export async function getPlan(
  db: PgDb,
  projectId: string,
  isoWeek: string,
): Promise<WeeklyPlan | undefined> {
  const rows = await db
    .select()
    .from(weeklyPlans)
    .where(and(eq(weeklyPlans.projectId, projectId), eq(weeklyPlans.isoWeek, isoWeek)))
    .limit(1);
  return rows[0] ? toPlan(rows[0]) : undefined;
}

/** Every week for a project, oldest ISO-week first. */
export async function listPlansForProject(db: PgDb, projectId: string): Promise<WeeklyPlan[]> {
  const rows = await db
    .select()
    .from(weeklyPlans)
    .where(eq(weeklyPlans.projectId, projectId))
    .orderBy(asc(weeklyPlans.isoWeek));
  return rows.map(toPlan);
}

// --- weekly_commits ---------------------------------------------------------

/** Map a commit row to the public `WeeklyCommit`, omitting absent optional fields. */
function toCommit(row: WeeklyCommitRow): WeeklyCommit {
  return {
    id: row.id,
    projectId: row.projectId,
    isoWeek: row.isoWeek,
    title: row.title,
    ...(row.supportingOutcomeId != null
      ? { supportingOutcomeId: row.supportingOutcomeId }
      : {}),
    ...(row.orphanReason != null
      ? { orphanReason: row.orphanReason as WeeklyCommit['orphanReason'] }
      : {}),
    alsoAdvances: row.alsoAdvances,
    category: row.category as WeeklyCommit['category'],
    priorityNumeric: row.priorityNumeric,
    status: row.status as WeeklyCommit['status'],
    ...(row.actualOutcome != null ? { actualOutcome: row.actualOutcome } : {}),
    ...(row.carriedFromWeek != null ? { carriedFromWeek: row.carriedFromWeek } : {}),
    ...(row.carriedToWeek != null ? { carriedToWeek: row.carriedToWeek } : {}),
    carryDepth: row.carryDepth,
  };
}

/** The full row shape Drizzle inserts — null fills the absent optionals. */
function commitToRow(c: WeeklyCommit): WeeklyCommitRow {
  return {
    id: c.id,
    projectId: c.projectId,
    isoWeek: c.isoWeek,
    title: c.title,
    supportingOutcomeId: c.supportingOutcomeId ?? null,
    orphanReason: c.orphanReason ?? null,
    alsoAdvances: c.alsoAdvances ?? [],
    category: c.category,
    priorityNumeric: c.priorityNumeric,
    status: c.status,
    actualOutcome: c.actualOutcome ?? null,
    carriedFromWeek: c.carriedFromWeek ?? null,
    carriedToWeek: c.carriedToWeek ?? null,
    carryDepth: c.carryDepth,
  };
}

/**
 * Insert a new commit. The SO-or-orphan invariant is enforced by the DB CHECK
 * (KTD10) — a commit with neither raises a constraint error from Postgres, which
 * the REST layer (U3) surfaces as a 400.
 */
export async function createCommit(db: PgDb, c: WeeklyCommit): Promise<void> {
  await db.insert(weeklyCommits).values(commitToRow(c));
}

/**
 * Patch an existing commit by id with the given partial fields. Returns the
 * updated commit, or undefined when no row matched (the caller 404s). The
 * SO-or-orphan CHECK still applies to the resulting row.
 */
export async function updateCommit(
  db: PgDb,
  id: string,
  patch: Partial<Omit<WeeklyCommit, 'id'>>,
): Promise<WeeklyCommit | undefined> {
  const set: Partial<WeeklyCommitRow> = {};
  if (patch.projectId !== undefined) set.projectId = patch.projectId;
  if (patch.isoWeek !== undefined) set.isoWeek = patch.isoWeek;
  if (patch.title !== undefined) set.title = patch.title;
  if (patch.supportingOutcomeId !== undefined)
    set.supportingOutcomeId = patch.supportingOutcomeId ?? null;
  if (patch.orphanReason !== undefined) set.orphanReason = patch.orphanReason ?? null;
  if (patch.alsoAdvances !== undefined) set.alsoAdvances = patch.alsoAdvances;
  if (patch.category !== undefined) set.category = patch.category;
  if (patch.priorityNumeric !== undefined) set.priorityNumeric = patch.priorityNumeric;
  if (patch.status !== undefined) set.status = patch.status;
  if (patch.actualOutcome !== undefined) set.actualOutcome = patch.actualOutcome ?? null;
  if (patch.carriedFromWeek !== undefined) set.carriedFromWeek = patch.carriedFromWeek ?? null;
  if (patch.carriedToWeek !== undefined) set.carriedToWeek = patch.carriedToWeek ?? null;
  if (patch.carryDepth !== undefined) set.carryDepth = patch.carryDepth;

  if (Object.keys(set).length === 0) {
    return getCommit(db, id);
  }
  const rows = await db
    .update(weeklyCommits)
    .set(set)
    .where(eq(weeklyCommits.id, id))
    .returning();
  return rows[0] ? toCommit(rows[0]) : undefined;
}

/** A single commit by id, or undefined when absent. */
export async function getCommit(db: PgDb, id: string): Promise<WeeklyCommit | undefined> {
  const rows = await db.select().from(weeklyCommits).where(eq(weeklyCommits.id, id)).limit(1);
  return rows[0] ? toCommit(rows[0]) : undefined;
}

/** Delete a commit by id. Returns true when a row was removed, false when absent. */
export async function deleteCommit(db: PgDb, id: string): Promise<boolean> {
  const rows = await db
    .delete(weeklyCommits)
    .where(eq(weeklyCommits.id, id))
    .returning({ id: weeklyCommits.id });
  return rows.length > 0;
}

/** Every commit for a `(projectId, isoWeek)` week, ordered by derived priority desc. */
export async function listWeekCommits(
  db: PgDb,
  projectId: string,
  isoWeek: string,
): Promise<WeeklyCommit[]> {
  const rows = await db
    .select()
    .from(weeklyCommits)
    .where(and(eq(weeklyCommits.projectId, projectId), eq(weeklyCommits.isoWeek, isoWeek)));
  // The list self-sorts by derived WSJF leverage (KTD4); ties fall back to id so
  // the order is deterministic.
  return rows
    .map(toCommit)
    .sort((a, b) => b.priorityNumeric - a.priorityNumeric || a.id.localeCompare(b.id));
}

// --- projects mirror (KTD7) -------------------------------------------------

/** Map a mirror row to its public slim shape. */
export interface ProjectMirror {
  id: string;
  org: string;
  ownerUserId: string;
  name: string;
}

function toMirror(row: ProjectMirrorRow): ProjectMirror {
  return { id: row.id, org: row.org, ownerUserId: row.ownerUserId, name: row.name };
}

/**
 * Upsert the slim project mirror (id / org / owner / name only — KTD7). Called
 * from the Dynamo project-write path so the two stores agree without the manager
 * joins reaching back into Dynamo.
 */
export async function upsertProjectMirror(db: PgDb, m: ProjectMirror): Promise<void> {
  await db
    .insert(projectsMirror)
    .values(m)
    .onConflictDoUpdate({
      target: projectsMirror.id,
      set: { org: m.org, ownerUserId: m.ownerUserId, name: m.name },
    });
}

/** A page of an org-scoped project listing (R7/R10 keyset pagination). */
export interface ProjectPage {
  items: ProjectMirror[];
  /** The id to pass as `cursor` for the next page; absent when the page is the last. */
  nextCursor?: string;
}

/**
 * Org-scoped project listing with keyset pagination (R7/R10). `SELECT … WHERE
 * org = $1 [AND id > cursor] ORDER BY id LIMIT $2` — the query that supersedes the
 * GSI2 the prior plan added to Dynamo. Returns up to `limit` rows plus the
 * `nextCursor` (the last id) when more may exist.
 */
export async function listProjectsForOrg(
  db: PgDb,
  org: string,
  opts: { limit?: number; cursor?: string } = {},
): Promise<ProjectPage> {
  const limit = opts.limit ?? 100;
  const where =
    opts.cursor !== undefined
      ? and(eq(projectsMirror.org, org), gt(projectsMirror.id, opts.cursor))
      : eq(projectsMirror.org, org);
  const rows = await db
    .select()
    .from(projectsMirror)
    .where(where)
    .orderBy(asc(projectsMirror.id))
    .limit(limit + 1);
  const items = rows.slice(0, limit).map(toMirror);
  const hasMore = rows.length > limit;
  return {
    items,
    ...(hasMore && items.length > 0 ? { nextCursor: items[items.length - 1]!.id } : {}),
  };
}
