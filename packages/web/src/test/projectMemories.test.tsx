import { describe, it, expect, afterEach, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter } from 'react-router-dom';
import type { ReactNode } from 'react';
import type { Memory, Project } from '@harness/shared';
import { App } from '../app/App.js';
import { makeStore } from '../app/store.js';
import { createMockClient } from '../auth/mockClient.js';
import { installFetchStub } from './testUtils.js';

/**
 * Project Memories sub-tab. Memories are stored PER USER, so the tab groups the
 * flat synced list by author and labels each group by userName. These tests
 * cover: the Memories tab link is present in the project sub-nav (so it is
 * reachable), both authors' groups render, and a memory's name + Markdown
 * content show through.
 */

const MATT = { userId: 'user-matt', username: 'matt', org: 'acme' };

const PROJECTS: Project[] = [
  {
    id: 'weekly-compass',
    name: 'weekly-compass',
    repo: 'gh/acme/weekly-compass',
    ownerUserId: 'user-matt',
    progressPct: 0,
    liveSessionCount: 0,
    enabledSkills: [],
    enabledAgents: [],
    enabledBundles: [],
    enabledMcpServers: [],
  },
];

// Two memories authored by two DIFFERENT users so the group-by-author rendering
// (and the >1-author filter) is exercised.
const MEMORIES: Memory[] = [
  {
    projectId: 'weekly-compass',
    userId: 'user-matt',
    userName: 'Matt',
    name: 'prefers-pnpm',
    description: 'Package manager preference',
    type: 'user',
    content: 'Always use **pnpm**, never npm.',
    updatedAt: 2000,
  },
  {
    projectId: 'weekly-compass',
    userId: 'user-ada',
    userName: 'Ada',
    name: 'variance-cache',
    type: 'project',
    content: 'Cache the variance; do not recompute per render.',
    updatedAt: 3000,
  },
];

const at = (route: string) => (children: ReactNode) => (
  <MemoryRouter initialEntries={[route]}>{children}</MemoryRouter>
);

function renderApp(route: string) {
  installFetchStub({ projects: PROJECTS, memories: { 'weekly-compass': MEMORIES } });
  return render(<App store={makeStore()} authClient={createMockClient(MATT)} router={at(route)} />);
}

describe('ProjectMemories — per-user memories tab', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the Memories tab link in the project sub-nav', async () => {
    renderApp('/projects/weekly-compass');
    const subnav = await screen.findByRole('navigation', { name: 'Project sections' });
    expect(within(subnav).getByRole('link', { name: 'Memories' })).toBeInTheDocument();
  });

  it('groups memories by author and shows a memory name + Markdown content', async () => {
    renderApp('/projects/weekly-compass/memories');

    // The tab mounted (reachable via its route).
    expect(await screen.findByTestId('project-memories')).toBeInTheDocument();

    // Both authors' groups render, labelled by userName.
    expect(await screen.findByTestId('memory-group-user-matt')).toBeInTheDocument();
    expect(screen.getByTestId('memory-group-user-ada')).toBeInTheDocument();
    expect(screen.getByText('Matt')).toBeInTheDocument();
    expect(screen.getByText('Ada')).toBeInTheDocument();

    // A memory's name (heading) and its rendered Markdown content show through.
    expect(screen.getByText('prefers-pnpm')).toBeInTheDocument();
    const mattGroup = screen.getByTestId('memory-group-user-matt');
    expect(within(mattGroup).getByText('pnpm')).toBeInTheDocument();
    expect(screen.getByText('variance-cache')).toBeInTheDocument();
  });

  it('filters to a single author when more than one is present', async () => {
    renderApp('/projects/weekly-compass/memories');
    await screen.findByTestId('project-memories');

    // Both groups visible by default (all authors).
    expect(screen.getByTestId('memory-group-user-ada')).toBeInTheDocument();

    // Narrow to Matt; Ada's group drops out.
    await userEvent.selectOptions(screen.getByLabelText('Author'), 'user-matt');
    expect(screen.getByTestId('memory-group-user-matt')).toBeInTheDocument();
    expect(screen.queryByTestId('memory-group-user-ada')).not.toBeInTheDocument();
  });
});
