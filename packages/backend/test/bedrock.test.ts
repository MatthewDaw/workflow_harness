import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { BedrockEmbedder, EMBEDDING_DIMENSION } from '../src/embeddings/bedrock.js';

/**
 * U2 — Bedrock Titan v2 embedding client. The Bedrock Runtime client is mocked
 * so nothing touches the network. We assert: the embed returns a 1024-length
 * vector stamped with model+version; the right Titan request is sent; and a
 * Bedrock error propagates rather than being swallowed.
 */

const bedrockMock = mockClient(BedrockRuntimeClient);
const client = new BedrockRuntimeClient({ region: 'us-east-1' });
const embedder = new BedrockEmbedder(client);

beforeEach(() => bedrockMock.reset());

/** A Titan-shaped response body of the given dimension, encoded as Bedrock does. */
function titanBody(dim: number): Uint8Array {
  const embedding = Array.from({ length: dim }, (_, i) => i / dim);
  return new TextEncoder().encode(JSON.stringify({ embedding, inputTextTokenCount: 7 }));
}

describe('BedrockEmbedder.embed', () => {
  it('returns a 1024-length vector stamped with model + version', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: titanBody(EMBEDDING_DIMENSION) });

    const res = await embedder.embed('prefer decimal money types');

    expect(res.vector).toHaveLength(1024);
    expect(res.embeddingModel).toBe('amazon.titan-embed-text-v2:0');
    expect(res.embeddingVersion).toBe('titan-embed-text-v2');
  });

  it('sends a Titan v2 invoke with the input text and 1024 dimensions', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: titanBody(EMBEDDING_DIMENSION) });

    await embedder.embed('hello world');

    const call = bedrockMock.commandCalls(InvokeModelCommand)[0]!.args[0].input;
    expect(call.modelId).toBe('amazon.titan-embed-text-v2:0');
    expect(call.contentType).toBe('application/json');
    const body = JSON.parse(call.body as string);
    expect(body).toMatchObject({ inputText: 'hello world', dimensions: 1024, normalize: true });
  });

  it('rejects a response whose dimension is not 1024', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: titanBody(512) });
    await expect(embedder.embed('x')).rejects.toThrow(/512 dimensions; expected 1024/);
  });

  it('propagates a Bedrock error (does not swallow)', async () => {
    bedrockMock.on(InvokeModelCommand).rejects(new Error('ThrottlingException'));
    await expect(embedder.embed('x')).rejects.toThrow('ThrottlingException');
  });
});
