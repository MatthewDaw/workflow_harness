import { PGlite } from '@electric-sql/pglite';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate, type PgDb } from '../../src/db/pg/migrate.js';
import * as schema from '../../src/db/pg/schema.js';

/**
 * A fresh, isolated in-process Postgres for a test (the pglite WASM build), with
 * the schema migrated. Mirrors `memtable.ts`'s role for the Dynamo repos: each
 * call returns a clean database so suites don't share state. No Docker, no Neon,
 * no network — the same `PgDb` the production repos take, so repo functions run
 * unchanged against it.
 */
export async function makePgliteDb(): Promise<PgDb> {
  const client = new PGlite();
  const db = drizzle(client, { schema }) as unknown as PgDb;
  await migrate(db);
  return db;
}
