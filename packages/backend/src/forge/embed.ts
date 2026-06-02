import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';
import type { SessionVector } from '@harness/shared';
import type { Repo } from '../db/repo.js';

/**
 * Forge embedding (U27, KTD7).
 *
 * On `session.done` a session is summarized and embedded, then stored as a
 * `SessionVector` for k-NN search. The model calls sit behind an injectable
 * `Embedder` interface so tests run with a deterministic fake — no Bedrock, no
 * network. The default implementation talks to Amazon Bedrock (Titan embeddings
 * + a Claude summarizer).
 */

/** Produces a fixed-dimension embedding for a text. Injectable for tests. */
export interface Embedder {
  embed(text: string): Promise<number[]>;
}

/** Summarizes a session transcript into a short description. Injectable. */
export interface Summarizer {
  summarize(transcript: string): Promise<string>;
}

/** The raw activity of a finished session, as gathered from its events. */
export interface SessionActivity {
  sessionId: string;
  userId: string;
  projectId: string;
  /** Concatenated message text used as the summarization/embedding input. */
  transcript: string;
  /** Skills the session used (for later frequency aggregation). */
  skills: string[];
  /** Tools the session used (for later frequency aggregation). */
  tools: string[];
}

/**
 * Bedrock-backed embedder using a Titan embeddings model. The model id is
 * configurable so the same code targets Titan or Cohere without change.
 */
export class BedrockEmbedder implements Embedder {
  constructor(
    private readonly client: BedrockRuntimeClient,
    private readonly modelId: string = process.env.BEDROCK_EMBED_MODEL ??
      'amazon.titan-embed-text-v2:0',
  ) {}

  async embed(text: string): Promise<number[]> {
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: this.modelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify({ inputText: text }),
      }),
    );
    const decoded = JSON.parse(new TextDecoder().decode(res.body)) as {
      embedding?: number[];
    };
    if (!decoded.embedding) throw new Error('Bedrock embed response missing embedding');
    return decoded.embedding;
  }
}

/**
 * Bedrock-backed summarizer using a Claude model (Messages API). Produces a
 * one-paragraph description of what the session accomplished.
 */
export class BedrockSummarizer implements Summarizer {
  constructor(
    private readonly client: BedrockRuntimeClient,
    private readonly modelId: string = process.env.BEDROCK_TEXT_MODEL ??
      'anthropic.claude-3-5-sonnet-20240620-v1:0',
  ) {}

  async summarize(transcript: string): Promise<string> {
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: this.modelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify({
          anthropic_version: 'bedrock-2023-05-31',
          max_tokens: 256,
          messages: [
            {
              role: 'user',
              content: `Summarize what this coding session accomplished in 1-2 sentences:\n\n${transcript}`,
            },
          ],
        }),
      }),
    );
    const decoded = JSON.parse(new TextDecoder().decode(res.body)) as {
      content?: { text?: string }[];
    };
    return decoded.content?.[0]?.text?.trim() ?? '';
  }
}

export interface EmbedDeps {
  repo: Repo;
  embedder: Embedder;
  summarizer: Summarizer;
  now?: () => number;
}

/**
 * Summarize + embed a finished session and persist its vector. Idempotent at the
 * storage layer (overwrites the same sessionId). Returns the stored vector so
 * the caller (the `session.done` handler) can index it in OpenSearch too.
 */
export async function summarizeAndEmbed(
  activity: SessionActivity,
  deps: EmbedDeps,
): Promise<SessionVector> {
  const summary = await deps.summarizer.summarize(activity.transcript);
  const vector = await deps.embedder.embed(summary || activity.transcript);

  const record: SessionVector = {
    sessionId: activity.sessionId,
    userId: activity.userId,
    projectId: activity.projectId,
    summary,
    vector,
    skills: activity.skills,
    tools: activity.tools,
    createdAt: (deps.now ?? Date.now)(),
  };
  await deps.repo.putSessionVector(record);
  return record;
}
