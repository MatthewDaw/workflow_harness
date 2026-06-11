import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, ScanCommand } from '@aws-sdk/lib-dynamodb';
import { migrateWeekly } from '../src/db/pg/migrateWeekly.js';
import { getPlan, listWeekCommits } from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { makePgliteDb } from './helpers/pgharness.js';

/**
 * U14 — legacy weekly migration + back-compat. The legacy two-state prose
 * `WeeklyUpdate` records (`PROJ#<id>` / `WEEK#<isoWeek>` Dynamo items) survive the
 * move to Postgres: each becomes a `weekly_plan` (RECONCILED when `validated`, else
 * DRAFT) plus a single read-only orphan commit carrying the prose in
 * `actualOutcome`, so it renders without itemizing and without polluting the
 * roll-up. Idempotent (upsert by `(projectId, isoWeek)`; the prose commit is only
 * synthesized once).
 */

const TABLE = 'harness-test';

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));

let db: PgDb;
beforeEach(async () => {
  ddbMock.reset();
  db = await makePgliteDb();
});

/** Stub the Scan over `WEEK#` items with a fixed page of legacy records. */
function stubLegacyWeeks(items: Record<string, unknown>[]): void {
  ddbMock.on(ScanCommand).resolves({ Items: items });
}

function legacyWeek(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    PK: 'PROJ#weekly-compass',
    SK: 'WEEK#2026-W23',
    projectId: 'weekly-compass',
    isoWeek: '2026-W23',
    done: 'shipped the roll-up',
    plan: 'wire the manager brief',
    validated: false,
    ...over,
  };
}

describe('migrateWeekly', () => {
  it('imports a validated legacy week as a RECONCILED plan with a read-only prose commit', async () => {
    // Input: one legacy week with validated:true and prose in done/plan.
    stubLegacyWeeks([legacyWeek({ validated: true })]);

    // Action: run the migration.
    const result = await migrateWeekly(doc, db, TABLE);

    // Expected: a RECONCILED plan + a single orphan prose commit carrying the prose.
    expect(result).toEqual({ imported: 1, skipped: 0 });
    const plan = await getPlan(db, 'weekly-compass', '2026-W23');
    expect(plan?.status).toBe('RECONCILED');

    const commits = await listWeekCommits(db, 'weekly-compass', '2026-W23');
    expect(commits).toHaveLength(1);
    const c = commits[0]!;
    expect(c.orphanReason).toBe('ExternalAsk');
    expect(c.supportingOutcomeId).toBeUndefined();
    expect(c.category).toBe('ExternalAsk');
    expect(c.actualOutcome).toContain('shipped the roll-up');
    expect(c.actualOutcome).toContain('wire the manager brief');
  });

  it('imports a non-validated legacy week as a DRAFT plan', async () => {
    // Input: one legacy week with validated:false.
    stubLegacyWeeks([legacyWeek({ validated: false })]);

    // Action: run the migration.
    const result = await migrateWeekly(doc, db, TABLE);

    // Expected: the plan is DRAFT.
    expect(result.imported).toBe(1);
    const plan = await getPlan(db, 'weekly-compass', '2026-W23');
    expect(plan?.status).toBe('DRAFT');
  });

  it('is idempotent — re-running is a no-op (no duplicate plans or prose commits)', async () => {
    // Input: the same legacy week, migrated twice.
    stubLegacyWeeks([legacyWeek({ validated: true })]);

    // Action: run the migration twice.
    await migrateWeekly(doc, db, TABLE);
    const second = await migrateWeekly(doc, db, TABLE);

    // Expected: still exactly one plan + one commit; the re-run upserts, not appends.
    expect(second.imported).toBe(1);
    const plan = await getPlan(db, 'weekly-compass', '2026-W23');
    expect(plan?.status).toBe('RECONCILED');
    const commits = await listWeekCommits(db, 'weekly-compass', '2026-W23');
    expect(commits).toHaveLength(1);
  });

  it('skips malformed legacy items without aborting the pass', async () => {
    // Input: one valid week and one malformed item (missing isoWeek).
    stubLegacyWeeks([
      legacyWeek({ validated: true }),
      { PK: 'PROJ#x', SK: 'WEEK#bad', projectId: 'x' /* no isoWeek */ },
    ]);

    // Action: run the migration.
    const result = await migrateWeekly(doc, db, TABLE);

    // Expected: the valid one imports; the malformed one is skipped, not fatal.
    expect(result).toEqual({ imported: 1, skipped: 1 });
  });

  it('migrates a week that renders through the weekly repo read shape (U10/U12 integration)', async () => {
    // Input: a validated legacy week.
    stubLegacyWeeks([legacyWeek({ validated: true })]);

    // Action: migrate, then read it back the way the REST/UI layer does.
    await migrateWeekly(doc, db, TABLE);

    // Expected: the plan + its single orphan commit read back cleanly, with the
    // orphan excluded from any SO link so it never breaks the itemized UI.
    const plan = await getPlan(db, 'weekly-compass', '2026-W23');
    expect(plan).toBeDefined();
    const commits = await listWeekCommits(db, 'weekly-compass', '2026-W23');
    expect(commits).toHaveLength(1);
    expect(commits[0]!.supportingOutcomeId).toBeUndefined();
    expect(commits[0]!.orphanReason).toBe('ExternalAsk');
  });
});
