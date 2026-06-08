import type { DynamoDBStreamEvent, DynamoDBRecord, DynamoDBStreamHandler } from 'aws-lambda';
import { unmarshall } from '@aws-sdk/util-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';
import { safeParseEnvelope, type Envelope, type Project } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { applyEvent } from './projection.js';
import { recomputeOrgRollup } from '../projections/rollupRepo.js';
import { defaultRepo } from './runtime.js';

/**
 * DynamoDB Streams consumer — the projection / roll-up backstop (U5/U10).
 *
 * The `harness` table publishes a NEW_AND_OLD_IMAGES stream. The synchronous
 * ingestion path (`ws/event.ts`) already folds each accepted event into the
 * session projection, but that write can be lost (a cold-path crash, a partial
 * batch) and nothing else recomputes the objective roll-ups when a project's
 * stored progress changes. This consumer closes both gaps:
 *
 *  - EVENT records (PK `SESS#…`, SK `EVT#…`) — re-fold the event into the session
 *    projection using the SAME pure `applyEvent` the synchronous path uses. It is
 *    seq-guarded and conditional, so re-processing a record converges to exactly
 *    the same state the inline fold produced (idempotent; never regresses).
 *  - PROJECT records (PK `PROJ#…`, SK `META`) whose `progressPct` changed —
 *    recompute the org objective roll-up off the EXISTING `recomputeOrgRollup`
 *    pure logic, so "Streams drive roll-ups" actually holds.
 *
 * Everything else (connection records, listeners, device-auth, the roll-up's own
 * objective writes, projection writes, …) is irrelevant noise and skipped. The
 * handler is tolerant of malformed / partially-shaped records: a record it can't
 * interpret is logged and ignored rather than failing the batch.
 */

export interface StreamConsumerDeps {
  repo: Repo;
}

type Img = Record<string, AttributeValue> | undefined;

/** Decode a stream image (DynamoDB attribute-value map) to a plain object. */
function decode(image: Img): Record<string, unknown> | undefined {
  if (!image) return undefined;
  try {
    return unmarshall(image);
  } catch {
    return undefined;
  }
}

const MAX_ATTEMPTS = 5;

/**
 * Re-fold an appended event into its session projection. Mirrors the inline fold
 * in `ws/event.ts`: read latest, fold, conditional-write, retry on conflict. The
 * seq guard in `applyEvent` makes a duplicate / out-of-order replay a no-op, so
 * this converges to the same projection the synchronous path wrote.
 */
async function reprojectEvent(repo: Repo, env: Envelope): Promise<void> {
  const sessionId = env.event.sessionId;
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const existing = await repo.getSessionById(sessionId);
    const projection = applyEvent(existing, env, {
      userId: existing?.ownerUserId ?? 'unknown',
      instanceId: existing?.instanceId ?? env.instanceId,
    });
    const { written } = await repo.putSessionProjectionConditional(projection, existing?.maxSeq);
    if (written) return;
    // A concurrent writer advanced the projection; re-read and re-fold.
  }
}

/** True when a PROJECT META record's roll-up-relevant fields changed. */
function projectProgressChanged(
  oldImg: Record<string, unknown> | undefined,
  newImg: Record<string, unknown> | undefined,
): boolean {
  if (!newImg) return false; // a delete contributes nothing to recompute
  const before = oldImg?.progressPct;
  const after = newImg.progressPct;
  if (before !== after) return true;
  // Re-pointing which Supporting Outcomes the project owns also moves roll-ups.
  return (
    JSON.stringify(oldImg?.supportingOutcomeIds) !== JSON.stringify(newImg.supportingOutcomeIds)
  );
}

async function processRecord(record: DynamoDBRecord, deps: StreamConsumerDeps): Promise<void> {
  const ddb = record.dynamodb;
  if (!ddb) return;
  // Only newly-written / modified items carry a recompute trigger.
  if (record.eventName !== 'INSERT' && record.eventName !== 'MODIFY') return;

  const keys = decode(ddb.Keys as Img);
  const pk = typeof keys?.PK === 'string' ? keys.PK : undefined;
  const sk = typeof keys?.SK === 'string' ? keys.SK : undefined;
  if (!pk || !sk) return;

  // --- Event record -> re-fold the session projection (backstop). ----------
  if (pk.startsWith('SESS#') && sk.startsWith('EVT#')) {
    const newImg = decode(ddb.NewImage as Img);
    const parsed = safeParseEnvelope(newImg);
    if (!parsed.success) return; // not a well-formed envelope; ignore noise
    await reprojectEvent(deps.repo, parsed.data);
    return;
  }

  // --- Project META record -> recompute the org objective roll-up. ---------
  if (pk.startsWith('PROJ#') && sk === 'META') {
    const newImg = decode(ddb.NewImage as Img);
    const oldImg = decode(ddb.OldImage as Img);
    if (!projectProgressChanged(oldImg, newImg)) return;
    const project = newImg as (Project & { org?: string }) | undefined;
    // `recomputeOrgRollup` is keyed by org (the objective tree lives in an
    // `ORG#<org>` partition). The project item carries its owning `org` so the
    // roll-up driver can resolve which org's tree to recompute; absent that we
    // cannot place the project's progress into a tree, so we skip.
    const org = typeof project?.org === 'string' ? project.org : undefined;
    if (!org || !project?.id) return;
    await recomputeOrgRollup(deps.repo, org, [project.id]);
    return;
  }

  // Any other record family is irrelevant to projections/roll-ups: skip.
}

/**
 * Process a whole stream batch. Records are independent, so a per-record failure
 * is logged and does not abort the batch (the projection fold is idempotent, so
 * a later redelivery still converges).
 */
export async function consume(event: DynamoDBStreamEvent, deps: StreamConsumerDeps): Promise<void> {
  for (const record of event.Records ?? []) {
    try {
      await processRecord(record, deps);
    } catch (err) {
      console.warn('streamConsumer: record failed', {
        eventID: record.eventID,
        reason: (err as Error).message,
      });
    }
  }
}

export const handler: DynamoDBStreamHandler = (event) => consume(event, { repo: defaultRepo() });
