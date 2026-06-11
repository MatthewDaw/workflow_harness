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
 * migrations while the schema is a single table; when `weekly_plans`/
 * `weekly_commits` land (U2) and columns start changing, this graduates to
 * ordered, versioned migration files. See the plan's U15/U2.
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
];

/** Apply every schema statement idempotently. */
export async function migrate(db: PgDb): Promise<void> {
  for (const statement of STATEMENTS) {
    await db.execute(sql.raw(statement));
  }
}
