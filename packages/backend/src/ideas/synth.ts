import { type FetchLike, openRouterChat } from '../llm/openrouter.js';

/**
 * U7 — OpenRouter Claude-Haiku "idea-writer".
 *
 * One of three chat call types in the skill-idea loop (alongside the topic→skill
 * rerank judge and the golden judge; embeddings are the fourth model capability).
 * An idea is a SYNTHESIZED, skill-ready concept — crisp guidance that could be
 * pasted into a skill body as-is — NOT a raw transcript snippet (the raw snippet
 * is kept only as provenance on the idea's `sources`).
 *
 * This writer is invoked in two places:
 *  - on idea CREATION (U10), to write the lesson from the topic `description` +
 *    the segment's `impl_learning`s;
 *  - on a MERGE (U7), to RE-SYNTHESIZE an existing idea's `text`, folding in the
 *    nuance a near-duplicate finding adds — corroboration sharpens the concept,
 *    not just the count. Re-synthesis takes the current `text` plus the incoming
 *    finding and rewrites a single tightened concept.
 *
 * Haiku is sufficient for this (the plan's Key Technical Decisions). Auth is the
 * shared `OPENROUTER_API_KEY` (read at call time, never hardcoded). A request
 * error is NOT swallowed.
 */

/** A skill-ready concept is short, foldable guidance — cap the rewrite tightly. */
const MAX_TOKENS = 512;

/** Resolve the configured idea-writer model id (env overrideable; defaults to the chat model). */
function modelId(): string | undefined {
  return process.env.OPENROUTER_IDEA_WRITER_MODEL;
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

/**
 * An OpenRouter Claude-Haiku idea-writer. The `fetch` impl is injectable so tests
 * drive it without touching the network (it defaults to the Node 20 global).
 * Auth/base/model come from the environment at call time.
 */
export class IdeaWriter {
  private fetchImpl?: FetchLike;

  constructor(fetchImpl?: FetchLike) {
    this.fetchImpl = fetchImpl;
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
   * Send one chat completion and return the trimmed text. A request error is NOT
   * swallowed — it propagates so the caller can retry/DLQ. An empty/garbled
   * response throws (inside `openRouterChat`) rather than silently writing an
   * empty concept.
   */
  private async invoke(userPrompt: string): Promise<string> {
    const text = await openRouterChat({
      system: SYSTEM_PROMPT,
      user: userPrompt,
      maxTokens: MAX_TOKENS,
      ...(modelId() ? { model: modelId() } : {}),
      ...(this.fetchImpl ? { fetchImpl: this.fetchImpl } : {}),
    });
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
