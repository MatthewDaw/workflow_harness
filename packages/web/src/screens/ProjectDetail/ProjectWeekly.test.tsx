import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import type { Project, WeeklyUpdate } from '@harness/shared';
import { ProjectWeekly } from './ProjectWeekly.js';
import { renderWithProviders } from '../../test/testUtils.js';

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 62,
  liveSessionCount: 0,
};

const UPDATE: WeeklyUpdate = {
  projectId: 'weekly-compass',
  isoWeek: '2026-W23',
  done: 'Shipped the login gate and wired the weekly REST endpoint.',
  plan: 'Build the conformity score interview and publish flow.',
  conformityScore: 81,
  validated: true,
};

const seed = {
  projects: [PROJECT],
  weekly: { 'weekly-compass': [UPDATE] },
};

function renderWeekly() {
  return renderWithProviders(<ProjectWeekly />, {
    route: '/projects/weekly-compass/weekly',
    routePath: '/projects/:projectId/weekly',
    seed,
  });
}

describe('ProjectWeekly (U19)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the done prose from the seeded weekly update', async () => {
    renderWeekly();
    expect(
      await screen.findByText('Shipped the login gate and wired the weekly REST endpoint.'),
    ).toBeInTheDocument();
  });

  it('renders the plan prose from the seeded weekly update', async () => {
    renderWeekly();
    expect(
      await screen.findByText('Build the conformity score interview and publish flow.'),
    ).toBeInTheDocument();
  });

  it('shows the conformity score pill', async () => {
    renderWeekly();
    await screen.findByText('Shipped the login gate and wired the weekly REST endpoint.');
    expect(screen.getByText('conformity 81')).toBeInTheDocument();
  });
});
