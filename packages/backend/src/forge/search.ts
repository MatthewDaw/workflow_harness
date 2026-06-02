import type { ScoredSession, SessionVector } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Embedder } from './embed.js';

/**
 * Forge k-NN search (U27, KTD7).
 *
 * Two backends behind one interface:
 *  - the **brute-force cosine fallback** over the user's DynamoDB-stored vectors
 *    (default; cheap, scales to zero, works before OpenSearch is provisioned);
 *  - an **OpenSearch Serverless** vector index (additive, gated by the
 *    `FORGE_VECTOR_BACKEND=opensearch` flag).
 *
 * The brute-force path is built first per the plan's execution note so Forge
 * works without infra. The config flag chooses the backend; tests exercise the
 * fallback directly with no network.
 */

/** Cosine similarity of two equal-length vectors. Returns 0 for degenerate input. */
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length === 0 || a.length !== b.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    const ai = a[i] as number;
    const bi = b[i] as number;
    dot += ai * bi;
    na += ai * ai;
    nb += bi * bi;
  }
  if (na === 0 || nb === 0) return 0;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

/** Rank a corpus of vectors against a query vector, top-k by cosine. */
export function rankByCosine(query: number[], corpus: SessionVector[], k: number): ScoredSession[] {
  return corpus
    .map((v) => ({
      sessionId: v.sessionId,
      score: cosineSimilarity(query, v.vector),
      summary: v.summary,
      skills: v.skills,
      tools: v.tools,
    }))
    .sort((x, y) => y.score - x.score)
    .slice(0, k);
}

/** A vector search backend. The brute-force fallback and OpenSearch both fit. */
export interface VectorIndex {
  /** Return the top-k sessions most similar to `query` for `userId`. */
  knn(userId: string, query: number[], k: number): Promise<ScoredSession[]>;
}

/**
 * Brute-force cosine k-NN over a user's DynamoDB-stored vectors. Reads the
 * user's whole vector partition and ranks in-process. Acceptable at launch
 * volume; the OpenSearch backend takes over when the flag is flipped.
 */
export class BruteForceIndex implements VectorIndex {
  constructor(private readonly repo: Repo) {}

  async knn(userId: string, query: number[], k: number): Promise<ScoredSession[]> {
    const corpus = await this.repo.listSessionVectors(userId);
    return rankByCosine(query, corpus, k);
  }
}

export interface SearchDeps {
  repo: Repo;
  embedder: Embedder;
  /** Defaults to the brute-force fallback; injected for tests / OpenSearch. */
  index?: VectorIndex;
}

/** Is the OpenSearch backend selected by config? Default is brute-force. */
export function useOpenSearch(): boolean {
  return (process.env.FORGE_VECTOR_BACKEND ?? 'bruteforce') === 'opensearch';
}

/**
 * Embed a free-text description and return the top-k similar sessions for the
 * user. The default backend is the brute-force cosine fallback; an injected
 * `index` (or the config flag, in production wiring) selects OpenSearch.
 */
export async function searchSimilarSessions(
  opts: { userId: string; description: string; k?: number },
  deps: SearchDeps,
): Promise<ScoredSession[]> {
  const k = opts.k ?? 5;
  const query = await deps.embedder.embed(opts.description);
  const index = deps.index ?? new BruteForceIndex(deps.repo);
  return index.knn(opts.userId, query, k);
}
