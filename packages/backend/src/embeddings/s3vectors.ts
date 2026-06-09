import {
  DeleteVectorsCommand,
  GetVectorsCommand,
  PutVectorsCommand,
  QueryVectorsCommand,
  S3VectorsClient,
} from '@aws-sdk/client-s3vectors';
import type { DocumentType } from '@smithy/types';
import { activeEmbeddingVersion } from './bedrock.js';

/**
 * U2 — Amazon S3 Vectors read/write client.
 *
 * The store/search substrate the skill-idea loop runs over. U1's `VectorsStack`
 * provisions ONE vector bucket (`command-hq-skill-idea-vectors`) with two fixed
 * indexes — `skills` and `ideas` — float32, 1024-dim, cosine. Orgs are isolated
 * at QUERY TIME via an `org` filterable metadata key (not per-org indexes), so
 * every put stamps `org` into metadata and every query filters on it (U8/U22).
 *
 * The bucket name is read from the environment (set by ApiStack when it wires
 * this in at U3/U8) — NOT hardcoded in logic — falling back to U1's literal so
 * the module is usable before the env is plumbed. The index name (`skills` /
 * `ideas`) is passed by the caller, since the two corpora are distinct.
 *
 * Auth is IAM (the `command-hq-skill-idea-vectors-put-query` managed policy);
 * there is no API key.
 */

/** U1's provisioned bucket name — the fallback when the env var is unset. */
const DEFAULT_VECTOR_BUCKET = 'command-hq-skill-idea-vectors';
/**
 * The S3 Vectors PutVectors request rejects batches that exceed the service's
 * per-request capacity; the documented per-request maximum is 500 vectors, so we
 * chunk writes at this boundary.
 */
export const PUT_BATCH_LIMIT = 500;

/**
 * U1's two fixed index names within the single vector bucket. Orgs are isolated
 * at query time via the `org` metadata filter, NOT per-org indexes (see header).
 */
export const SKILL_VECTOR_INDEX = 'skills';
export const IDEA_VECTOR_INDEX = 'ideas';

/**
 * The deterministic vector key for a skill family within the `skills` index:
 * `<org>#<skillBaseName>`. One vector per skill family per org. Used by the
 * write path (U3) and the delete path (U21): on skill delete/rename this key is
 * removed from the index so the dead skill never matches a topic query again.
 */
export function skillVectorKey(org: string, skillBaseName: string): string {
  return `${org}#${skillBaseName}`;
}

/** Resolve the configured vector bucket name (env overrideable; U1 literal default). */
export function vectorBucketName(): string {
  return process.env.SKILL_IDEA_VECTOR_BUCKET ?? DEFAULT_VECTOR_BUCKET;
}

/** A vector to write: a unique key, the float data, and filterable metadata. */
export interface VectorItem {
  /** Unique key within the index (e.g. `<org>#<skillBaseName>`). */
  key: string;
  /** The float embedding (1024-dim for Titan v2). */
  vector: number[];
  /**
   * Filterable metadata. MUST include `org` for query-time isolation; may carry
   * `skillBaseName`, `embeddingVersion`, etc. (all filterable by default, U1).
   */
  metadata: Record<string, unknown>;
}

/** A query hit: the vector's key, similarity score, and metadata. */
export interface QueryHit {
  key: string;
  /** Cosine SIMILARITY in [0,1] (1 - cosine distance), higher is closer. */
  score: number;
  metadata: Record<string, unknown>;
}

/** Options for a top-k query. */
export interface QueryOptions {
  /** Restrict to one org's vectors via the `org` metadata filter (isolation). */
  orgFilter?: string;
  /** Drop hits whose similarity is below this floor (the pre-judge floor, U8). */
  floor?: number;
  /**
   * The embedding version the query vector was produced with — the ONLY version
   * its hits may be compared against (U5). A hit stamped with a different
   * `embeddingVersion` lives in an incomparable vector space; comparing the two
   * yields meaningless similarity, so `queryTopK` REFUSES rather than returning
   * mismatched-space results. Defaults to the env-configured active version
   * (`activeEmbeddingVersion()`); injectable so a reindex/test can pin it.
   */
  expectVersion?: string;
}

/**
 * Raised when `queryTopK` finds an index hit whose `embeddingVersion` does not
 * match the query's version. Cross-version comparison is impossible — it would
 * silently return garbage similarities — so this surfaces loudly and signals
 * that a reindex (`infra/scripts/reindex-embeddings.mjs`) is needed.
 */
export class EmbeddingVersionMismatchError extends Error {
  constructor(
    readonly indexName: string,
    readonly expected: string,
    readonly found: string,
    readonly key: string,
  ) {
    super(
      `embedding version mismatch in index '${indexName}': query is '${expected}' but ` +
        `vector '${key}' is '${found}' — cross-version comparison is refused; reindex ` +
        `(infra/scripts/reindex-embeddings.mjs) before querying.`,
    );
    this.name = 'EmbeddingVersionMismatchError';
  }
}

/** Split `items` into chunks no larger than `size`. */
function chunk<T>(items: T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) {
    out.push(items.slice(i, i + size));
  }
  return out;
}

/**
 * Read/write wrapper over an S3 Vectors index. The client is created lazily and
 * memoised (mirrors `rest/runtime`), and is injectable so tests mock it. The
 * index name is supplied per call; the bucket comes from env/config.
 */
export class S3Vectors {
  private client: S3VectorsClient;

  constructor(client?: S3VectorsClient, private readonly bucket: string = vectorBucketName()) {
    this.client = client ?? new S3VectorsClient({});
  }

  /**
   * Write `items` to `indexName`, chunked at the 500-vector per-request limit.
   * Errors are NOT swallowed — a failed chunk propagates so the caller can
   * retry/DLQ. Each item's float data is wrapped in the SDK's `{ float32 }`
   * union and metadata is passed through (the `org` key drives isolation).
   */
  async putVectors(indexName: string, items: VectorItem[]): Promise<void> {
    if (items.length === 0) return;
    for (const batch of chunk(items, PUT_BATCH_LIMIT)) {
      await this.client.send(
        new PutVectorsCommand({
          vectorBucketName: this.bucket,
          indexName,
          vectors: batch.map((it) => ({
            key: it.key,
            data: { float32: it.vector },
            metadata: it.metadata as DocumentType,
          })),
        }),
      );
    }
  }

  /**
   * Delete `keys` from `indexName`, chunked at the same per-request limit as
   * `putVectors` (additive U21 mirror of `putVectors`). Used when a skill is
   * deleted/renamed: its skill vector key (`<org>#<skillBaseName>`) must be
   * removed so the dead skill never matches a topic query again. Errors are NOT
   * swallowed — a failed chunk propagates so the caller can retry/DLQ. Deleting
   * a key that does not exist is a no-op on the service side.
   */
  async deleteVectors(indexName: string, keys: string[]): Promise<void> {
    if (keys.length === 0) return;
    for (const batch of chunk(keys, PUT_BATCH_LIMIT)) {
      await this.client.send(
        new DeleteVectorsCommand({
          vectorBucketName: this.bucket,
          indexName,
          keys: batch,
        }),
      );
    }
  }

  /**
   * Fetch stored vectors BY KEY (not by similarity), returning each found key's
   * metadata. Used by the `skills-missing-embeddings` probe (U23): a skill's
   * expected vector key (`<org>#<skillBaseName>`) is looked up directly so the
   * probe can compare the STORED `descHash`/`embeddingVersion` metadata against
   * the skill's CURRENT content hash + active version — a direct fetch is exact,
   * whereas a similarity query could miss a stale vector. Keys are chunked at the
   * same per-request limit; a key with no stored vector is simply absent from the
   * result map (the probe treats absent as "missing embedding"). Vector floats are
   * NOT requested (the probe only needs metadata). Errors are NOT swallowed.
   */
  async getVectors(
    indexName: string,
    keys: string[],
  ): Promise<Map<string, Record<string, unknown>>> {
    const found = new Map<string, Record<string, unknown>>();
    if (keys.length === 0) return found;
    for (const batch of chunk(keys, PUT_BATCH_LIMIT)) {
      const res = await this.client.send(
        new GetVectorsCommand({
          vectorBucketName: this.bucket,
          indexName,
          keys: batch,
          returnMetadata: true,
          returnData: false,
        }),
      );
      for (const v of res.vectors ?? []) {
        if (typeof v.key === 'string') {
          found.set(v.key, (v.metadata as Record<string, unknown> | undefined) ?? {});
        }
      }
    }
    return found;
  }

  /**
   * Top-k similarity search over `indexName`. The `orgFilter` is applied as an
   * `org` metadata filter so one org never sees another's vectors. Results are
   * converted from cosine DISTANCE to SIMILARITY (`1 - distance`), filtered to
   * those at/above `floor`, sorted by score descending, and capped at `k`.
   * Errors are NOT swallowed.
   */
  async queryTopK(
    indexName: string,
    vector: number[],
    k: number,
    opts: QueryOptions = {},
  ): Promise<QueryHit[]> {
    const filter = opts.orgFilter !== undefined ? { org: opts.orgFilter } : undefined;
    const res = await this.client.send(
      new QueryVectorsCommand({
        vectorBucketName: this.bucket,
        indexName,
        topK: k,
        queryVector: { float32: vector },
        returnMetadata: true,
        returnDistance: true,
        ...(filter ? { filter } : {}),
      }),
    );
    const hits: QueryHit[] = (res.vectors ?? []).map((v) => ({
      key: v.key ?? '',
      // Cosine distance ∈ [0,2]; similarity = 1 - distance ∈ [-1,1] (≈[0,1] for
      // the normalized Titan vectors we store).
      score: 1 - (v.distance ?? 0),
      metadata: (v.metadata as Record<string, unknown> | undefined) ?? {},
    }));
    // Version guard (U5): the query vector and the index vectors must share an
    // `embeddingVersion` or the cosine score is meaningless. A hit stamped with a
    // different version means the index is mid-migration / un-reindexed — refuse
    // rather than silently returning mismatched-space results.
    const expectVersion = opts.expectVersion ?? activeEmbeddingVersion();
    for (const h of hits) {
      const found = h.metadata.embeddingVersion;
      // Untagged legacy vectors (no stamp) are treated as same-space and pass;
      // a present-but-different stamp is the migration-skew signal we refuse.
      if (typeof found === 'string' && found !== expectVersion) {
        throw new EmbeddingVersionMismatchError(indexName, expectVersion, found, h.key);
      }
    }
    const floor = opts.floor;
    return hits
      .filter((h) => (floor === undefined ? true : h.score >= floor))
      .sort((a, b) => b.score - a.score)
      .slice(0, k);
  }
}

/** Lazy, memoised default S3 Vectors client for the Lambda runtime. */
let defaultS3Vectors: S3Vectors | undefined;

/** The process-wide default S3 Vectors wrapper, created on first use. */
export function getS3Vectors(): S3Vectors {
  defaultS3Vectors ??= new S3Vectors();
  return defaultS3Vectors;
}
