import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { corroborationCount } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import {
  CORROBORATION_K,
  corroborateFinding,
  type Finding,
} from '../src/ideas/corroborate.js';
import type { OpenRouterEmbedder } from '../src/embeddings/embed.js';
import type { S3Vectors, VectorItem, QueryHit, QueryOptions } from '../src/embeddings/s3vectors.js';
import type { IdeaWriter, IdeaFinding } from '../src/ideas/synth.js';

/**
 * U7 — corroboration + within-skill dedup with MERGE-REWRITE. The embedder,
 * idea-writer, and S3 Vectors are FAKES so nothing touches the network:
 *
 *  - The fake embedder maps text to a deterministic vector via a "lesson key" —
 *    the dominant unit-axis. Two phrasings of the same lesson share a key, so
 *    their vectors are identical (similarity 1.0); distinct lessons get
 *    orthogonal axes (similarity 0).
 *  - The fake idea-writer echoes a stable concept per lesson on `write`, and on
 *    `merge` produces a marked re-synthesized concept so the test can assert the
 *    text was actually rewritten.
 *  - The fake S3 Vectors is an in-memory index honouring the org filter +
 *    skillBaseName metadata; cosine similarity is computed on the stored floats.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

const ORG = 'acme';
const SKILL = 'hq-update-skills';

/**
 * Map a finding's content to its "lesson key" — the test's notion of which
 * underlying lesson it expresses. Two phrasings of one lesson share a key.
 */
function lessonKey(content: IdeaFinding): string {
  const text = `${content.description ?? ''} ${(content.implLearnings ?? []).join(' ')}`;
  if (/decimal|money|currency|float/i.test(text)) return 'money';
  if (/retry|backoff|idempot/i.test(text)) return 'retry';
  return 'other';
}

/** A 4-dim one-hot vector keyed on the lesson, so same lesson → identical vector. */
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

/** A fake embedder: text → one-hot lesson vector, stamped like the real one. */
class FakeEmbedder {
  // The embedder receives the SYNTHESIZED concept, which the fake writer encodes
  // as `concept:<lessonKey>` so the lesson survives synthesis.
  async embed(text: string) {
    const key = text.startsWith('concept:') ? text.slice('concept:'.length).split(' ')[0]! : 'other';
    return {
      vector: vectorFor(key),
      embeddingModel: 'fake',
      embeddingVersion: 'fake-v1',
    };
  }
}

/** A fake idea-writer recording merge calls and marking re-synthesized text. */
class FakeWriter {
  mergeCalls: Array<{ existing: string; finding: IdeaFinding }> = [];
  async write(finding: IdeaFinding): Promise<string> {
    // Encode the lesson key into the concept so the fake embedder can recover it.
    return `concept:${lessonKey(finding)}`;
  }
  async merge(existingText: string, finding: IdeaFinding): Promise<string> {
    this.mergeCalls.push({ existing: existingText, finding });
    // Preserve the lesson key prefix so re-embedding stays on the same axis, but
    // mark the text so the test can prove a rewrite happened.
    const key = lessonKey(finding);
    return `concept:${key} [resynth+${this.mergeCalls.length}]`;
  }
}

/** A fake in-memory S3 Vectors index honouring org + skillBaseName filtering. */
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

let embedder: FakeEmbedder;
let writer: FakeWriter;
let vectors: FakeVectors;

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
  embedder = new FakeEmbedder();
  writer = new FakeWriter();
  vectors = new FakeVectors();
});

function deps() {
  return {
    repo,
    embedder: embedder as unknown as OpenRouterEmbedder,
    vectors: vectors as unknown as S3Vectors,
    writer: writer as unknown as IdeaWriter,
  };
}

function finding(over: Partial<Finding> = {}): Finding {
  return {
    org: ORG,
    skillBaseName: SKILL,
    content: { description: 'use decimal money types', implLearnings: ['never float for currency'] },
    sessionId: 's-1',
    segments: [{ segmentId: 'seg-1', seq: 1, snippet: 'raw' }],
    ...over,
  };
}

describe('corroborateFinding — merge-rewrite within threshold', () => {
  it('two phrasings of the same lesson merge into ONE idea with re-synthesized text', async () => {
    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    expect(first.created).toBe(true);

    // A different phrasing of the SAME lesson, from a DIFFERENT session.
    const second = await corroborateFinding(
      finding({
        sessionId: 's-2',
        content: { description: 'prefer currency decimals', implLearnings: ['avoid floats for money'] },
      }),
      deps(),
    );

    expect(second.created).toBe(false); // merged, not a twin
    expect(second.idea.ideaId).toBe(first.idea.ideaId);
    // The text was actually re-synthesized (writer.merge ran and marked it).
    expect(writer.mergeCalls).toHaveLength(1);
    expect(second.idea.text).toContain('[resynth');

    // Exactly one idea exists for the skill.
    const all = await repo.listIdeasForSkill(ORG, SKILL);
    expect(all).toHaveLength(1);
    expect(corroborationCount(all[0]!)).toBe(2);
  });

  it('two DISTINCT lessons stay separate', async () => {
    await corroborateFinding(
      finding({ content: { description: 'use decimal money types', implLearnings: [] } }),
      deps(),
    );
    const other = await corroborateFinding(
      finding({
        sessionId: 's-2',
        content: { description: 'retry with exponential backoff', implLearnings: ['make writes idempotent'] },
      }),
      deps(),
    );
    expect(other.created).toBe(true);

    const all = await repo.listIdeasForSkill(ORG, SKILL);
    expect(all).toHaveLength(2);
    expect(writer.mergeCalls).toHaveLength(0); // nothing merged
  });
});

describe('corroborateFinding — session is the corroboration unit', () => {
  it('the same session across two segments counts ONCE', async () => {
    // First finding from session s-1, segment seg-1.
    await corroborateFinding(finding({ sessionId: 's-1', segments: [{ segmentId: 'seg-1', seq: 1 }] }), deps());

    // The SAME session, a different segment, same lesson → merges but no new session.
    const again = await corroborateFinding(
      finding({ sessionId: 's-1', segments: [{ segmentId: 'seg-2', seq: 5 }] }),
      deps(),
    );

    expect(again.sessionCounted).toBe(false); // AE4 no-op
    expect(again.created).toBe(false);
    // No rewrite for a re-emit from an already-counted session.
    expect(writer.mergeCalls).toHaveLength(0);

    const all = await repo.listIdeasForSkill(ORG, SKILL);
    expect(all).toHaveLength(1);
    expect(corroborationCount(all[0]!)).toBe(1);
  });

  it('multiple segments WITHIN one finding collapse to one source', async () => {
    const res = await corroborateFinding(
      finding({
        sessionId: 's-1',
        segments: [
          { segmentId: 'seg-2', seq: 5, snippet: 'later' },
          { segmentId: 'seg-1', seq: 1, snippet: 'earlier' },
        ],
      }),
      deps(),
    );
    expect(res.idea.sources).toHaveLength(1);
    // The earliest (lowest seq) segment is the representative.
    expect(res.idea.sources[0]!.segmentId).toBe('seg-1');
    expect(res.idea.sources[0]!.seq).toBe(1);
  });
});

describe('corroborateFinding — K flips an idea to corroborated', () => {
  it('the 2nd distinct session flips the idea to corroborated (K=2)', async () => {
    expect(CORROBORATION_K).toBe(2);

    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    expect(first.corroborated).toBe(false);
    expect(corroborationCount(first.idea)).toBe(1);

    const second = await corroborateFinding(
      finding({
        sessionId: 's-2',
        content: { description: 'currency in decimal', implLearnings: ['no float money'] },
      }),
      deps(),
    );
    expect(second.sessionCounted).toBe(true);
    expect(second.corroborated).toBe(true);
    expect(corroborationCount(second.idea)).toBe(2);
  });
});

describe('corroborateFinding — folded-idea lifecycle (U20)', () => {
  /**
   * Fold an idea the way the U16 fold endpoint does: flip status to `folded`,
   * stamp the rev, and write it through the optimistic-concurrency conditional so
   * its corroborationVersion advances exactly as it would in production.
   */
  async function fold(idea: import('@harness/shared').Idea) {
    const folded = { ...idea, status: 'folded' as const, foldedIntoRev: 1, updatedAt: Date.now() };
    const res = await repo.corroborateIdeaConditional(folded, idea.corroborationVersion);
    expect(res.written).toBe(true);
    return (await repo.getIdea(ORG, SKILL, idea.ideaId))!;
  }

  it('a post-fold session attaches as evidence — no reopen, no re-surfacing twin', async () => {
    // Corroborate to K then fold the idea (its lesson is now in the skill body).
    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    await corroborateFinding(
      finding({ sessionId: 's-2', content: { description: 'currency in decimal', implLearnings: ['no float money'] } }),
      deps(),
    );
    const foldedIdea = await fold((await repo.getIdea(ORG, SKILL, first.idea.ideaId))!);
    expect(foldedIdea.status).toBe('folded');
    const liveCountBefore = corroborationCount(foldedIdea);
    writer.mergeCalls = [];

    // A NEW session expresses the SAME (already-folded) lesson.
    const post = await corroborateFinding(
      finding({ sessionId: 's-3', content: { description: 'use decimals for money', implLearnings: ['floats lose cents'] } }),
      deps(),
    );

    // It attached to the existing folded idea as post-fold evidence.
    expect(post.postFoldAttached).toBe(true);
    expect(post.created).toBe(false); // NOT a re-surfacing twin
    expect(post.idea.ideaId).toBe(first.idea.ideaId);
    // Did NOT reopen, did NOT re-synthesize the folded body, did NOT re-count live.
    expect(post.idea.status).toBe('folded');
    expect(post.sessionCounted).toBe(false);
    expect(writer.mergeCalls).toHaveLength(0);

    const stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    expect(stored.status).toBe('folded');
    expect(stored.text).toBe(foldedIdea.text); // body unchanged
    expect(corroborationCount(stored)).toBe(liveCountBefore); // live set unchanged
    // The post-fold session is recorded separately, for HQ history only.
    expect(stored.postFoldSources.map((s) => s.sessionId)).toEqual(['s-3']);

    // Exactly ONE idea exists — no duplicate that could re-surface.
    const all = await repo.listIdeasForSkill(ORG, SKILL);
    expect(all).toHaveLength(1);
  });

  it('a folded idea stays out of the corroborated/open candidate set', async () => {
    const { candidateLearnings } = await import('../src/rest/ideas.js');
    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    await corroborateFinding(
      finding({ sessionId: 's-2', content: { description: 'currency in decimal', implLearnings: ['no float money'] } }),
      deps(),
    );
    let stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    // Before folding it is corroborated and surfaces in the live block.
    expect(candidateLearnings([stored])).toHaveLength(1);

    await fold(stored);
    // A post-fold session lands as evidence.
    await corroborateFinding(
      finding({ sessionId: 's-3', content: { description: 'use decimals for money', implLearnings: ['floats lose cents'] } }),
      deps(),
    );
    stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    // Still folded and still EXCLUDED from candidate-learnings (status gate),
    // regardless of accumulating post-fold evidence — never re-surfaces.
    expect(stored.status).toBe('folded');
    expect(candidateLearnings([stored])).toHaveLength(0);
  });

  it('re-emit of a session already on the folded idea (live or post-fold) is a no-op', async () => {
    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    await corroborateFinding(
      finding({ sessionId: 's-2', content: { description: 'currency in decimal', implLearnings: ['no float money'] } }),
      deps(),
    );
    await fold((await repo.getIdea(ORG, SKILL, first.idea.ideaId))!);

    // s-1 was an ORIGINAL fold session (live `sources`) — re-emitting it post-fold
    // must NOT also record it as post-fold evidence.
    const dupLive = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    expect(dupLive.postFoldAttached).toBe(false);
    let stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    expect(stored.postFoldSources).toHaveLength(0);

    // First post-fold session records once...
    await corroborateFinding(finding({ sessionId: 's-3' }), deps());
    // ...a re-emit of that same post-fold session is a no-op (monotonic).
    const dupPost = await corroborateFinding(finding({ sessionId: 's-3' }), deps());
    expect(dupPost.postFoldAttached).toBe(false);
    stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    expect(stored.postFoldSources.map((s) => s.sessionId)).toEqual(['s-3']);
  });

  it('an OPEN idea still merges normally (folded handling does not change the open path)', async () => {
    const first = await corroborateFinding(finding({ sessionId: 's-1' }), deps());
    expect(first.idea.status).toBe('open');
    const second = await corroborateFinding(
      finding({ sessionId: 's-2', content: { description: 'prefer currency decimals', implLearnings: ['avoid floats for money'] } }),
      deps(),
    );
    expect(second.postFoldAttached).toBe(false);
    expect(second.created).toBe(false); // merged into the open idea
    expect(second.sessionCounted).toBe(true);
    expect(writer.mergeCalls).toHaveLength(1); // open path still re-synthesizes
    const stored = (await repo.getIdea(ORG, SKILL, first.idea.ideaId))!;
    expect(corroborationCount(stored)).toBe(2);
    expect(stored.postFoldSources).toHaveLength(0);
  });
});

describe('corroborateFinding — provenance', () => {
  it('carries projectId/repoId onto the source for variant-scoped folding', async () => {
    const res = await corroborateFinding(
      finding({ projectId: 'proj-1', repoId: 'repo-1' }),
      deps(),
    );
    expect(res.idea.sources[0]).toMatchObject({ projectId: 'proj-1', repoId: 'repo-1' });
  });
});
