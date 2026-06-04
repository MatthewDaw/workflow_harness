import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Project } from '@harness/shared';
import { ProjectRequirements } from './ProjectRequirements.js';
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

  it('renders HQ-owned requirements markdown with task-list glyphs', async () => {
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

  it('opens an HQ-owned editor on ✎ Edit', async () => {
    renderReq();
    await screen.findByRole('heading', { name: 'Goal' });
    await userEvent.click(screen.getByRole('button', { name: '✎ Edit' }));
    await waitFor(() => expect(screen.getByTestId('requirements-editor')).toBeInTheDocument());
    expect(screen.getByLabelText('Requirements markdown')).toHaveValue(
      '# Goal\n\nReplace 15-Five.\n\n- [x] login\n- [ ] reports',
    );
    expect(screen.getByRole('button', { name: 'Save' })).toBeInTheDocument();
  });
});
