import { describe, it, expect } from 'vitest';
import {
  definitionOfDoneSchema,
  orgConfigSchema,
  DEFAULT_DEFINITION_OF_DONE,
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

describe('orgConfigSchema', () => {
  it('accepts an empty config (dod optional)', () => {
    expect(orgConfigSchema.parse({})).toEqual({});
  });

  it('embeds a Definition of Done', () => {
    const cfg = orgConfigSchema.parse({ dod: { requiresProdE2E: true } });
    expect(cfg.dod).toEqual({ requiresUnitTests: true, requiresProdE2E: true });
  });
});
