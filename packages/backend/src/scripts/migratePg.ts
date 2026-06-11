import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { neon } from '@neondatabase/serverless';
import { drizzle } from 'drizzle-orm/neon-http';
import * as schema from '../db/pg/schema.js';
import type { PgDb } from '../db/pg/migrate.js';
import { migrate } from '../db/pg/migrate.js';
import { migrateObjectives } from '../db/pg/migrateObjectives.js';
import { migrateWeekly } from '../db/pg/migrateWeekly.js';

/**
 * Deploy-time Postgres migration runner (the Flyway stand-in entry point). Run
 * this ONCE against the Neon database before/with the first deploy of the
 * strategic-execution domain (KTD7):
 *
 *   npm run build -w @harness/backend
 *   DATABASE_URL='postgres://...neon...' HARNESS_TABLE=harness \
 *     node packages/backend/dist/scripts/migratePg.js
 *
 * 1. `migrate` creates every table idempotently (objectives, weekly_plans,
 *    weekly_commits, projects_mirror, calibrations) — safe to re-run.
 * 2. If `HARNESS_TABLE` is set, it then imports the legacy RCDO objective tree
 *    and any legacy prose weekly records out of DynamoDB into Postgres (both
 *    idempotent upserts), so existing objectives + weeks survive the cutover.
 *    Omit `HARNESS_TABLE` to run the schema migration only.
 */
async function main(): Promise<void> {
  const url = process.env.DATABASE_URL;
  if (!url) throw new Error('DATABASE_URL is required (the Neon connection string)');

  const db = drizzle(neon(url), { schema }) as unknown as PgDb;

  console.log('[migrate-pg] creating schema (idempotent)…');
  await migrate(db);
  console.log('[migrate-pg] schema ready.');

  const table = process.env.HARNESS_TABLE;
  if (!table) {
    console.log('[migrate-pg] HARNESS_TABLE not set — schema only, skipping Dynamo data import.');
    return;
  }

  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({}));

  console.log('[migrate-pg] importing objectives from DynamoDB…');
  const obj = await migrateObjectives(doc, db, table);
  console.log(`[migrate-pg] objectives: imported ${obj.imported}, skipped ${obj.skipped}.`);

  console.log('[migrate-pg] importing legacy weekly records from DynamoDB…');
  const wk = await migrateWeekly(doc, db, table);
  console.log(`[migrate-pg] weekly: imported ${wk.imported}, skipped ${wk.skipped}.`);

  console.log('[migrate-pg] done.');
}

main().catch((err) => {
  console.error('[migrate-pg] failed:', err);
  process.exitCode = 1;
});
