import { describe, expect, it } from 'vitest';
import { RerankJudge, type JudgeCandidate, type JudgeTopic } from '../src/rerank/judge.js';
import { chatBody, fetchRejecting, fetchResolving, postedBody } from './helpers/fetchmock.js';

/**
 * U9 — the OpenRouter Claude-Haiku RERANK judge, in isolation. The injected
 * `fetch` is a stub so nothing touches the network. We assert: a `best` verdict
 * maps through with its confidence; a null pick maps to `none`; a name the judge
 * was NOT offered collapses to `none` (no hallucinated associations); the topic +
 * candidate names/descriptions are sent; a ```json fence is tolerated; an
 * unparseable verdict becomes `none`; an empty candidate list never calls the
 * model; and a request error propagates.
 */

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
    const judge = new RerankJudge(
      fetchResolving(chatBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 0.92 }))),
    );
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.92 });
  });

  it('maps a null pick to none', async () => {
    const judge = new RerankJudge(
      fetchResolving(chatBody(JSON.stringify({ skillBaseName: null, confidence: 0 }))),
    );
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('collapses a not-offered (hallucinated) name to none', async () => {
    const judge = new RerankJudge(
      fetchResolving(chatBody(JSON.stringify({ skillBaseName: 'hq-made-up', confidence: 0.99 }))),
    );
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('clamps an out-of-range confidence into [0,1]', async () => {
    const judge = new RerankJudge(
      fetchResolving(chatBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 1.5 }))),
    );
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 1 });
  });

  it('sends the topic and candidate names/descriptions to the model', async () => {
    const f = fetchResolving(chatBody(JSON.stringify({ skillBaseName: 'hq-money', confidence: 0.8 })));
    const judge = new RerankJudge(f);
    await judge.judge(TOPIC, CANDIDATES);
    const body = postedBody(f);
    expect(body.model).toBe('anthropic/claude-haiku-4.5');
    const system = body.messages[0].content as string;
    const user = body.messages[1].content as string;
    expect(system).toContain('routing judge');
    expect(user).toContain('decimal money handling');
    expect(user).toContain('hq-money');
    expect(user).toContain('Money and currency handling.');
    expect(user).toContain('hq-retry');
  });

  it('tolerates a ```json-fenced verdict', async () => {
    const judge = new RerankJudge(
      fetchResolving(chatBody('```json\n{"skillBaseName": "hq-money", "confidence": 0.7}\n```')),
    );
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'best', skillBaseName: 'hq-money', confidence: 0.7 });
  });

  it('treats an unparseable verdict as none (never a wrong pick)', async () => {
    const judge = new RerankJudge(fetchResolving(chatBody('not json at all')));
    const v = await judge.judge(TOPIC, CANDIDATES);
    expect(v).toEqual({ outcome: 'none' });
  });

  it('returns none for an empty candidate list without calling the model', async () => {
    const f = fetchResolving(chatBody('{}'));
    const judge = new RerankJudge(f);
    const v = await judge.judge(TOPIC, []);
    expect(v).toEqual({ outcome: 'none' });
    expect(f.mock.calls).toHaveLength(0);
  });

  it('propagates a request error (does not swallow)', async () => {
    const judge = new RerankJudge(fetchRejecting(new Error('ThrottlingException')));
    await expect(judge.judge(TOPIC, CANDIDATES)).rejects.toThrow('ThrottlingException');
  });
});
