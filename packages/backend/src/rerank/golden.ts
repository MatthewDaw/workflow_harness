import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import type { GoldenCase, GoldenReplayResult } from '@harness/shared';

/**
 * U18 — the Bedrock Claude-Haiku GOLDEN judge.
 *
 * Golden-set regression at fold. Every fold captures the lesson it folded as a
 * before→after expectation (a `GoldenCase`: the body BEFORE, the body AFTER, and
 * the synthesized `lesson`). Before a later promote, this judge REPLAYS each
 * prior golden case against the CANDIDATE revision body and answers one question
 * per case: does the candidate STILL satisfy the prior lesson? A `satisfied:
 * false` is a REGRESSION — the candidate appears to undo an earlier fold.
 *
 * This is the fourth Bedrock call type in the loop, alongside Titan embeddings
 * (`embeddings/`), the topic→skill rerank judge (U9), and the idea-writer
 * (`ideas/synth.ts`). It mirrors the U9 / idea-writer judge contract exactly:
 * Anthropic Messages on Bedrock, a strict structured verdict, Haiku is enough.
 * Auth is IAM/Bedrock runtime — there is NO API key. A Bedrock error is NOT
 * swallowed; it propagates so the caller can decide (advisory v1: a replay error
 * surfaces, never silently passes a regression).
 *
 * v1 is ADVISORY: this judge only REPORTS. The human is the gate at promote —
 * `replayGoldenCases` returns the per-case verdicts and the caller surfaces any
 * regression rather than hard-blocking the promote.
 */

/** The Claude Haiku model id. Overridable via env so a model bump needs no code change. */
const DEFAULT_MODEL_ID = 'us.anthropic.claude-3-5-haiku-20241022-v1:0';
/** The Anthropic-on-Bedrock invoke contract version. */
const ANTHROPIC_VERSION = 'bedrock-2023-05-31';
/** A verdict is a small JSON object — cap the response tightly. */
const MAX_TOKENS = 256;

/** Resolve the configured golden-judge model id (env overrideable; defaults to Haiku). */
function modelId(): string {
  return process.env.BEDROCK_GOLDEN_JUDGE_MODEL_ID ?? DEFAULT_MODEL_ID;
}

const SYSTEM_PROMPT =
  'You are a regression judge for a skill library. A skill was previously edited ' +
  'to encode a specific lesson. You are given that lesson and a CANDIDATE new ' +
  'version of the skill body. Decide whether the candidate STILL satisfies the ' +
  'lesson (the guidance is still present and not contradicted). Respond with ' +
  'ONLY a JSON object: {"satisfied": <true|false>, "reason": "<one short ' +
  'sentence>"}. Set satisfied=false ONLY when the candidate clearly drops or ' +
  'contradicts the lesson (a regression). No prose, no markdown fences.';

/** The Anthropic-on-Bedrock invoke-response body shape (the fields we read). */
interface AnthropicResponse {
  content?: Array<{ type?: string; text?: string }>;
}

/** The strict verdict the model is asked to emit. */
interface Verdict {
  satisfied?: boolean;
  reason?: string;
}

/** Render one case's lesson + candidate body into the judge prompt body. */
function renderCase(lesson: string, candidateBody: string): string {
  return (
    `Lesson the skill was edited to encode:\n${lesson}\n\n` +
    `Candidate new skill body:\n${candidateBody}`
  );
}

/**
 * A Bedrock Claude-Haiku golden judge. The Bedrock Runtime client is created
 * lazily and memoised across warm Lambda invocations (mirrors `ideas/synth.ts`),
 * and is injectable so tests mock it without touching the network. Region /
 * credentials come from the environment / IAM role.
 */
export class GoldenJudge {
  private client: BedrockRuntimeClient;

  constructor(client?: BedrockRuntimeClient) {
    this.client = client ?? new BedrockRuntimeClient({});
  }

  /**
   * Judge a single golden case against a candidate body. Returns whether the
   * candidate still satisfies the case's `lesson`, plus the judge's rationale.
   * A Bedrock error is NOT swallowed; a garbled/empty response is treated as a
   * regression to surface (advisory v1 errs toward flagging, never toward a
   * silent pass).
   */
  async judge(c: GoldenCase, candidateBody: string): Promise<GoldenReplayResult> {
    const verdict = await this.invoke(renderCase(c.lesson, candidateBody));
    return {
      caseId: c.caseId,
      lesson: c.lesson,
      satisfied: verdict.satisfied === true,
      reason: verdict.reason ?? '',
    };
  }

  /** Send one Messages-API invoke and parse the strict JSON verdict. */
  private async invoke(userPrompt: string): Promise<Verdict> {
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
      return JSON.parse(json) as Verdict;
    } catch {
      // A garbled verdict is surfaced as a regression to flag, not a silent pass.
      return { satisfied: false, reason: 'golden judge returned an unparseable verdict' };
    }
  }
}

/** Lazy, memoised default golden judge for the Lambda runtime (tests inject their own). */
let defaultGoldenJudge: GoldenJudge | undefined;

/** The process-wide default golden judge, created on first use. */
export function getGoldenJudge(): GoldenJudge {
  defaultGoldenJudge ??= new GoldenJudge();
  return defaultGoldenJudge;
}

/**
 * Replay EVERY golden case for a skill against a candidate revision body, in
 * order, returning the per-case verdicts. The caller surfaces any
 * `satisfied: false` as a regression to the human (advisory v1 — this never
 * hard-blocks a promote). Cases are judged sequentially so a Bedrock error on
 * one case propagates rather than racing other invokes; with the small golden
 * sets per skill this is fine.
 */
export async function replayGoldenCases(
  judge: GoldenJudge,
  cases: GoldenCase[],
  candidateBody: string,
): Promise<GoldenReplayResult[]> {
  const results: GoldenReplayResult[] = [];
  for (const c of cases) {
    results.push(await judge.judge(c, candidateBody));
  }
  return results;
}
