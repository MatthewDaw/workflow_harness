import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import {
  DeleteVectorsCommand,
  PutVectorsCommand,
  QueryVectorsCommand,
  S3VectorsClient,
} from '@aws-sdk/client-s3vectors';
import {
  EmbeddingVersionMismatchError,
  PUT_BATCH_LIMIT,
  S3Vectors,
  skillVectorKey,
  type VectorItem,
} from '../src/embeddings/s3vectors.js';

/**
 * U2 — S3 Vectors put/query client. The S3 Vectors client is mocked. We assert:
 * a batch put chunks at the 500-vector limit; a query passes the org filter and
 * returns ≤k hits sorted by score with sub-floor hits dropped; and errors
 * propagate (not swallowed). The bucket is pinned so it is independent of env.
 */

const s3vMock = mockClient(S3VectorsClient);
const client = new S3VectorsClient({ region: 'us-east-1' });
const store = new S3Vectors(client, 'test-bucket');

beforeEach(() => s3vMock.reset());

function vec(key: string, org = 'acme'): VectorItem {
  return { key, vector: Array.from({ length: 1024 }, () => 0.1), metadata: { org } };
}

describe('S3Vectors.putVectors', () => {
  it('writes to the named index with the configured bucket', async () => {
    s3vMock.on(PutVectorsCommand).resolves({});
    await store.putVectors('skills', [vec('acme#hq-update-skills')]);

    const call = s3vMock.commandCalls(PutVectorsCommand)[0]!.args[0].input;
    expect(call.vectorBucketName).toBe('test-bucket');
    expect(call.indexName).toBe('skills');
    expect(call.vectors![0]).toMatchObject({
      key: 'acme#hq-update-skills',
      data: { float32: expect.any(Array) },
      metadata: { org: 'acme' },
    });
  });

  it('chunks a batch at the 500-vector limit', async () => {
    s3vMock.on(PutVectorsCommand).resolves({});
    const items = Array.from({ length: 1100 }, (_, i) => vec(`k-${i}`));

    await store.putVectors('skills', items);

    const calls = s3vMock.commandCalls(PutVectorsCommand);
    expect(calls).toHaveLength(3); // 500 + 500 + 100
    expect(calls[0]!.args[0].input.vectors).toHaveLength(PUT_BATCH_LIMIT);
    expect(calls[1]!.args[0].input.vectors).toHaveLength(PUT_BATCH_LIMIT);
    expect(calls[2]!.args[0].input.vectors).toHaveLength(100);
  });

  it('no-ops on an empty batch (no put call)', async () => {
    s3vMock.on(PutVectorsCommand).resolves({});
    await store.putVectors('skills', []);
    expect(s3vMock.commandCalls(PutVectorsCommand)).toHaveLength(0);
  });

  it('propagates a put error (does not swallow)', async () => {
    s3vMock.on(PutVectorsCommand).rejects(new Error('ServiceUnavailableException'));
    await expect(store.putVectors('skills', [vec('k')])).rejects.toThrow(
      'ServiceUnavailableException',
    );
  });
});

describe('S3Vectors.queryTopK', () => {
  it('passes the org filter and returns ≤k hits sorted by score, dropping sub-floor', async () => {
    // Distances → similarities: 0.05→0.95, 0.20→0.80, 0.60→0.40 (below a 0.5 floor).
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [
        { key: 'b', distance: 0.2, metadata: { org: 'acme' } },
        { key: 'a', distance: 0.05, metadata: { org: 'acme' } },
        { key: 'c', distance: 0.6, metadata: { org: 'acme' } },
      ],
    });

    const hits = await store.queryTopK('skills', Array(1024).fill(0.1), 5, {
      orgFilter: 'acme',
      floor: 0.5,
    });

    // Sub-floor 'c' dropped; remaining sorted by score descending.
    expect(hits.map((h) => h.key)).toEqual(['a', 'b']);
    expect(hits[0]!.score).toBeCloseTo(0.95);
    expect(hits[1]!.score).toBeCloseTo(0.8);

    const call = s3vMock.commandCalls(QueryVectorsCommand)[0]!.args[0].input;
    expect(call.filter).toEqual({ org: 'acme' });
    expect(call.topK).toBe(5);
    expect(call.returnMetadata).toBe(true);
    expect(call.returnDistance).toBe(true);
  });

  it('caps results at k even when more come back', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [
        { key: 'a', distance: 0.1, metadata: {} },
        { key: 'b', distance: 0.2, metadata: {} },
        { key: 'c', distance: 0.3, metadata: {} },
      ],
    });
    const hits = await store.queryTopK('skills', Array(1024).fill(0.1), 2);
    expect(hits).toHaveLength(2);
    expect(hits.map((h) => h.key)).toEqual(['a', 'b']);
  });

  it('omits the filter when no orgFilter is given', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({ distanceMetric: 'cosine', vectors: [] });
    await store.queryTopK('skills', Array(1024).fill(0.1), 3);
    const call = s3vMock.commandCalls(QueryVectorsCommand)[0]!.args[0].input;
    expect(call.filter).toBeUndefined();
  });

  it('propagates a query error (does not swallow)', async () => {
    s3vMock.on(QueryVectorsCommand).rejects(new Error('AccessDeniedException'));
    await expect(store.queryTopK('skills', Array(1024).fill(0.1), 3)).rejects.toThrow(
      'AccessDeniedException',
    );
  });
});

describe('S3Vectors.queryTopK — embedding version guard (U5)', () => {
  it('raises when an index hit is tagged with a different embeddingVersion', async () => {
    // Query is v2; the index still holds a v1-stamped vector → incomparable space.
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [
        { key: 'acme#a', distance: 0.1, metadata: { org: 'acme', embeddingVersion: 'v1' } },
      ],
    });

    await expect(
      store.queryTopK('skills', Array(1024).fill(0.1), 5, { expectVersion: 'v2' }),
    ).rejects.toBeInstanceOf(EmbeddingVersionMismatchError);
    await expect(
      store.queryTopK('skills', Array(1024).fill(0.1), 5, { expectVersion: 'v2' }),
    ).rejects.toThrow(/version mismatch.*'v2'.*'acme#a'.*'v1'.*reindex/);
  });

  it('refuses the whole query if ANY hit mismatches (no partial mixed-space result)', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [
        { key: 'acme#a', distance: 0.05, metadata: { org: 'acme', embeddingVersion: 'v2' } },
        { key: 'acme#b', distance: 0.1, metadata: { org: 'acme', embeddingVersion: 'v1' } },
      ],
    });
    await expect(
      store.queryTopK('skills', Array(1024).fill(0.1), 5, { expectVersion: 'v2' }),
    ).rejects.toBeInstanceOf(EmbeddingVersionMismatchError);
  });

  it('returns hits when every hit matches the expected version', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [
        { key: 'acme#a', distance: 0.05, metadata: { org: 'acme', embeddingVersion: 'v2' } },
        { key: 'acme#b', distance: 0.2, metadata: { org: 'acme', embeddingVersion: 'v2' } },
      ],
    });
    const hits = await store.queryTopK('skills', Array(1024).fill(0.1), 5, {
      expectVersion: 'v2',
    });
    expect(hits.map((h) => h.key)).toEqual(['acme#a', 'acme#b']);
  });

  it('treats untagged legacy vectors (no embeddingVersion stamp) as same-space', async () => {
    s3vMock.on(QueryVectorsCommand).resolves({
      distanceMetric: 'cosine',
      vectors: [{ key: 'acme#a', distance: 0.1, metadata: { org: 'acme' } }],
    });
    const hits = await store.queryTopK('skills', Array(1024).fill(0.1), 5, {
      expectVersion: 'v2',
    });
    expect(hits.map((h) => h.key)).toEqual(['acme#a']);
  });

  it('defaults the expected version to the env-configured active version', async () => {
    const prev = process.env.BEDROCK_EMBEDDING_VERSION;
    process.env.BEDROCK_EMBEDDING_VERSION = 'v9';
    try {
      s3vMock.on(QueryVectorsCommand).resolves({
        distanceMetric: 'cosine',
        vectors: [
          { key: 'acme#a', distance: 0.1, metadata: { org: 'acme', embeddingVersion: 'v8' } },
        ],
      });
      // No expectVersion → falls back to BEDROCK_EMBEDDING_VERSION (v9) ≠ v8 → raises.
      await expect(store.queryTopK('skills', Array(1024).fill(0.1), 5)).rejects.toBeInstanceOf(
        EmbeddingVersionMismatchError,
      );
    } finally {
      if (prev === undefined) delete process.env.BEDROCK_EMBEDDING_VERSION;
      else process.env.BEDROCK_EMBEDDING_VERSION = prev;
    }
  });
});

describe('S3Vectors.deleteVectors (U21)', () => {
  it('deletes keys from the named index with the configured bucket', async () => {
    s3vMock.on(DeleteVectorsCommand).resolves({});
    await store.deleteVectors('skills', [skillVectorKey('acme', 'reconcile')]);

    const call = s3vMock.commandCalls(DeleteVectorsCommand)[0]!.args[0].input;
    expect(call.vectorBucketName).toBe('test-bucket');
    expect(call.indexName).toBe('skills');
    expect(call.keys).toEqual(['acme#reconcile']);
  });

  it('chunks a large delete at the put batch limit', async () => {
    s3vMock.on(DeleteVectorsCommand).resolves({});
    const keys = Array.from({ length: 1100 }, (_, i) => `k-${i}`);

    await store.deleteVectors('skills', keys);

    const calls = s3vMock.commandCalls(DeleteVectorsCommand);
    expect(calls).toHaveLength(3); // 500 + 500 + 100
    expect(calls[0]!.args[0].input.keys).toHaveLength(PUT_BATCH_LIMIT);
    expect(calls[2]!.args[0].input.keys).toHaveLength(100);
  });

  it('no-ops on an empty key list (no delete call)', async () => {
    s3vMock.on(DeleteVectorsCommand).resolves({});
    await store.deleteVectors('skills', []);
    expect(s3vMock.commandCalls(DeleteVectorsCommand)).toHaveLength(0);
  });

  it('propagates a delete error (does not swallow)', async () => {
    s3vMock.on(DeleteVectorsCommand).rejects(new Error('ServiceUnavailableException'));
    await expect(store.deleteVectors('skills', ['k'])).rejects.toThrow(
      'ServiceUnavailableException',
    );
  });
});
