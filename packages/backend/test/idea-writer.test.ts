import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import {
  BedrockRuntimeClient,
  InvokeModelCommand,
} from '@aws-sdk/client-bedrock-runtime';
import { IdeaWriter } from '../src/ideas/synth.js';

/**
 * U7 — the Bedrock Claude-Haiku idea-writer. The Bedrock Runtime client is
 * mocked so nothing touches the network. We assert: `write` returns the
 * synthesized concept; `merge` sends both the existing concept and the new
 * finding (re-synthesis, not concatenation); an empty/garbled response throws
 * rather than silently writing an empty concept; a Bedrock error propagates.
 */

const bedrockMock = mockClient(BedrockRuntimeClient);
const client = new BedrockRuntimeClient({ region: 'us-east-1' });
const writer = new IdeaWriter(client);

beforeEach(() => bedrockMock.reset());

/** An Anthropic-Messages-shaped response body, encoded as Bedrock returns it. */
function anthropicBody(text: string): Uint8Array {
  return new TextEncoder().encode(JSON.stringify({ content: [{ type: 'text', text }] }));
}

describe('IdeaWriter.write', () => {
  it('returns the synthesized skill-ready concept (trimmed)', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({
      body: anthropicBody('  Use decimal types for money; never float.  '),
    });
    const concept = await writer.write({
      description: 'money handling',
      implLearnings: ['avoid float for currency'],
    });
    expect(concept).toBe('Use decimal types for money; never float.');
  });

  it('sends the topic description and impl learnings to the model', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: anthropicBody('concept') });
    await writer.write({ description: 'TOPIC_DESC', implLearnings: ['LEARNING_A'] });
    const body = JSON.parse(
      bedrockMock.commandCalls(InvokeModelCommand)[0]!.args[0].input.body as string,
    );
    expect(body.anthropic_version).toBe('bedrock-2023-05-31');
    const user = body.messages[0].content as string;
    expect(user).toContain('TOPIC_DESC');
    expect(user).toContain('LEARNING_A');
  });
});

describe('IdeaWriter.merge', () => {
  it('re-synthesizes: sends both the existing concept and the new finding', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: anthropicBody('tighter merged concept') });
    const merged = await writer.merge('EXISTING_CONCEPT', { description: 'NEW_NUANCE' });
    expect(merged).toBe('tighter merged concept');
    const user = JSON.parse(
      bedrockMock.commandCalls(InvokeModelCommand)[0]!.args[0].input.body as string,
    ).messages[0].content as string;
    expect(user).toContain('EXISTING_CONCEPT');
    expect(user).toContain('NEW_NUANCE');
  });
});

describe('IdeaWriter error handling', () => {
  it('throws on an empty response rather than writing an empty concept', async () => {
    bedrockMock.on(InvokeModelCommand).resolves({ body: anthropicBody('   ') });
    await expect(writer.write({ description: 'x' })).rejects.toThrow(/empty concept/);
  });

  it('propagates a Bedrock error (does not swallow)', async () => {
    bedrockMock.on(InvokeModelCommand).rejects(new Error('ThrottlingException'));
    await expect(writer.write({ description: 'x' })).rejects.toThrow('ThrottlingException');
  });
});
