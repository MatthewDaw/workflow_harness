import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Project, Skill } from '@harness/shared';
import { ProjectSkills } from './ProjectSkills.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 0,
  liveSessionCount: 0,
  enabledSkills: ['gh'],
  enabledAgents: [],
};

const SKILLS: Skill[] = [
  { name: 'gh', scope: ORG, kind: 'skill', description: 'GitHub CLI', source: 'built-in', members: [], body: '', createdBy: { userId: 'u', name: 'Matt' } },
  { name: 'browse', scope: ORG, kind: 'skill', description: 'Browser', source: 'local', members: [], body: '', createdBy: { userId: 'u', name: 'Sam' } },
];

interface StubReq {
  url: string;
  method: string;
}

function lastMatching(pred: (u: string, m: string) => boolean): StubReq | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (pred(req.url, req.method)) return req;
  }
  return undefined;
}

describe('ProjectSkills (project opt-in)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists enabled skills and removes one via disableProjectSkill', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('enabled-skill-gh');

    await userEvent.click(screen.getByTestId('remove-skill-gh'));
    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'DELETE' && u.includes('projects/weekly-compass/skills/gh')),
      ).toBeDefined(),
    );
  });

  it('adds a skill from the catalog via enableProjectSkill', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('enable-skill-input');

    await userEvent.click(screen.getByTestId('enable-skill-input'));
    await userEvent.click(await screen.findByTestId('enable-skill-option-browse'));
    await userEvent.click(screen.getByTestId('enable-skill-commit'));

    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/skills/browse'),
        ),
      ).toBeDefined(),
    );
  });
});
