import { ScanCommand, type DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { objectiveNodeSchema } from '@harness/shared';
import { putObjective } from './objectivesRepo.js';
import type { PgDb } from './migrate.js';

/**
 * One-shot migration of the RCDO objective tree from DynamoDB to Postgres
 * (KTD7/U16). Objectives used to live as `ORG#<org>` / `RCDO#<id>` items in the
 * single table; they now live in the `objectives` relation. This reads every
 * legacy `RCDO#` item (a table Scan with a `begins_with(SK, 'RCDO#')` filter —
 * fine for a one-time pass across all orgs) and upserts each into Postgres.
 *
 * **Idempotent:** `putObjective` upserts on the composite `(org, id)` key, so a
 * re-run overwrites with the same values rather than duplicating. Malformed
 * legacy items are counted and skipped (never fatal), mirroring the read
 * tolerance the Dynamo repos had.
 *
 * Run at deploy time AFTER the schema migration (`migrate`) and BEFORE the
 * objectives handlers cut over to reading from Postgres.
 */

export interface MigrateObjectivesResult {
  /** Legacy nodes upserted into Postgres. */
  imported: number;
  /** Legacy items that failed `objectiveNodeSchema` and were skipped. */
  skipped: number;
}

export async function migrateObjectives(
  doc: DynamoDBDocumentClient,
  db: PgDb,
  table = process.env.HARNESS_TABLE ?? 'harness',
): Promise<MigrateObjectivesResult> {
  let imported = 0;
  let skipped = 0;
  let exclusiveStartKey: Record<string, unknown> | undefined;

  do {
    const res = await doc.send(
      new ScanCommand({
        TableName: table,
        FilterExpression: 'begins_with(SK, :rcdo)',
        ExpressionAttributeValues: { ':rcdo': 'RCDO#' },
        ...(exclusiveStartKey ? { ExclusiveStartKey: exclusiveStartKey } : {}),
      }),
    );

    for (const item of res.Items ?? []) {
      const parsed = objectiveNodeSchema.safeParse(item);
      if (!parsed.success) {
        skipped += 1;
        continue;
      }
      await putObjective(db, parsed.data);
      imported += 1;
    }

    exclusiveStartKey = res.LastEvaluatedKey as Record<string, unknown> | undefined;
  } while (exclusiveStartKey);

  return { imported, skipped };
}
