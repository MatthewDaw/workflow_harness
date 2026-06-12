import type { GoldenCase, GoldenReplayResult } from '@harness/shared';
import { type FetchLike, openRouterChat } from '../llm/openrouter.js';

/**
 * U18 / U10 — the golden judge.
 *
 * **U10 DRY contract (MAT-141 Gap 3):** the canonical judge is the Python
 * learning service's ``judge.py`` (which reuses agent-families' ``judge.py``).
 * This TS module is now a **thin proxy**: when ``PYTHON_JUDGE_URL`` is set in
 * the environment the ``GoldenJudge`` forwards each verdict call to the Python
 * judge endpoint; when it is absent (local dev / legacy path) it falls back to
 * the OpenRouter chat call below.
 *
 * The Python judge endpoint (served by the learning-service Lambda):
 *   POST <PYTHON_JUDGE_URL>/judge/golden
 *   Body: { lesson, candidateBody }
 *   Returns: { satisfied: boolean, reason: string }
 *
 * Only ONE golden-judge *implementation* may exist (MAT-141 Gap 3). The single
 * authoritative verdict implementation is the Python golden-judge endpoint:
 *   packages/learning-service/src/learning_service/entrypoints/golden_judge.py
 * (POST /judge/golden). In the deployed Lambda ``PYTHON_JUDGE_URL`` is always
 * set, so EVERY production verdict is produced by that one Python judge. The
 * OpenRouter chat path below is NOT a second production judge — it is a local-dev
 * / test seam reached only when an explicit ``fetchImpl`` is injected (tests) or
 * ``PYTHON_JUDGE_URL`` is unset (local dev). It must never run in production.
 *
 * Golden-set regression at fold. Every fold captures the lesson it folded as a
 * before→after expectation (a `GoldenCase`: the body BEFORE, the body AFTER, and
 * the synthesized `lesson`). Before a later promote, this judge REPLAYS each
 * prior golden case against the CANDIDATE revision body and answers one question
 * per case: does the candidate STILL satisfy the prior lesson? A `satisfied:
 * false` is a REGRESSION — the candidate appears to undo an earlier fold.
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
 * The golden judge.
 *
 * **U10 DRY contract:** when ``PYTHON_JUDGE_URL`` is set in the environment,
 * each verdict call is forwarded to the Python learning-service judge endpoint.
 * The Python endpoint is the single authoritative implementation; this class is
 * a thin proxy (in the deployed Lambda with ``PYTHON_JUDGE_URL`` set) or a local
 * fallback (in dev / test, when ``PYTHON_JUDGE_URL`` is absent and an injectable
 * ``fetchImpl`` drives OpenRouter).
 *
 * The ``fetchImpl`` constructor argument is for offline tests only (it overrides
 * both the Python-proxy path and the local-fallback path so tests never touch
 * the network).
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
    const pythonJudgeUrl = process.env.PYTHON_JUDGE_URL;
    if (pythonJudgeUrl && !this.fetchImpl) {
      // Route to the Python judge (the single authoritative implementation).
      return this.invokeViaPython(pythonJudgeUrl, c, candidateBody);
    }
    // Local-dev / test fallback: OpenRouter chat (injectable fetchImpl for tests).
    const verdict = await this.invokeViaOpenRouter(renderCase(c.lesson, candidateBody));
    return {
      caseId: c.caseId,
      lesson: c.lesson,
      satisfied: verdict.satisfied === true,
      reason: verdict.reason ?? '',
    };
  }

  /**
   * Forward the verdict call to the Python learning-service judge endpoint.
   * Body: { lesson, candidateBody }
   * Response: { satisfied: boolean, reason: string }
   */
  private async invokeViaPython(
    baseUrl: string,
    c: GoldenCase,
    candidateBody: string,
  ): Promise<GoldenReplayResult> {
    const fetchFn = (globalThis as { fetch?: FetchLike }).fetch;
    if (!fetchFn) throw new Error('GoldenJudge: fetch not available for Python judge proxy');
    const resp = await fetchFn(`${baseUrl.replace(/\/$/, '')}/judge/golden`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lesson: c.lesson, candidateBody }),
    } as Parameters<FetchLike>[1]);
    const raw = await (resp as Response).json() as { satisfied?: boolean; reason?: string };
    return {
      caseId: c.caseId,
      lesson: c.lesson,
      satisfied: raw.satisfied === true,
      reason: raw.reason ?? '',
    };
  }

  /** Send one OpenRouter chat completion and parse the strict JSON verdict. */
  private async invokeViaOpenRouter(userPrompt: string): Promise<Verdict> {
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
