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
});
