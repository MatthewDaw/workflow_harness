import { sql } from 'drizzle-orm';
import type { PgDatabase } from 'drizzle-orm/pg-core';
import type * as schema from './schema.js';

/**
 * Schema migration for the Postgres domain — the Flyway stand-in (KTD7). Each
 * statement is **idempotent** (`CREATE TABLE IF NOT EXISTS`), so `migrate` is
 * safe to run on every deploy and at the top of every pglite-backed test. It is
 * driver-agnostic: `db.execute` works identically over the Neon HTTP driver (in
 * Lambda) and the pglite driver (in tests).
 *
 * This deliberately uses idempotent DDL rather than versioned drizzle-kit
 * migrations: every statement is `CREATE … IF NOT EXISTS` (table, index) or a
 * CHECK declared inline in its table, so applying the whole list on a populated
 * database is a no-op. When columns start *changing* (not just being added), this
 * graduates to ordered, versioned migration files. See the plan's U15/U2.
 */

/**
 * Driver-agnostic Drizzle handle — satisfied by both the Neon-HTTP instance (prod)
 * and the pglite instance (tests). The query-result HKT slot is intentionally
 * `any`: it is the one type that differs per driver, and pinning it would force
 * the repos to know which driver they run against — the opposite of the goal.
 */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type PgDb = PgDatabase<any, typeof schema>;

const STATEMENTS: string[] = [
  `CREATE TABLE IF NOT EXISTS objectives (
     org        text NOT NULL,
     id         text NOT NULL,
     level      text NOT NULL,
     title      text NOT NULL,
     parent_id  text,
     weight     real NOT NULL DEFAULT 1,
     pct_cache  real,
     PRIMARY KEY (org, id)
   )`,
  // The slim projects mirror (KTD7) — synced from the Dynamo project-write path.
  `CREATE TABLE IF NOT EXISTS projects_mirror (
     id              text PRIMARY KEY,
     org             text NOT NULL,
     owner_user_id   text NOT NULL,
     name            text NOT NULL
   )`,
  // `listProjectsForOrg` is WHERE org = ? ORDER BY id — keyset pagination (R7/R10).
  `CREATE INDEX IF NOT EXISTS projects_mirror_org_idx ON projects_mirror (org, id)`,
  // The week itself (KTD1) — lifecycle status + declared posture + transition stamps.
  `CREATE TABLE IF NOT EXISTS weekly_plans (
     project_id     text NOT NULL,
     iso_week       text NOT NULL,
     status         text NOT NULL DEFAULT 'DRAFT',
     posture        text NOT NULL DEFAULT 'focus',
     locked_at      bigint,
     reconciled_at  bigint,
     PRIMARY KEY (project_id, iso_week)
   )`,
  // The itemized commits (KTD1). The SO-or-orphan CHECK (KTD10) is the DB-level
  // half of the invariant the Zod refinement + the lock guard also enforce: a
  // commit must carry EITHER a supporting_outcome_id OR an orphan_reason.
  `CREATE TABLE IF NOT EXISTS weekly_commits (
     id                     text PRIMARY KEY,
     project_id             text NOT NULL,
     iso_week               text NOT NULL,
     title                  text NOT NULL,
     supporting_outcome_id  text,
     orphan_reason          text,
     also_advances          text[] NOT NULL DEFAULT '{}',
     category               text NOT NULL,
     priority_numeric       real NOT NULL,
     status                 text NOT NULL DEFAULT 'planned',
     actual_outcome         text,
     carried_from_week      text,
     carried_to_week        text,
     carry_depth            integer NOT NULL DEFAULT 0,
     CONSTRAINT weekly_commits_so_or_orphan
       CHECK (supporting_outcome_id IS NOT NULL OR orphan_reason IS NOT NULL)
   )`,
  `CREATE INDEX IF NOT EXISTS weekly_commits_week_idx ON weekly_commits (project_id, iso_week)`,
];

/** Apply every schema statement idempotently. */
export async function migrate(db: PgDb): Promise<void> {
  for (const statement of STATEMENTS) {
    await db.execute(sql.raw(statement));
  }
}
