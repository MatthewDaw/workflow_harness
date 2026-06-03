import { BedrockRuntimeClient, InvokeModelCommand } from '@aws-sdk/client-bedrock-runtime';

/**
 * Forge prompt optimizer (the deferred "gradient-descent" / refine loop for U27).
 *
 * AgentForge v1 distills a starting prompt (propose.ts). This module takes that
 * draft and iteratively refines it: an LLM-as-judge scores the current best,
 * a generator produces a refined candidate using the critique, the judge scores
 * the candidate, and the candidate is kept only if it scores strictly higher.
 * The loop stops at `maxRounds` or when a round fails to improve by at least
 * `minImprovement` (plateau / early-stop).
 *
 * Both LLM calls (`generate`, `judge`) are injected so the pure loop is unit-
 * testable WITHOUT live AWS — tests pass deterministic fakes. A Bedrock-backed
 * default (`bedrockOptimizeDeps`) mirrors the client pattern in propose.ts for
 * production use.
 *
 * The returned `history` (each round's score + critique) is the "gradient": the
 * trajectory of improvement the editor (U24) can show alongside the final prompt.
 */

/** The starting distilled agent plus the grounding context the judge scores against. */
export interface OptimizeInput {
  /** The starting (distilled) prompt to improve. */
  prompt: string;
  /** What the agent is for; grounds coverage/actionability. */
  description: string;
  /** The agent's curated skills; the prompt should cover/reference these. */
  skills: string[];
  /** Evidence excerpts (e.g. session summaries) the prompt should stay grounded in. */
  evidence?: string[];
}

/** One round of the optimization trajectory. */
export interface OptimizeRound {
  round: number;
  score: number;
  critique: string;
}

export interface OptimizeResult {
  /** The highest-scoring prompt found (never worse than the starting prompt). */
  prompt: string;
  /** The score of the returned prompt. */
  score: number;
  /** How many refinement rounds ran (excludes the initial baseline scoring). */
  rounds: number;
  /** The score trajectory: the baseline (round 0) plus each refinement round. */
  history: OptimizeRound[];
}

export interface JudgeResult {
  /** 0..100 quality on specificity, groundedness, coverage, actionability. */
  score: number;
  critique: string;
}

/**
 * The two injectable LLM seams. Both are async so the Bedrock default fits, and
 * both take the grounding `context` so the judge can score groundedness and the
 * generator can stay anchored to the evidence.
 */
export interface OptimizeDeps {
  /** Produce a refined candidate prompt from the current prompt + a critique. */
  generate(promptToImprove: string, critique: string, context: OptimizeInput): Promise<string>;
  /** Score a candidate prompt 0..100 with a critique of what to fix next. */
  judge(candidatePrompt: string, context: OptimizeInput): Promise<JudgeResult>;
}

export interface OptimizeOpts {
  /** Maximum refinement rounds (default 4). */
  maxRounds?: number;
  /**
   * Minimum score gain a round must produce to avoid being treated as a plateau.
   * If a round improves by less than this, the loop early-stops (default 1.0).
   */
  minImprovement?: number;
}

/**
 * Iteratively optimize a distilled agent prompt.
 *
 * Baseline: judge the starting prompt (recorded as round 0). Then up to
 * `maxRounds` times: generate a refined candidate from the current best + its
 * critique, judge it, and keep it only if it scores strictly higher. The loop
 * early-stops when a round's improvement over the previous best is below
 * `minImprovement` (a plateau) — including when a candidate regresses.
 *
 * Guarantees:
 *  - Returns the highest-scoring variant seen.
 *  - Never regresses below the starting score.
 *  - `history` records every round's score (round 0 = baseline) and critique.
 */
export async function optimizeAgentPrompt(
  input: OptimizeInput,
  deps: OptimizeDeps,
  opts: OptimizeOpts = {},
): Promise<OptimizeResult> {
  const maxRounds = opts.maxRounds ?? 4;
  const minImprovement = opts.minImprovement ?? 1.0;

  // Round 0: score the starting prompt. This is the floor we never drop below.
  const baseline = await deps.judge(input.prompt, input);
  const history: OptimizeRound[] = [
    { round: 0, score: baseline.score, critique: baseline.critique },
  ];

  let bestPrompt = input.prompt;
  let bestScore = baseline.score;
  let lastCritique = baseline.critique;
  let rounds = 0;

  for (let r = 1; r <= maxRounds; r++) {
    const candidate = await deps.generate(bestPrompt, lastCritique, input);
    const scored = await deps.judge(candidate, input);
    rounds = r;
    history.push({ round: r, score: scored.score, critique: scored.critique });

    const improvement = scored.score - bestScore;
    // Keep the candidate only if it strictly improves on the best so far.
    if (improvement > 0) {
      bestPrompt = candidate;
      bestScore = scored.score;
      lastCritique = scored.critique;
    }
    // Plateau / regression: the round did not buy enough improvement. Stop.
    if (improvement < minImprovement) break;
  }

  return { prompt: bestPrompt, score: bestScore, rounds, history };
}

/**
 * Bedrock-backed default for `generate` + `judge`, mirroring the client pattern
 * in propose.ts (Claude via the Messages API on Bedrock). Production code wires
 * this; tests never touch it.
 */
export function bedrockOptimizeDeps(
  client: BedrockRuntimeClient,
  modelId: string = process.env.BEDROCK_TEXT_MODEL ??
    'anthropic.claude-3-5-sonnet-20240620-v1:0',
): OptimizeDeps {
  const invoke = async (prompt: string, maxTokens: number): Promise<string> => {
    const res = await client.send(
      new InvokeModelCommand({
        modelId,
        contentType: 'application/json',
        accept: 'application/json',
        body: JSON.stringify({
          anthropic_version: 'bedrock-2023-05-31',
          max_tokens: maxTokens,
          messages: [{ role: 'user', content: prompt }],
        }),
      }),
    );
    const decoded = JSON.parse(new TextDecoder().decode(res.body)) as {
      content?: { text?: string }[];
    };
    return decoded.content?.[0]?.text ?? '';
  };

  const contextBlock = (ctx: OptimizeInput): string =>
    `Agent purpose: "${ctx.description}".\n` +
    `Skills it must cover: ${ctx.skills.join(', ') || '(none)'}.\n` +
    `Grounding evidence:\n${(ctx.evidence ?? []).map((e) => `- ${e}`).join('\n') || '(none)'}`;

  return {
    async generate(promptToImprove, critique, context) {
      const text = await invoke(
        `You are refining a system prompt for an AI agent.\n\n${contextBlock(context)}\n\n` +
          `Current prompt:\n"""\n${promptToImprove}\n"""\n\n` +
          `A reviewer's critique to address:\n${critique}\n\n` +
          `Rewrite the prompt so it is more specific, grounded in the evidence, ` +
          `covers the skills, and is actionable. Avoid vague filler / AI-slop. ` +
          `Reply with ONLY the rewritten prompt, no preamble.`,
        1024,
      );
      return text.trim() || promptToImprove;
    },
    async judge(candidatePrompt, context) {
      const text = await invoke(
        `You are an exacting reviewer scoring an AI agent's system prompt 0..100 on: ` +
          `specificity, groundedness in the evidence, coverage of the skills, and ` +
          `actionability. Penalize vagueness and generic AI-slop.\n\n${contextBlock(context)}\n\n` +
          `Prompt to score:\n"""\n${candidatePrompt}\n"""\n\n` +
          `Reply as JSON: {"score": <0-100 number>, "critique": "<what to improve>"}.`,
        512,
      );
      try {
        const parsed = JSON.parse(text) as { score?: number; critique?: string };
        const score = typeof parsed.score === 'number' ? clamp(parsed.score, 0, 100) : 0;
        return { score, critique: parsed.critique ?? '' };
      } catch {
        return { score: 0, critique: text };
      }
    },
  };
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, n));
}
