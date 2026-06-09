import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, SessionProjection, Skill } from '@harness/shared';
import { corroborationCount, orgScope } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import {
  associateAndFinalize,
  finalizeRoute,
  routeTopic,
  type AssociateDeps,
  type RouteResult,
  type TopicFinding,
} from '../src/ideas/associate.js';
import type { OpenRouterEmbedder } from '../src/embeddings/embed.js';
import type { S3Vectors, VectorItem, QueryHit, QueryOptions } from '../src/embeddings/s3vectors.js';
import type { IdeaFinding } from '../src/ideas/synth.js';
import type { JudgeCandidate, JudgeTopic, JudgeVerdict, RerankJudge } from '../src/rerank/judge.js';

/**
 * U10 — idea creation & re-evaluation MOVE semantics (the FINAL pipeline step).
 *
 * `finalizeRoute` maps a `routed` association into the corroborate `Finding` and
 * calls `corroborateFinding` (U7) — create/merge happens there. `associateAndFinalize`
 * composes route (U9) + finalize. The MOVE (R13): a segment that re-evaluates to a
 * DIFFERENT best skill strips the session off the prior skill's idea and adds it to
 * the new one — no double counting.
 *
 * The embedder / idea-writer / S3 Vectors are FAKES so nothing touches the network,
 * mirroring corroborate.test.ts: the writer encodes the lesson key into the concept
 * (`concept:<key>`); the embedder maps that to a one-hot vector, so two phrasings of
 * one lesson share a vector (similarity 1.0) and distinct lessons are orthogonal.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

const ORG = 'acme';

function lessonKey(content: IdeaFinding): string {
  const text = `${content.description ?? ''} ${(content.implLearnings ?? []).join(' ')}`;
  if (/decimal|money|currency|float/i.test(text)) return 'money';
  if (/retry|backoff|idempot/i.test(text)) return 'retry';
  return 'other';
}

function vectorFor(key: string): number[] {
  const axes: Record<string, number[]> = {
    money: [1, 0, 0, 0],
    retry: [0, 1, 0, 0],
    other: [0, 0, 1, 0],
  };
  return axes[key] ?? [0, 0, 0, 1];
}

function cosine(a: number[], b: number[]): number {
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += (a[i] ?? 0) * (b[i] ?? 0);
    na += (a[i] ?? 0) ** 2;
    nb += (b[i] ?? 0) ** 2;
  }
  return na && nb ? dot / (Math.sqrt(na) * Math.sqrt(nb)) : 0;
}

/**
 * A fake embedder used for BOTH the skill-retrieval embed (raw topic text) and
 * the idea-index embed (writer-encoded `concept:<key>`). Either way it resolves
 * the lesson key, so a topic about money and its money concept share a vector.
 */
class FakeEmbedder {
  async embed(text: string) {
    const key = text.startsWith('concept:')
      ? text.slice('concept:'.length).split(' ')[0]!
      : lessonKey({ description: text });
    return { vector: vectorFor(key), embeddingModel: 'fake', embeddingVersion: 'fake-v1' };
  }
}

/** A fake idea-writer encoding the lesson key into the concept (mirrors corroborate.test.ts). */
class FakeWriter {
  async write(finding: IdeaFinding): Promise<string> {
    return `concept:${lessonKey(finding)}`;
  }
  async merge(_existing: string, finding: IdeaFinding): Promise<string> {
    return `concept:${lessonKey(finding)} [resynth]`;
  }
}

/** A fake in-memory S3 Vectors idea index honouring org + skillBaseName metadata. */
class FakeVectors {
  items: VectorItem[] = [];
  async putVectors(_index: string, items: VectorItem[]): Promise<void> {
    for (const it of items) {
      this.items = this.items.filter((x) => x.key !== it.key);
      this.items.push(it);
    }
  }
  async queryTopK(
    _index: string,
    vector: number[],
    k: number,
    opts: QueryOptions = {},
  ): Promise<QueryHit[]> {
    return this.items
      .filter((it) => opts.orgFilter === undefined || it.metadata.org === opts.orgFilter)
      .map((it) => ({ key: it.key, score: cosine(vector, it.vector), metadata: it.metadata }))
      .filter((h) => (opts.floor === undefined ? true : h.score >= opts.floor))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

/** A scripted judge for the end-to-end route → finalize path. */
class FakeJudge {
  lastCandidates?: JudgeCandidate[];
  constructor(private readonly verdict: JudgeVerdict) {}
  async judge(_topic: JudgeTopic, candidates: JudgeCandidate[]): Promise<JudgeVerdict> {
    this.lastCandidates = candidates;
    return this.verdict;
  }
}

let embedder: FakeEmbedder;
let writer: FakeWriter;
let vectors: FakeVectors;

function deps(judge?: FakeJudge): AssociateDeps {
  return {
    repo,
    embedder: embedder as unknown as OpenRouterEmbedder,
    vectors: vectors as unknown as S3Vectors,
    writer: writer as unknown as never,
    ...(judge ? { judge: judge as unknown as RerankJudge } : {}),
  };
}

async function seedSession(opts: {
  sessionId: string;
  projectId: string;
  org: string;
}): Promise<void> {
  const project = {
    id: opts.projectId,
    name: opts.projectId,
    repo: `gh/${opts.org}/${opts.projectId}`,
    ownerUserId: 'matt',
    liveSessionCount: 0,
    org: opts.org,
  } as Project;
  await repo.putProject(project);
  const projection = {
    sessionId: opts.sessionId,
    projectId: opts.projectId,
    name: 'a-session',
    host: 'matt@mbp',
    status: 'live',
    tokens: 0,
    startedAt: 1,
    lastEventAt: 1,
    maxSeq: 0,
  } as SessionProjection;
  await repo.putSessionProjectionConditional(projection, undefined);
}

async function seedSkill(org: string, name: string, description: string): Promise<void> {
  const skill = {
    name,
    scope: orgScope(org),
    kind: 'skill',
    description,
    source: 'built-in',
    members: [],
    body: '',
  } as unknown as Skill;
  await repo.putSkill(skill);
}

function topic(over: Partial<TopicFinding> = {}): TopicFinding {
  return {
    sessionId: 's-1',
    segmentId: 'seg-1',
    topicLabel: 'decimal money handling',
    description: 'Always use a decimal type for currency, never a float.',
    implLearnings: ['Use BigDecimal for money math.'],
    seq: 3,
    ...over,
  };
}

/** A pre-built `routed` RouteResult onto a chosen skill (drives finalizeRoute directly). */
function routed(over: Partial<RouteResult> & { skillBaseName: string }): RouteResult {
  return {
    outcome: 'routed',
    org: ORG,
    projectId: 'p-1',
    repoId: `gh/${ORG}/p-1`,
    confidence: 0.9,
    finding: topic(),
    ...over,
  };
}

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
  embedder = new FakeEmbedder();
  writer = new FakeWriter();
  vectors = new FakeVectors();
});

describe('finalizeRoute — idea creation (U10)', () => {
  it('turns a routed result into a created idea on the chosen skill, with provenance (R12/R15)', async () => {
    const res = await finalizeRoute(routed({ skillBaseName: 'hq-money' }), deps());

    expect(res.corroboration.created).toBe(true);
    expect(res.movedFrom).toEqual([]);

    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    const idea = ideas[0]!;
    expect(idea.skillBaseName).toBe('hq-money');
    expect(idea.org).toBe(ORG);
    expect(idea.status).toBe('open');
    // The synthesized concept (not the raw transcript) is the idea text.
    expect(idea.text).toBe('concept:money');
    // Provenance carried onto the source (R15): session/segment/seq + project/repo.
    expect(idea.sources).toHaveLength(1);
    const src = idea.sources[0]!;
    expect(src.sessionId).toBe('s-1');
    expect(src.segmentId).toBe('seg-1');
    expect(src.seq).toBe(3);
    expect(src.projectId).toBe('p-1');
    expect(src.repoId).toBe(`gh/${ORG}/p-1`);
    expect(src.snippet).toBe('Always use a decimal type for currency, never a float.');
  });

  it('merges a second routed finding of the same lesson onto the same idea (corroborateFinding invoked)', async () => {
    await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic({ sessionId: 's-1' }) }),
      deps(),
    );
    const res = await finalizeRoute(
      routed({
        skillBaseName: 'hq-money',
        finding: topic({
          sessionId: 's-2',
          segmentId: 'seg-2',
          description: 'Never represent currency as a float; use a decimal type.',
        }),
      }),
      deps(),
    );

    expect(res.corroboration.created).toBe(false);
    expect(res.corroboration.sessionCounted).toBe(true);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Two distinct sessions corroborate the one idea.
    expect(corroborationCount(ideas[0]!)).toBe(2);
    expect(res.corroboration.corroborated).toBe(true);
  });

  it('rejects a non-routed RouteResult', async () => {
    await expect(
      finalizeRoute({ outcome: 'unassigned', org: ORG, finding: topic() }, deps()),
    ).rejects.toThrow(/routed/);
  });
});

describe('finalizeRoute — re-evaluation MOVE (R13)', () => {
  it('moves a session from a prior skill to a newly-chosen one (prior loses it, new gains it)', async () => {
    // First evaluation: the segment routes to hq-money.
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    let moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(1);

    // Re-evaluation of the SAME (sessionId, segmentId): now best matches hq-retry.
    // (A different lesson key → a distinct idea on the new skill.)
    const reEval = routed({
      skillBaseName: 'hq-retry',
      finding: topic({
        description: 'Retry transient failures with exponential backoff.',
        implLearnings: ['Cap retries; jitter the backoff.'],
      }),
    });
    const res = await finalizeRoute(reEval, deps());

    // The session was MOVED OFF hq-money...
    expect(res.movedFrom.map((i) => i.skillBaseName)).toEqual(['hq-money']);
    moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(0); // decremented — no double count.

    // ...and ADDED to hq-retry.
    const retryIdeas = await repo.listIdeasForSkill(ORG, 'hq-retry');
    expect(retryIdeas).toHaveLength(1);
    expect(corroborationCount(retryIdeas[0]!)).toBe(1);
    const src = retryIdeas[0]!.sources[0]!;
    expect(src.sessionId).toBe('s-1');
    expect(src.segmentId).toBe('seg-1');
  });

  it('does not move when re-evaluating to the SAME skill (idempotent, no self-strip)', async () => {
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    const res = await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic() }),
      deps(),
    );

    expect(res.movedFrom).toEqual([]);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Same session re-emitted → still counted exactly once (AE4 no-op, no self-strip).
    expect(corroborationCount(ideas[0]!)).toBe(1);
  });

  it('leaves OTHER sessions on the prior skill untouched when one drifts away', async () => {
    // Two distinct sessions corroborate the money idea.
    await finalizeRoute(
      routed({ skillBaseName: 'hq-money', finding: topic({ sessionId: 's-1' }) }),
      deps(),
    );
    await finalizeRoute(
      routed({
        skillBaseName: 'hq-money',
        finding: topic({ sessionId: 's-2', segmentId: 'seg-9' }),
      }),
      deps(),
    );
    let moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(corroborationCount(moneyIdeas[0]!)).toBe(2);

    // Only s-1 drifts to hq-retry.
    await finalizeRoute(
      routed({
        skillBaseName: 'hq-retry',
        finding: topic({ sessionId: 's-1', description: 'Retry with backoff and idempotency.' }),
      }),
      deps(),
    );

    moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    // s-1 stripped, s-2 stays — exactly one session left on money.
    expect(corroborationCount(moneyIdeas[0]!)).toBe(1);
    expect(moneyIdeas[0]!.sources[0]!.sessionId).toBe('s-2');
  });

  it('does not strip a session out of a FOLDED prior idea (U20 immutable history)', async () => {
    await finalizeRoute(routed({ skillBaseName: 'hq-money', finding: topic() }), deps());
    const moneyIdeas = await repo.listIdeasForSkill(ORG, 'hq-money');
    const folded = { ...moneyIdeas[0]!, status: 'folded' as const, foldedIntoRev: 2 };
    await repo.putIdea(folded);

    // Same session re-evaluates to hq-retry — the folded money idea must keep its source.
    const res = await finalizeRoute(
      routed({
        skillBaseName: 'hq-retry',
        finding: topic({ description: 'Retry with backoff.' }),
      }),
      deps(),
    );

    expect(res.movedFrom).toEqual([]); // folded idea was not touched.
    const after = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(after[0]!.status).toBe('folded');
    expect(corroborationCount(after[0]!)).toBe(1); // unchanged.
  });
});

describe('associateAndFinalize — full pipeline (U8→U9→U10)', () => {
  it('routes a clear match and creates the idea on the chosen skill', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: ORG });
    await seedSkill(ORG, 'hq-money', 'Handling money and currency in code.');
    // Pre-seed the skill vector index so retrieval surfaces hq-money above the floor.
    await vectors.putVectors('skills', [
      {
        key: `${ORG}#hq-money`,
        vector: vectorFor('money'),
        metadata: { org: ORG, skillBaseName: 'hq-money', embeddingVersion: 'fake-v1' },
      },
    ]);
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.9 });

    const res = await associateAndFinalize(topic(), deps(judge));

    expect(res.route.outcome).toBe('routed');
    expect(res.route.skillBaseName).toBe('hq-money');
    expect(res.finalize?.corroboration.created).toBe(true);
    const ideas = await repo.listIdeasForSkill(ORG, 'hq-money');
    expect(ideas).toHaveLength(1);
    // Provenance from the resolved project rode through onto the source (R15).
    expect(ideas[0]!.sources[0]!.projectId).toBe('p-1');
    expect(ideas[0]!.sources[0]!.repoId).toBe(`gh/${ORG}/p-1`);
  });

  it('does not finalize when the route is unassigned (bin path, no idea)', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: ORG });
    // No skill vectors seeded → nothing clears the floor → bin.
    const judge = new FakeJudge({ outcome: 'none' });

    const res = await associateAndFinalize(topic(), deps(judge));

    expect(res.route.outcome).toBe('unassigned');
    expect(res.finalize).toBeUndefined();
    expect(await repo.listIdeasForOrg(ORG)).toEqual([]);
  });
});
