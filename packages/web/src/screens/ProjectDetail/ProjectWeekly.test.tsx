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
  enabledSkills: [],
  enabledAgents: [],
  enabledMcpServers: [],
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

  it('renders the three done sections as headings when done is section Markdown', async () => {
    const sectioned: WeeklyUpdate = {
      projectId: 'weekly-compass',
      isoWeek: '2026-W24',
      done: '## Summary\nShipped the thing.\n\n## Conformity report\nOverall 50. Goal 1 fully ladders.\n\n## Additional things coded up\n- Fixed a flaky test.',
      plan: '1. Wire the gate.',
      conformityScore: 50,
      validated: true,
    };
    renderWithProviders(<ProjectWeekly />, {
      route: '/projects/weekly-compass/weekly',
      routePath: '/projects/:projectId/weekly',
      seed: { projects: [PROJECT], weekly: { 'weekly-compass': [sectioned] } },
    });
    // The three sections render as their own headings (not a flat blob of text).
    expect(await screen.findByRole('heading', { name: 'Summary' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Conformity report' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Additional things coded up' })).toBeInTheDocument();
    expect(screen.getByText('Fixed a flaky test.')).toBeInTheDocument();
  });

  it('shows the conformity score pill', async () => {
    renderWeekly();
    await screen.findByText('Shipped the login gate and wired the weekly REST endpoint.');
    expect(screen.getByText('conformity 81')).toBeInTheDocument();
  });

  it('shows the LATEST week even when the list arrives oldest-first', async () => {
    const older: WeeklyUpdate = {
      projectId: 'weekly-compass',
      isoWeek: '2026-W20',
      done: 'Older week done.',
      plan: 'Older week plan.',
      validated: false,
    };
    const newer: WeeklyUpdate = {
      projectId: 'weekly-compass',
      isoWeek: '2026-W23',
      done: 'Newer week done.',
      plan: 'Newer week plan.',
      validated: false,
    };
    // Seed oldest-first (data[0] is the OLDEST) — the screen must still pick W23.
    renderWithProviders(<ProjectWeekly />, {
      route: '/projects/weekly-compass/weekly',
      routePath: '/projects/:projectId/weekly',
      seed: { projects: [PROJECT], weekly: { 'weekly-compass': [older, newer] } },
    });

    expect(await screen.findByText('Week 2026-W23')).toBeInTheDocument();
    expect(screen.getByText('Newer week done.')).toBeInTheDocument();
    expect(screen.queryByText('Older week done.')).not.toBeInTheDocument();
  });
});
