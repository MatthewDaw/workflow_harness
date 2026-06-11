import { pgTable, text, real, integer, bigint, primaryKey, index } from 'drizzle-orm/pg-core';

/**
 * Drizzle schema for the strategic-execution domain (KTD7). Objectives — the
 * RCDO tree — move here from DynamoDB (U16) because the enforced Supporting
 * Outcome link is a foreign key and the roll-up joins commits↔objectives. The
 * weekly relations (`weekly_plans`, `weekly_commits`) and the slim `projects`
 * mirror land in U2 (below).
 *
 * `objectives` mirrors the shared `ObjectiveNode` shape (`id`, `org`, `level`,
 * `title`, `parentId`, `pct`) plus two columns the metric work needs:
 *  - `weight` (default 1) — the branch weight WSJF's Cost-of-Delay reads (U6).
 *  - `pctCache` — the roll-up's cached completion (was `ObjectiveNode.pct`).
 *
 * The primary key is composite `(org, id)`: an objective id is unique only
 * within its org (the Dynamo key was `ORG#<org>` / `RCDO#<id>`), so the org is
 * part of identity. `parentId` is intentionally NOT a foreign key — the roll-up
 * already tolerates a dangling parent link (an orphaned parent contributes to no
 * node and is never fatal), and enforcing it would reject otherwise-valid trees
 * mid-migration.
 */
export const objectives = pgTable(
  'objectives',
  {
    org: text('org').notNull(),
    id: text('id').notNull(),
    level: text('level').notNull(),
    title: text('title').notNull(),
    parentId: text('parent_id'),
    weight: real('weight').notNull().default(1),
    pctCache: real('pct_cache'),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.org, t.id] }),
  }),
);

export type ObjectiveRow = typeof objectives.$inferSelect;

/**
 * The slim `projects` mirror (KTD7). Synced from the Dynamo project-write path on
 * every project write (id / org / owner / name only) so the manager joins and the
 * org-scoped project listing (`listProjectsForOrg`) never reach back into Dynamo.
 * Dynamo stays the source of truth for the full project record; this is a
 * read-optimized mirror for the relational queries that Postgres owns.
 *
 * `org` is indexed because `listProjectsForOrg` is `WHERE org = ? ORDER BY id`
 * (keyset pagination — R7/R10), the query that supersedes the GSI2 the prior plan
 * would have added to Dynamo.
 */
export const projectsMirror = pgTable(
  'projects_mirror',
  {
    id: text('id').primaryKey(),
    org: text('org').notNull(),
    ownerUserId: text('owner_user_id').notNull(),
    name: text('name').notNull(),
  },
  (t) => ({
    byOrg: index('projects_mirror_org_idx').on(t.org, t.id),
  }),
);

export type ProjectMirrorRow = typeof projectsMirror.$inferSelect;

/**
 * `weekly_plans` — the week itself (KTD1), keyed `(project_id, iso_week)`. Carries
 * the lifecycle `status` (DRAFT → LOCKED → RECONCILING → RECONCILED — KTD2), the
 * declared concentration `posture` (KTD8), and the transition timestamps stamped
 * by the lifecycle endpoints (U4). Mirrors the shared `WeeklyPlan` shape.
 *
 * `project_id` is intentionally NOT a hard foreign key to `projects_mirror`: the
 * mirror is an eventually-synced read model, and a plan must not be rejected
 * because the mirror row has not landed yet. Identity is the composite
 * `(project_id, iso_week)`.
 */
export const weeklyPlans = pgTable(
  'weekly_plans',
  {
    projectId: text('project_id').notNull(),
    isoWeek: text('iso_week').notNull(),
    status: text('status').notNull().default('DRAFT'),
    posture: text('posture').notNull().default('focus'),
    // Epoch-MS transition stamps (KTD2) — `bigint` because epoch-ms overflows int4.
    lockedAt: bigint('locked_at', { mode: 'number' }),
    reconciledAt: bigint('reconciled_at', { mode: 'number' }),
  },
  (t) => ({
    pk: primaryKey({ columns: [t.projectId, t.isoWeek] }),
  }),
);

export type WeeklyPlanRow = typeof weeklyPlans.$inferSelect;

/**
 * `weekly_commits` — the itemized weekly commits (KTD1). Each has a surrogate `id`
 * and belongs to a `(project_id, iso_week)` plan. It carries EITHER a primary
 * `supporting_outcome_id` (the FK that drives ALL roll-up + concentration math —
 * KTD9) OR a typed `orphan_reason` (KTD10); the SO-or-orphan invariant is enforced
 * by a DB CHECK declared in the migration (`migrate.ts`) — Drizzle's pg-core does
 * not yet model table CHECK constraints, so it lives in the DDL the migration
 * runs, alongside this table definition.
 *
 * `category` + `priority_numeric` are DERIVED server-side (KTD4), not
 * client-authored. `also_advances` is informational only (never splits credit).
 * Carry provenance (KTD3): `carried_from_week` / `carried_to_week` / `carry_depth`.
 */
export const weeklyCommits = pgTable(
  'weekly_commits',
  {
    id: text('id').primaryKey(),
    projectId: text('project_id').notNull(),
    isoWeek: text('iso_week').notNull(),
    title: text('title').notNull(),
    supportingOutcomeId: text('supporting_outcome_id'),
    orphanReason: text('orphan_reason'),
    alsoAdvances: text('also_advances').array().notNull().default([]),
    category: text('category').notNull(),
    priorityNumeric: real('priority_numeric').notNull(),
    status: text('status').notNull().default('planned'),
    actualOutcome: text('actual_outcome'),
    carriedFromWeek: text('carried_from_week'),
    carriedToWeek: text('carried_to_week'),
    carryDepth: integer('carry_depth').notNull().default(0),
  },
  (t) => ({
    byWeek: index('weekly_commits_week_idx').on(t.projectId, t.isoWeek),
  }),
);

export type WeeklyCommitRow = typeof weeklyCommits.$inferSelect;

/**
 * `calibrations` — per-person reconciliation calibration (U19). On every
 * `completeReconcile`, the just-reconciled week's terminal commits accumulate into
 * the owner's running totals so the agent can right-size next week's proposal: a
 * person who completes ~60% of what they lock is shown 6 high-leverage units, not
 * 10. Keyed by `user_id` (KTD: calibration is per-person, not per-project) — a
 * trailing accumulation across that person's reconciled weeks.
 *
 *  - `locked_count` — terminal commits the person has reconciled (the denominator).
 *  - `done_count` — of those, the ones reconciled `done` (the numerator).
 *  - `rate` — `done_count / locked_count`, the headline completion calibration.
 *  - `high_priority_first_count` / `high_priority_total` — of the higher-priority
 *    half of each window, how many shipped (`done`), so the agent can tell whether
 *    leverage-first ordering actually held (advisory only).
 *  - `updated_at` — epoch-ms of the last `completeReconcile` that touched the row.
 *
 * Advisory only — it never blocks a transition.
 */
export const calibrations = pgTable('calibrations', {
  userId: text('user_id').primaryKey(),
  lockedCount: integer('locked_count').notNull().default(0),
  doneCount: integer('done_count').notNull().default(0),
  rate: real('rate'),
  highPriorityFirstCount: integer('high_priority_first_count').notNull().default(0),
  highPriorityTotal: integer('high_priority_total').notNull().default(0),
  updatedAt: bigint('updated_at', { mode: 'number' }),
});

export type CalibrationRow = typeof calibrations.$inferSelect;
