import type {
  DynamoDBStreamEvent,
  DynamoDBRecord,
  DynamoDBStreamHandler,
  DynamoDBBatchResponse,
} from 'aws-lambda';
import { createHash } from 'node:crypto';
import { unmarshall } from '@aws-sdk/util-dynamodb';
import type { AttributeValue } from '@aws-sdk/client-dynamodb';
import { safeParseEnvelope, type Envelope, type Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { applyEvent } from './projection.js';
import { defaultRepo } from './runtime.js';
import { getDb } from '../db/pg/client.js';
import type { PgDb } from '../db/pg/migrate.js';
import { orgFromScopePartition, isVersionSideRecord } from '../db/keys.js';
import { embed as defaultEmbed, type Embedding } from '../embeddings/embed.js';
import {
  getS3Vectors,
  SKILL_VECTOR_INDEX,
  skillVectorKey,
  type S3Vectors,
} from '../embeddings/s3vectors.js';
import {
  associateAndFinalize as defaultAssociateAndFinalize,
  type AssociateDeps,
  type PipelineResult,
  type RouteOutcome,
  type TopicFinding,
} from '../ideas/associate.js';
import {
  emitEmbedOutcome,
  emitAssociationOutcome,
  type AssociationMetricOutcome,
  type MetricSink,
} from '../observability/metrics.js';

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
 *
 * The objective roll-up is NO LONGER driven from a Dynamo PROJECT progress change
 * (U6): its single source of truth is now reconciled weekly commits (KTD5), so the
 * recompute runs in `/reconcile/complete` (`rest/weeklyTransitions.ts`), not here.
 * The GitHub `progressPct → rollup` consumer is removed.
 *
 * Everything else (connection records, listeners, device-auth, project writes,
 * the roll-up's own objective writes, projection writes, …) is irrelevant noise
 * and skipped. The handler is tolerant of malformed / partially-shaped records: a
 * record it can't interpret is logged and ignored rather than failing the batch.
 */

export interface StreamConsumerDeps {
  repo: Repo;
  /** Postgres client — the objective roll-up driven by a PROJECT progress change lives here (KTD7/U16). */
  db: PgDb;
  /**
   * Skill-embedding dependencies (U3). A SKILL# write re-embeds the skill's
   * desc+body via `embed` and upserts the vector to the org's skill index via
   * `vectors`. Both are injectable so tests mock them without the network and
   * default to the OpenRouter / S3 Vectors runtime clients.
   */
  embed?: (text: string) => Promise<Embedding>;
  vectors?: S3Vectors;
  /**
   * Topic→skill association (U8/U9/U10). A `session.topic` EVT# record drives the
   * FULL pipeline end-to-end: resolve the org, embed the topic, retrieve the
   * org's top-k candidate skills (U8), judge-rerank to the single best skill
   * (U9), and create/merge the idea on that skill — or route the topic to the
   * unassigned bin when nothing clears the floor / the judge rejects (U10).
   * Injectable so tests mock it without the network; defaults to the
   * `ideas/associate` runtime `associateAndFinalize`.
   */
  associateAndFinalize?: (finding: TopicFinding, deps: AssociateDeps) => Promise<PipelineResult>;
  /**
   * Where observability metrics (U23) are written. Defaults to stdout (CloudWatch
   * EMF). Injectable so tests assert the emitted embed / association outcomes
   * without scraping stdout. Embed and association both fail to EMPTY-STATE rather
   * than to a user-visible error, so these counts are what makes a silently
   * un-embedded skill or an always-binned topic observable instead of inferred
   * from an empty ideas list.
   */
  metrics?: MetricSink;
  /**
   * Upper bound on how many records in a single batch are processed concurrently
   * (U4). A seed of every skill × every org writes a BURST of SKILL# records into
   * one shard; each non-hash-skipped record fires an embed. Fully serial
   * (one at a time) is safe but slow; an unbounded `Promise.all` over the batch
   * fans out N simultaneous embed calls and storms the embedding rate limit. This
   * caps the in-flight fan-out so a burst is drained quickly WITHOUT exceeding the
   * embed concurrency the API tolerates. Defaults to `STREAM_EMBED_CONCURRENCY`
   * (env) or {@link DEFAULT_MAX_CONCURRENCY}. The hash-skip (U3) already no-ops
   * unchanged skills, so this cap only bites on the changed subset of a burst.
   */
  maxConcurrency?: number;
}

/**
 * Default in-flight cap for a single stream batch (U4). Chosen below the typical
 * Bedrock per-account embed concurrency so a full-catalog re-seed burst never
 * trips the rate limiter, while still draining a batch far faster than serial.
 * Override per-environment via the `STREAM_EMBED_CONCURRENCY` env var.
 */
export const DEFAULT_MAX_CONCURRENCY = 4;

/** Resolve the effective per-batch concurrency cap (≥1). */
function resolveMaxConcurrency(deps: StreamConsumerDeps): number {
  const fromDeps = deps.maxConcurrency;
  if (typeof fromDeps === 'number' && Number.isFinite(fromDeps) && fromDeps >= 1) {
    return Math.floor(fromDeps);
  }
  const fromEnv = Number(process.env.STREAM_EMBED_CONCURRENCY);
  if (Number.isFinite(fromEnv) && fromEnv >= 1) return Math.floor(fromEnv);
  return DEFAULT_MAX_CONCURRENCY;
}

/**
 * Run `worker` over `items` with at most `limit` in flight at once (U4). A small
 * fixed pool of workers pulls from a shared cursor, so the peak concurrency is
 * exactly `limit` regardless of batch size — bounding the embed fan-out a burst
 * produces. `worker` never rejects (callers wrap their own failures), so the pool
 * cannot leave the batch half-processed.
 */
async function runBounded<T>(
  items: readonly T[],
  limit: number,
  worker: (item: T, index: number) => Promise<void>,
): Promise<void> {
  let cursor = 0;
  const pump = async (): Promise<void> => {
    while (cursor < items.length) {
      const i = cursor++;
      await worker(items[i]!, i);
    }
  };
  const workers = Array.from({ length: Math.min(limit, items.length) }, () => pump());
  await Promise.all(workers);
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

/**
 * The content hash a skill's vector is keyed on (U3 idempotency). Hashes the
 * `description + body` — the only fields that drive the embedding — so a MODIFY
 * that touches unrelated fields (e.g. `files`, `members`) recomputes the SAME
 * hash and is skipped. Bundles / metadata-only records hash an empty body fine.
 */
export function skillContentHash(description: string, body: string): string {
  return createHash('sha256').update(description).update(' ').update(body).digest('hex');
}

/**
 * Re-embed a mutated skill and upsert its vector to the org's skill index (U3).
 * Idempotent on `skillContentHash(description, body)`: if the recomputed hash
 * equals the stored `descHash`, the desc/body did not change, so this is a no-op
 * (no embed call, no vector write) — which is what keeps the bulk seed write of
 * every skill × every org from storming the embedding API. Otherwise it embeds
 * the desc+body, writes the vector keyed `skillVectorKey(org, baseName)` with the
 * `org` (isolation) + `embeddingVersion` + `descHash` metadata, and stamps
 * `descHash`/`embeddingVersion` back onto the skill record. Eventually consistent
 * (the stamp write races no one — the stream is the single re-embed driver).
 *
 * Errors (embed or vector write) are NOT swallowed: they propagate so `consume`
 * marks the record a batch-item-failure and the stream redelivers / DLQs it,
 * rather than leaving the skill silently un-embedded.
 */
async function reembedSkill(
  deps: StreamConsumerDeps,
  org: string,
  skill: Skill,
): Promise<void> {
  const baseName = skill.baseName ?? skill.name;
  const hash = skillContentHash(skill.description ?? '', skill.body ?? '');
  if (hash === skill.descHash) return; // desc/body unchanged — nothing to re-embed.

  const embed = deps.embed ?? defaultEmbed;
  const vectors = deps.vectors ?? getS3Vectors();
  // Embed + vector-write is the failure-prone half (Bedrock throttle, S3 Vectors
  // outage). Emit an `EmbedOutcome` metric (U23) on BOTH paths so a run of
  // failures is countable immediately — the DLQ alarm only fires AFTER retries
  // exhaust, so the failure metric is its leading indicator. The error still
  // propagates so the record becomes a batch-item-failure (retry/DLQ); the metric
  // is observability, not flow control.
  let embeddingVersion: string;
  let vector: number[];
  try {
    const result = await embed(`${skill.description ?? ''}\n\n${skill.body ?? ''}`);
    vector = result.vector;
    embeddingVersion = result.embeddingVersion;
    await vectors.putVectors(SKILL_VECTOR_INDEX, [
      {
        key: skillVectorKey(org, baseName),
        vector,
        metadata: { org, skillBaseName: baseName, embeddingVersion, descHash: hash },
      },
    ]);
  } catch (err) {
    emitEmbedOutcome('failure', deps.metrics);
    throw err;
  }
  emitEmbedOutcome('success', deps.metrics);
  // Stamp the record so the NEXT MODIFY with the same content hash-skips. This
  // write itself re-enters the stream, but the stamp makes that redelivery a
  // no-op (the hash now matches), so it does not loop.
  await deps.repo.putSkill({ ...skill, descHash: hash, embeddingVersion });
}

/**
 * Map a pipeline `RouteOutcome` onto the U23 `AssociationOutcome` metric
 * dimension. The metric was defined over the U8 retrieval outcome
 * (`candidates|unassigned|unresolved`); the consumer now runs the FULL pipeline,
 * whose terminal outcome is `routed|unassigned|unresolved`. A `routed` topic is
 * exactly one that cleared the pre-judge floor (≥1 candidate reached the judge)
 * AND the judge accepted it, so it folds into the existing `candidates`
 * dimension — preserving the `unassigned`/total = association-to-bin rate the
 * plan calls out as the must-be-observable signal (a `routed` is NOT a bin).
 */
function routeOutcomeMetric(outcome: RouteOutcome): AssociationMetricOutcome {
  return outcome === 'routed' ? 'candidates' : outcome;
}

/**
 * Run the FULL topic→skill ideas pipeline for a `session.topic` event (U8→U10).
 * Builds the `TopicFinding` from the event and delegates to
 * `associateAndFinalize`, which resolves the org from the session's project
 * (NEVER `principal.org`), embeds the topic, retrieves the org's top-k
 * candidates (U8), judge-reranks to the single best skill (U9), and either
 * creates/merges the idea on that skill or routes the topic to the unassigned
 * bin (U10). End-to-end: a `session.topic` event now PRODUCES or MERGES an idea
 * (or a bin entry), not just a candidate seam.
 *
 *  - `routed`      → idea created/merged on the chosen skill (move-on-re-eval).
 *  - `unassigned`  → bin entry written (no candidate cleared the floor, or the
 *                    judge rejected / was below the confidence bar).
 *  - `unresolved`  → no-op (org/description unresolvable; never a wrong-org guess).
 *
 * Errors propagate (not swallowed) so the record is redelivered/DLQ'd.
 */
async function associateTopicEvent(
  deps: StreamConsumerDeps,
  event: { sessionId: string; segmentId: string; topicLabel: string; description?: string },
  seq: number,
): Promise<PipelineResult> {
  const associate = deps.associateAndFinalize ?? defaultAssociateAndFinalize;
  const finding: TopicFinding = {
    sessionId: event.sessionId,
    segmentId: event.segmentId,
    topicLabel: event.topicLabel,
    ...(event.description !== undefined ? { description: event.description } : {}),
    seq,
  };
  const result = await associate(finding, {
    repo: deps.repo,
    // `associate` takes a `BedrockEmbedder` (not the raw `embed` fn the skill
    // path uses), so we forward only the shared S3 Vectors client and let the
    // embedder/judge/writer default inside `associateAndFinalize`. Tests mock
    // `associateAndFinalize` itself (or inject the collaborators), so this wiring
    // stays simple.
    ...(deps.vectors ? { vectors: deps.vectors } : {}),
  });
  // Count the association outcome (U23). The `unassigned` rate over total is the
  // association-to-bin rate the plan calls out as the must-be-observable signal —
  // a catalog mis-route or a mis-tuned similarity floor shows here as a rising bin
  // rate instead of an inexplicably empty ideas list. A THROWN association (embed
  // throttle, query/judge outage) never reaches this line — it propagates to the
  // batch-item-failure path and shows up as an `EmbedOutcome=failure` + DLQ depth.
  emitAssociationOutcome(routeOutcomeMetric(result.route.outcome), deps.metrics);
  return result;
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
    // A `session.topic` ALSO drives the FULL topic→skill ideas pipeline
    // (U8→U10): embed the topic, retrieve the org's top-k candidate skills above
    // the floor (U8), judge-rerank to the best skill (U9), and create/merge the
    // idea on that skill — or route to the unassigned bin (U10). Done AFTER the
    // reprojection so the session projection (the org-resolution path) is current.
    if (parsed.data.event.kind === 'session.topic') {
      await associateTopicEvent(deps, parsed.data.event, parsed.data.seq);
    }
    return;
  }

  // NOTE: the objective roll-up is no longer driven by a Dynamo PROJECT progress
  // change (U6) — its single source of truth is reconciled weekly commits, so the
  // recompute runs in `/reconcile/complete` (`rest/weeklyTransitions.ts`).

  // --- Skill record -> re-embed the skill into the org's vector index. -----
  // The live skill item is `SCOPE#org#<org>` / `SKILL#<name>`. The same prefix
  // also covers version side-records (`SKILL#<name>#r<N>` revision snapshots and
  // `SKILL#<name>#TRUE` pointers) — those are NOT current skill bodies, so they
  // are skipped via `isVersionSideRecord` (only the live record carries the
  // canonical desc+body the embedding is built from).
  const org = orgFromScopePartition(pk);
  if (org && sk.startsWith('SKILL#') && !isVersionSideRecord(sk)) {
    const newImg = decode(ddb.NewImage as Img);
    // A skill DELETE clears NewImage — vector teardown is U21, not here.
    if (!newImg || typeof newImg.name !== 'string') return;
    await reembedSkill(deps, org, newImg as unknown as Skill);
    return;
  }

  // Any other record family is irrelevant to projections/roll-ups: skip.
}

/**
 * Process a whole stream batch. Records are independent, so a per-record failure
 * does not abort the batch — BUT it is no longer silently swallowed: the failed
 * record's `eventID` is returned in `batchItemFailures` so the event-source
 * (`reportBatchItemFailures: true`, see api-stack) redelivers just that record
 * and, on exhausted retries, routes it to the DLQ. This is what keeps a skill
 * embed failure (U3) from leaving the skill silently un-embedded. The projection
 * / roll-up folds are idempotent, so redelivering one of them is harmless too.
 *
 * Records are drained with a BOUNDED concurrency (U4): a full-catalog re-seed
 * pushes a burst of SKILL# writes through one shard, and each changed skill fires
 * a Bedrock embed. Processing them with an unbounded `Promise.all` would fan out
 * N simultaneous embed calls and storm the rate limit; processing them strictly
 * serially is needlessly slow. A small fixed worker pool (`maxConcurrency`) caps
 * the in-flight fan-out so the burst drains fast without exceeding the embed
 * concurrency Bedrock tolerates. The U3 hash-skip already no-ops unchanged skills,
 * so this cap only governs the changed subset of a burst.
 */
export async function consume(
  event: DynamoDBStreamEvent,
  deps: StreamConsumerDeps,
): Promise<DynamoDBBatchResponse> {
  const records = event.Records ?? [];
  const batchItemFailures: { itemIdentifier: string }[] = [];
  const limit = resolveMaxConcurrency(deps);
  // The worker NEVER rejects — a per-record failure is captured into
  // `batchItemFailures` here, so the bounded pool always drains the whole batch.
  await runBounded(records, limit, async (record) => {
    try {
      await processRecord(record, deps);
    } catch (err) {
      console.warn('streamConsumer: record failed', {
        eventID: record.eventID,
        reason: (err as Error).message,
      });
      if (record.eventID) batchItemFailures.push({ itemIdentifier: record.eventID });
    }
  });
  return { batchItemFailures };
}

export const handler: DynamoDBStreamHandler = (event) =>
  consume(event, { repo: defaultRepo(), db: getDb() });
