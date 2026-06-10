import { describe, expect, it } from 'vitest';
import { memRepoHarness } from './helpers/memtable.js';
import {
  FakeSkillVectors as FakeVectors,
  fakeEmbedder,
  seedSession as seedSessionFor,
} from './helpers/idea-fakes.js';
import {
  associateTopic,
  ASSOCIATION_TOP_K,
  SIMILARITY_FLOOR,
  type AssociateDeps,
  type TopicFinding,
} from '../src/ideas/associate.js';
import { SKILL_VECTOR_INDEX, type QueryHit, type S3Vectors } from '../src/embeddings/s3vectors.js';

/**
 * U8 — topic ingestion & top-k retrieval (the RETRIEVAL half of association).
 *
 * The embedder + S3 Vectors are FAKES (helpers/idea-fakes.ts) so nothing
 * touches the network: the fake index is keyed by org and `queryTopK` honours
 * the `orgFilter` (so org A's query NEVER sees org B's skills) and the floor.
 *
 * Org resolution is the security-critical assertion: it is resolved from the
 * SESSION → PROJECT → stamped `project.org`, NEVER from a caller `principal.org`.
 */

const { repo } = memRepoHarness();

function deps(vectors: FakeVectors): AssociateDeps {
  return { repo, embedder: fakeEmbedder, vectors: vectors as unknown as S3Vectors };
}

const seedSession = (opts: { sessionId: string; projectId: string; org?: string }) =>
  seedSessionFor(repo, opts);

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

describe('associateTopic — top-k retrieval (U8)', () => {
  it('yields org-scoped top-k candidates above the floor', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    vectors.seed([
      { org: 'acme', skillBaseName: 'hq-money', score: 0.95 },
      { org: 'acme', skillBaseName: 'hq-retry', score: 0.72 },
      { org: 'acme', skillBaseName: 'hq-irrelevant', score: 0.2 }, // below floor
    ]);

    const res = await associateTopic(topic(), deps(vectors));

    expect(res.outcome).toBe('candidates');
    expect(res.org).toBe('acme');
    // The query was scoped to the resolved org and the skill index.
    expect(vectors.lastQuery?.index).toBe(SKILL_VECTOR_INDEX);
    expect(vectors.lastQuery?.opts.orgFilter).toBe('acme');
    expect(vectors.lastQuery?.opts.floor).toBe(SIMILARITY_FLOOR);
    // Sorted strongest-first, the below-floor candidate dropped.
    expect(res.candidates.map((c) => c.skillBaseName)).toEqual(['hq-money', 'hq-retry']);
    expect(res.candidates[0]!.score).toBeGreaterThan(res.candidates[1]!.score);
  });

  it('isolates orgs: org A topic never retrieves org B skills', async () => {
    await seedSession({ sessionId: 's-A', projectId: 'p-A', org: 'orgA' });
    const vectors = new FakeVectors();
    // Only org B has a (strongly-matching) skill; org A has none.
    vectors.seed([{ org: 'orgB', skillBaseName: 'b-only-skill', score: 0.99 }]);

    const res = await associateTopic(topic({ sessionId: 's-A' }), deps(vectors));

    expect(res.org).toBe('orgA');
    expect(vectors.lastQuery?.opts.orgFilter).toBe('orgA');
    // No org-A vectors → nothing retrieved → unassigned (org B's skill unseen).
    expect(res.outcome).toBe('unassigned');
    expect(res.candidates).toEqual([]);
  });

  it('returns unassigned when all candidates are below the floor (bin seam)', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    vectors.seed([
      { org: 'acme', skillBaseName: 'hq-weak-a', score: 0.1 },
      { org: 'acme', skillBaseName: 'hq-weak-b', score: 0.3 },
    ]);

    const res = await associateTopic(topic(), deps(vectors));

    expect(res.outcome).toBe('unassigned');
    expect(res.org).toBe('acme');
    expect(res.candidates).toEqual([]);
  });

  it('resolves org from the project, NOT a caller principal.org', async () => {
    // The session's project is stamped org `realorg`. A wrong `principal.org`
    // would be `attacker` — but associate has no principal and must resolve via
    // the project pointer, so the query is scoped to `realorg`.
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'realorg' });
    const vectors = new FakeVectors();
    vectors.seed([{ org: 'realorg', skillBaseName: 'hq-real', score: 0.9 }]);
    // Seed an `attacker`-org skill too — it must NEVER be returned.
    vectors.seed([{ org: 'attacker', skillBaseName: 'hq-attacker', score: 0.99 }]);

    const res = await associateTopic(topic(), deps(vectors));

    expect(res.org).toBe('realorg');
    expect(vectors.lastQuery?.opts.orgFilter).toBe('realorg');
    expect(res.candidates.map((c) => c.skillBaseName)).toEqual(['hq-real']);
  });

  it('is unresolved when the session/project/org cannot be resolved (never a wrong-org guess)', async () => {
    const vectors = new FakeVectors();
    // No session seeded → no pointer → cannot resolve org.
    const res = await associateTopic(topic({ sessionId: 'ghost' }), deps(vectors));

    expect(res.outcome).toBe('unresolved');
    expect(res.org).toBeUndefined();
    // We never even queried (no org to scope to).
    expect(vectors.lastQuery).toBeUndefined();
  });

  it('is unresolved when the project carries no stamped org', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1' }); // no org stamped
    const vectors = new FakeVectors();
    const res = await associateTopic(topic(), deps(vectors));

    expect(res.outcome).toBe('unresolved');
    expect(res.org).toBeUndefined();
    expect(vectors.lastQuery).toBeUndefined();
  });

  it('is unresolved (no-op) when the topic has no embeddable description', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    const res = await associateTopic(topic({ description: '   ' }), deps(vectors));

    expect(res.outcome).toBe('unresolved');
    // Org resolved, but nothing to embed → still never queried.
    expect(res.org).toBe('acme');
    expect(vectors.lastQuery).toBeUndefined();
  });

  it('requests the configured top-k from the index', async () => {
    await seedSession({ sessionId: 's-1', projectId: 'p-1', org: 'acme' });
    const vectors = new FakeVectors();
    let requestedK = -1;
    const spy = {
      async queryTopK(_i: string, _v: number[], k: number) {
        requestedK = k;
        return [] as QueryHit[];
      },
    } as unknown as S3Vectors;

    await associateTopic(topic(), { repo, embedder: fakeEmbedder, vectors: spy });
    expect(requestedK).toBe(ASSOCIATION_TOP_K);
  });
});
