import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DetailedRequirements } from './DetailedRequirements.js';
import { renderWithProviders } from '../../test/testUtils.js';

const seed = {
  docs: {
    'weekly-compass': [
      { path: 'docs/plans/overview.md', title: 'Overview', completion: 80 },
      { path: 'docs/plans/command-hq/01-mapping.md', title: 'Plan Mapping', completion: 90 },
      { path: 'docs/plans/command-hq/02-weekly.md', title: 'Weekly Update', completion: 30 },
    ],
  },
  docContent: {
    'weekly-compass::docs/plans/overview.md': '# Overview\n\nThe overview doc.',
    'weekly-compass::docs/plans/command-hq/01-mapping.md': '# Mapping\n\nThe mapping doc.',
    'weekly-compass::docs/plans/command-hq/02-weekly.md': '# Weekly\n\nThe weekly doc.',
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
  it('lists docs with per-doc completion badges grouped into a folder tree', async () => {
    renderDetailed();
    await screen.findByRole('button', { name: /Overview/ });
    const sidebar = screen.getByRole('complementary', { name: 'Requirement documents' });
    expect(sidebar.textContent).toContain('Overview');
    expect(sidebar.textContent).toContain('80%');
    expect(sidebar.textContent).toContain('Plan Mapping');
    expect(sidebar.textContent).toContain('90%');
    // Folder header with aggregate (mean of 90 and 30 = 60%).
    expect(sidebar.textContent).toContain('command-hq/');
    expect(sidebar.textContent).toContain('60%');
  });

  it('shows only the first doc initially, with the bar on its completion', async () => {
    renderDetailed();
    expect(await screen.findByText('The overview doc.')).toBeInTheDocument();
    // The other docs are NOT rendered until selected (one doc at a time).
    expect(screen.queryByText('The weekly doc.')).not.toBeInTheDocument();
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '80');
    expect(screen.getByText(/Overview · 80%/)).toBeInTheDocument();
  });

  it('switches to a doc (and updates the bar) when its sidebar link is clicked', async () => {
    renderDetailed();
    await screen.findByText('The overview doc.');

    await userEvent.click(screen.getByRole('button', { name: /Weekly Update/ }));

    expect(await screen.findByText('The weekly doc.')).toBeInTheDocument();
    expect(screen.queryByText('The overview doc.')).not.toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '30');
    expect(screen.getByText(/Weekly Update · 30%/)).toBeInTheDocument();
  });
});
