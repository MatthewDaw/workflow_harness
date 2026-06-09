import { describe, expect, it } from 'vitest';
import type { GoldenCase } from '@harness/shared';
import { GoldenJudge, replayGoldenCases } from '../src/rerank/golden.js';
import { chatBody, fetchRejecting, fetchResolving, postedBody } from './helpers/fetchmock.js';

/**
 * U18 — the OpenRouter Claude-Haiku GOLDEN judge. The injected `fetch` is a stub
 * so nothing touches the network. We assert: a satisfied verdict maps to
 * `satisfied: true`; a regression maps to `satisfied: false`; the lesson +
 * candidate body are both sent to the model; an unparseable verdict is surfaced
 * as a regression (advisory v1 errs toward flagging, never a silent pass); a
 * request error propagates; and `replayGoldenCases` runs every case.
 */

function goldenCase(over: Partial<GoldenCase> = {}): GoldenCase {
  return {
    caseId: 'c-1',
    skillBaseName: 'reconcile',
    org: 'acme',
    ideaId: 'i-1',
    lesson: 'Always reconcile in the ledger currency.',
    before: 'before body',
    after: 'after body with the lesson',
    foldedIntoRev: 2,
    createdAt: 1,
    ...over,
  };
}

describe('GoldenJudge.judge', () => {
  it('maps a satisfied verdict to satisfied: true', async () => {
    const judge = new GoldenJudge(
      fetchResolving(chatBody(JSON.stringify({ satisfied: true, reason: 'still present' }))),
    );
    const res = await judge.judge(goldenCase(), 'candidate body still has the lesson');
    expect(res).toMatchObject({ caseId: 'c-1', satisfied: true, reason: 'still present' });
  });

  it('maps a regression verdict to satisfied: false', async () => {
    const judge = new GoldenJudge(
      fetchResolving(chatBody(JSON.stringify({ satisfied: false, reason: 'lesson dropped' }))),
    );
    const res = await judge.judge(goldenCase(), 'candidate body that removed the lesson');
    expect(res.satisfied).toBe(false);
    expect(res.reason).toBe('lesson dropped');
  });

  it('sends BOTH the lesson and the candidate body to the model', async () => {
    const f = fetchResolving(chatBody(JSON.stringify({ satisfied: true, reason: 'ok' })));
    const judge = new GoldenJudge(f);
    await judge.judge(goldenCase({ lesson: 'LESSON_TEXT' }), 'CANDIDATE_BODY');
    const body = postedBody(f);
    expect(body.model).toBe('anthropic/claude-haiku-4.5');
    const system = body.messages[0].content as string;
    const user = body.messages[1].content as string;
    expect(system).toContain('regression judge');
    expect(user).toContain('LESSON_TEXT');
    expect(user).toContain('CANDIDATE_BODY');
  });

  it('tolerates a ```json-fenced verdict', async () => {
    const judge = new GoldenJudge(
      fetchResolving(chatBody('```json\n{"satisfied": true, "reason": "fenced ok"}\n```')),
    );
    const res = await judge.judge(goldenCase(), 'candidate');
    expect(res.satisfied).toBe(true);
    expect(res.reason).toBe('fenced ok');
  });

  it('surfaces an unparseable verdict as a regression (never a silent pass)', async () => {
    const judge = new GoldenJudge(fetchResolving(chatBody('not json at all')));
    const res = await judge.judge(goldenCase(), 'candidate');
    expect(res.satisfied).toBe(false);
    expect(res.reason).toMatch(/unparseable/);
  });

  it('propagates a request error (does not swallow)', async () => {
    const judge = new GoldenJudge(fetchRejecting(new Error('ThrottlingException')));
    await expect(judge.judge(goldenCase(), 'candidate')).rejects.toThrow('ThrottlingException');
  });
});

describe('replayGoldenCases', () => {
  it('replays EVERY case against the candidate body', async () => {
    const f = fetchResolving(chatBody(JSON.stringify({ satisfied: true, reason: 'ok' })));
    const judge = new GoldenJudge(f);
    const cases = [goldenCase({ caseId: 'c-1' }), goldenCase({ caseId: 'c-2' })];
    const results = await replayGoldenCases(judge, cases, 'candidate body');
    expect(results.map((r) => r.caseId)).toEqual(['c-1', 'c-2']);
    expect(results.every((r) => r.satisfied)).toBe(true);
    expect(f.mock.calls).toHaveLength(2);
  });

  it('returns [] for an empty golden set without calling the model', async () => {
    const f = fetchResolving(chatBody('{}'));
    const judge = new GoldenJudge(f);
    const results = await replayGoldenCases(judge, [], 'candidate');
    expect(results).toEqual([]);
    expect(f.mock.calls).toHaveLength(0);
  });
});
