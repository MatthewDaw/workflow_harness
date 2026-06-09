import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, SessionProjection } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import {
  associateTopic,
  ASSOCIATION_TOP_K,
  SIMILARITY_FLOOR,
  type AssociateDeps,
  type TopicFinding,
} from '../src/ideas/associate.js';
import type { BedrockEmbedder } from '../src/embeddings/bedrock.js';
import { SKILL_VECTOR_INDEX, type QueryHit, type QueryOptions, type S3Vectors } from '../src/embeddings/s3vectors.js';

/**
 * U8 — topic ingestion & top-k retrieval (the RETRIEVAL half of association).
 *
 * The embedder + S3 Vectors are FAKES so nothing touches the network:
 *  - the fake embedder echoes a deterministic vector per topic so the test can
 *    steer which org's skill it "matches";
 *  - the fake S3 Vectors is an in-memory index keyed by org; `queryTopK` honours
 *    the `orgFilter` (so org A's query NEVER sees org B's skills) and the floor.
 *
 * Org resolution is the security-critical assertion: it is resolved from the
 * SESSION → PROJECT → stamped `project.org`, NEVER from a caller `principal.org`.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

/** A stored skill vector in the fake index: keyed by org, carries a score. */
interface StoredVector {
  org: string;
  skillBaseName: string;
  score: number;
}

/** A fake S3 Vectors index: returns pre-seeded hits for the queried org only. */
class FakeVectors {
  // org -> the hits to return for any query in that org (already "scored").
  private byOrg = new Map<string, StoredVector[]>();
  lastQuery?: { index: string; opts: QueryOptions };

  seed(vectors: StoredVector[]): void {
    for (const v of vectors) {
      const list = this.byOrg.get(v.org) ?? [];
      list.push(v);
      this.byOrg.set(v.org, list);
    }
  }

  async queryTopK(
    indexName: string,
    _vector: number[],
    k: number,
    opts: QueryOptions = {},
  ): Promise<QueryHit[]> {
    this.lastQuery = { index: indexName, opts };
    // ISOLATION: only ever return vectors stamped with the queried org. A query
    // with no org filter returns nothing (the caller always passes one).
    const org = opts.orgFilter;
    const pool = org ? this.byOrg.get(org) ?? [] : [];
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

/** A fake embedder — returns a fixed vector (the fake index ignores it). */
const fakeEmbedder = {
  async embed() {
    return { vector: [1, 0, 0, 0], embeddingModel: 'fake', embeddingVersion: 'fake-v1' };
  },
} as unknown as BedrockEmbedder;

function deps(vectors: FakeVectors): AssociateDeps {
  return { repo, embedder: fakeEmbedder, vectors: vectors as unknown as S3Vectors };
}

/** Seed a session pointer + projection under a project stamped with `org`. */
async function seedSession(opts: {
  sessionId: string;
  projectId: string;
  org?: string;
}): Promise<void> {
  const project: Project = {
    id: opts.projectId,
    name: opts.projectId,
    repo: `gh/${opts.org ?? 'x'}/${opts.projectId}`,
    ownerUserId: 'matt',
    liveSessionCount: 0,
    ...(opts.org !== undefined ? { org: opts.org } : {}),
  } as Project;
  await repo.putProject(project);

  const projection: SessionProjection = {
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
