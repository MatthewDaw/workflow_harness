import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';

/**
 * U2 — Bedrock Titan Text Embeddings v2 client.
 *
 * Turns text into a stamped 1024-dimension float vector via Amazon Bedrock
 * Runtime. This is the backend's first model capability — a deliberate
 * departure from the platform's "no server-side embeddings" stance (see the
 * plan's Key Technical Decisions): topic→skill association needs the org-wide
 * skill catalog, which only exists server-side, so the embedding generation
 * runs here. Auth is IAM/Bedrock runtime — there is NO API key and none is
 * read; the Lambda role grants `bedrock:InvokeModel`.
 *
 * Every embedding is stamped with the `embeddingModel`/`embeddingVersion` it was
 * generated with so cross-version comparison can be refused downstream (U5).
 */

/** The Titan v2 model id. Overridable via env so a reindex (U5) can pin a model. */
const DEFAULT_MODEL_ID = 'amazon.titan-embed-text-v2:0';
/** Titan v2 default output dimension; matches the S3 Vectors index (U1). */
export const EMBEDDING_DIMENSION = 1024;
/**
 * The version stamp written alongside each vector. It is the model id by default
 * (the model id IS the version for Titan), but kept as its own knob so a future
 * config change can bump the stamp without changing the model id. U5's reindex
 * compares this stamp and refuses to mix versions.
 */
const DEFAULT_EMBEDDING_VERSION = 'titan-embed-text-v2';

/** A stamped embedding: the vector plus the model + version it came from. */
export interface Embedding {
  /** The float embedding; length === EMBEDDING_DIMENSION. */
  vector: number[];
  /** The Bedrock model id used (e.g. `amazon.titan-embed-text-v2:0`). */
  embeddingModel: string;
  /** The version stamp for cross-version-comparison guards (U5). */
  embeddingVersion: string;
}

/** Resolve the configured model id (env overrideable; defaults to Titan v2). */
function modelId(): string {
  return process.env.BEDROCK_EMBEDDING_MODEL_ID ?? DEFAULT_MODEL_ID;
}

/**
 * Resolve the ACTIVE embedding version stamp — the version every NEW embedding is
 * stamped with and the only version `queryTopK` will compare a query against (U5).
 * Env-overrideable (`BEDROCK_EMBEDDING_VERSION`) so a reindex (U5) can flip the
 * active version pointer by setting it; defaults to Titan v2.
 */
export function activeEmbeddingVersion(): string {
  return process.env.BEDROCK_EMBEDDING_VERSION ?? DEFAULT_EMBEDDING_VERSION;
}

/** The Titan v2 invoke-request body shape. */
interface TitanRequest {
  inputText: string;
  dimensions: number;
  normalize: boolean;
}

/** The Titan v2 invoke-response body shape (the fields we read). */
interface TitanResponse {
  embedding: number[];
  inputTextTokenCount?: number;
}

/**
 * A Bedrock Titan embedding client. The Bedrock Runtime client is created lazily
 * and memoised across warm Lambda invocations (mirrors `rest/runtime`), and is
 * injectable so tests mock it without touching the network. Region/credentials
 * come from the environment/IAM role — `new BedrockRuntimeClient({})`.
 */
export class BedrockEmbedder {
  private client: BedrockRuntimeClient;

  constructor(client?: BedrockRuntimeClient) {
    this.client = client ?? new BedrockRuntimeClient({});
  }

  /**
   * Embed `text` into a stamped 1024-dim vector. A Bedrock error is NOT
   * swallowed — it propagates so the caller (the stream consumer, U3) can route
   * the failure to retry/DLQ rather than silently producing no vector.
   */
  async embed(text: string): Promise<Embedding> {
    const model = modelId();
    const body: TitanRequest = {
      inputText: text,
      dimensions: EMBEDDING_DIMENSION,
      normalize: true,
    };
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: model,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify(body),
      }),
    );
    // The Bedrock SDK returns the body as a Uint8Array; decode + parse it.
    const decoded = new TextDecoder().decode(res.body);
    const parsed = JSON.parse(decoded) as TitanResponse;
    const vector = parsed.embedding;
    if (!Array.isArray(vector) || vector.length !== EMBEDDING_DIMENSION) {
      throw new Error(
        `Titan returned ${
          Array.isArray(vector) ? vector.length : 'no'
        } dimensions; expected ${EMBEDDING_DIMENSION}`,
      );
    }
    return { vector, embeddingModel: model, embeddingVersion: activeEmbeddingVersion() };
  }
}

/** Lazy, memoised default embedder for the Lambda runtime (tests inject their own). */
let defaultEmbedder: BedrockEmbedder | undefined;

/** The process-wide default embedder, created on first use. */
export function getEmbedder(): BedrockEmbedder {
  defaultEmbedder ??= new BedrockEmbedder();
  return defaultEmbedder;
}

/** Convenience: embed via the default embedder. */
export function embed(text: string): Promise<Embedding> {
  return getEmbedder().embed(text);
}
