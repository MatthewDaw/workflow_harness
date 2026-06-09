import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';

/**
 * U9 — the Bedrock Claude-Haiku topic→skill RERANK judge.
 *
 * U8 retrieval hands the judge the topic plus the top-k candidate skills that
 * cleared the pre-judge similarity FLOOR. The embedding pre-filter is the part
 * Claude Code's native skill selection lacks; this judge mirrors native
 * selection over that FOCUSED set, keeping rerank cost flat as the catalog
 * grows. The judge picks the SINGLE best `skillBaseName` with a confidence, or
 * answers `none` (no candidate is a real home for the topic).
 *
 * TWO thresholds gate the pipeline (the plan's "three knobs"):
 *  - the U8 similarity FLOOR — does anything reach the judge at all;
 *  - the judge CONFIDENCE BAR (here) — does the judge's pick clear the bar.
 * A `none` verdict, OR a `best` whose confidence is below the bar, routes the
 * topic to the unassigned bin (R6). The bin entry carries topic provenance (R7).
 *
 * This is the fourth Bedrock call type in the loop, alongside Titan embeddings
 * (`embeddings/`), the idea-writer (`ideas/synth.ts`), and the golden judge
 * (`rerank/golden.ts`). It mirrors their contract EXACTLY: Anthropic Messages on
 * Bedrock, a strict structured verdict, Haiku is enough. Auth is IAM/Bedrock
 * runtime — there is NO API key. A Bedrock error is NOT swallowed; it propagates
 * so the stream consumer marks the record a batch-item-failure and the stream
 * redelivers/DLQs it rather than silently dropping a topic. A garbled/empty
 * verdict is treated as `none` (route to the bin) rather than a wrong pick.
 */

/** The Claude Haiku model id. Overridable via env so a model bump needs no code change. */
const DEFAULT_MODEL_ID = 'us.anthropic.claude-3-5-haiku-20241022-v1:0';
/** The Anthropic-on-Bedrock invoke contract version. */
const ANTHROPIC_VERSION = 'bedrock-2023-05-31';
/** A verdict is a small JSON object — cap the response tightly. */
const MAX_TOKENS = 256;

/**
 * Judge confidence bar (in [0,1]). A `best` pick whose confidence is BELOW this
 * routes to the unassigned bin (R6). Env overrideable for tuning against real
 * topics; conservative default kept here as a documented knob (the second of the
 * two thresholds, alongside U8's similarity floor).
 */
export const JUDGE_CONFIDENCE_BAR = Number(process.env.JUDGE_CONFIDENCE_BAR ?? 0.6);

/** Resolve the configured judge confidence bar at call time (env overrideable). */
export function judgeConfidenceBar(): number {
  return Number(process.env.JUDGE_CONFIDENCE_BAR ?? JUDGE_CONFIDENCE_BAR);
}

/** Resolve the configured rerank-judge model id (env overrideable; defaults to Haiku). */
function modelId(): string {
  return process.env.BEDROCK_RERANK_JUDGE_MODEL_ID ?? DEFAULT_MODEL_ID;
}

/** The topic the judge is reranking candidates against (label + rich summary). */
export interface JudgeTopic {
  /** The stable topic label. */
  topicLabel: string;
  /** The rich, self-contained topic summary the judge reasons over. */
  description: string;
}

/** One candidate skill the judge chooses among (name + description). */
export interface JudgeCandidate {
  /** The skill family name — the judge answers with one of these verbatim. */
  skillBaseName: string;
  /** The skill's description — what the judge reasons over (may be empty). */
  description: string;
}

/**
 * The judge's verdict. `outcome: 'best'` carries the chosen `skillBaseName` and a
 * confidence in [0,1]; `outcome: 'none'` means no candidate is a real home for
 * the topic. The CALLER applies the confidence bar — a `best` below the bar is
 * routed to the bin exactly like a `none` (see `associate.ts`).
 */
export type JudgeVerdict =
  | { outcome: 'best'; skillBaseName: string; confidence: number }
  | { outcome: 'none' };

const SYSTEM_PROMPT =
  'You are a routing judge for a skill library. You are given a session TOPIC ' +
  '(a lesson/correction that surfaced during work) and a short list of CANDIDATE ' +
  'skills (name + description) that a vector pre-filter found similar. Pick the ' +
  'SINGLE best skill the topic belongs to — the one whose body this lesson could ' +
  'be folded into — or answer none if NONE of the candidates is a genuine home ' +
  'for it. Respond with ONLY a JSON object and nothing else: ' +
  '{"skillBaseName": "<exact candidate name|null>", "confidence": <number 0..1>}. ' +
  'Use the exact skillBaseName as given. Set skillBaseName to null when no ' +
  'candidate fits. confidence is your certainty in the pick (0 when null). ' +
  'No prose, no markdown fences.';

/** Render the topic + candidate skills into the judge prompt body. */
function renderPrompt(topic: JudgeTopic, candidates: JudgeCandidate[]): string {
  const list = candidates
    .map(
      (c, i) =>
        `${i + 1}. ${c.skillBaseName}\n   ${c.description.trim() || '(no description)'}`,
    )
    .join('\n');
  return (
    `Topic label: ${topic.topicLabel}\n` +
    `Topic description:\n${topic.description}\n\n` +
    `Candidate skills:\n${list}`
  );
}

/** The Anthropic-on-Bedrock invoke-response body shape (the fields we read). */
interface AnthropicResponse {
  content?: Array<{ type?: string; text?: string }>;
}

/** The strict verdict the model is asked to emit. */
interface RawVerdict {
  skillBaseName?: string | null;
  confidence?: number;
}

/**
 * A Bedrock Claude-Haiku rerank judge. The Bedrock Runtime client is created
 * lazily and memoised across warm Lambda invocations (mirrors `ideas/synth.ts`
 * and `rerank/golden.ts`), and is injectable so tests mock it without touching
 * the network. Region / credentials come from the environment / IAM role.
 */
export class RerankJudge {
  private client: BedrockRuntimeClient;

  constructor(client?: BedrockRuntimeClient) {
    this.client = client ?? new BedrockRuntimeClient({});
  }

  /**
   * Pick the single best candidate skill for the topic, or `none`. The verdict's
   * `skillBaseName` is validated against the supplied candidates — a model that
   * names a skill that was not offered (or names nothing) collapses to `none`,
   * so a hallucinated name can never become a real association. The caller then
   * applies the confidence bar.
   */
  async judge(topic: JudgeTopic, candidates: JudgeCandidate[]): Promise<JudgeVerdict> {
    if (candidates.length === 0) return { outcome: 'none' };
    const raw = await this.invoke(renderPrompt(topic, candidates));
    const name = raw.skillBaseName;
    if (name == null) return { outcome: 'none' };
    // Reject a name the judge was not offered (no hallucinated associations).
    if (!candidates.some((c) => c.skillBaseName === name)) return { outcome: 'none' };
    const confidence = clamp01(raw.confidence);
    return { outcome: 'best', skillBaseName: name, confidence };
  }

  /** Send one Messages-API invoke and parse the strict JSON verdict. */
  private async invoke(userPrompt: string): Promise<RawVerdict> {
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
    // The model is told to emit bare JSON; tolerate an accidental ```json fence.
    const json = text.replace(/^```(?:json)?/i, '').replace(/```$/, '').trim();
    try {
      return JSON.parse(json) as RawVerdict;
    } catch {
      // A garbled verdict routes to the bin (none), never a wrong pick.
      return { skillBaseName: null };
    }
  }
}

/** Clamp a confidence to [0,1]; a missing/NaN value reads as 0 (route to bin). */
function clamp01(n: number | undefined): number {
  if (typeof n !== 'number' || Number.isNaN(n)) return 0;
  return Math.max(0, Math.min(1, n));
}

/** Lazy, memoised default rerank judge for the Lambda runtime (tests inject their own). */
let defaultRerankJudge: RerankJudge | undefined;

/** The process-wide default rerank judge, created on first use. */
export function getRerankJudge(): RerankJudge {
  defaultRerankJudge ??= new RerankJudge();
  return defaultRerankJudge;
}
