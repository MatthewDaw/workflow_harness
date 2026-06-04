import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent, Project, Skill } from '@harness/shared';
import { ProjectAgents } from './ProjectAgents.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 0,
  liveSessionCount: 0,
  enabledSkills: [],
  enabledAgents: ['reviewer'],
};

const SKILLS: Skill[] = [
  { name: 'gh', scope: ORG, kind: 'skill', description: '', source: 'built-in', members: [], body: '' },
  { name: 'kit', scope: ORG, kind: 'bundle', description: '', source: 'built-in', members: ['gh'], resolvedMembers: ['gh'], body: '' },
];

const AGENTS: Agent[] = [
  { name: 'builder', scope: ORG, model: 'claude-sonnet-4', prompt: '', skills: ['kit'], tools: [] },
  { name: 'reviewer', scope: ORG, model: 'claude-opus-4', prompt: '', skills: [], tools: [] },
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

describe('ProjectAgents (project opt-in)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('enables a catalog agent and flattens bundle skills it brings', async () => {
    renderWithProviders(<ProjectAgents />, {
      route: '/projects/weekly-compass/agents',
      routePath: '/projects/:projectId/agents',
      seed: { projects: [PROJECT], agents: AGENTS, skills: SKILLS },
    });
    await screen.findByTestId('project-agent-builder');

    // Bundle skill 'kit' flattens to leaf 'gh' in the brings display.
    expect(screen.getByTestId('brings-note-builder')).toBeInTheDocument();

    await userEvent.click(screen.getByTestId('enable-agent-builder'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/agents/builder'),
        ),
      ).toBeDefined(),
    );
  });

  it('shows enabled agents with a disable control', async () => {
    renderWithProviders(<ProjectAgents />, {
      route: '/projects/weekly-compass/agents',
      routePath: '/projects/:projectId/agents',
      seed: { projects: [PROJECT], agents: AGENTS, skills: SKILLS },
    });
    await screen.findByTestId('project-agent-reviewer');

    await userEvent.click(screen.getByTestId('disable-agent-reviewer'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'DELETE' && u.includes('projects/weekly-compass/agents/reviewer'),
        ),
      ).toBeDefined(),
    );
  });
});
