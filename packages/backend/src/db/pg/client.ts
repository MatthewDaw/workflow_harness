import { drizzle, type NeonHttpDatabase } from 'drizzle-orm/neon-http';
import { neon } from '@neondatabase/serverless';
import * as schema from './schema.js';

/**
 * Postgres client for the strategic-execution domain (KTD7). Production runs
 * against **Neon serverless Postgres** over its **HTTP driver** — no VPC wiring
 * for the Lambdas (the property that made the Aurora Data API attractive,
 * without the standing cost). The connection URL arrives as `DATABASE_URL`,
 * injected from the `command-hq/neon-database-url` Secrets-Manager secret (U15
 * infra), mirroring how the device-token secret is wired.
 *
 * Tests do NOT use this module — they build a `pglite` (in-process WASM
 * Postgres) Drizzle instance via `test/helpers/pgharness.ts`, so the suite needs
 * no live database. Repo functions therefore accept a `Db` argument rather than
 * reaching for a singleton, which keeps them driver-agnostic (Neon in prod,
 * pglite in tests).
 *
 * NOTE (Phase 1): the Neon **HTTP** driver does not support interactive
 * transactions. `reconcile/complete` (U4) needs one atomic transaction, so that
 * path will switch to the Neon **WebSocket** driver (`drizzle-orm/neon-serverless`,
 * still VPC-free). Phase 0 (objectives) has no transactions, so HTTP is correct here.
 */
export type Db = NeonHttpDatabase<typeof schema>;

let cached: Db | undefined;

/** The process-wide Neon-backed Drizzle client (lazy; reused across warm invocations). */
export function getDb(): Db {
  if (cached) return cached;
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error('DATABASE_URL is not set (Neon connection string for the Postgres domain)');
  }
  cached = drizzle(neon(url), { schema });
  return cached;
}
