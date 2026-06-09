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
  enabledAgentBundles: [],
  enabledBundles: [],
  enabledMcpServers: [],
};

const SKILLS: Skill[] = [
  {
    name: 'gh',
    scope: ORG,
    kind: 'skill',
    description: '',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'kit',
    scope: ORG,
    kind: 'bundle',
    description: '',
    source: 'built-in',
    members: ['gh'],
    resolvedMembers: ['gh'],
    body: '',
  },
];

const AGENTS: Agent[] = [
  {
    name: 'builder',
    scope: ORG,
    description: '',
    model: 'claude-sonnet-4',
    kind: 'agent',
    members: [],
    prompt: '',
    skills: ['kit'],
    tools: [],
    mcpServers: [],
  },
  {
    name: 'reviewer',
    scope: ORG,
    description: '',
    model: 'claude-opus-4',
    kind: 'agent',
    members: [],
    prompt: '',
    skills: [],
    tools: [],
    mcpServers: [],
  },
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

  it('shows only the enabled agents in the page body', async () => {
    renderWithProviders(<ProjectAgents />, {
      route: '/projects/weekly-compass/agents',
      routePath: '/projects/:projectId/agents',
      seed: { projects: [PROJECT], agents: AGENTS, skills: SKILLS },
    });

    // The enabled 'reviewer' card renders; the not-yet-enabled 'builder' does not.
    await screen.findByTestId('project-agent-reviewer');
    expect(screen.queryByTestId('project-agent-builder')).not.toBeInTheDocument();
  });

  it('enables a catalog agent via the picker (Apply fires the enable POST)', async () => {
    renderWithProviders(<ProjectAgents />, {
      route: '/projects/weekly-compass/agents',
      routePath: '/projects/:projectId/agents',
      seed: { projects: [PROJECT], agents: AGENTS, skills: SKILLS },
    });
    await screen.findByTestId('project-agent-reviewer');

    // Open the picker and toggle the catalog agent 'builder' on.
    await userEvent.click(screen.getByTestId('add-agent'));
    await screen.findByTestId('catalog-picker');
    await userEvent.click(screen.getByTestId('catalog-picker-row-agent-builder'));

    // Apply stages -> enable diff -> POST projects/.../agents/builder.
    await userEvent.click(screen.getByTestId('catalog-picker-apply'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/agents/builder'),
        ),
      ).toBeDefined(),
    );
  });

  it('disables an enabled agent from its card', async () => {
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
