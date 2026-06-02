import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { Project, ObjectiveNode } from '@harness/shared';
import { App } from '../app/App.js';
import { makeStore } from '../app/store.js';
import { createMockClient } from '../auth/mockClient.js';
import { installFetchStub } from './testUtils.js';

const MATT = { userId: 'user-matt', username: 'matt', org: 'acme' };

const PROJECTS: Project[] = [
  {
    id: 'weekly-compass',
    name: 'weekly-compass',
    repo: 'gh/acme/weekly-compass',
    ownerUserId: 'user-matt',
    prdGoal: 'Replace 15-Five with an RCDO-linked weekly planning module.',
    progressPct: 62,
    liveSessionCount: 2,
  },
];

const OBJECTIVES: ObjectiveNode[] = [
  { id: 'rc1', org: 'acme', level: 'rally_cry', title: 'Win the category', pct: 58 },
  {
    id: 'do1',
    org: 'acme',
    level: 'defining_objective',
    title: 'Weekly work aligned to strategy',
    parentId: 'rc1',
    pct: 64,
  },
];

const at = (route: string) => (children: ReactNode) => (
  <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
);

function renderApp(route: string) {
  installFetchStub({ projects: PROJECTS, objectives: OBJECTIVES });
  return render(<App store={makeStore()} authClient={createMockClient(MATT)} router={at(route)} />);
}

describe('navigation + routing', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lands on Objectives by default and renders the RCDO tree', async () => {
    renderApp('/');
    expect(await screen.findByTestId('objectives-screen')).toBeInTheDocument();
    expect(await screen.findByText('Win the category')).toBeInTheDocument();
  });

  it('navigates via the top nav to Projects', async () => {
    renderApp('/');
    await screen.findByTestId('objectives-screen');
    await userEvent.click(screen.getByRole('link', { name: 'Projects' }));
    expect(await screen.findByTestId('projects-screen')).toBeInTheDocument();
    expect(await screen.findByText('weekly-compass')).toBeInTheDocument();
  });

  it('deep-links into a project and routes its sub-tabs', async () => {
    renderApp('/projects/weekly-compass');
    expect(await screen.findByTestId('project-overview')).toBeInTheDocument();

    // Scope clicks to the project sub-nav (the top nav also has a "Sessions" link).
    const subnav = screen.getByRole('navigation', { name: 'Project sections' });

    await userEvent.click(within(subnav).getByRole('link', { name: 'Tickets' }));
    expect(await screen.findByTestId('project-tickets')).toBeInTheDocument();

    await userEvent.click(within(subnav).getByRole('link', { name: 'Weekly' }));
    expect(await screen.findByTestId('project-weekly')).toBeInTheDocument();

    await userEvent.click(within(subnav).getByRole('link', { name: 'Sessions' }));
    await waitFor(() => expect(screen.getByTestId('project-sessions')).toBeInTheDocument());
  });
});
