import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DetailedRequirements } from './DetailedRequirements.js';
import { renderWithProviders } from '../../test/testUtils.js';

const seed = {
  docs: {
    'weekly-compass': [
      { path: 'docs/overview.md', title: 'Overview', completion: 80 },
      { path: 'docs/auth.md', title: 'Auth', completion: 40 },
    ],
  },
  docContent: {
    'weekly-compass::docs/overview.md': '# Overview\n\nThe overview doc.',
    'weekly-compass::docs/auth.md': '# Auth\n\nThe auth doc.',
  },
};

function renderDetailed() {
  return renderWithProviders(<DetailedRequirements />, {
    route: '/projects/weekly-compass/detailed-requirements',
    routePath: '/projects/:projectId/detailed-requirements',
    seed,
  });
}

describe('DetailedRequirements (U11)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists docs with per-doc completion badges and renders the first by default', async () => {
    renderDetailed();
    // Wait for the doc list to load.
    await screen.findByRole('button', { name: /Overview/ });
    const sidebar = screen.getByRole('complementary', { name: 'Requirement documents' });
    expect(sidebar.textContent).toContain('Overview');
    expect(sidebar.textContent).toContain('80%');
    expect(sidebar.textContent).toContain('Auth');
    expect(sidebar.textContent).toContain('40%');
    // First doc renders by default.
    expect(await screen.findByText('The overview doc.')).toBeInTheDocument();
  });

  it('uses the top doc completion for the overall bar', async () => {
    renderDetailed();
    await screen.findByText('The overview doc.');
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '80');
  });

  it('switches the reading pane when a doc is selected', async () => {
    renderDetailed();
    await screen.findByText('The overview doc.');
    await userEvent.click(screen.getByRole('button', { name: /Auth/ }));
    await waitFor(() => expect(screen.getByText('The auth doc.')).toBeInTheDocument());
  });
});
