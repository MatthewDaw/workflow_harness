import { describe, expect, it } from 'vitest';
import { IdeaWriter } from '../src/ideas/synth.js';
import { chatBody, fetchRejecting, fetchResolving, postedBody } from './helpers/fetchmock.js';

/**
 * U7 — the OpenRouter Claude-Haiku idea-writer. The injected `fetch` is a stub so
 * nothing touches the network. We assert: `write` returns the synthesized concept
 * (trimmed); `merge` sends both the existing concept and the new finding
 * (re-synthesis, not concatenation); an empty/garbled response throws rather than
 * silently writing an empty concept; a request error propagates.
 */

describe('IdeaWriter.write', () => {
  it('returns the synthesized skill-ready concept (trimmed)', async () => {
    const writer = new IdeaWriter(
      fetchResolving(chatBody('  Use decimal types for money; never float.  ')),
    );
    const concept = await writer.write({
      description: 'money handling',
      implLearnings: ['avoid float for currency'],
    });
    expect(concept).toBe('Use decimal types for money; never float.');
  });

  it('sends the topic description and impl learnings to the model', async () => {
    const f = fetchResolving(chatBody('concept'));
    const writer = new IdeaWriter(f);
    await writer.write({ description: 'TOPIC_DESC', implLearnings: ['LEARNING_A'] });
    const body = postedBody(f);
    expect(body.model).toBe('anthropic/claude-haiku-4.5');
    const system = body.messages[0].content as string;
    const user = body.messages[1].content as string;
    expect(system).toContain('idea-writer');
    expect(user).toContain('TOPIC_DESC');
    expect(user).toContain('LEARNING_A');
  });
});

describe('IdeaWriter.merge', () => {
  it('re-synthesizes: sends both the existing concept and the new finding', async () => {
    const f = fetchResolving(chatBody('tighter merged concept'));
    const writer = new IdeaWriter(f);
    const merged = await writer.merge('EXISTING_CONCEPT', { description: 'NEW_NUANCE' });
    expect(merged).toBe('tighter merged concept');
    const user = postedBody(f).messages[1].content as string;
    expect(user).toContain('EXISTING_CONCEPT');
    expect(user).toContain('NEW_NUANCE');
  });
});

describe('IdeaWriter error handling', () => {
  it('throws on an empty response rather than writing an empty concept', async () => {
    const writer = new IdeaWriter(fetchResolving(chatBody('   ')));
    await expect(writer.write({ description: 'x' })).rejects.toThrow(/empty content/);
  });

  it('propagates a request error (does not swallow)', async () => {
    const writer = new IdeaWriter(fetchRejecting(new Error('ThrottlingException')));
    await expect(writer.write({ description: 'x' })).rejects.toThrow('ThrottlingException');
  });
});
