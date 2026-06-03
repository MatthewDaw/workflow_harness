import { describe, it, expect } from 'vitest';
import {
  definitionOfDoneSchema,
  orgConfigSchema,
  DEFAULT_DEFINITION_OF_DONE,
  CONTROL_ACTIONS,
  controlActionSchema,
  controlFrameSchema,
} from '../src/dto.js';

/**
 * The org-wide Definition of Done (plan-mapping feature 1). Advisory config that
 * `/update-progress` reports against; it has a sensible floor as defaults.
 */
describe('definitionOfDoneSchema', () => {
  it('applies the org-wide floor as defaults on an empty object', () => {
    const dod = definitionOfDoneSchema.parse({});
    expect(dod.requiresUnitTests).toBe(true); // floor: unit tests
    expect(dod.requiresProdE2E).toBe(false); // tighten-only opt-in
    expect(dod.notes).toBeUndefined();
  });

  it('round-trips a tightened DoD (prod-E2E + notes)', () => {
    const dod = definitionOfDoneSchema.parse({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
    expect(dod).toEqual({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'Run npm run e2e:prod against the deployed env.',
    });
  });

  it('rejects a non-boolean flag', () => {
    expect(definitionOfDoneSchema.safeParse({ requiresUnitTests: 'yes' }).success).toBe(false);
  });

  it('DEFAULT_DEFINITION_OF_DONE matches the parsed defaults', () => {
    expect(definitionOfDoneSchema.parse({})).toEqual(DEFAULT_DEFINITION_OF_DONE);
  });
});

/**
 * Control actions are the shared source of truth for HQ → daemon steering: the
 * web sender, the REST/WS control routes, and the wrapper receiver all key off
 * this enum. `shutdown`/`kill` were added so a live session can be terminated
 * from HQ (graceful, with a force fallback).
 */
describe('controlActionSchema', () => {
  it('includes inject/pause/interrupt and the terminate actions shutdown/kill', () => {
    expect(CONTROL_ACTIONS).toEqual(['inject', 'pause', 'interrupt', 'shutdown', 'kill']);
    for (const a of ['shutdown', 'kill'] as const) {
      expect(controlActionSchema.safeParse(a).success).toBe(true);
    }
  });

  it('rejects an unknown action', () => {
    expect(controlActionSchema.safeParse('restart').success).toBe(false);
  });

  it('parses a shutdown control frame with an empty payload default', () => {
    const frame = controlFrameSchema.parse({ sessionId: 's1', action: 'shutdown' });
    expect(frame).toEqual({ sessionId: 's1', action: 'shutdown', payload: {} });
  });
});

describe('orgConfigSchema', () => {
  it('accepts an empty config (dod optional)', () => {
    expect(orgConfigSchema.parse({})).toEqual({});
  });

  it('embeds a Definition of Done', () => {
    const cfg = orgConfigSchema.parse({ dod: { requiresProdE2E: true } });
    expect(cfg.dod).toEqual({ requiresUnitTests: true, requiresProdE2E: true });
  });
});
