/**
 * Shared OpenRouter client — the ONE model transport for the skill-idea loop.
 *
 * Both halves of the loop's inference run through OpenRouter on a single API key:
 *  - CHAT  (`openRouterChat`) — the rerank judge (U9), the idea-writer (U7), and
 *    the golden judge (U18). OpenAI-compatible `/chat/completions`.
 *  - EMBED (`openRouterEmbed`) — the skill/topic/idea embeddings (U2/U3/U7/U8).
 *    OpenAI-compatible `/embeddings`.
 *
 * Auth is a bearer API key read from `OPENROUTER_API_KEY` at CALL TIME — it is
 * NEVER hardcoded here and NEVER read at import time, so a missing key surfaces
 * as a thrown request error (which propagates to `batchItemFailures`/DLQ) rather
 * than a load-time crash. The base URL and both default models are env-overridable.
 *
 * `fetch` is injectable (Node 20 global by default) so tests drive the client
 * with a stub and nothing touches the network.
 */

/** The OpenRouter API base (env overrideable). */
const DEFAULT_BASE_URL = 'https://openrouter.ai/api/v1';
/** Default chat model (env overrideable via `OPENROUTER_CHAT_MODEL`). */
export const DEFAULT_CHAT_MODEL = 'anthropic/claude-haiku-4.5';
/** Default embedding model (env overrideable via `OPENROUTER_EMBEDDING_MODEL`). */
export const DEFAULT_EMBEDDING_MODEL = 'openai/text-embedding-3-small';

/** The `fetch` signature the client depends on (Node 20 global by default). */
export type FetchLike = typeof fetch;

/** Resolve the configured base URL at call time (env overrideable). */
function baseUrl(): string {
  return process.env.OPENROUTER_BASE_URL ?? DEFAULT_BASE_URL;
}

/** Resolve the default chat model at call time (env overrideable). */
export function chatModel(): string {
  return process.env.OPENROUTER_CHAT_MODEL ?? DEFAULT_CHAT_MODEL;
}

/** Resolve the default embedding model at call time (env overrideable). */
export function embeddingModel(): string {
  return process.env.OPENROUTER_EMBEDDING_MODEL ?? DEFAULT_EMBEDDING_MODEL;
}

/**
 * Build the request headers. The API key is read from the environment at CALL
 * TIME (never hardcoded). The optional `HTTP-Referer`/`X-Title` headers are
 * OpenRouter's app-attribution headers — set from env when present.
 */
function headers(): Record<string, string> {
  const h: Record<string, string> = {
    'content-type': 'application/json',
    authorization: `Bearer ${process.env.OPENROUTER_API_KEY ?? ''}`,
  };
  const referer = process.env.OPENROUTER_HTTP_REFERER;
  const title = process.env.OPENROUTER_X_TITLE;
  if (referer) h['HTTP-Referer'] = referer;
  if (title) h['X-Title'] = title;
  return h;
}

/** Options for a single chat completion. */
export interface OpenRouterChatOptions {
  /** The system prompt (role: system). */
  system: string;
  /** The user prompt (role: user). */
  user: string;
  /** Override the chat model (defaults to `OPENROUTER_CHAT_MODEL`). */
  model?: string;
  /** Cap the response length. */
  maxTokens?: number;
  /** Injectable fetch (defaults to the Node 20 global). */
  fetchImpl?: FetchLike;
}

/** The OpenAI chat-completions response shape (the fields we read). */
interface ChatCompletionResponse {
  choices?: Array<{ message?: { content?: string | null } }>;
}

/**
 * One chat completion via OpenRouter's OpenAI-compatible `/chat/completions`.
 * Returns the assistant message content, trimmed. A non-2xx response or an empty
 * content string THROWS — errors are never swallowed, so the stream consumer
 * routes the failure to `batchItemFailures`/DLQ rather than producing a wrong/no
 * result.
 */
export async function openRouterChat(opts: OpenRouterChatOptions): Promise<string> {
  const doFetch = opts.fetchImpl ?? fetch;
  const body: Record<string, unknown> = {
    model: opts.model ?? chatModel(),
    messages: [
      { role: 'system', content: opts.system },
      { role: 'user', content: opts.user },
    ],
  };
  if (typeof opts.maxTokens === 'number') body.max_tokens = opts.maxTokens;

  const res = await doFetch(`${baseUrl()}/chat/completions`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await safeText(res);
    throw new Error(`OpenRouter chat failed: ${res.status} ${res.statusText} ${text}`.trim());
  }
  const parsed = (await res.json()) as ChatCompletionResponse;
  const content = (parsed.choices?.[0]?.message?.content ?? '').trim();
  if (content.length === 0) {
    throw new Error('OpenRouter chat returned empty content');
  }
  return content;
}

/** Options for a single embedding call. */
export interface OpenRouterEmbedOptions {
  /** Override the embedding model (defaults to `OPENROUTER_EMBEDDING_MODEL`). */
  model?: string;
  /** Injectable fetch (defaults to the Node 20 global). */
  fetchImpl?: FetchLike;
}

/** The OpenAI embeddings response shape (the fields we read). */
interface EmbeddingResponse {
  data?: Array<{ embedding?: number[] }>;
}

/**
 * One embedding via OpenRouter's OpenAI-compatible `/embeddings`. Returns the
 * first (only) embedding vector. A non-2xx response or a missing vector THROWS —
 * errors are never swallowed.
 */
export async function openRouterEmbed(
  text: string,
  opts: OpenRouterEmbedOptions = {},
): Promise<number[]> {
  const doFetch = opts.fetchImpl ?? fetch;
  const res = await doFetch(`${baseUrl()}/embeddings`, {
    method: 'POST',
    headers: headers(),
    body: JSON.stringify({ model: opts.model ?? embeddingModel(), input: text }),
  });
  if (!res.ok) {
    const t = await safeText(res);
    throw new Error(`OpenRouter embed failed: ${res.status} ${res.statusText} ${t}`.trim());
  }
  const parsed = (await res.json()) as EmbeddingResponse;
  const vector = parsed.data?.[0]?.embedding;
  if (!Array.isArray(vector)) {
    throw new Error('OpenRouter embed returned no embedding');
  }
  return vector;
}

/** Read a response body as text without throwing (for richer error messages). */
async function safeText(res: Response): Promise<string> {
  try {
    return await res.text();
  } catch {
    return '';
  }
}
