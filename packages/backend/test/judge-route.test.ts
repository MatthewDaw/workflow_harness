import { afterEach, describe, expect, it } from 'vitest';
import { memRepoHarness } from './helpers/memtable.js';
import {
  FakeJudge,
  FakeSkillVectors as FakeVectors,
  fakeEmbedder,
  seedSession as seedSessionFor,
  seedSkill as seedSkillFor,
} from './helpers/idea-fakes.js';
import {
  routeTopic,
  type AssociateDeps,
  type TopicFinding,
} from '../src/ideas/associate.js';
import type { S3Vectors } from '../src/embeddings/s3vectors.js';
import type { RerankJudge } from '../src/rerank/judge.js';

/**
 * U9 — judge rerank → best skill or the unassigned bin.
 *
 * The judge is a FAKE (no network): it records the topic + candidates it was
 * handed and returns a scripted verdict, so a test can drive a clear match, a
 * below-confidence pick, or a `none`. The embedder + S3 Vectors are the shared
 * fakes (helpers/idea-fakes.ts). The assertions cover the U9 contract:
 *  - a clear, above-bar match returns the chosen skill (the U10 seam);
 *  - all-low candidates (nothing reaches the judge) → bin;
 *  - the judge `none` → bin;
 *  - a below-confidence pick → bin;
 *  - the bin entry carries topic provenance (R7).
 */

const { repo } = memRepoHarness();

function deps(vectors: FakeVectors, judge: FakeJudge): AssociateDeps {
  return {
    repo,
    embedder: fakeEmbedder,
    vectors: vectors as unknown as S3Vectors,
    judge: judge as unknown as RerankJudge,
  };
}

const seedSession = (opts: { sessionId: string; projectId: string; org: string }) =>
  seedSessionFor(repo, opts);
const seedSkill = (org: string, name: string, description: string) =>
  seedSkillFor(repo, org, name, description);

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
