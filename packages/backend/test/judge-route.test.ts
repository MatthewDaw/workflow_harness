import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, SessionProjection, Skill } from '@harness/shared';
import { orgScope } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import {
  routeTopic,
  type AssociateDeps,
  type TopicFinding,
} from '../src/ideas/associate.js';
import type { BedrockEmbedder } from '../src/embeddings/bedrock.js';
import type { QueryHit, QueryOptions, S3Vectors } from '../src/embeddings/s3vectors.js';
import type { JudgeCandidate, JudgeTopic, JudgeVerdict, RerankJudge } from '../src/rerank/judge.js';

/**
 * U9 — judge rerank → best skill or the unassigned bin.
 *
 * The judge is a FAKE (no network): it records the topic + candidates it was
 * handed and returns a scripted verdict, so a test can drive a clear match, a
 * below-confidence pick, or a `none`. The embedder + S3 Vectors are fakes too
 * (mirrors associate.test.ts). The assertions cover the U9 contract:
 *  - a clear, above-bar match returns the chosen skill (the U10 seam);
 *  - all-low candidates (nothing reaches the judge) → bin;
 *  - the judge `none` → bin;
 *  - a below-confidence pick → bin;
 *  - the bin entry carries topic provenance (R7).
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

interface StoredVector {
  org: string;
  skillBaseName: string;
  score: number;
}

/** A fake skill index scoped by org (isolation honoured), mirroring associate.test.ts. */
class FakeVectors {
  private byOrg = new Map<string, StoredVector[]>();
  seed(vectors: StoredVector[]): void {
    for (const v of vectors) {
      const list = this.byOrg.get(v.org) ?? [];
      list.push(v);
      this.byOrg.set(v.org, list);
    }
  }
  async queryTopK(
    _index: string,
    _vector: number[],
    k: number,
    opts: QueryOptions = {},
  ): Promise<QueryHit[]> {
    const pool = opts.orgFilter ? this.byOrg.get(opts.orgFilter) ?? [] : [];
    const floor = opts.floor;
    return pool
      .filter((v) => (floor === undefined ? true : v.score >= floor))
      .map((v) => ({
        key: `${v.org}#${v.skillBaseName}`,
        score: v.score,
        metadata: { org: v.org, skillBaseName: v.skillBaseName },
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

const fakeEmbedder = {
  async embed() {
    return { vector: [1, 0, 0, 0], embeddingModel: 'fake', embeddingVersion: 'fake-v1' };
  },
} as unknown as BedrockEmbedder;

/** A scripted judge: records its inputs, returns the verdict it was constructed with. */
class FakeJudge {
  lastTopic?: JudgeTopic;
  lastCandidates?: JudgeCandidate[];
  constructor(private readonly verdict: JudgeVerdict) {}
  async judge(topic: JudgeTopic, candidates: JudgeCandidate[]): Promise<JudgeVerdict> {
    this.lastTopic = topic;
    this.lastCandidates = candidates;
    return this.verdict;
  }
}

function deps(vectors: FakeVectors, judge: FakeJudge): AssociateDeps {
  return {
    repo,
    embedder: fakeEmbedder,
    vectors: vectors as unknown as S3Vectors,
    judge: judge as unknown as RerankJudge,
  };
}

async function seedSession(opts: { sessionId: string; projectId: string; org: string }): Promise<void> {
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
    seq: 3,
    ...over,
  };
}

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

afterEach(() => {
  delete process.env.JUDGE_CONFIDENCE_BAR;
});

describe('routeTopic — judge rerank (U9)', () => {
  it('routes a clear above-bar match to the chosen skill (the U10 seam)', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    await seedSkill('acme', 'hq-money', 'Handling money and currency in code.');
    await seedSkill('acme', 'hq-retry', 'Retry and backoff guidance.');
    const vectors = new FakeVectors();
    vectors.seed([
      { org: 'acme', skillBaseName: 'hq-money', score: 0.95 },
      { org: 'acme', skillBaseName: 'hq-retry', score: 0.72 },
    ]);
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.9 });

    const res = await routeTopic(topic(), deps(vectors, judge));

    expect(res.outcome).toBe('routed');
    expect(res.skillBaseName).toBe('hq-money');
    expect(res.confidence).toBe(0.9);
    expect(res.org).toBe('acme');
    // No bin entry was written on a successful route.
    expect(res.binEntry).toBeUndefined();
    expect(await repo.listUnassignedForOrg('acme')).toEqual([]);
  });

  it('hydrates candidate descriptions for the judge', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    await seedSkill('acme', 'hq-money', 'Handling money and currency in code.');
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'acme', skillBaseName: 'hq-money', score: 0.95 }]);
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.9 });

    await routeTopic(topic(), deps(vectors, judge));

    expect(judge.lastTopic?.topicLabel).toBe('decimal money handling');
    expect(judge.lastCandidates).toEqual([
      { skillBaseName: 'hq-money', description: 'Handling money and currency in code.' },
    ]);
  });

  it('routes all-low candidates (nothing reaches the judge) to the bin', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'acme', skillBaseName: 'hq-weak', score: 0.1 }]);
    // The judge should never be consulted when nothing clears the floor.
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-weak', confidence: 0.99 });

    const res = await routeTopic(topic(), deps(vectors, judge));

    expect(res.outcome).toBe('unassigned');
    expect(res.reason).toBe('no-candidates');
    expect(judge.lastCandidates).toBeUndefined(); // judge not called
    const bin = await repo.listUnassignedForOrg('acme');
    expect(bin).toHaveLength(1);
  });

  it('routes a judge `none` to the bin', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    await seedSkill('acme', 'hq-money', 'Handling money.');
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'acme', skillBaseName: 'hq-money', score: 0.95 }]);
    const judge = new FakeJudge({ outcome: 'none' });

    const res = await routeTopic(topic(), deps(vectors, judge));

    expect(res.outcome).toBe('unassigned');
    expect(res.reason).toBe('judge-none');
    expect(await repo.listUnassignedForOrg('acme')).toHaveLength(1);
  });

  it('routes a below-confidence pick to the bin', async () => {
    process.env.JUDGE_CONFIDENCE_BAR = '0.8';
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    await seedSkill('acme', 'hq-money', 'Handling money.');
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'acme', skillBaseName: 'hq-money', score: 0.95 }]);
    const judge = new FakeJudge({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.5 });

    const res = await routeTopic(topic(), deps(vectors, judge));

    expect(res.outcome).toBe('unassigned');
    expect(res.reason).toBe('below-confidence');
    expect(res.skillBaseName).toBeUndefined();
    expect(await repo.listUnassignedForOrg('acme')).toHaveLength(1);
  });

  it('the bin entry carries topic provenance (R7)', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'acme', skillBaseName: 'hq-weak', score: 0.1 }]);
    const judge = new FakeJudge({ outcome: 'none' });

    const res = await routeTopic(
      topic({ sessionId: 's-1', segmentId: 'seg-9', seq: 7 }),
      deps(vectors, judge),
    );

    const entry = res.binEntry!;
    expect(entry.org).toBe('acme');
    expect(entry.text).toBe('Always use a decimal type for currency, never a float.');
    expect(entry.sources).toHaveLength(1);
    const src = entry.sources[0]!;
    expect(src.sessionId).toBe('s-1');
    expect(src.segmentId).toBe('seg-9');
    expect(src.seq).toBe(7);
    expect(src.projectId).toBe('p-1');
    expect(src.repoId).toBe('gh/acme/p-1');
    // Snippet is the topic description (raw provenance).
    expect(src.snippet).toBe('Always use a decimal type for currency, never a float.');
    // And it is durably written to the bin.
    const stored = await repo.listUnassignedForOrg('acme');
    expect(stored[0]!.entryId).toBe(entry.entryId);
  });

  it('is unresolved (no-op, nothing written) when the org cannot be resolved', async () => {
    const vectors = new FakeVectors();
    const judge = new FakeJudge({ outcome: 'none' });
    const res = await routeTopic(topic({ sessionId: 'ghost' }), deps(vectors, judge));

    expect(res.outcome).toBe('unresolved');
    expect(res.binEntry).toBeUndefined();
    expect(judge.lastCandidates).toBeUndefined();
  });
});
