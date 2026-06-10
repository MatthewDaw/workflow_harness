import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { Skills } from './Skills.js';
import { lastMatching, renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const BUNDLE_SKILLS: Skill[] = [
  {
    name: 'command-hq-starter',
    scope: ORG,
    kind: 'bundle',
    description: 'Bundled skills',
    source: 'built-in',
    members: ['endforge', 'weekly-update'],
    resolvedMembers: ['endforge', 'weekly-update'],
    body: '',
    createdBy: { userId: 'system', name: 'system' },
  },
  {
    name: 'endforge',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
    createdBy: { userId: 'system', name: 'system' },
  },
  {
    name: 'weekly-update',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
    createdBy: { userId: 'system', name: 'system' },
  },
  {
    name: 'loner',
    scope: ORG,
    kind: 'skill',
    description: 'standalone',
    source: 'local',
    members: [],
    body: '',
    createdBy: { userId: 'u-matt', name: 'Matt' },
  },
];

function lastScopePost(): { url: string; body: unknown } | undefined {
  const req = lastMatching((u, m) => m === 'POST' && u.includes('/scope'));
  if (!req) return undefined;
  return { url: req.url, body: req.body ? JSON.parse(String(req.body)) : undefined };
}

describe('Skills scope controls (U17)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders a flat org catalog without scope groups or pickers', async () => {
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: BUNDLE_SKILLS } });
    await screen.findByTestId('skill-card-command-hq-starter');

    expect(screen.queryByTestId('scope-group-org')).not.toBeInTheDocument();
    expect(screen.queryByTestId('skill-scope-picker-loner')).not.toBeInTheDocument();
    expect(screen.getByTestId('skill-card-loner')).toBeInTheDocument();
  });

  it('hides bundle members from the top-level grid by default, reveals on toggle', async () => {
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: BUNDLE_SKILLS } });
    await screen.findByTestId('skill-card-command-hq-starter');

    expect(screen.getByTestId('skill-card-loner')).toBeInTheDocument();
    expect(screen.queryByTestId('skill-card-endforge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('skill-card-weekly-update')).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId('show-in-bundles-checkbox'));
    expect(screen.getByTestId('skill-card-endforge')).toBeInTheDocument();
    expect(screen.getByTestId('skill-card-weekly-update')).toBeInTheDocument();
    expect(screen.getByTestId('skill-card-command-hq-starter')).toBeInTheDocument();
  });

  it('filters by author and shows the author on each card', async () => {
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: BUNDLE_SKILLS } });
    await screen.findByTestId('skill-card-loner');

    expect(screen.getByTestId('skill-author-loner')).toHaveTextContent('by Matt');

    await userEvent.selectOptions(screen.getByTestId('author-filter'), 'Matt');
    expect(screen.getByTestId('skill-card-loner')).toBeInTheDocument();
    expect(screen.queryByTestId('skill-card-command-hq-starter')).not.toBeInTheDocument();
  });
});

describe('Skills catalog versioning (KTD6)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('shows the per-skill variant switcher + promote on plain cards', async () => {
    renderWithProviders(<Skills />, {
      route: '/skills',
      seed: {
        skills: BUNDLE_SKILLS,
        skillVariants: {
          loner: [
            { variantId: 'loner#base', baseName: 'loner', name: 'loner', version: 1, isTrue: true },
            {
              variantId: 'loner#R#x#U#y',
              baseName: 'loner',
              name: 'loner',
              version: 2,
              repoId: 'x',
              authorUserId: 'y',
            },
          ],
        },
      },
    });
    await screen.findByTestId('skill-card-loner');

    // The standalone skill card carries the version dropdown + promote action.
    expect(await screen.findByTestId('variant-select-loner')).toBeInTheDocument();
    expect(screen.getByTestId('promote-variant-loner')).toBeInTheDocument();
  });
});

describe('Skills bundle-member visibility (U17)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('hides bundle members from the top-level grid by default, reveals on toggle', async () => {
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: BUNDLE_SKILLS } });
    await screen.findByTestId('skill-card-command-hq-starter');

    // Standalone skill + the bundle card show; bundle members are hidden.
    expect(screen.getByTestId('skill-card-loner')).toBeInTheDocument();
    expect(screen.queryByTestId('skill-card-endforge')).not.toBeInTheDocument();
    expect(screen.queryByTestId('skill-card-weekly-update')).not.toBeInTheDocument();

    // Toggle reveals the members at top level (bundle card still present).
    await userEvent.click(screen.getByTestId('show-in-bundles-checkbox'));
    expect(screen.getByTestId('skill-card-endforge')).toBeInTheDocument();
    expect(screen.getByTestId('skill-card-weekly-update')).toBeInTheDocument();
    expect(screen.getByTestId('skill-card-command-hq-starter')).toBeInTheDocument();
  });
});
