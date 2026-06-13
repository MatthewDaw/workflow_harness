import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { SkillCard } from './SkillCard.js';
import { renderWithProviders } from '../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

/** A plain skill with a body, so the Expand button (which opens the modal) shows. */
const SKILL: Skill = {
  name: 'endforge',
  scope: ORG,
  kind: 'skill',
  description: 'The endpoint forge skill.',
  source: 'built-in',
  members: [],
  body: '# Endforge\n\nThe full skill body.',
  createdBy: { userId: 'system', name: 'system' },
};

/** Each idea as the all-ideas endpoint serves it: an Idea + derived corroborationCount. */
const IDEAS = {
  endforge: [
    {
      ideaId: 'idea-corr',
      skillBaseName: 'endforge',
      org: 'acme',
      text: 'Always validate the request body before routing.',
      sources: [{ sessionId: 's1', segmentId: 'g1', seq: 1, snippet: '' }],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 1,
      updatedAt: 3,
      corroborationCount: 3,
    },
    {
      ideaId: 'idea-open',
      skillBaseName: 'endforge',
      org: 'acme',
      text: 'Consider rate-limiting per device token.',
      sources: [{ sessionId: 's2', segmentId: 'g2', seq: 1, snippet: '' }],
      status: 'open',
      corroborationVersion: 0,
      createdAt: 1,
      updatedAt: 2,
      corroborationCount: 1,
    },
    {
      ideaId: 'idea-folded',
      skillBaseName: 'endforge',
      org: 'acme',
      text: 'Stamp the org on every record (folded already).',
      sources: [{ sessionId: 's3', segmentId: 'g3', seq: 1, snippet: '' }],
      status: 'folded',
      foldedIntoRev: 7,
      corroborationVersion: 0,
      createdAt: 1,
      updatedAt: 1,
      corroborationCount: 2,
    },
  ],
};

/** Expand the card into its full-screen modal, then open the ideas dropdown. */
async function openIdeas(name: string) {
  await userEvent.click(await screen.findByTestId(`skill-expand-${name}`));
  await userEvent.click(await screen.findByTestId(`skill-ideas-toggle-${name}`));
}

describe('SkillCard ideas dropdown (skill-idea loop, U14)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists corroborated, uncorroborated, and folded ideas with the right badges', async () => {
    renderWithProviders(<SkillCard skill={SKILL} />, { seed: { skillIdeas: IDEAS } });
    await openIdeas('endforge');

    // Corroborated (≥K distinct sessions) → "corroborated" badge + session count.
    const corr = await screen.findByTestId('idea-idea-corr');
    expect(within(corr).getByTestId('idea-badge-idea-corr')).toHaveTextContent('corroborated');
    expect(corr).toHaveTextContent('3 sessions');

    // Below K → "open" badge.
    const open = screen.getByTestId('idea-idea-open');
    expect(within(open).getByTestId('idea-badge-idea-open')).toHaveTextContent('open');
    expect(open).toHaveTextContent('1 session');

    // Folded → grouped as history, badge carries the folded revision.
    const folded = screen.getByTestId('idea-idea-folded');
    expect(within(folded).getByTestId('idea-badge-idea-folded')).toHaveTextContent('folded · rev 7');

    // The three groups render under their headings.
    expect(screen.getByTestId('idea-group-corroborated')).toBeInTheDocument();
    expect(screen.getByTestId('idea-group-not-yet-corroborated')).toBeInTheDocument();
    expect(screen.getByTestId('idea-group-folded-history')).toBeInTheDocument();
  });

  it('renders an empty state when the skill has no ideas', async () => {
    renderWithProviders(<SkillCard skill={SKILL} />, { seed: { skillIdeas: { endforge: [] } } });
    await openIdeas('endforge');
    expect(await screen.findByTestId('skill-ideas-empty-endforge')).toBeInTheDocument();
    // No idea-group sections when the list is empty.
    expect(screen.queryByTestId('idea-group-corroborated')).not.toBeInTheDocument();
  });
});

describe("getSkillIdeasFromPython query (A1 - web reads via Python API)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("tags the list per-skill in the learningApi slice (Python API repoint)", async () => {
    // A1 / MAT-154: the SkillCard now reads ideas via the Python learning API
    // (learningApi slice), not the TS DynamoDB backend (baseApi slice).
    // The tag is keyed per-skill (LearningIdea / endforge) so a future fold
    // mutation can invalidate it.
    const { store } = renderWithProviders(<SkillCard skill={SKILL} />, {
      seed: { skillIdeas: IDEAS },
    });
    await openIdeas('endforge');
    await screen.findByTestId('idea-idea-corr');

    // The query lives in the 'learningApi' reducer (not 'api') and uses the
    // 'getSkillIdeasFromPython' endpoint name (not 'getSkillIdeas').
    await waitFor(() => {
      const state = store.getState() as Record<string, { queries?: Record<string, unknown> }>;
      const queries = state.learningApi?.queries ?? {};
      const entry = Object.entries(queries).find(([k]) =>
        k.startsWith('getSkillIdeasFromPython('),
      );
      expect(entry).toBeDefined();
      expect(entry![0]).toContain('endforge');
      expect((entry![1] as { data?: unknown[] }).data).toHaveLength(3);
    });
  });
});
