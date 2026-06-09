import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';

/**
 * U7 — Bedrock Claude-Haiku "idea-writer".
 *
 * The third Bedrock call type in the skill-idea loop (alongside Titan embeddings
 * and the topic→skill rerank judge). An idea is a SYNTHESIZED, skill-ready
 * concept — crisp guidance that could be pasted into a skill body as-is — NOT a
 * raw transcript snippet (the raw snippet is kept only as provenance on the
 * idea's `sources`).
 *
 * This writer is invoked in two places:
 *  - on idea CREATION (U10), to write the lesson from the topic `description` +
 *    the segment's `impl_learning`s;
 *  - on a MERGE (U7), to RE-SYNTHESIZE an existing idea's `text`, folding in the
 *    nuance a near-duplicate finding adds — corroboration sharpens the concept,
 *    not just the count. Re-synthesis takes the current `text` plus the incoming
 *    finding and rewrites a single tightened concept.
 *
 * Haiku is sufficient for this (the plan's Key Technical Decisions). Auth is
 * IAM/Bedrock runtime — there is NO API key and none is read; the Lambda role
 * grants `bedrock:InvokeModel`. The Bedrock error is NOT swallowed.
 */

/** The Claude Haiku model id. Overridable via env so a model bump needs no code change. */
const DEFAULT_MODEL_ID = 'anthropic.claude-3-5-haiku-20241022-v1:0';
/** The Anthropic-on-Bedrock invoke contract version. */
const ANTHROPIC_VERSION = 'bedrock-2023-05-31';
/** A skill-ready concept is short, foldable guidance — cap the rewrite tightly. */
const MAX_TOKENS = 512;

/** Resolve the configured idea-writer model id (env overrideable; defaults to Haiku). */
function modelId(): string {
  return process.env.BEDROCK_IDEA_WRITER_MODEL_ID ?? DEFAULT_MODEL_ID;
}

/** A new finding to synthesize into (or fold into) an idea's skill-ready concept. */
export interface IdeaFinding {
  /** The topic `description` (rich, session-derived) the finding came from. */
  description?: string;
  /** The segment's `impl_learning`s — the lesson content to fold. */
  implLearnings?: string[];
  /** Raw evidence snippet (provenance), included as context for the writer. */
  snippet?: string;
}

const SYSTEM_PROMPT =
  'You are an idea-writer for a skill library. You turn raw, session-derived ' +
  'findings into a single crisp, skill-ready concept: prose that could be ' +
  'pasted directly into a skill body as guidance. Write imperative, ' +
  'self-contained guidance — no transcript quotes, no meta-commentary, no ' +
  'preamble. Output ONLY the concept text, 1–3 sentences.';

/** Render a finding's inputs into the writer prompt body. */
function renderFinding(finding: IdeaFinding): string {
  const parts: string[] = [];
  if (finding.description) parts.push(`Topic description:\n${finding.description}`);
  const learnings = (finding.implLearnings ?? []).filter((l) => l.trim().length > 0);
  if (learnings.length > 0) {
    parts.push(`Implementation learnings:\n${learnings.map((l) => `- ${l}`).join('\n')}`);
  }
  if (finding.snippet) parts.push(`Raw evidence (provenance only):\n${finding.snippet}`);
  return parts.join('\n\n');
}

/** The Anthropic-on-Bedrock invoke-response body shape (the fields we read). */
interface AnthropicResponse {
  content?: Array<{ type?: string; text?: string }>;
}

/**
 * A Bedrock Claude-Haiku idea-writer. The Bedrock Runtime client is created
 * lazily and memoised across warm Lambda invocations (mirrors `embeddings/`),
 * and is injectable so tests mock it without touching the network. Region /
 * credentials come from the environment / IAM role.
 */
export class IdeaWriter {
  private client: BedrockRuntimeClient;

  constructor(client?: BedrockRuntimeClient) {
    this.client = client ?? new BedrockRuntimeClient({});
  }

  /**
   * Write a brand-new skill-ready concept from a finding (idea CREATION, U10).
   */
  async write(finding: IdeaFinding): Promise<string> {
    return this.invoke(
      `Write a single skill-ready concept from this finding.\n\n${renderFinding(finding)}`,
    );
  }

  /**
   * RE-SYNTHESIZE an existing idea's concept, folding in a near-duplicate's
   * nuance (MERGE, U7). The result is ONE tightened concept that covers both —
   * not a concatenation. If the new finding adds nothing, the existing concept
   * is returned essentially unchanged.
   */
  async merge(existingText: string, finding: IdeaFinding): Promise<string> {
    return this.invoke(
      'A near-duplicate finding has landed on an existing skill concept. ' +
        'Rewrite the existing concept into a SINGLE, tightened concept that ' +
        'folds in any new nuance from the finding. Do not concatenate; ' +
        'produce one crisp concept.\n\n' +
        `Existing concept:\n${existingText}\n\n` +
        `New finding:\n${renderFinding(finding)}`,
    );
  }

  /**
   * Send one Messages-API invoke and return the trimmed text. A Bedrock error is
   * NOT swallowed — it propagates so the caller can retry/DLQ. An empty/garbled
   * response throws rather than silently writing an empty concept.
   */
  private async invoke(userPrompt: string): Promise<string> {
    const body = {
      anthropic_version: ANTHROPIC_VERSION,
      max_tokens: MAX_TOKENS,
      system: SYSTEM_PROMPT,
      messages: [{ role: 'user', content: userPrompt }],
    };
    const res = await this.client.send(
      new InvokeModelCommand({
        modelId: modelId(),
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify(body),
      }),
    );
    const decoded = new TextDecoder().decode(res.body);
    const parsed = JSON.parse(decoded) as AnthropicResponse;
    const text = (parsed.content ?? [])
      .filter((b) => b.type === 'text' || b.text !== undefined)
      .map((b) => b.text ?? '')
      .join('')
      .trim();
    if (text.length === 0) {
      throw new Error('idea-writer returned an empty concept');
    }
    return text;
  }
}

/** Lazy, memoised default idea-writer for the Lambda runtime (tests inject their own). */
let defaultIdeaWriter: IdeaWriter | undefined;

/** The process-wide default idea-writer, created on first use. */
export function getIdeaWriter(): IdeaWriter {
  defaultIdeaWriter ??= new IdeaWriter();
  return defaultIdeaWriter;
}
