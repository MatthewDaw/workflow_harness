import type { GoldenCase, GoldenReplayResult } from '@harness/shared';
import { type FetchLike, openRouterChat } from '../llm/openrouter.js';

/**
 * U18 — the OpenRouter Claude-Haiku GOLDEN judge.
 *
 * Golden-set regression at fold. Every fold captures the lesson it folded as a
 * before→after expectation (a `GoldenCase`: the body BEFORE, the body AFTER, and
 * the synthesized `lesson`). Before a later promote, this judge REPLAYS each
 * prior golden case against the CANDIDATE revision body and answers one question
 * per case: does the candidate STILL satisfy the prior lesson? A `satisfied:
 * false` is a REGRESSION — the candidate appears to undo an earlier fold.
 *
 * This is one of three chat call types in the loop, alongside the topic→skill
 * rerank judge (U9) and the idea-writer (`ideas/synth.ts`); embeddings are the
 * fourth model capability. It mirrors the U9 / idea-writer judge contract
 * exactly: an OpenRouter chat completion, a strict structured verdict, Haiku is
 * enough. Auth is the shared `OPENROUTER_API_KEY` (read at call time, never
 * hardcoded). A request error is NOT swallowed; it propagates so the caller can
 * decide (advisory v1: a replay error surfaces, never silently passes a
 * regression).
 *
 * v1 is ADVISORY: this judge only REPORTS. The human is the gate at promote —
 * `replayGoldenCases` returns the per-case verdicts and the caller surfaces any
 * regression rather than hard-blocking the promote.
 */

/** A verdict is a small JSON object — cap the response tightly. */
const MAX_TOKENS = 256;

/** Resolve the configured golden-judge model id (env overrideable; defaults to the chat model). */
function modelId(): string | undefined {
  return process.env.OPENROUTER_GOLDEN_JUDGE_MODEL;
}

const SYSTEM_PROMPT =
  'You are a regression judge for a skill library. A skill was previously edited ' +
  'to encode a specific lesson. You are given that lesson and a CANDIDATE new ' +
  'version of the skill body. Decide whether the candidate STILL satisfies the ' +
  'lesson (the guidance is still present and not contradicted). Respond with ' +
  'ONLY a JSON object: {"satisfied": <true|false>, "reason": "<one short ' +
  'sentence>"}. Set satisfied=false ONLY when the candidate clearly drops or ' +
  'contradicts the lesson (a regression). No prose, no markdown fences.';

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
 * An OpenRouter Claude-Haiku golden judge. The `fetch` impl is injectable so
 * tests drive it without touching the network (it defaults to the Node 20
 * global). Auth/base/model come from the environment at call time.
 */
export class GoldenJudge {
  private fetchImpl?: FetchLike;

  constructor(fetchImpl?: FetchLike) {
    this.fetchImpl = fetchImpl;
  }

  /**
   * Judge a single golden case against a candidate body. Returns whether the
   * candidate still satisfies the case's `lesson`, plus the judge's rationale.
   * A request error is NOT swallowed; a garbled/empty verdict is treated as a
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

  /** Send one chat completion and parse the strict JSON verdict. */
  private async invoke(userPrompt: string): Promise<Verdict> {
    const text = await openRouterChat({
      system: SYSTEM_PROMPT,
      user: userPrompt,
      maxTokens: MAX_TOKENS,
      ...(modelId() ? { model: modelId() } : {}),
      ...(this.fetchImpl ? { fetchImpl: this.fetchImpl } : {}),
    });
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
 * hard-blocks a promote). Cases are judged sequentially so a request error on
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
