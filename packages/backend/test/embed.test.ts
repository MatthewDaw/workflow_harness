import { describe, expect, it } from 'vitest';
import { OpenRouterEmbedder, EMBEDDING_DIMENSION } from '../src/embeddings/embed.js';
import {
  embedBody,
  fetchRejecting,
  fetchResolving,
  postedBody,
  postedUrl,
} from './helpers/fetchmock.js';

/**
 * U2 — OpenRouter embedding client. The injected `fetch` is a stub so nothing
 * touches the network. We assert: the embed returns a 1536-length vector stamped
 * with model+version; the right OpenAI embeddings request is sent; a wrong-dim
 * response is rejected; and a request error propagates rather than being swallowed.
 */

describe('OpenRouterEmbedder.embed', () => {
  it('returns a 1536-length vector stamped with model + version', async () => {
    const embedder = new OpenRouterEmbedder(fetchResolving(embedBody(EMBEDDING_DIMENSION)));
    const res = await embedder.embed('prefer decimal money types');

    expect(res.vector).toHaveLength(1536);
    expect(res.embeddingModel).toBe('openai/text-embedding-3-small');
    expect(res.embeddingVersion).toBe('openai/text-embedding-3-small');
  });

  it('sends an OpenAI embeddings request with the input text + model', async () => {
    const f = fetchResolving(embedBody(EMBEDDING_DIMENSION));
    const embedder = new OpenRouterEmbedder(f);
    await embedder.embed('hello world');

    expect(postedUrl(f)).toBe('https://openrouter.ai/api/v1/embeddings');
    const body = postedBody(f);
    expect(body).toMatchObject({ model: 'openai/text-embedding-3-small', input: 'hello world' });
    // Auth header carries the bearer key (read from env at call time).
    const headers = f.mock.calls[0]![1]!.headers as Record<string, string>;
    expect(headers.authorization).toMatch(/^Bearer /);
  });

  it('rejects a response whose dimension is not 1536', async () => {
    const embedder = new OpenRouterEmbedder(fetchResolving(embedBody(512)));
    await expect(embedder.embed('x')).rejects.toThrow(/512 dimensions; expected 1536/);
  });

  it('propagates a request error (does not swallow)', async () => {
    const embedder = new OpenRouterEmbedder(fetchRejecting(new Error('ThrottlingException')));
    await expect(embedder.embed('x')).rejects.toThrow('ThrottlingException');
  });

  it('propagates a non-2xx response as an error', async () => {
    const embedder = new OpenRouterEmbedder(fetchResolving({ error: 'rate limited' }, 429));
    await expect(embedder.embed('x')).rejects.toThrow(/embed failed: 429/);
  });
});
