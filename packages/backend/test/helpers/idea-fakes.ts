import type { Project, SessionProjection, Skill } from '@harness/shared';
import { orgScope } from '@harness/shared';
import type { Repo } from '../../src/db/repo.js';
import type { OpenRouterEmbedder } from '../../src/embeddings/embed.js';
import type { QueryHit, QueryOptions, VectorItem } from '../../src/embeddings/s3vectors.js';
import type { IdeaFinding } from '../../src/ideas/synth.js';
import type { JudgeCandidate, JudgeTopic, JudgeVerdict } from '../../src/rerank/judge.js';

/**
 * Shared fakes for the skill-idea loop tests (associate / corroborate /
 * finalize / judge-route / cross-org isolation). Nothing touches the network:
 *
 *  - `lessonKey`/`vectorFor` map a finding to a deterministic one-hot vector, so
 *    two phrasings of one lesson share a vector (similarity 1.0) and distinct
 *    lessons are orthogonal.
 *  - `FakeEmbedder` resolves the lesson key from either raw topic text or the
 *    writer-encoded `concept:<key>`; the constant `fakeEmbedder` returns a fixed
 *    vector for tests where the fake index ignores it.
 *  - `FakeSkillVectors` is a pre-seeded skill index honouring orgFilter + floor;
 *    `FakeIdeaVectors` is a writable idea index scoring by cosine. Both record
 *    their queries so isolation tests can assert the org filter was carried.
 *  - `FakeWriter` echoes a stable concept per lesson on `write` and marks
 *    re-synthesized text on `merge` (recorded in `mergeCalls`).
 */

/** Map a finding's content to the underlying lesson it expresses. */
export function lessonKey(content: IdeaFinding): string {
  const text = `${content.description ?? ''} ${(content.implLearnings ?? []).join(' ')}`;
  if (/decimal|money|currency|float/i.test(text)) return 'money';
  if (/retry|backoff|idempot/i.test(text)) return 'retry';
  return 'other';
}

/** A 4-dim one-hot vector keyed on the lesson, so same lesson → identical vector. */
export function vectorFor(key: string): number[] {
  const axes: Record<string, number[]> = {
    money: [1, 0, 0, 0],
    retry: [0, 1, 0, 0],
    other: [0, 0, 1, 0],
  };
  return axes[key] ?? [0, 0, 0, 1];
}

export function cosine(a: number[], b: number[]): number {
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
 * A fake embedder for BOTH the skill-retrieval embed (raw topic text) and the
 * idea-index embed (writer-encoded `concept:<key>`); either way it resolves the
 * lesson key, so a topic and its synthesized concept share a vector.
 */
export class FakeEmbedder {
  async embed(text: string) {
    const key = text.startsWith('concept:')
      ? text.slice('concept:'.length).split(' ')[0]!
      : lessonKey({ description: text });
    return { vector: vectorFor(key), embeddingModel: 'fake', embeddingVersion: 'fake-v1' };
  }
}

/** A fixed-vector embedder for tests whose fake index ignores the vector. */
export const fakeEmbedder = {
  async embed() {
    return { vector: [1, 0, 0, 0], embeddingModel: 'fake', embeddingVersion: 'fake-v1' };
  },
} as unknown as OpenRouterEmbedder;

/** A stored skill vector in the fake index: keyed by org, carries a score. */
export interface StoredVector {
  org: string;
  skillBaseName: string;
  score: number;
}

/**
 * A fake skill index returning pre-seeded hits for the queried org ONLY: a
 * query with no orgFilter returns nothing (the real S3 filter does the same
 * server-side). Records every query for isolation assertions.
 */
export class FakeSkillVectors {
  private byOrg = new Map<string, StoredVector[]>();
  queries: Array<{ index: string; opts: QueryOptions }> = [];

  get lastQuery(): { index: string; opts: QueryOptions } | undefined {
    return this.queries[this.queries.length - 1];
  }

  seed(vectors: StoredVector | StoredVector[]): void {
    for (const v of Array.isArray(vectors) ? vectors : [vectors]) {
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
    this.queries.push({ index: indexName, opts });
    const pool = opts.orgFilter ? (this.byOrg.get(opts.orgFilter) ?? []) : [];
    return pool
      .filter((v) => (opts.floor === undefined ? true : v.score >= opts.floor))
      .map((v) => ({
        key: `${v.org}#${v.skillBaseName}`,
        score: v.score,
        metadata: { org: v.org, skillBaseName: v.skillBaseName },
      }))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

/** A fake in-memory idea index honouring org filtering, scored by cosine. */
export class FakeIdeaVectors {
  items: VectorItem[] = [];
  queries: QueryOptions[] = [];

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
    this.queries.push(opts);
    return this.items
      .filter((it) => opts.orgFilter === undefined || it.metadata.org === opts.orgFilter)
      .map((it) => ({ key: it.key, score: cosine(vector, it.vector), metadata: it.metadata }))
      .filter((h) => (opts.floor === undefined ? true : h.score >= opts.floor))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

/** A fake idea-writer recording merge calls and marking re-synthesized text. */
export class FakeWriter {
  mergeCalls: Array<{ existing: string; finding: IdeaFinding }> = [];

  async write(finding: IdeaFinding): Promise<string> {
    // Encode the lesson key into the concept so the fake embedder can recover it.
    return `concept:${lessonKey(finding)}`;
  }

  async merge(existingText: string, finding: IdeaFinding): Promise<string> {
    this.mergeCalls.push({ existing: existingText, finding });
    // Preserve the lesson key prefix so re-embedding stays on the same axis, but
    // mark the text so a test can prove a rewrite happened.
    return `concept:${lessonKey(finding)} [resynth+${this.mergeCalls.length}]`;
  }
}

/** A scripted judge: records its inputs, returns the verdict it was built with. */
export class FakeJudge {
  lastTopic?: JudgeTopic;
  lastCandidates?: JudgeCandidate[];
  constructor(private readonly verdict: JudgeVerdict) {}
  async judge(topic: JudgeTopic, candidates: JudgeCandidate[]): Promise<JudgeVerdict> {
    this.lastTopic = topic;
    this.lastCandidates = candidates;
    return this.verdict;
  }
}

/** Seed a session pointer + projection under a project stamped with `org`. */
export async function seedSession(
  repo: Repo,
  opts: { sessionId: string; projectId: string; org?: string },
): Promise<void> {
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

/** Seed an org-scope catalog skill (for judge hydration of candidate descriptions). */
export async function seedSkill(
  repo: Repo,
  org: string,
  name: string,
  description: string,
): Promise<void> {
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
