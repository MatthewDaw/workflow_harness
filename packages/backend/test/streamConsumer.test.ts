import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { marshall } from '@aws-sdk/util-dynamodb';
import type { DynamoDBRecord, DynamoDBStreamEvent } from 'aws-lambda';
import type { Envelope, Event, ObjectiveNode, Project, Skill } from '@harness/shared';
import type { AssociationResult, TopicFinding } from '../src/ideas/associate.js';
import { Repo } from '../src/db/repo.js';
import {
  consume,
  skillContentHash,
  type StreamConsumerDeps,
} from '../src/ws/streamConsumer.js';
import { EMBEDDING_DIMENSION, type Embedding } from '../src/embeddings/bedrock.js';
import {
  SKILL_VECTOR_INDEX,
  skillVectorKey,
  type S3Vectors,
  type VectorItem,
} from '../src/embeddings/s3vectors.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U5/U10 Streams backstop: a DynamoDB-stream record for an appended event must
 * re-fold the session projection (idempotent, converges to the same state the
 * inline ingestion path produced), and a project-progress change must drive the
 * objective roll-up — exactly what "Streams drive projections/roll-ups" promises.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

function deps(): StreamConsumerDeps {
  return { repo };
}

const SESSION = 's-1';
const INSTANCE = 'inst-a';

function env(seq: number, event: Event, ts = 1_700_000_000_000 + seq): Envelope {
  return { v: 1, instanceId: INSTANCE, host: 'matt@mbp', ts, seq, event };
}

const startEvent: Event = {
  kind: 'session.start',
  sessionId: SESSION,
  projectId: 'weekly-compass',
  host: 'matt@mbp',
  name: 'reconcile-variance',
};

/** Build an INSERT stream record carrying an event envelope as its NewImage. */
function eventRecord(envelope: Envelope): DynamoDBRecord {
  const item = { ...k.eventKey(envelope.event.sessionId, envelope.seq), ...envelope };
  return {
    eventName: 'INSERT',
    eventID: `evt-${envelope.seq}`,
    dynamodb: {
      Keys: marshall(k.eventKey(envelope.event.sessionId, envelope.seq)),
      NewImage: marshall(item, { removeUndefinedValues: true }),
    },
  } as unknown as DynamoDBRecord;
}

/** Build a MODIFY stream record for a project META item (progress change). */
function projectRecord(
  oldProject: Record<string, unknown> | undefined,
  newProject: Record<string, unknown>,
): DynamoDBRecord {
  const id = newProject.id as string;
  return {
    eventName: oldProject ? 'MODIFY' : 'INSERT',
    eventID: `proj-${id}`,
    dynamodb: {
      Keys: marshall(k.projectKey(id)),
      ...(oldProject
        ? {
            OldImage: marshall(
              { ...k.projectKey(id), ...oldProject },
              { removeUndefinedValues: true },
            ),
          }
        : {}),
      NewImage: marshall({ ...k.projectKey(id), ...newProject }, { removeUndefinedValues: true }),
    },
  } as unknown as DynamoDBRecord;
}

function streamEvent(...records: DynamoDBRecord[]): DynamoDBStreamEvent {
  return { Records: records };
}

describe('stream consumer — session projection backstop', () => {
  it('re-folds an appended event into the session projection', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.name).toBe('reconcile-variance');
    expect(proj?.projectId).toBe('weekly-compass');
    expect(proj?.maxSeq).toBe(0);
  });

  it('advances latest-activity fields as later events stream through', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());
    const msg: Event = {
      kind: 'assistant.msg',
      sessionId: SESSION,
      tokens: 200,
    };
    await consume(streamEvent(eventRecord(env(1, msg))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.maxSeq).toBe(1);
    expect(proj?.tokens).toBe(200);
  });

  it('is idempotent: re-processing the same event record does not regress state', async () => {
    const msg: Event = {
      kind: 'assistant.msg',
      sessionId: SESSION,
      tokens: 200,
    };
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());
    await consume(streamEvent(eventRecord(env(1, msg))), deps());
    // Redeliver seq 1 (Streams at-least-once) — must be a no-op.
    await consume(streamEvent(eventRecord(env(1, msg))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.maxSeq).toBe(1);
    expect(proj?.tokens).toBe(200);
  });

  it('tolerates non-event / malformed records without throwing', async () => {
    const noise: DynamoDBRecord = {
      eventName: 'INSERT',
      eventID: 'noise-1',
      dynamodb: {
        Keys: marshall({ PK: 'CONN#abc', SK: 'META' }),
        NewImage: marshall({ PK: 'CONN#abc', SK: 'META', connectionId: 'abc' }),
      },
    } as unknown as DynamoDBRecord;
    const remove: DynamoDBRecord = {
      eventName: 'REMOVE',
      eventID: 'rm-1',
      dynamodb: { Keys: marshall(k.eventKey(SESSION, 9)) },
    } as unknown as DynamoDBRecord;

    await expect(consume(streamEvent(noise, remove), deps())).resolves.toMatchObject({
      batchItemFailures: [],
    });
    // Nothing projected from noise.
    expect(await repo.getSessionById(SESSION)).toBeUndefined();
  });
});

describe('stream consumer — objective roll-up driver', () => {
  const ORG = 'acme';

  function node(id: string, level: ObjectiveNode['level'], parentId?: string): ObjectiveNode {
    return { id, org: ORG, level, title: id, parentId };
  }

  async function seedTree(): Promise<void> {
    await repo.putObjective(node('rally', 'rally_cry'));
    await repo.putObjective(node('out', 'outcome', 'rally'));
    await repo.putObjective(node('so-a', 'supporting_outcome', 'out'));
    await repo.putObjective(node('so-b', 'supporting_outcome', 'out'));
  }

  const baseProject = (
    progressPct?: number,
  ): Project & { org: string; supportingOutcomeIds: string[] } => ({
    id: 'weekly-compass',
    name: 'weekly-compass',
    repo: 'gh/acme/wc',
    ownerUserId: 'matt',
    liveSessionCount: 0,
    org: ORG,
    supportingOutcomeIds: ['so-a'],
    ...(progressPct === undefined ? {} : { progressPct }),
  });

  it('recomputes the org roll-up when a project progress increases', async () => {
    await seedTree();
    // The stored project must be readable by recomputeOrgRollup.
    await repo.putProject(baseProject(100));

    await consume(streamEvent(projectRecord(baseProject(0), baseProject(100))), deps());

    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(100);
    expect((await repo.getObjective(ORG, 'out'))?.pct).toBe(50); // so-a 100, so-b 0
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(50);
  });

  it('skips recompute when no roll-up-relevant field changed', async () => {
    await seedTree();
    await repo.putProject(baseProject(100));
    // Same progress + same SOs on both images: name-only churn -> no recompute.
    const oldImg = { ...baseProject(100), name: 'old-name' };
    const newImg = { ...baseProject(100), name: 'new-name' };

    await consume(streamEvent(projectRecord(oldImg, newImg)), deps());

    // No roll-up was driven, so the objective pct stays absent (unwritten).
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBeUndefined();
  });

  it('skips a project record carrying no org (cannot place into a tree)', async () => {
    await seedTree();
    const noOrg = {
      id: 'p2',
      name: 'p2',
      repo: 'gh/x/y',
      ownerUserId: 'matt',
      liveSessionCount: 0,
      progressPct: 100,
    };
    await expect(
      consume(streamEvent(projectRecord({ ...noOrg, progressPct: 0 }, noOrg)), deps()),
    ).resolves.toMatchObject({ batchItemFailures: [] });
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBeUndefined();
  });
});

/**
 * U3 — re-embed a skill on every mutation, async off the stream, idempotent on a
 * `description + body` content hash. A SKILL# INSERT embeds + writes a vector; a
 * MODIFY whose desc/body is unchanged (same hash) skips; a changed description
 * re-embeds; `#TRUE`/`#r<N>` version side-records are ignored; and an embed
 * failure routes to the batch-item-failure list (retry/DLQ), not a silent drop.
 * The Bedrock embedder + S3 Vectors client are injected fakes — no network.
 */
describe('stream consumer — skill embedding on write (U3)', () => {
  const ORG = 'acme';
  const ORG_SCOPE = { tier: 'org' as const, id: ORG };

  /** A tracked fake embedder: records inputs, returns a deterministic vector. */
  function fakeEmbed() {
    const calls: string[] = [];
    const fn = vi.fn(async (text: string): Promise<Embedding> => {
      calls.push(text);
      return {
        vector: Array.from({ length: EMBEDDING_DIMENSION }, () => 0.1),
        embeddingModel: 'amazon.titan-embed-text-v2:0',
        embeddingVersion: 'titan-embed-text-v2',
      };
    });
    return { fn, calls };
  }

  /** A tracked fake S3 Vectors store capturing every put (no network). */
  function fakeVectors() {
    const puts: { index: string; items: VectorItem[] }[] = [];
    const store = {
      putVectors: vi.fn(async (index: string, items: VectorItem[]) => {
        puts.push({ index, items });
      }),
    } as unknown as S3Vectors;
    return { store, puts };
  }

  function skill(over: Partial<Skill> = {}): Skill {
    return {
      name: 'reconcile',
      scope: ORG_SCOPE,
      kind: 'skill',
      description: 'Reconcile weekly variance against the budget.',
      source: 'local',
      members: [],
      body: '# Reconcile\nSteps to reconcile.',
      ...over,
    } as Skill;
  }

  /** Build a stream record for a SKILL# item (live record, a revision, or TRUE). */
  function skillRecord(
    s: Skill,
    opts: { sk?: string; eventName?: 'INSERT' | 'MODIFY'; old?: Skill } = {},
  ): DynamoDBRecord {
    const key = opts.sk
      ? { PK: `SCOPE#org#${ORG}`, SK: opts.sk }
      : k.skillKey(s.scope, s.name);
    return {
      eventName: opts.eventName ?? 'INSERT',
      eventID: `skill-${s.name}-${opts.sk ?? 'live'}`,
      dynamodb: {
        Keys: marshall(key),
        ...(opts.old
          ? { OldImage: marshall({ ...key, ...opts.old }, { removeUndefinedValues: true }) }
          : {}),
        NewImage: marshall({ ...key, ...s }, { removeUndefinedValues: true }),
      },
    } as unknown as DynamoDBRecord;
  }

  it('embeds a SKILL# INSERT and writes a stamped vector to the org skill index', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    const s = skill();

    const res = await consume(streamEvent(skillRecord(s)), { repo, embed, vectors });

    expect(res.batchItemFailures).toEqual([]);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain(s.description);
    expect(calls[0]).toContain(s.body);

    expect(puts).toHaveLength(1);
    expect(puts[0]!.index).toBe(SKILL_VECTOR_INDEX);
    const item = puts[0]!.items[0]!;
    expect(item.key).toBe(skillVectorKey(ORG, 'reconcile'));
    expect(item.vector).toHaveLength(EMBEDDING_DIMENSION);
    expect(item.metadata).toMatchObject({
      org: ORG,
      skillBaseName: 'reconcile',
      embeddingVersion: 'titan-embed-text-v2',
      descHash: skillContentHash(s.description, s.body),
    });
  });

  it('keys the vector on baseName for a forked variant record', async () => {
    const { fn: embed } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    // A fork's live record carries the variant name but stamps baseName.
    const s = skill({ name: 'reconcile#R#repo1#U#matt', baseName: 'reconcile' });

    await consume(streamEvent(skillRecord(s)), { repo, embed, vectors });

    expect(puts[0]!.items[0]!.key).toBe(skillVectorKey(ORG, 'reconcile'));
    expect(puts[0]!.items[0]!.metadata).toMatchObject({ skillBaseName: 'reconcile' });
  });

  it('skips a MODIFY whose desc+body hash is unchanged (no embed, no put)', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    const base = skill();
    // The record already carries the matching descHash (as the stamp write left it).
    const stamped = skill({ descHash: skillContentHash(base.description, base.body) });

    const res = await consume(
      streamEvent(skillRecord(stamped, { eventName: 'MODIFY', old: base })),
      { repo, embed, vectors },
    );

    expect(res.batchItemFailures).toEqual([]);
    expect(calls).toHaveLength(0);
    expect(puts).toHaveLength(0);
  });

  it('re-embeds a MODIFY whose description changed (hash differs)', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    const before = skill();
    // Same stored descHash as the OLD content, but the NEW description differs.
    const changed = skill({
      description: 'Reconcile variance AND flag anomalies over 5%.',
      descHash: skillContentHash(before.description, before.body),
    });

    await consume(streamEvent(skillRecord(changed, { eventName: 'MODIFY', old: before })), {
      repo,
      embed,
      vectors,
    });

    expect(calls).toHaveLength(1);
    expect(calls[0]).toContain('flag anomalies');
    expect(puts).toHaveLength(1);
    expect(puts[0]!.items[0]!.metadata).toMatchObject({
      descHash: skillContentHash(changed.description, changed.body),
    });
  });

  it('stamps descHash + embeddingVersion back onto the skill record', async () => {
    const { fn: embed } = fakeEmbed();
    const { store: vectors } = fakeVectors();
    const s = skill();

    await consume(streamEvent(skillRecord(s)), { repo, embed, vectors });

    const stored = await repo.getSkill(ORG_SCOPE, 'reconcile');
    expect(stored?.descHash).toBe(skillContentHash(s.description, s.body));
    expect(stored?.embeddingVersion).toBe('titan-embed-text-v2');
  });

  it('ignores #TRUE pointer and #r<N> revision side-records (no embed)', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    const s = skill();

    await consume(
      streamEvent(
        skillRecord(s, { sk: 'SKILL#reconcile#TRUE' }),
        skillRecord(s, { sk: 'SKILL#reconcile#r000000000001' }),
      ),
      { repo, embed, vectors },
    );

    expect(calls).toHaveLength(0);
    expect(puts).toHaveLength(0);
  });

  it('routes an embed failure to batchItemFailures (retry/DLQ, not silent drop)', async () => {
    const embed = vi.fn(async (): Promise<Embedding> => {
      throw new Error('ThrottlingException');
    });
    const { store: vectors, puts } = fakeVectors();
    const s = skill();

    const res = await consume(streamEvent(skillRecord(s)), { repo, embed, vectors });

    expect(res.batchItemFailures).toEqual([{ itemIdentifier: `skill-reconcile-live` }]);
    expect(puts).toHaveLength(0); // never reached the vector write
    // The record was NOT stamped, so a redelivery re-attempts the embed.
    expect((await repo.getSkill(ORG_SCOPE, 'reconcile'))?.descHash).toBeUndefined();
  });

  it('routes a vector-write failure to batchItemFailures', async () => {
    const { fn: embed } = fakeEmbed();
    const vectors = {
      putVectors: vi.fn(async () => {
        throw new Error('ServiceUnavailableException');
      }),
    } as unknown as S3Vectors;
    const s = skill();

    const res = await consume(streamEvent(skillRecord(s)), { repo, embed, vectors });

    expect(res.batchItemFailures).toEqual([{ itemIdentifier: `skill-reconcile-live` }]);
  });
});

/**
 * U8 — a `session.topic` EVT# record ALSO triggers topic→skill association
 * (the retrieval half). The injected `associateTopic` is a spy so the test
 * asserts the topic branch fires with the event's finding, WITHOUT disturbing
 * the existing reprojection branch (the projection still folds the topic). The
 * spy stands in for the network-touching `ideas/associate` runtime.
 */
describe('stream consumer — topic association branch (U8)', () => {
  function topicEnvelope(seq: number): Envelope {
    return env(seq, {
      kind: 'session.topic',
      sessionId: SESSION,
      segmentId: 'seg-1',
      topicLabel: 'decimal money handling',
      description: 'Always use a decimal type for currency, never a float.',
    });
  }

  function spyAssociate() {
    const calls: TopicFinding[] = [];
    const fn = vi.fn(async (finding: TopicFinding): Promise<AssociationResult> => {
      calls.push(finding);
      return { outcome: 'candidates', org: 'acme', finding, candidates: [] };
    });
    return { fn, calls };
  }

  it('fires association for a session.topic event with the event finding', async () => {
    // The session must exist so reprojection has something to fold the topic into.
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const { fn: associateTopic, calls } = spyAssociate();

    const res = await consume(streamEvent(eventRecord(topicEnvelope(1))), {
      repo,
      associateTopic,
    });

    expect(res.batchItemFailures).toEqual([]);
    expect(calls).toHaveLength(1);
    expect(calls[0]).toMatchObject({
      sessionId: SESSION,
      segmentId: 'seg-1',
      topicLabel: 'decimal money handling',
      description: 'Always use a decimal type for currency, never a float.',
      seq: 1,
    });
    // The reprojection branch was NOT disturbed: the topic folded into the projection.
    const proj = await repo.getSessionById(SESSION);
    expect(proj?.topic).toBe('decimal money handling');
    expect(proj?.maxSeq).toBe(1);
  });

  it('does NOT fire association for a non-topic event', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const { fn: associateTopic, calls } = spyAssociate();

    const msg: Event = { kind: 'assistant.msg', sessionId: SESSION, tokens: 10 };
    await consume(streamEvent(eventRecord(env(1, msg))), { repo, associateTopic });

    expect(calls).toHaveLength(0);
  });

  it('surfaces an association failure as a batch-item-failure (not a silent drop)', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const associateTopic = vi.fn(async (): Promise<AssociationResult> => {
      throw new Error('ThrottlingException');
    });

    const topicRec = eventRecord(topicEnvelope(1));
    const res = await consume(streamEvent(topicRec), { repo, associateTopic });

    expect(res.batchItemFailures).toEqual([{ itemIdentifier: 'evt-1' }]);
  });
});
