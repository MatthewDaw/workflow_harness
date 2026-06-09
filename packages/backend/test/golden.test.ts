import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import type { GoldenCase } from '@harness/shared';
import { GoldenJudge, replayGoldenCases } from '../src/rerank/golden.js';

/**
 * U18 — the Bedrock Claude-Haiku GOLDEN judge. The Bedrock Runtime client is
 * mocked so nothing touches the network. We assert: a satisfied verdict maps to
 * `satisfied: true`; a regression maps to `satisfied: false`; the lesson +
 * candidate body are both sent to the model; an unparseable verdict is surfaced
 * as a regression (advisory v1 errs toward flagging, never a silent pass); a
 * Bedrock error propagates; and `replayGoldenCases` runs every case.
 */

const bedrockMock = mockClient(BedrockRuntimeClient);
const client = new BedrockRuntimeClient({ region: 'us-east-1' });
const judge = new GoldenJudge(client);

beforeEach(() => bedrockMock.reset());

/** An Anthropic-Messages-shaped response body, encoded as Bedrock returns it. */
function anthropicBody(text: string): Uint8Array {
  return new TextEncoder().encode(JSON.stringify({ content: [{ type: 'text', text }] }));
}

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
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ satisfied: true, reason: 'still present' })),
    });
    const res = await judge.judge(goldenCase(), 'candidate body still has the lesson');
    expect(res).toMatchObject({ caseId: 'c-1', satisfied: true, reason: 'still present' });
  });

  it('maps a regression verdict to satisfied: false', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ satisfied: false, reason: 'lesson dropped' })),
    });
    const res = await judge.judge(goldenCase(), 'candidate body that removed the lesson');
    expect(res.satisfied).toBe(false);
    expect(res.reason).toBe('lesson dropped');
  });

  it('sends BOTH the lesson and the candidate body to the model', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ satisfied: true, reason: 'ok' })),
    });
    await judge.judge(goldenCase({ lesson: 'LESSON_TEXT' }), 'CANDIDATE_BODY');
    const body = JSON.parse(
      bedrockMock.commandCalls(InvokeModelCommand)[0]!.args[0].input.body as string,
    );
    expect(body.anthropic_version).toBe('bedrock-2023-05-31');
    const user = body.messages[0].content as string;
    expect(user).toContain('LESSON_TEXT');
    expect(user).toContain('CANDIDATE_BODY');
  });

  it('tolerates a ```json-fenced verdict', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody('```json\n{"satisfied": true, "reason": "fenced ok"}\n```'),
    });
    const res = await judge.judge(goldenCase(), 'candidate');
    expect(res.satisfied).toBe(true);
    expect(res.reason).toBe('fenced ok');
  });

  it('surfaces an unparseable verdict as a regression (never a silent pass)', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: anthropicBody('not json at all') });
    const res = await judge.judge(goldenCase(), 'candidate');
    expect(res.satisfied).toBe(false);
    expect(res.reason).toMatch(/unparseable/);
  });

  it('propagates a Bedrock error (does not swallow)', async () => {
    bedrockMock.on(InvokeModelCommand).rejects(new Error('ThrottlingException'));
    await expect(judge.judge(goldenCase(), 'candidate')).rejects.toThrow('ThrottlingException');
  });
});

describe('replayGoldenCases', () => {
  it('replays EVERY case against the candidate body', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ satisfied: true, reason: 'ok' })),
    });
    const cases = [goldenCase({ caseId: 'c-1' }), goldenCase({ caseId: 'c-2' })];
    const results = await replayGoldenCases(judge, cases, 'candidate body');
    expect(results.map((r) => r.caseId)).toEqual(['c-1', 'c-2']);
    expect(results.every((r) => r.satisfied)).toBe(true);
    expect(bedrockMock.commandCalls(InvokeModelCommand)).toHaveLength(2);
  });

  it('returns [] for an empty golden set without calling Bedrock', async () => {
    const results = await replayGoldenCases(judge, [], 'candidate');
    expect(results).toEqual([]);
    expect(bedrockMock.commandCalls(InvokeModelCommand)).toHaveLength(0);
  });
});
