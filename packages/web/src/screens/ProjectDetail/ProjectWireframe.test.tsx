import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import type { Project } from '@harness/shared';
import { ProjectWireframeFull, WireframePreview } from './ProjectWireframe.js';
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

const WF = '<!doctype html><title>WF</title><body>hello wireframe</body>';

describe('WireframePreview', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders a preview that links to the full-screen wireframe route', async () => {
    renderWithProviders(<WireframePreview />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: { projects: [PROJECT], wireframe: { 'weekly-compass': WF } },
    });
    expect(await screen.findByTestId('wireframe-preview')).toBeInTheDocument();
    const links = screen.getAllByRole('link');
    expect(links.some((a) => a.getAttribute('href')?.endsWith('/requirements/wireframe'))).toBe(
      true,
    );
  });

  it('renders nothing when the repo has no wireframe', async () => {
    renderWithProviders(<WireframePreview />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: { projects: [PROJECT], wireframe: { 'weekly-compass': '' } },
    });
    // Give the query a tick to resolve, then assert the card never appears.
    await Promise.resolve();
    expect(screen.queryByTestId('wireframe-preview')).not.toBeInTheDocument();
  });
});

describe('ProjectWireframeFull', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the wireframe in a full-screen iframe', async () => {
    renderWithProviders(<ProjectWireframeFull />, {
      route: '/projects/weekly-compass/requirements/wireframe',
      routePath: '/projects/:projectId/requirements/wireframe',
      seed: { projects: [PROJECT], wireframe: { 'weekly-compass': WF } },
    });
    const frame = (await screen.findByTitle('Wireframe')) as HTMLIFrameElement;
    expect(frame.getAttribute('srcdoc')).toContain('hello wireframe');
  });

  it('shows the empty state when docs/wireframe.html is missing', async () => {
    renderWithProviders(<ProjectWireframeFull />, {
      route: '/projects/weekly-compass/requirements/wireframe',
      routePath: '/projects/:projectId/requirements/wireframe',
      seed: { projects: [PROJECT], wireframe: { 'weekly-compass': '' } },
    });
    expect(await screen.findByText('No docs/wireframe.html found.')).toBeInTheDocument();
  });
});
