import { vi } from 'vitest';
import type { FetchLike } from '../../src/llm/openrouter.js';

/**
 * Test helpers for the OpenRouter client. The LLM tests inject a stub `fetch` so
 * nothing touches the network. These build the OpenAI-shaped JSON bodies the
 * OpenRouter chat / embeddings endpoints return, and a `vi.fn` fetch that resolves
 * a 200 with that body (or a non-2xx / rejection for the error-path tests).
 */

/** Build a `Response`-like object the OpenRouter client reads (`ok`, `json`, `text`). */
export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? 'OK' : 'Error',
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

/** A `/chat/completions` response body carrying the given assistant content. */
export function chatBody(content: string): unknown {
  return { choices: [{ message: { role: 'assistant', content } }] };
}

/** An `/embeddings` response body carrying a single vector of the given dimension. */
export function embedBody(dim: number): unknown {
  const embedding = Array.from({ length: dim }, (_, i) => i / dim);
  return { data: [{ embedding }] };
}

/** A stub fetch that always resolves the given response. */
export function fetchResolving(body: unknown, status = 200): ReturnType<typeof vi.fn> & FetchLike {
  return vi.fn(async () => jsonResponse(body, status)) as ReturnType<typeof vi.fn> & FetchLike;
}

/** A stub fetch that rejects (simulates a transport / network error). */
export function fetchRejecting(err: Error): ReturnType<typeof vi.fn> & FetchLike {
  return vi.fn(async () => {
    throw err;
  }) as ReturnType<typeof vi.fn> & FetchLike;
}

/** Parse the JSON body the client POSTed in call `index` of a stub fetch. */
export function postedBody(fetchMock: ReturnType<typeof vi.fn>, index = 0): any {
  const call = fetchMock.mock.calls[index];
  return JSON.parse(call?.[1]?.body as string);
}

/** The URL the client POSTed to in call `index` of a stub fetch. */
export function postedUrl(fetchMock: ReturnType<typeof vi.fn>, index = 0): string {
  return fetchMock.mock.calls[index]?.[0] as string;
}
