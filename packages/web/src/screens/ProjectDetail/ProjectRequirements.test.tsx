import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import type { Project } from '@harness/shared';
import { ProjectRequirements, ProjectRequirementsFull } from './ProjectRequirements.js';
import { renderWithProviders } from '../../test/testUtils.js';

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 62,
  liveSessionCount: 0,
  enabledSkills: [],
  enabledAgents: [],
};

const seed = {
  projects: [PROJECT],
  requirements: { 'weekly-compass': '# Goal\n\nReplace 15-Five.\n\n- [x] login\n- [ ] reports' },
};

function renderReq() {
  return renderWithProviders(<ProjectRequirements />, {
    route: '/projects/weekly-compass/requirements',
    routePath: '/projects/:projectId/requirements',
    seed,
  });
}

describe('ProjectRequirements (U10)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders requirements markdown from docs/PRD.md with task-list glyphs', async () => {
    renderReq();
    expect(await screen.findByRole('heading', { name: 'Goal' })).toBeInTheDocument();
    expect(screen.getByText('Replace 15-Five.')).toBeInTheDocument();
    const view = screen.getByTestId('markdown-view');
    expect(view.textContent).toContain('☑');
    expect(view.textContent).toContain('☐');
  });

  it('shows the GitHub-sourced completion progress bar', async () => {
    renderReq();
    await screen.findByRole('heading', { name: 'Goal' });
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '62');
  });

  it('is read-only: no ✎ Edit button and no editor textarea', async () => {
    renderReq();
    await screen.findByRole('heading', { name: 'Goal' });
    expect(screen.queryByRole('button', { name: '✎ Edit' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Requirements markdown')).not.toBeInTheDocument();
    expect(screen.queryByTestId('requirements-editor')).not.toBeInTheDocument();
  });

  it('shows the GitHub-sourced empty state when docs/PRD.md is missing', async () => {
    renderWithProviders(<ProjectRequirements />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: { projects: [PROJECT], requirements: { 'weekly-compass': '' } },
    });
    expect(await screen.findByText('No docs/PRD.md found.')).toBeInTheDocument();
  });
});

describe('ProjectRequirementsFull (U10)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the markdown read-only with no editor', async () => {
    renderWithProviders(<ProjectRequirementsFull />, {
      route: '/projects/weekly-compass/requirements/full',
      routePath: '/projects/:projectId/requirements/full',
      seed,
    });
    expect(await screen.findByRole('heading', { name: 'Goal' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '✎ Edit' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Requirements markdown')).not.toBeInTheDocument();
  });
});
