import { describe, it, expect } from 'vitest';
import {
  envelopeSchema,
  parseEnvelope,
  safeParseEnvelope,
  type EventKind,
} from '../src/events.js';
import golden from './golden/event-envelope.json' with { type: 'json' };

describe('event envelope schema', () => {
  it('parses every event kind in the golden fixture', () => {
    const kinds = (golden as unknown[]).map((e) => parseEnvelope(e).event.kind);
    expect(kinds).toEqual([
      'session.start',
      'session.rename',
      'user.msg',
      'assistant.msg',
      'tool.call',
      'tool.result',
      'cost.tick',
      'status.change',
    ] satisfies EventKind[]);
  });

  it('round-trips the golden fixture (parse -> serialize -> deep equal)', () => {
    for (const raw of golden as unknown[]) {
      const parsed = parseEnvelope(raw);
      const roundTripped = JSON.parse(JSON.stringify(parsed));
      expect(roundTripped).toEqual(raw);
    }
  });

  it('parses a session.start envelope without a ticket field', () => {
    const parsed = parseEnvelope({
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      ts: 1,
      seq: 0,
      event: {
        kind: 'session.start',
        sessionId: 'a91f',
        projectId: 'weekly-compass',
        host: 'h',
        name: 'reconcile-variance',
        agent: 'builder',
      },
    });
    expect(parsed.event).not.toHaveProperty('ticket');
    const roundTripped = JSON.parse(JSON.stringify(parsed));
    expect(roundTripped.event).toMatchObject({ kind: 'session.start', sessionId: 'a91f' });
  });

  it('rejects an unknown event kind', () => {
    const bad = {
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      ts: 1,
      seq: 0,
      event: { kind: 'nope', sessionId: 'a91f' },
    };
    expect(safeParseEnvelope(bad).success).toBe(false);
  });

  it('rejects an envelope missing seq', () => {
    const bad = {
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      ts: 1,
      event: { kind: 'user.msg', sessionId: 'a91f', tokens: 1 },
    };
    expect(safeParseEnvelope(bad).success).toBe(false);
  });

  it('rejects an envelope missing ts', () => {
    const bad = {
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      seq: 0,
      event: { kind: 'user.msg', sessionId: 'a91f', tokens: 1 },
    };
    expect(safeParseEnvelope(bad).success).toBe(false);
  });

  it('rejects a negative seq', () => {
    const bad = {
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      ts: 1,
      seq: -1,
      event: { kind: 'user.msg', sessionId: 'a91f', tokens: 1 },
    };
    expect(safeParseEnvelope(bad).success).toBe(false);
  });

  it('applies the argsSummary default for tool.call', () => {
    const parsed = envelopeSchema.parse({
      v: 1,
      instanceId: 'inst-0',
      host: 'h',
      ts: 1,
      seq: 0,
      event: { kind: 'tool.call', sessionId: 'a91f', tool: 'Bash' },
    });
    expect(parsed.event).toMatchObject({ kind: 'tool.call', argsSummary: '' });
  });
});
