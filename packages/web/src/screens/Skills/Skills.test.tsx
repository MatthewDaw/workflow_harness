import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { Skills } from './Skills.js';
import { renderWithProviders } from '../../test/testUtils.js';

const SKILLS: Skill[] = [
  {
    name: 'browse',
    scope: { tier: 'project', id: 'weekly-compass' },
    kind: 'skill',
    description: 'Headless browser',
    source: 'local',
    members: [],
    body: '',
  },
];

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
  },
  {
    name: 'endforge',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'weekly-update',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'loner',
    scope: ORG,
    kind: 'skill',
    description: 'standalone',
    source: 'local',
    members: [],
    body: '',
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

  it('elevates a skill via changeSkillScope', async () => {
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: SKILLS } });
    await screen.findByTestId('skill-card-browse');

    await userEvent.selectOptions(screen.getByTestId('skill-scope-picker-browse'), 'user');

    await waitFor(() => expect(lastScopePost()).toBeDefined());
    const post = lastScopePost()!;
    expect(post.url).toContain('skills/browse/scope');
    expect(post.body).toMatchObject({ scope: { tier: 'user', id: 'user-matt' } });
  });

  it('moves a skill into a new scope group on success (cache invalidation)', async () => {
    // After the scope change, RTK Query refetches; the fetch stub serves the
    // moved record so the skill should render under the new tier group.
    const moved = [
      { ...SKILLS[0]!, scope: { tier: 'user', id: 'user-matt' } as const },
    ];
    renderWithProviders(<Skills />, { route: '/skills', seed: { skills: SKILLS } });
    await screen.findByTestId('skill-card-browse');
    // Starts under the project group.
    expect(screen.getByTestId('scope-group-project')).toContainElement(
      screen.getByTestId('skill-card-browse'),
    );

    // Re-point the stub to the moved record before firing the change.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(async (input: unknown) => {
      const url = String((input as { url: string }).url);
      const method = ((input as { method?: string }).method ?? 'GET').toUpperCase();
      const body = url.includes('/skills') && method === 'GET' ? moved : { skill: moved[0] };
      return new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    });

    await userEvent.selectOptions(screen.getByTestId('skill-scope-picker-browse'), 'user');

    await waitFor(() =>
      expect(screen.getByTestId('scope-group-user')).toContainElement(
        screen.getByTestId('skill-card-browse'),
      ),
    );
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
