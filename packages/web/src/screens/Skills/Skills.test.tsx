import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { Skills } from './Skills.js';
import { renderWithProviders } from '../../test/testUtils.js';

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

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

function lastScopePost(): { url: string; body: unknown } | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (req.method === 'POST' && req.url.includes('/scope')) {
      return { url: req.url, body: req.body ? JSON.parse(String(req.body)) : undefined };
    }
  }
  return undefined;
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
