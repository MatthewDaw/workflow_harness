/**
 * one-golden-judge.test.ts — MAT-141 (U10) Gap 3.
 *
 * Proves the standalone TS golden judge is SUPERSEDED by the one Python judge:
 * in production (PYTHON_JUDGE_URL set, no injected fetch) the GoldenJudge forwards
 * each verdict to the Python `/judge/golden` endpoint and does NOT run an
 * independent OpenRouter verdict — so only ONE judge implementation produces the
 * verdict.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import type { GoldenCase } from '@harness/shared';
import { GoldenJudge } from '../src/rerank/golden.js';

function goldenCase(over: Partial<GoldenCase> = {}): GoldenCase {
  return {
    caseId: 'c-1',
    skillBaseName: 'reconcile',
    org: 'acme',
    ideaId: 'i-1',
    lesson: 'Always reconcile in the ledger currency.',
    before: 'before',
    after: 'after',
    foldedIntoRev: 2,
    createdAt: 1,
    ...over,
  };
}

describe('Gap 3 — production routes to the ONE Python golden judge', () => {
  const realFetch = globalThis.fetch;

  afterEach(() => {
    delete process.env.PYTHON_JUDGE_URL;
    globalThis.fetch = realFetch;
  });

  it('forwards the verdict to the Python /judge/golden endpoint (no OpenRouter call)', async () => {
    process.env.PYTHON_JUDGE_URL = 'https://learning.internal';

    const calls: Array<{ url: string; body: unknown }> = [];
    // Stub the global fetch the Python proxy uses. If the judge instead ran its
    // OpenRouter path it would post to openrouter.ai, not this endpoint.
    globalThis.fetch = vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, body: JSON.parse(init.body as string) });
      return new Response(JSON.stringify({ satisfied: true, reason: 'python verdict' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    }) as unknown as typeof fetch;

    // No injected fetchImpl → production proxy path.
    const judge = new GoldenJudge();
    const res = await judge.judge(goldenCase({ lesson: 'L1' }), 'CANDIDATE');

    // The single Python judge produced the verdict.
    expect(res.satisfied).toBe(true);
    expect(res.reason).toBe('python verdict');

    // It hit the Python golden endpoint with the lesson + candidate, NOT OpenRouter.
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe('https://learning.internal/judge/golden');
    expect(calls[0]!.url).not.toContain('openrouter');
    expect(calls[0]!.body).toMatchObject({ lesson: 'L1', candidateBody: 'CANDIDATE' });
  });
});
