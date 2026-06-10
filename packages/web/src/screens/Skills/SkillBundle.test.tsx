import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { SkillBundle } from './SkillBundle.js';
import { lastMatching, renderWithProviders } from '../../test/testUtils.js';

const SCOPE = { tier: 'org', id: 'acme' } as const;

const SKILLS: Skill[] = [
  {
    name: 'review-kit',
    scope: SCOPE,
    kind: 'bundle',
    description: 'Review bundle',
    source: 'custom',
    members: ['gh', 'browse'],
    body: '',
  },
  {
    name: 'gh',
    scope: SCOPE,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'browse',
    scope: SCOPE,
    kind: 'skill',
    description: '',
    source: 'local',
    members: [],
    body: '',
  },
  {
    name: 'qa',
    scope: SCOPE,
    kind: 'skill',
    description: '',
    source: 'local',
    members: [],
    body: '',
  },
];

describe('SkillBundle ops (U17)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('adds a member via a searchable combobox (filters as you type)', async () => {
    renderWithProviders(<SkillBundle />, {
      route: '/skills/review-kit',
      routePath: '/skills/:bundleName',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('skill-bundle');
    await screen.findByTestId('member-gh');

    // The combobox lists candidate skills (qa is not yet a member).
    const input = screen.getByTestId('add-member-input');
    await userEvent.click(input);
    expect(screen.getByTestId('add-member-option-qa')).toBeInTheDocument();

    // Typing filters the list down to the match.
    await userEvent.type(input, 'qa');
    expect(screen.getByTestId('add-member-option-qa')).toBeInTheDocument();
    expect(screen.queryByTestId('add-member-option-gh')).not.toBeInTheDocument();

    // Choose the option, then commit.
    await userEvent.click(screen.getByTestId('add-member-option-qa'));
    await userEvent.click(screen.getByTestId('add-member-commit'));

    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'POST' && u.includes('skills/review-kit/members')),
      ).toBeDefined(),
    );
    const post = lastMatching((u, m) => m === 'POST' && u.includes('skills/review-kit/members'))!;
    expect(JSON.parse(String(post.body))).toMatchObject({ member: 'qa' });
  });

  it('removes a member via removeBundleMember (DELETE)', async () => {
    renderWithProviders(<SkillBundle />, {
      route: '/skills/review-kit',
      routePath: '/skills/:bundleName',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('skill-bundle');
    await screen.findByTestId('member-gh');

    await userEvent.click(screen.getByTestId('remove-member-gh'));

    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'DELETE' && u.includes('skills/review-kit/members/gh')),
      ).toBeDefined(),
    );
  });

  it('shows blast-radius then dissolves via dissolveBundle', async () => {
    renderWithProviders(<SkillBundle />, {
      route: '/skills/review-kit',
      routePath: '/skills/:bundleName',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('skill-bundle');
    await screen.findByTestId('member-gh');

    await userEvent.click(screen.getByTestId('dissolve-bundle'));
    // Blast-radius count surfaces before the destructive op.
    expect(screen.getByTestId('blast-radius')).toHaveTextContent('2 members');

    await userEvent.click(screen.getByTestId('dissolve-confirm-btn'));

    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'POST' && u.includes('skills/review-kit/dissolve')),
      ).toBeDefined(),
    );
  });
});
