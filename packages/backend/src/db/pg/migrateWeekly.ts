import { randomUUID } from 'node:crypto';
import { ScanCommand, type DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { weeklyUpdateSchema, type WeeklyCommit, type WeeklyPlan } from '@harness/shared';
import { upsertPlan, createCommit, listWeekCommits } from './weeklyRepo.js';
import type { PgDb } from './migrate.js';

/**
 * One-shot migration of the legacy two-state prose weekly records from DynamoDB to
 * Postgres (KTD4/U14). Weekly updates used to live as `PROJ#<id>` / `WEEK#<isoWeek>`
 * items in the single table — a `{ projectId, isoWeek, done, plan, conformityScore?,
 * validated }` prose blob. They now live in the itemized weekly-commit lifecycle
 * (`weekly_plans` + `weekly_commits`).
 *
 * **Lossless import:** each legacy week becomes a `weekly_plan` whose `status` is
 * `RECONCILED` when the record was `validated` (else `DRAFT`) plus a SINGLE
 * read-only orphan commit (`orphanReason: ExternalAsk`) carrying the legacy prose
 * (`done` + `plan`) in `actualOutcome`. The commit is an orphan — a typed
 * non-link (KTD10) — so the migrated prose renders read-only WITHOUT itemized
 * commits and WITHOUT polluting the roll-up (orphan commits are excluded from
 * leaf credit and concentration). The deprecated `weeklyUpdateSchema` alias (U1)
 * is what parses the legacy items here.
 *
 * **Idempotent:** the plan upserts on the composite `(projectId, isoWeek)` key, so
 * a re-run overwrites the plan with the same values rather than duplicating; the
 * synthesized commit is only inserted when the week has no commits yet, so a
 * re-run does not pile up duplicate prose commits. Malformed legacy items are
 * counted and skipped (never fatal), mirroring the read tolerance the Dynamo repos
 * had.
 *
 * Run at deploy time AFTER the schema migration (`migrate`) and AFTER the
 * objectives migration (`migrateObjectives`), BEFORE the weekly handlers cut over
 * to reading from Postgres.
 */

export interface MigrateWeeklyResult {
  /** Legacy weeks upserted into Postgres as plans. */
  imported: number;
  /** Legacy items that failed `weeklyUpdateSchema` and were skipped. */
  skipped: number;
}

/** Join the legacy prose fields into a single read-only note for `actualOutcome`. */
function proseNote(done: string, plan: string): string {
  const parts: string[] = [];
  if (done.trim().length > 0) parts.push(`Done: ${done.trim()}`);
  if (plan.trim().length > 0) parts.push(`Plan: ${plan.trim()}`);
  return parts.join('\n\n');
}

export async function migrateWeekly(
  doc: DynamoDBDocumentClient,
  db: PgDb,
  table = process.env.HARNESS_TABLE ?? 'harness',
): Promise<MigrateWeeklyResult> {
  let imported = 0;
  let skipped = 0;
  let exclusiveStartKey: Record<string, unknown> | undefined;

  do {
    const res = await doc.send(
      new ScanCommand({
        TableName: table,
        FilterExpression: 'begins_with(SK, :week)',
        ExpressionAttributeValues: { ':week': 'WEEK#' },
        ...(exclusiveStartKey ? { ExclusiveStartKey: exclusiveStartKey } : {}),
      }),
    );

    for (const item of res.Items ?? []) {
      const parsed = weeklyUpdateSchema.safeParse(item);
      if (!parsed.success) {
        skipped += 1;
        continue;
      }
      const legacy = parsed.data;

      // A legacy week is RECONCILED when it was validated, else DRAFT (KTD2).
      const status: WeeklyPlan['status'] = legacy.validated ? 'RECONCILED' : 'DRAFT';
      const plan: WeeklyPlan = {
        projectId: legacy.projectId,
        isoWeek: legacy.isoWeek,
        status,
        posture: 'focus',
      };
      await upsertPlan(db, plan);

      // Synthesize a single read-only prose commit only when none exist yet, so a
      // re-run never duplicates it (idempotent).
      const existing = await listWeekCommits(db, legacy.projectId, legacy.isoWeek);
      if (existing.length === 0) {
        const commit: WeeklyCommit = {
          id: randomUUID(),
          projectId: legacy.projectId,
          isoWeek: legacy.isoWeek,
          title: `Legacy weekly note (${legacy.isoWeek})`,
          // Orphan — a typed non-link (KTD10) — so the migrated prose is excluded
          // from roll-up + concentration. ExternalAsk reads as "imported, not
          // itemized."
          orphanReason: 'ExternalAsk',
          alsoAdvances: [],
          // An orphan commit's reason IS its category (KTD4/KTD10).
          category: 'ExternalAsk',
          priorityNumeric: 0,
          status: 'planned',
          actualOutcome: proseNote(legacy.done, legacy.plan),
          carryDepth: 0,
        };
        await createCommit(db, commit);
      }

      imported += 1;
    }

    exclusiveStartKey = res.LastEvaluatedKey as Record<string, unknown> | undefined;
  } while (exclusiveStartKey);

  return { imported, skipped };
}
