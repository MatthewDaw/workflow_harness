import { describe, expect, it, vi } from 'vitest';
import {
  optimizeAgentPrompt,
  type OptimizeDeps,
  type OptimizeInput,
} from '../src/forge/optimize.js';

/**
 * The deferred AgentForge prompt-optimization ("gradient-descent") loop. These
 * exercise the PURE loop with deterministic fake generate/judge — no network,
 * no AWS. They assert: it returns the highest-scoring variant, never regresses
 * below the starting score, respects maxRounds, early-stops on a plateau, and
 * records each round's score in history.
 */

const input: OptimizeInput = {
  prompt: 'v0',
  description: 'run database migrations safely',
  skills: ['db-migrate', 'review'],
  evidence: ['s1: schema migration with rollback', 's2: migration dry-run first'],
};

/**
 * A fake generator that just labels each candidate by round (v1, v2, ...) and a
 * judge driven by a fixed score table keyed on the candidate prompt. Deterministic.
 */
function fakeDeps(scoreByPrompt: Record<string, number>): OptimizeDeps {
  let round = 0;
  return {
    async generate() {
      round += 1;
      return `v${round}`;
    },
    async judge(candidatePrompt) {
      return { score: scoreByPrompt[candidatePrompt] ?? 0, critique: `fix ${candidatePrompt}` };
    },
  };
}

describe('optimizeAgentPrompt', () => {
  it('returns the highest-scoring variant and climbs the gradient', async () => {
    // Monotonic improvement: each round beats the last by a wide margin.
    const deps = fakeDeps({ v0: 40, v1: 60, v2: 75, v3: 88, v4: 95 });
    const result = await optimizeAgentPrompt(input, deps, { maxRounds: 4, minImprovement: 1 });

    expect(result.prompt).toBe('v4');
    expect(result.score).toBe(95);
    expect(result.rounds).toBe(4);
    // history is baseline + 4 rounds, strictly ascending scores.
    expect(result.history.map((h) => h.score)).toEqual([40, 60, 75, 88, 95]);
    expect(result.history[0]?.round).toBe(0);
  });

  it('never regresses below the starting score and keeps the best seen', async () => {
    // The starting prompt is already strong; refinements only make it worse.
    const deps = fakeDeps({ v0: 90, v1: 50, v2: 30 });
    const result = await optimizeAgentPrompt(input, deps, { maxRounds: 4, minImprovement: 1 });

    expect(result.prompt).toBe('v0');
    expect(result.score).toBe(90);
    // First regressing round (improvement < minImprovement) early-stops the loop.
    expect(result.rounds).toBe(1);
    // The regression is still recorded in the trajectory.
    expect(result.history.map((h) => h.score)).toEqual([90, 50]);
  });

  it('respects maxRounds even while still improving', async () => {
    const generate = vi.fn(async () => 'cand');
    let n = 0;
    const judge = vi.fn(async () => ({ score: (n += 10), critique: '' }));
    const result = await optimizeAgentPrompt(input, { generate, judge }, { maxRounds: 2 });

    expect(result.rounds).toBe(2);
    // 1 baseline judge + 2 refinement judges; generate called once per round.
    expect(judge).toHaveBeenCalledTimes(3);
    expect(generate).toHaveBeenCalledTimes(2);
    expect(result.history).toHaveLength(3); // round 0,1,2
  });

  it('early-stops on a plateau (improvement below minImprovement)', async () => {
    // Round 1 gains +5, round 2 would only gain +0.5 -> plateau, stop after round 2.
    const deps = fakeDeps({ v0: 70, v1: 75, v2: 75.5, v3: 99 });
    const result = await optimizeAgentPrompt(input, deps, { maxRounds: 4, minImprovement: 1 });

    expect(result.rounds).toBe(2);
    expect(result.prompt).toBe('v2'); // v2 still improved, so it is kept...
    expect(result.score).toBe(75.5);
    // ...but the sub-threshold gain ended the loop before round 3/4 ran.
    expect(result.history.map((h) => h.round)).toEqual([0, 1, 2]);
  });

  it('records baseline + each round score and critique in history', async () => {
    const deps = fakeDeps({ v0: 40, v1: 80 });
    const result = await optimizeAgentPrompt(input, deps, { maxRounds: 1, minImprovement: 1 });

    expect(result.history).toEqual([
      { round: 0, score: 40, critique: 'fix v0' },
      { round: 1, score: 80, critique: 'fix v1' },
    ]);
  });

  it('feeds the kept candidate forward (best prompt + its critique drive the next round)', async () => {
    const seen: { prompt: string; critique: string }[] = [];
    let round = 0;
    const deps: OptimizeDeps = {
      async generate(promptToImprove, critique) {
        seen.push({ prompt: promptToImprove, critique });
        round += 1;
        return `v${round}`;
      },
      async judge(candidatePrompt) {
        const table: Record<string, number> = { v0: 40, v1: 70, v2: 90 };
        return { score: table[candidatePrompt] ?? 0, critique: `crit-${candidatePrompt}` };
      },
    };
    await optimizeAgentPrompt(input, deps, { maxRounds: 2, minImprovement: 1 });

    // Round 1 refines v0 with v0's critique; round 2 refines the kept v1 with v1's critique.
    expect(seen[0]).toEqual({ prompt: 'v0', critique: 'crit-v0' });
    expect(seen[1]).toEqual({ prompt: 'v1', critique: 'crit-v1' });
  });
});
