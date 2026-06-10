import {
  type FetchLike,
  embeddingModel,
  openRouterEmbed,
} from '../llm/openrouter.js';

/**
 * U2 — text-embedding client (OpenRouter).
 *
 * Turns text into a stamped 1536-dimension float vector via OpenRouter's
 * OpenAI-compatible `/embeddings` endpoint (default model
 * `openai/text-embedding-3-small`). This is the backend's first model capability
 * — a deliberate departure from the platform's "no server-side embeddings"
 * stance (see the plan's Key Technical Decisions): topic→skill association needs
 * the org-wide skill catalog, which only exists server-side, so the embedding
 * generation runs here. Auth is a bearer API key read from `OPENROUTER_API_KEY`
 * at call time inside the shared client — no key is hardcoded here.
 *
 * Every embedding is stamped with the `embeddingModel`/`embeddingVersion` it was
 * generated with so cross-version comparison can be refused downstream (U5).
 */

/** OpenRouter embedding output dimension; matches the S3 Vectors index (U1). */
export const EMBEDDING_DIMENSION = 1536;

/**
 * Resolve the ACTIVE embedding version stamp — the version every NEW embedding is
 * stamped with and the only version `queryTopK` will compare a query against (U5).
 * Env-overrideable (`OPENROUTER_EMBEDDING_VERSION`) so a reindex (U5) can flip the
 * active version pointer by setting it; defaults to the configured embedding model
 * id (the model IS the version).
 */
export function activeEmbeddingVersion(): string {
  return process.env.OPENROUTER_EMBEDDING_VERSION ?? embeddingModel();
}

/** A stamped embedding: the vector plus the model + version it came from. */
export interface Embedding {
  /** The float embedding; length === EMBEDDING_DIMENSION. */
  vector: number[];
  /** The OpenRouter embedding model id used (e.g. `openai/text-embedding-3-small`). */
  embeddingModel: string;
  /** The version stamp for cross-version-comparison guards (U5). */
  embeddingVersion: string;
}

/**
 * An OpenRouter embedding client. The `fetch` impl is injectable so tests mock it
 * without touching the network; it defaults to the Node 20 global. There is no
 * memoised SDK client — the request is a plain HTTP call whose auth/base come from
 * the environment at call time.
 */
export class OpenRouterEmbedder {
  private fetchImpl?: FetchLike;

  constructor(fetchImpl?: FetchLike) {
    this.fetchImpl = fetchImpl;
  }

  /**
   * Embed `text` into a stamped {@link EMBEDDING_DIMENSION}-dim vector. A request
   * error is NOT swallowed — it propagates so the caller (the stream consumer, U3)
   * can route the failure to retry/DLQ rather than silently producing no vector.
   */
  async embed(text: string): Promise<Embedding> {
    const model = embeddingModel();
    const vector = await openRouterEmbed(text, {
      model,
      ...(this.fetchImpl ? { fetchImpl: this.fetchImpl } : {}),
    });
    if (!Array.isArray(vector) || vector.length !== EMBEDDING_DIMENSION) {
      throw new Error(
        `OpenRouter returned ${
          Array.isArray(vector) ? vector.length : 'no'
        } dimensions; expected ${EMBEDDING_DIMENSION}`,
      );
    }
    return { vector, embeddingModel: model, embeddingVersion: activeEmbeddingVersion() };
  }
}

/** Lazy, memoised default embedder for the Lambda runtime (tests inject their own). */
let defaultEmbedder: OpenRouterEmbedder | undefined;

/** The process-wide default embedder, created on first use. */
export function getEmbedder(): OpenRouterEmbedder {
  defaultEmbedder ??= new OpenRouterEmbedder();
  return defaultEmbedder;
}

/** Convenience: embed via the default embedder (the stream consumer's default). */
export function embed(text: string): Promise<Embedding> {
  return getEmbedder().embed(text);
}
