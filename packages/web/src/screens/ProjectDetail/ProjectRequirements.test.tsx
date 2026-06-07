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
  enabledMcpServers: [],
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

  it("drives the bar from the doc's completion: frontmatter and hides the frontmatter block", async () => {
    renderWithProviders(<ProjectRequirements />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: {
        projects: [PROJECT], // progressPct: 62
        requirements: { 'weekly-compass': '---\ncompletion: 88\nstatus: active\n---\n\n# Goal\n\nShip it.' },
      },
    });
    await screen.findByRole('heading', { name: 'Goal' });
    // Bar reflects the doc's 88, NOT the stored progressPct of 62.
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '88');
    // The frontmatter block is stripped from the rendered body.
    const view = screen.getByTestId('markdown-view');
    expect(view.textContent).not.toContain('completion: 88');
    expect(view.textContent).not.toContain('---');
  });

  it('is read-only: no ✎ Edit button and no editor textarea', async () => {
    renderReq();
    await screen.findByRole('heading', { name: 'Goal' });
    expect(screen.queryByRole('button', { name: '✎ Edit' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText('Requirements markdown')).not.toBeInTheDocument();
    expect(screen.queryByTestId('requirements-editor')).not.toBeInTheDocument();
  });

  it('renders the embedded compliance block as its own panel and not in the body', async () => {
    renderWithProviders(<ProjectRequirements />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: {
        projects: [PROJECT], // progressPct: 62
        requirements: {
          'weekly-compass':
            '<!--hq:compliance v1-->## Compliance breakdown\n| Requirement | Status | Evidence |\n|---|---|---|\n| Source Code | met | packages/** |\n<!--/hq:compliance-->\n# Goal\n\nbody',
        },
      },
    });
    await screen.findByRole('heading', { name: 'Goal' });
    const panel = screen.getByTestId('compliance-panel');
    expect(panel).toBeInTheDocument();
    expect(panel.textContent).toContain('Compliance breakdown');
    // The bar still works (falls back to stored progressPct of 62).
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '62');
    // The body region must not contain the raw sentinel text.
    const views = screen.getAllByTestId('markdown-view');
    const bodyView = views[views.length - 1]!;
    expect(bodyView.textContent).not.toContain('hq:compliance');
    expect(bodyView.textContent).toContain('body');
  });

  it('renders no compliance panel when there is no block', async () => {
    renderReq();
    await screen.findByRole('heading', { name: 'Goal' });
    expect(screen.queryByTestId('compliance-panel')).not.toBeInTheDocument();
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
