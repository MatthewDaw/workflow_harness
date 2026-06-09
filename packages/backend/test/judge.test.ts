import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { RerankJudge, type JudgeCandidate, type JudgeTopic } from '../src/rerank/judge.js';

/**
 * U9 — the Bedrock Claude-Haiku RERANK judge, in isolation. The Bedrock Runtime
 * client is mocked so nothing touches the network. We assert: a `best` verdict
 * maps through with its confidence; a null pick maps to `none`; a name the judge
 * was NOT offered collapses to `none` (no hallucinated associations); the topic +
 * candidate names/descriptions are sent; a ```json fence is tolerated; an
 * unparseable verdict becomes `none`; an empty candidate list never calls
 * Bedrock; and a Bedrock error propagates.
 */

const bedrockMock = mockClient(BedrockRuntimeClient);
const client = new BedrockRuntimeClient({ region: 'us-east-1' });
const judge = new RerankJudge(client);

beforeEach(() => bedrockMock.reset());

function anthropicBody(text: string): Uint8Array {
  return new TextEncoder().encode(JSON.stringify({ content: [{ type: 'text', text }] }));
}

const TOPIC: JudgeTopic = {
  topicLabel: 'decimal money handling',
  description: 'Always use a decimal type for currency.',
};
const CANDIDATES: JudgeCandidate[] = [
  { skillBaseName: 'hq-money', description: 'Money and currency handling.' },
  { skillBaseName: 'hq-retry', description: 'Retry/backoff.' },
];

describe('RerankJudge.judge', () => {
  it('maps a best verdict to the chosen skill + confidence', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 0.92 })),
    });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.92 });
  });

  it('maps a null pick to none', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ skillBaseName: null, confidence: 0 })),
    });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('collapses a not-offered (hallucinated) name to none', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ skillBaseName: 'hq-made-up', confidence: 0.99 })),
    });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('clamps an out-of-range confidence into [0,1]', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 1.5 })),
    });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 1 });
  });

  it('sends the topic and candidate names/descriptions to the model', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 0.8 })),
    });
    await judge.judge(TOPIC, CANDIDATES);
    const body = JSON.parse(
      bedrockMock.commandCalls(InvokeModelCommand)[0]!.args[0].input.body as string,
    );
    expect(body.anthropic_version).toBe('bedrock-2023-05-31');
    const user = body.messages[0].content as string;
    expect(user).toContain('decimal money handling');
    expect(user).toContain('hq-money');
    expect(user).toContain('Money and currency handling.');
    expect(user).toContain('hq-retry');
  });

  it('tolerates a ```json-fenced verdict', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody('```json\n{"skillBaseName": "hq-money", "confidence": 0.7}\n```'),
    });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.7 });
  });

  it('treats an unparseable verdict as none (never a wrong pick)', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: anthropicBody('not json at all') });
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('returns none for an empty candidate list without calling Bedrock', async () => {
    const v = await judge.judge(TOPIC, []);
    expect(v).toEqual({ outcome: 'none' });
    expect(bedrockMock.commandCalls(InvokeModelCommand)).toHaveLength(0);
  });

  it('propagates a Bedrock error (does not swallow)', async () => {
    bedrockMock.on(InvokeModelCommand).rejects(new Error('ThrottlingException'));
    await expect(judge.judge(TOPIC, CANDIDATES)).rejects.toThrow('ThrottlingException');
  });
});
