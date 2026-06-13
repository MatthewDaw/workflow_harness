/**
 * mat-150-session-recurrence-disabled.test.ts — MAT-150 (R1) acceptance checklist.
 *
 * KTD (plan §Key Technical Decisions):
 *   "The existing session-recurrence pipeline is repointed, not run in parallel.
 *   Today's `topic-mining → associate → corroborate` (plan 008) mints ideas from
 *   raw session topics by recurrence. That input is switched off as an
 *   idea-creator: the inferred lane's only idea source is merged-PR distillation
 *   (U1). Session topics survive solely as enrichment context (U7) — never
 *   idea-creating votes — otherwise un-verified session ideas would leak in beside
 *   the PR-gated ones."
 *
 * Acceptance checklist (Linear MAT-150):
 *  [x] test_recurring_session_topic_does_not_create_idea
 *  [x] Inferred-lane idea source is merged-PR distillation only (asserted via
 *      zero ideas after N topic events across N sessions)
 *  [x] Session topics still usable as enrichment context (U7) — the session
 *      projection is still updated with the topic label/description
 *  [x] No regression to existing folded ideas (existing ideas are untouched)
 *
 * All tests are OFFLINE: in-memory repo, injected spies — no DynamoDB, no network.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';
import { marshall } from '@aws-sdk/util-dynamodb';
import type { DynamoDBRecord, DynamoDBStreamEvent } from 'aws-lambda';
import type { Envelope, Event, Idea, Project } from '@harness/shared';
import type {
  AssociateDeps,
  PipelineResult,
  TopicFinding,
} from '../src/ideas/associate.js';
import { consume, type StreamConsumerDeps } from '../src/ws/streamConsumer.js';
import * as k from '../src/db/keys.js';
import { memRepoHarness } from './helpers/memtable.js';

// ---------------------------------------------------------------------------
// Shared fixtures
// ---------------------------------------------------------------------------

const { repo } = memRepoHarness();

const SESSION = 's-mat150';
const INSTANCE = 'inst-mat150';
const ORG = 'acme-mat150';
const PROJECT_ID = 'proj-mat150';

function env(seq: number, event: Event, ts = 1_800_000_000_000 + seq): Envelope {
  return { v: 1, instanceId: INSTANCE, host: 'dev@host', ts, seq, event };
}

const startEvent: Event = {
  kind: 'session.start',
  sessionId: SESSION,
  projectId: PROJECT_ID,
  host: 'dev@host',
  name: 'mat-150-test-session',
};

function eventRecord(envelope: Envelope): DynamoDBRecord {
  const item = { ...k.eventKey(envelope.event.sessionId, envelope.seq), ...envelope };
  return {
    eventName: 'INSERT',
    eventID: `evt-mat150-${envelope.seq}`,
    dynamodb: {
      Keys: marshall(k.eventKey(envelope.event.sessionId, envelope.seq)),
      NewImage: marshall(item, { removeUndefinedValues: true }),
    },
  } as unknown as DynamoDBRecord;
}

function streamEvent(...records: DynamoDBRecord[]): DynamoDBStreamEvent {
  return { Records: records };
}

function topicEnvelope(seq: number, label: string, description: string): Envelope {
  return env(seq, {
    kind: 'session.topic',
    sessionId: SESSION,
    segmentId: `seg-${seq}`,
    topicLabel: label,
    description,
  });
}

/** Seed the session projection and the owning project (so org is resolvable). */
async function seedSessionAndProject(): Promise<void> {
  await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
  await repo.putProject({
    id: PROJECT_ID,
    name: PROJECT_ID,
    repo: 'gh/acme/wc',
    ownerUserId: 'matt',
    liveSessionCount: 0,
    org: ORG,
  } as unknown as Project);
}

/** A pipeline spy that tracks whether it was called and would create an idea. */
function spyPipeline() {
  const calls: TopicFinding[] = [];
  const fn = vi.fn(async (finding: TopicFinding): Promise<PipelineResult> => {
    calls.push(finding);
    // Would return a 'routed' result — but since R1 disables the call, this
    // body is never reached in normal operation.
    return {
      route: {
        outcome: 'routed',
        org: ORG,
        finding,
        skillBaseName: 'some-skill',
        confidence: 0.95,
      },
    };
  });
  return { fn, calls };
}

function deps(extra: Partial<StreamConsumerDeps> = {}): StreamConsumerDeps {
  return { repo, ...extra };
}

// ---------------------------------------------------------------------------
// MAT-150 acceptance tests
// ---------------------------------------------------------------------------

describe('MAT-150 (R1) — session-recurrence pipeline disabled as idea-creator', () => {
  beforeEach(async () => {
    await seedSessionAndProject();
  });

  /**
   * Primary acceptance test: `test_recurring_session_topic_does_not_create_idea`.
   *
   * A topic that recurs across multiple session segments must NOT create any idea.
   * Before R1, each `session.topic` event drove the full `associate → corroborate`
   * pipeline, eventually minting a folded idea once K distinct sessions saw the
   * same topic. R1 switches that off: the only inferred-lane idea source is
   * merged-PR distillation (U1).
   */
  it('test_recurring_session_topic_does_not_create_idea — recurring session topic leaves idea store empty', async () => {
    const { fn: associateAndFinalize } = spyPipeline();

    // Simulate K recurring topic events (the old pipeline would fold at K=2).
    for (let i = 1; i <= 3; i++) {
      const rec = eventRecord(
        topicEnvelope(i, 'decimal money handling', 'Always use a decimal type for currency, never a float.'),
      );
      const res = await consume(streamEvent(rec), deps({ associateAndFinalize }));
      // Each topic event must succeed without a batch-item-failure.
      expect(res.batchItemFailures).toEqual([]);
    }

    // R1: the pipeline was never called — raw topic recurrence is not an idea-creator.
    expect(associateAndFinalize).not.toHaveBeenCalled();

    // R1: the idea store is still empty after N recurring topic events.
    const ideas: Idea[] = await repo.listIdeasForOrg(ORG);
    expect(ideas).toHaveLength(0);
  });

  /**
   * Inferred-lane idea source is merged-PR distillation only.
   *
   * Even when the injected `associateAndFinalize` would happily create ideas,
   * the consumer must not call it. The idea store must remain empty no matter
   * how many distinct session topics arrive.
   */
  it('inferred-lane idea source is merged-PR distillation only — many topic events, zero ideas', async () => {
    const topics = [
      ['decimal money', 'Use decimal for currency'],
      ['retry logic', 'Implement exponential backoff'],
      ['auth tokens', 'Always validate JWT expiry'],
      ['error handling', 'Never swallow exceptions silently'],
    ];

    const { fn: associateAndFinalize } = spyPipeline();

    let seq = 1;
    for (const [label, desc] of topics) {
      const rec = eventRecord(topicEnvelope(seq++, label!, desc!));
      await consume(streamEvent(rec), deps({ associateAndFinalize }));
    }

    // R1: pipeline never called for any topic.
    expect(associateAndFinalize).not.toHaveBeenCalled();
    // R1: idea store is empty — all topics are enrichment only, not idea-creators.
    expect(await repo.listIdeasForOrg(ORG)).toHaveLength(0);
  });

  /**
   * Session topics survive as enrichment context (U7).
   *
   * Even with idea creation disabled, the session projection must still be updated
   * with the topic label and description. U7 reads this for distillation enrichment
   * at PR-merge time.
   */
  it('session topics survive as enrichment — session projection is still updated', async () => {
    const { fn: associateAndFinalize } = spyPipeline();

    const rec = eventRecord(
      topicEnvelope(1, 'decimal money handling', 'Always use a decimal type for currency.'),
    );
    const res = await consume(streamEvent(rec), deps({ associateAndFinalize }));

    expect(res.batchItemFailures).toEqual([]);

    // The session projection must carry the latest topic (U7 enrichment).
    const proj = await repo.getSessionById(SESSION);
    expect(proj?.topic).toBe('decimal money handling');
    expect(proj?.maxSeq).toBe(1);

    // But no idea was created.
    expect(await repo.listIdeasForOrg(ORG)).toHaveLength(0);
  });

  /**
   * No regression to existing folded ideas.
   *
   * Ideas that already exist in the store (from the PR-gated inferred lane or
   * the authored lane) must be untouched by incoming topic events. Topic events
   * must not modify, merge into, or corrupt existing ideas.
   */
  it('no regression to existing ideas — pre-existing ideas are untouched by topic events', async () => {
    // Plant a pre-existing idea (simulating one from the PR-gated inferred lane).
    const preExistingIdea: Idea = {
      ideaId: 'idea-preexisting-001',
      skillBaseName: 'money-handling',
      org: ORG,
      text: 'Always use a decimal type for currency, never a float.',
      sources: [{ sessionId: 'some-other-session', segmentId: 'seg-0', seq: 0, snippet: '' }],
      postFoldSources: [],
      status: 'open',
      corroborationVersion: 0,
      createdAt: Date.now(),
      updatedAt: Date.now(),
    };
    const { written } = await repo.corroborateIdeaConditional(preExistingIdea, undefined);
    expect(written).toBe(true);

    const { fn: associateAndFinalize } = spyPipeline();

    // A recurring topic event on the same subject must not touch the existing idea.
    for (let i = 1; i <= 2; i++) {
      await consume(
        streamEvent(
          eventRecord(topicEnvelope(i, 'decimal money handling', 'Always use decimal for currency.')),
        ),
        deps({ associateAndFinalize }),
      );
    }

    // The pipeline was not called.
    expect(associateAndFinalize).not.toHaveBeenCalled();

    // The pre-existing idea is unchanged (same corroborationVersion, same sources).
    const ideas: Idea[] = await repo.listIdeasForOrg(ORG);
    expect(ideas).toHaveLength(1);
    expect(ideas[0]!.ideaId).toBe('idea-preexisting-001');
    expect(ideas[0]!.corroborationVersion).toBe(0); // unchanged
    expect(ideas[0]!.sources).toHaveLength(1);       // no new session source added
  });

  /**
   * Non-topic events are still processed normally.
   *
   * R1 only disables the topic-based idea-creation path. Other event types
   * (session.start, assistant.msg, etc.) must continue to be handled correctly.
   */
  it('non-topic events are processed normally and do not call associateAndFinalize', async () => {
    const { fn: associateAndFinalize, calls } = spyPipeline();

    const msgEvent = env(1, { kind: 'assistant.msg', sessionId: SESSION, tokens: 500 });
    const res = await consume(streamEvent(eventRecord(msgEvent)), deps({ associateAndFinalize }));

    expect(res.batchItemFailures).toEqual([]);
    expect(calls).toHaveLength(0);

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.tokens).toBe(500);
    expect(proj?.maxSeq).toBe(1);
  });
});

// ---------------------------------------------------------------------------
// Source-text assertion: the disable flag is wired correctly
// ---------------------------------------------------------------------------

describe('MAT-150 (R1) — source-text assertions', () => {
  it('streamConsumer.ts contains the MAT-150 disable guard around associateTopicEvent', () => {
    const fs = require('node:fs') as typeof import('node:fs');
    const path = require('node:path') as typeof import('node:path');
    const { fileURLToPath } = require('node:url') as typeof import('node:url');

    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const src = fs.readFileSync(
      path.join(__dirname, '../src/ws/streamConsumer.ts'),
      'utf-8',
    );

    // The guard function must exist.
    expect(src).toContain('isSessionTopicIdeaCreationDisabled');
    // The guard must wrap the associateTopicEvent call.
    expect(src).toContain('!isSessionTopicIdeaCreationDisabled()');
    // The session.topic branch must reference the guard (not always call the pipeline).
    expect(src).toMatch(/session\.topic.*isSessionTopicIdeaCreationDisabled|isSessionTopicIdeaCreationDisabled.*session\.topic/s);
    // MAT-150 attribution comment must be present.
    expect(src).toContain('MAT-150');
  });

  it('streamConsumer.ts does NOT call associateTopicEvent unconditionally on session.topic', () => {
    const fs = require('node:fs') as typeof import('node:fs');
    const path = require('node:path') as typeof import('node:path');
    const { fileURLToPath } = require('node:url') as typeof import('node:url');

    const __dirname = path.dirname(fileURLToPath(import.meta.url));
    const src = fs.readFileSync(
      path.join(__dirname, '../src/ws/streamConsumer.ts'),
      'utf-8',
    );

    // The old unconditional call pattern must be absent.
    // Old pattern: `if (parsed.data.event.kind === 'session.topic') {`
    // without the disable guard in the same condition.
    const unconditionalPattern =
      /if\s*\(parsed\.data\.event\.kind\s*===\s*'session\.topic'\s*\)\s*\{/;
    expect(src).not.toMatch(unconditionalPattern);
  });
});
