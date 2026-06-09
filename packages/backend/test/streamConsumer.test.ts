import { beforeEach, describe, expect, it, vi } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { marshall } from '@aws-sdk/util-dynamodb';
import type { DynamoDBRecord, DynamoDBStreamEvent } from 'aws-lambda';
import type { Envelope, Event, Idea, ObjectiveNode, Project, Skill } from '@harness/shared';
import type {
  AssociateDeps,
  PipelineResult,
  TopicFinding,
} from '../src/ideas/associate.js';
import type { RerankJudge } from '../src/rerank/judge.js';
import type { IdeaWriter } from '../src/ideas/synth.js';
import type { OpenRouterEmbedder } from '../src/embeddings/embed.js';
import { Repo } from '../src/db/repo.js';
import {
  consume,
  skillContentHash,
  DEFAULT_MAX_CONCURRENCY,
  type StreamConsumerDeps,
} from '../src/ws/streamConsumer.js';
import { EMBEDDING_DIMENSION, type Embedding } from '../src/embeddings/embed.js';
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
 * The OpenRouter embedder + S3 Vectors client are injected fakes — no network.
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
        embeddingModel: 'openai/text-embedding-3-small',
        embeddingVersion: 'openai/text-embedding-3-small',
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
      embeddingVersion: 'openai/text-embedding-3-small',
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
    expect(stored?.embeddingVersion).toBe('openai/text-embedding-3-small');
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
 * U8→U10 — a `session.topic` EVT# record drives the FULL ideas pipeline
 * end-to-end (`associateAndFinalize`): retrieve top-k candidates (U8) → judge
 * rerank (U9) → create/merge the idea on the chosen skill, or route to the
 * unassigned bin (U10). The first three tests inject `associateAndFinalize` as a
 * spy to assert the consumer wiring (the topic branch fires with the event's
 * finding, the reprojection is undisturbed, a throw routes to a batch-item
 * failure); the `end-to-end` describe below drives the REAL pipeline through the
 * in-memory table with injected OpenRouter collaborators.
 */
describe('stream consumer — topic association branch (U8→U10)', () => {
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
    const fn = vi.fn(async (finding: TopicFinding): Promise<PipelineResult> => {
      calls.push(finding);
      return { route: { outcome: 'routed', org: 'acme', finding, skillBaseName: 's', confidence: 1 } };
    });
    return { fn, calls };
  }

  it('drives the pipeline for a session.topic event with the event finding', async () => {
    // The session must exist so reprojection has something to fold the topic into.
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const { fn: associateAndFinalize, calls } = spyAssociate();

    const res = await consume(streamEvent(eventRecord(topicEnvelope(1))), {
      repo,
      associateAndFinalize,
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
    const { fn: associateAndFinalize, calls } = spyAssociate();

    const msg: Event = { kind: 'assistant.msg', sessionId: SESSION, tokens: 10 };
    await consume(streamEvent(eventRecord(env(1, msg))), { repo, associateAndFinalize });

    expect(calls).toHaveLength(0);
  });

  it('surfaces an association failure as a batch-item-failure (not a silent drop)', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const associateAndFinalize = vi.fn(async (): Promise<PipelineResult> => {
      throw new Error('ThrottlingException');
    });

    const topicRec = eventRecord(topicEnvelope(1));
    const res = await consume(streamEvent(topicRec), { repo, associateAndFinalize });

    expect(res.batchItemFailures).toEqual([{ itemIdentifier: 'evt-1' }]);
  });
});

/**
 * U8→U10 end-to-end through the REAL pipeline (no `associateAndFinalize` spy):
 * a `session.topic` event, fed through the in-memory table with injected OpenRouter
 * collaborators (embedder / vectors / judge / writer), must PRODUCE a real idea
 * on the judge-chosen skill — or write a real unassigned-bin entry when nothing
 * routes. This is the consolidation the wiring exists for: a topic event now
 * creates/merges an idea, not just a candidate seam.
 */
describe('stream consumer — topic pipeline end-to-end (U8→U10)', () => {
  const ORG = 'acme';
  const PROJECT = 'weekly-compass';

  /** Seed the session→project→org chain the pipeline resolves the org from. */
  async function seedSessionAndProject(): Promise<void> {
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    await repo.putProject({
      id: PROJECT,
      name: PROJECT,
      repo: 'gh/acme/wc',
      ownerUserId: 'matt',
      liveSessionCount: 0,
      org: ORG,
    } as unknown as Project);
  }

  function topicRecord(seq: number, description: string): DynamoDBRecord {
    return eventRecord(
      env(seq, {
        kind: 'session.topic',
        sessionId: SESSION,
        segmentId: 'seg-1',
        topicLabel: 'decimal money handling',
        description,
      }),
    );
  }

  /** A deterministic embedder — every text maps to the same unit vector. */
  function fakeEmbedder(): OpenRouterEmbedder {
    return {
      embed: vi.fn(async () => ({
        vector: Array.from({ length: EMBEDDING_DIMENSION }, () => 0.1),
        embeddingModel: 'openai/text-embedding-3-small',
        embeddingVersion: 'openai/text-embedding-3-small',
      })),
    } as unknown as OpenRouterEmbedder;
  }

  /**
   * A vectors fake whose skill query returns the seeded candidate above the
   * floor, and whose idea query returns NO match (so the pipeline CREATES rather
   * than merges). `putVectors` is captured so the idea-vector write is assertable.
   */
  function fakeVectors(candidateSkill: string | null) {
    const puts: { index: string; items: VectorItem[] }[] = [];
    const store = {
      queryTopK: vi.fn(async (index: string) => {
        if (index === SKILL_VECTOR_INDEX && candidateSkill) {
          return [{ key: candidateSkill, score: 0.95, metadata: { skillBaseName: candidateSkill } }];
        }
        return []; // idea index: no near-duplicate → create
      }),
      putVectors: vi.fn(async (index: string, items: VectorItem[]) => {
        puts.push({ index, items });
      }),
    } as unknown as S3Vectors;
    return { store, puts };
  }

  /** A judge that always picks the given skill at high confidence (or `none`). */
  function fakeJudge(pick: string | null): RerankJudge {
    return {
      judge: vi.fn(async () =>
        pick ? { outcome: 'best', skillBaseName: pick, confidence: 0.9 } : { outcome: 'none' },
      ),
    } as unknown as RerankJudge;
  }

  /** A writer that synthesizes deterministic concept text. */
  function fakeWriter(): IdeaWriter {
    return {
      write: vi.fn(async () => 'Use a decimal type for currency, never a float.'),
      merge: vi.fn(async (existing: string) => existing),
    } as unknown as IdeaWriter;
  }

  function metricCapture() {
    const lines: string[] = [];
    return { sink: (l: string) => lines.push(l), lines };
  }
  function outcomes(lines: string[]): { metric: string; outcome: string }[] {
    return lines.map((l) => {
      const obj = JSON.parse(l);
      return { metric: obj._aws.CloudWatchMetrics[0].Metrics[0].Name, outcome: obj.Outcome };
    });
  }

  /** Wire the real `associateAndFinalize` with injected collaborators. */
  function realAssociate(deps: Partial<AssociateDeps>) {
    return async (finding: TopicFinding, base: AssociateDeps): Promise<PipelineResult> => {
      const { associateAndFinalize } = await import('../src/ideas/associate.js');
      return associateAndFinalize(finding, { ...base, ...deps });
    };
  }

  it('creates an idea on the judge-chosen skill (routed) and emits candidates', async () => {
    await seedSessionAndProject();
    // The chosen skill must be readable for loadCandidateDescriptions.
    await repo.putSkill({
      name: 'money-handling',
      scope: { tier: 'org', id: ORG },
      kind: 'skill',
      description: 'Currency + money handling guidance.',
      source: 'local',
      members: [],
      body: '# money',
    } as unknown as Skill);

    const { store: vectors, puts } = fakeVectors('money-handling');
    const { sink, lines } = metricCapture();
    const res = await consume(streamEvent(topicRecord(1, 'Use decimal for currency.')), {
      repo,
      metrics: sink,
      associateAndFinalize: realAssociate({
        embedder: fakeEmbedder(),
        vectors,
        judge: fakeJudge('money-handling'),
        writer: fakeWriter(),
      }),
    });

    expect(res.batchItemFailures).toEqual([]);
    // A real idea was created on the chosen skill.
    const ideas: Idea[] = await repo.listIdeasForOrg(ORG);
    expect(ideas).toHaveLength(1);
    expect(ideas[0]!.skillBaseName).toBe('money-handling');
    expect(ideas[0]!.sources.some((s) => s.sessionId === SESSION)).toBe(true);
    // Its vector was indexed.
    expect(puts.some((p) => p.index !== SKILL_VECTOR_INDEX)).toBe(true);
    // U23 metric still fires; a routed topic counts as `candidates` (reached + passed the judge).
    expect(outcomes(lines)).toContainEqual({ metric: 'AssociationOutcome', outcome: 'candidates' });
  });

  it('writes an unassigned-bin entry when the judge rejects (and emits unassigned)', async () => {
    await seedSessionAndProject();
    await repo.putSkill({
      name: 'money-handling',
      scope: { tier: 'org', id: ORG },
      kind: 'skill',
      description: 'Currency guidance.',
      source: 'local',
      members: [],
      body: '# money',
    } as unknown as Skill);

    const { store: vectors } = fakeVectors('money-handling');
    const { sink, lines } = metricCapture();
    const res = await consume(streamEvent(topicRecord(1, 'Something unrelated.')), {
      repo,
      metrics: sink,
      associateAndFinalize: realAssociate({
        embedder: fakeEmbedder(),
        vectors,
        judge: fakeJudge(null), // judge says none → bin
        writer: fakeWriter(),
      }),
    });

    expect(res.batchItemFailures).toEqual([]);
    // No idea created; a bin entry exists instead.
    expect(await repo.listIdeasForOrg(ORG)).toHaveLength(0);
    const bin = await repo.listUnassignedForOrg(ORG);
    expect(bin).toHaveLength(1);
    expect(bin[0]!.sources[0]!.sessionId).toBe(SESSION);
    expect(outcomes(lines)).toContainEqual({ metric: 'AssociationOutcome', outcome: 'unassigned' });
  });

  it('routes a pipeline failure to batchItemFailures (not a silent drop)', async () => {
    await seedSessionAndProject();
    const throwingEmbedder = {
      embed: vi.fn(async () => {
        throw new Error('ThrottlingException');
      }),
    } as unknown as OpenRouterEmbedder;

    const { store: vectors } = fakeVectors('money-handling');
    const res = await consume(streamEvent(topicRecord(1, 'Use decimal for currency.')), {
      repo,
      associateAndFinalize: realAssociate({
        embedder: throwingEmbedder,
        vectors,
        judge: fakeJudge('money-handling'),
        writer: fakeWriter(),
      }),
    });

    expect(res.batchItemFailures).toEqual([{ itemIdentifier: 'evt-1' }]);
    expect(await repo.listIdeasForOrg(ORG)).toHaveLength(0);
  });
});

/**
 * U23 — observability. Embed + association both fail to EMPTY-STATE, not to a
 * user-visible error, so the consumer emits a CloudWatch EMF metric per outcome.
 * We inject the metric sink and parse the EMF JSON to assert: an embed success
 * emits `EmbedOutcome=success`; a repeated embed FAILURE emits
 * `EmbedOutcome=failure` (the DLQ alarm's leading indicator); and each
 * association emits its `AssociationOutcome` (so the to-bin rate is observable).
 */
describe('stream consumer — observability metrics (U23)', () => {
  const ORG = 'acme';
  const ORG_SCOPE = { tier: 'org' as const, id: ORG };

  function metricCapture() {
    const lines: string[] = [];
    return { sink: (l: string) => lines.push(l), lines };
  }

  /** Parse captured EMF lines into `{ metric, outcome }` pairs. */
  function outcomes(lines: string[]): { metric: string; outcome: string }[] {
    return lines.map((l) => {
      const obj = JSON.parse(l);
      const metric = obj._aws.CloudWatchMetrics[0].Metrics[0].Name;
      return { metric, outcome: obj.Outcome };
    });
  }

  function skill(over: Partial<Skill> = {}): Skill {
    return {
      name: 'reconcile',
      scope: ORG_SCOPE,
      kind: 'skill',
      description: 'Reconcile weekly variance.',
      source: 'local',
      members: [],
      body: '# Reconcile',
      ...over,
    } as Skill;
  }

  function skillRecord(s: Skill): DynamoDBRecord {
    const key = k.skillKey(s.scope, s.name);
    return {
      eventName: 'INSERT',
      eventID: `skill-${s.name}`,
      dynamodb: {
        Keys: marshall(key),
        NewImage: marshall({ ...key, ...s }, { removeUndefinedValues: true }),
      },
    } as unknown as DynamoDBRecord;
  }

  const okEmbed = vi.fn(
    async (): Promise<Embedding> => ({
      vector: Array.from({ length: EMBEDDING_DIMENSION }, () => 0.1),
      embeddingModel: 'openai/text-embedding-3-small',
      embeddingVersion: 'openai/text-embedding-3-small',
    }),
  );
  const okVectors = { putVectors: vi.fn(async () => {}) } as unknown as S3Vectors;

  it('emits EmbedOutcome=success on a successful skill embed', async () => {
    const { sink, lines } = metricCapture();
    await consume(streamEvent(skillRecord(skill())), {
      repo,
      embed: okEmbed,
      vectors: okVectors,
      metrics: sink,
    });
    expect(outcomes(lines)).toContainEqual({ metric: 'EmbedOutcome', outcome: 'success' });
  });

  it('emits EmbedOutcome=failure on each failed embed (DLQ-alarm leading indicator)', async () => {
    const { sink, lines } = metricCapture();
    const embed = vi.fn(async (): Promise<Embedding> => {
      throw new Error('ThrottlingException');
    });
    // Two distinct skills both fail → two failure metrics (repeated failures are
    // observable, not collapsed into one).
    const res = await consume(
      streamEvent(skillRecord(skill()), skillRecord(skill({ name: 'audit' }))),
      { repo, embed, vectors: okVectors, metrics: sink },
    );
    expect(res.batchItemFailures).toHaveLength(2);
    const failures = outcomes(lines).filter((o) => o.outcome === 'failure');
    expect(failures).toHaveLength(2);
    expect(failures.every((f) => f.metric === 'EmbedOutcome')).toBe(true);
  });

  it('emits the AssociationOutcome for a topic association (to-bin rate observable)', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), { repo });
    const { sink, lines } = metricCapture();
    const associateAndFinalize = vi.fn(
      async (finding: TopicFinding): Promise<PipelineResult> => ({
        route: { outcome: 'unassigned', org: ORG, finding, reason: 'judge-none' },
      }),
    );
    const topicEnv = env(1, {
      kind: 'session.topic',
      sessionId: SESSION,
      segmentId: 'seg-1',
      topicLabel: 'decimal money',
      description: 'Use decimal for currency.',
    });
    await consume(streamEvent(eventRecord(topicEnv)), { repo, associateAndFinalize, metrics: sink });

    expect(outcomes(lines)).toContainEqual({ metric: 'AssociationOutcome', outcome: 'unassigned' });
  });
});

/**
 * U4 — seed re-embed WITHOUT a storm. A full re-seed pushes a BURST of SKILL#
 * writes through one shard. Two properties keep that from storming the embed API:
 *
 *  - Re-seeding an UNCHANGED catalog produces zero new embeddings — every skill
 *    already carries the matching `descHash`, so the U3 hash-skip no-ops it (no
 *    embed, no vector write), even across a burst spanning every org.
 *  - A burst is drained with BOUNDED concurrency — at most `maxConcurrency` embed
 *    calls are ever in flight at once, never one-per-record unbounded.
 *
 * And seeding a CHANGED skill across all orgs re-embeds only that one (per org),
 * not the whole catalog — the hash-skip still gates every other skill.
 */
describe('stream consumer — seed re-embed without a storm (U4)', () => {
  const ORGS = ['acme', 'abc', 'test-org'];

  function fakeEmbed() {
    const calls: string[] = [];
    const fn = vi.fn(async (text: string): Promise<Embedding> => {
      calls.push(text);
      return {
        vector: Array.from({ length: EMBEDDING_DIMENSION }, () => 0.1),
        embeddingModel: 'openai/text-embedding-3-small',
        embeddingVersion: 'openai/text-embedding-3-small',
      };
    });
    return { fn, calls };
  }

  function fakeVectors() {
    const puts: { index: string; items: VectorItem[] }[] = [];
    const store = {
      putVectors: vi.fn(async (index: string, items: VectorItem[]) => {
        puts.push({ index, items });
      }),
    } as unknown as S3Vectors;
    return { store, puts };
  }

  function skill(org: string, name: string, over: Partial<Skill> = {}): Skill {
    const description = `Skill ${name} for ${org}.`;
    const body = `# ${name}\nBody for ${name}.`;
    return {
      name,
      scope: { tier: 'org', id: org },
      kind: 'skill',
      description,
      source: 'local',
      members: [],
      body,
      ...over,
    } as Skill;
  }

  /** A MODIFY record for a skill that ALREADY carries its matching descHash. */
  function unchangedSkillRecord(org: string, name: string): DynamoDBRecord {
    const s = skill(org, name, {
      descHash: skillContentHash(`Skill ${name} for ${org}.`, `# ${name}\nBody for ${name}.`),
    });
    const key = k.skillKey(s.scope, s.name);
    return {
      eventName: 'MODIFY',
      eventID: `skill-${org}-${name}`,
      dynamodb: {
        Keys: marshall(key),
        OldImage: marshall({ ...key, ...s }, { removeUndefinedValues: true }),
        NewImage: marshall({ ...key, ...s }, { removeUndefinedValues: true }),
      },
    } as unknown as DynamoDBRecord;
  }

  /** An INSERT record for a freshly-seeded (never-embedded) skill. */
  function freshSkillRecord(org: string, name: string): DynamoDBRecord {
    const s = skill(org, name);
    const key = k.skillKey(s.scope, s.name);
    return {
      eventName: 'INSERT',
      eventID: `skill-${org}-${name}`,
      dynamodb: {
        Keys: marshall(key),
        NewImage: marshall({ ...key, ...s }, { removeUndefinedValues: true }),
      },
    } as unknown as DynamoDBRecord;
  }

  it('re-seeding an unchanged catalog (burst, all orgs) produces zero new embeddings', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    // 3 orgs × 4 skills = a 12-record burst, every one already-stamped.
    const names = ['reconcile', 'audit', 'forecast', 'close'];
    const records = ORGS.flatMap((org) => names.map((n) => unchangedSkillRecord(org, n)));

    const res = await consume(streamEvent(...records), { repo, embed, vectors });

    expect(res.batchItemFailures).toEqual([]);
    expect(calls).toHaveLength(0); // hash-skip: nothing re-embedded
    expect(puts).toHaveLength(0); // no vector writes
  });

  it('re-seeding a changed skill re-embeds only that one across all orgs', async () => {
    const { fn: embed, calls } = fakeEmbed();
    const { store: vectors, puts } = fakeVectors();
    const names = ['reconcile', 'audit', 'forecast', 'close'];
    // Every skill is unchanged EXCEPT `audit`, whose stored hash is stale (its
    // body changed) — so only `audit` re-embeds, once per org.
    const records = ORGS.flatMap((org) =>
      names.map((n) =>
        n === 'audit'
          ? freshSkillRecord(org, n) // no descHash → re-embeds
          : unchangedSkillRecord(org, n),
      ),
    );

    await consume(streamEvent(...records), { repo, embed, vectors });

    expect(calls).toHaveLength(ORGS.length); // exactly one per org, only `audit`
    expect(puts).toHaveLength(ORGS.length);
    // Every vector written is the `audit` skill, one per distinct org.
    const writtenOrgs = puts.map((p) => p.items[0]!.metadata!.org).sort();
    expect(writtenOrgs).toEqual([...ORGS].sort());
    expect(puts.every((p) => p.items[0]!.metadata!.skillBaseName === 'audit')).toBe(true);
  });

  /**
   * A gated embedder: it parks each call on a deferred and counts how many are
   * concurrently parked, so the test can observe the TRUE peak in-flight count
   * under the bounded pool. `drain` repeatedly lets a short real timer fire (so
   * the pool tops up to its cap) and then releases the parked calls, until every
   * one of the N expected calls has been made and resolved.
   */
  function gatedEmbed() {
    let inFlight = 0;
    let peak = 0;
    let made = 0;
    const release: (() => void)[] = [];
    const fn = vi.fn(async (): Promise<Embedding> => {
      made++;
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise<void>((resolve) => release.push(resolve));
      inFlight--;
      return {
        vector: Array.from({ length: EMBEDDING_DIMENSION }, () => 0.1),
        embeddingModel: 'openai/text-embedding-3-small',
        embeddingVersion: 'openai/text-embedding-3-small',
      };
    });
    const tick = () => new Promise<void>((r) => setTimeout(r, 0));
    /** Drive the pool to completion, asserting the cap holds at every settle. */
    async function drain(expectedCalls: number, cap: number): Promise<void> {
      // Bounded loop (no infinite spin if the pool stalls): each pass lets the
      // pool top up, checks the cap, then releases everyone currently parked.
      for (let guard = 0; guard < expectedCalls * 3 && made < expectedCalls; guard++) {
        await tick();
        expect(inFlight).toBeLessThanOrEqual(cap);
        while (release.length > 0) release.shift()!();
      }
      // Flush any final stragglers parked after the last release.
      for (let guard = 0; guard < expectedCalls && (release.length > 0 || inFlight > 0); guard++) {
        await tick();
        while (release.length > 0) release.shift()!();
      }
    }
    return { fn, drain, peak: () => peak };
  }

  it('drains a burst with bounded concurrency (never one embed per record at once)', async () => {
    const { store: vectors, puts } = fakeVectors();
    const g = gatedEmbed();

    const N = DEFAULT_MAX_CONCURRENCY * 4; // a burst well above the cap
    const records = Array.from({ length: N }, (_, i) => freshSkillRecord('acme', `skill-${i}`));
    const done = consume(streamEvent(...records), { repo, embed: g.fn, vectors });

    await g.drain(N, DEFAULT_MAX_CONCURRENCY);

    const res = await done;
    expect(res.batchItemFailures).toEqual([]);
    expect(g.fn).toHaveBeenCalledTimes(N);
    expect(puts).toHaveLength(N);
    expect(g.peak()).toBeLessThanOrEqual(DEFAULT_MAX_CONCURRENCY);
    expect(g.peak()).toBeGreaterThan(1); // proves it is NOT serial
  });

  it('honors an injected maxConcurrency cap', async () => {
    const { store: vectors } = fakeVectors();
    const g = gatedEmbed();

    const cap = 2;
    const N = 8;
    const records = Array.from({ length: N }, (_, i) => freshSkillRecord('acme', `s-${i}`));
    const done = consume(streamEvent(...records), {
      repo,
      embed: g.fn,
      vectors,
      maxConcurrency: cap,
    });

    await g.drain(N, cap);

    await done;
    expect(g.peak()).toBeLessThanOrEqual(cap);
    expect(g.fn).toHaveBeenCalledTimes(N);
  });
});
