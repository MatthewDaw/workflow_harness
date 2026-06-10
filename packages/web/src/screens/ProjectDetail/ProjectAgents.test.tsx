import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent, Skill } from '@harness/shared';
import { ProjectAgents } from './ProjectAgents.js';
import {
  lastMatching,
  makeAgent,
  makeProject,
  makeSkill,
  renderWithProviders,
} from '../../test/testUtils.js';

const PROJECT = makeProject({ enabledAgents: ['reviewer'] });

const SKILLS: Skill[] = [
  makeSkill({ name: 'gh', source: 'built-in' }),
  makeSkill({
    name: 'kit',
    kind: 'bundle',
    source: 'built-in',
    members: ['gh'],
    resolvedMembers: ['gh'],
  }),
];

const AGENTS: Agent[] = [
  makeAgent({ name: 'builder', skills: ['kit'] }),
  makeAgent({ name: 'reviewer', model: 'claude-opus-4' }),
];

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
