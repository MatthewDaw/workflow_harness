import { describe, it, expect } from 'vitest';
import {
  ideaSchema,
  ideaSourceSchema,
  unassignedEntrySchema,
  corroborationCount,
  IDEA_STATUSES,
  type Idea,
} from '../src/dto.js';

/**
 * Skill-idea loop (U6) — the persistent shape of an idea and the unassigned bin.
 * An idea's `text` is a SYNTHESIZED, skill-ready concept (not raw transcript);
 * the `snippet` on each source is provenance only. Corroboration is DERIVED as
 * the count of distinct `sessionId`s, deduped across segments.
 */

function source(over: Partial<ReturnType<typeof ideaSourceSchema.parse>> = {}) {
  return ideaSourceSchema.parse({ sessionId: 's-1', segmentId: 'seg-1', seq: 0, ...over });
}

describe('ideaSchema', () => {
  it('parses a minimal idea with sensible defaults', () => {
    const idea = ideaSchema.parse({
      ideaId: 'i-1',
      skillBaseName: 'hq-update-skills',
      org: 'acme',
      createdAt: 1,
      updatedAt: 1,
    });
    expect(idea.text).toBe('');
    expect(idea.sources).toEqual([]);
    expect(idea.status).toBe('open');
    expect(idea.corroborationVersion).toBe(0);
    expect(idea.foldedIntoRev).toBeUndefined();
  });

  it('only allows open|folded statuses', () => {
    expect(IDEA_STATUSES).toEqual(['open', 'folded']);
    expect(
      ideaSchema.safeParse({
        ideaId: 'i-1',
        skillBaseName: 'x',
        org: 'acme',
        status: 'archived',
        createdAt: 1,
        updatedAt: 1,
      }).success,
    ).toBe(false);
  });

  it('defaults a source snippet to empty (provenance is optional text)', () => {
    const s = ideaSourceSchema.parse({ sessionId: 's-1', segmentId: 'seg-1', seq: 3 });
    expect(s.snippet).toBe('');
    expect(s.projectId).toBeUndefined();
  });
});

describe('corroborationCount', () => {
  it('counts distinct sessions, deduping segments within a session', () => {
    const idea = ideaSchema.parse({
      ideaId: 'i-1',
      skillBaseName: 'x',
      org: 'acme',
      createdAt: 1,
      updatedAt: 1,
      sources: [
        source({ sessionId: 's-1', segmentId: 'seg-1' }),
        source({ sessionId: 's-1', segmentId: 'seg-2' }), // same session, second segment
        source({ sessionId: 's-2', segmentId: 'seg-1' }),
      ],
    });
    // 3 source rows, but only 2 DISTINCT sessions.
    expect(idea.sources).toHaveLength(3);
    expect(corroborationCount(idea)).toBe(2);
  });

  it('is zero for an idea with no sources', () => {
    expect(corroborationCount({ sources: [] })).toBe(0);
  });
});

describe('unassignedEntrySchema', () => {
  it('parses a bin entry with provenance sources', () => {
    const entry = unassignedEntrySchema.parse({
      entryId: 'e-1',
      org: 'acme',
      text: 'wants a terraform skill that does not exist',
      createdAt: 1,
      updatedAt: 1,
      sources: [source()],
    });
    expect(entry.text).toContain('terraform');
    expect(entry.sources).toHaveLength(1);
  });
});
