import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, fireEvent } from '@testing-library/react';
import type { Project } from '@harness/shared';
import { WireframePreview } from './ProjectWireframe.js';
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

const WF = '<!doctype html><title>WF</title><body>hello wireframe</body>';

describe('WireframePreview', () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('renders a preview with an "open in new tab" button and no full-screen route link', async () => {
    renderWithProviders(<WireframePreview />, {
      route: '/projects/weekly-compass/requirements',
      routePath: '/projects/:projectId/requirements',
      seed: { projects: [PROJECT], wireframe: { 'weekly-compass': WF } },
    });
    expect(await screen.findByTestId('wireframe-preview')).toBeInTheDocument();
    expect(screen.getByTestId('wireframe-open-newtab')).toBeInTheDocument();
    // The full-screen wireframe route is gone — nothing should link to it.
    const links = screen.queryAllByRole('link');
    expect(links.some((a) => a.getAttribute('href')?.endsWith('/requirements/wireframe'))).toBe(
      false,
    );
  });

  it('opens the raw wireframe in a new tab via a blob URL (no app chrome)', async () => {
    const createObjectURL = vi.fn((_blob: Blob) => 'blob:mock-wf');
    const urlRef = URL as unknown as { createObjectURL?: unknown; revokeObjectURL?: unknown };
    const origCreate = urlRef.createObjectURL;
    const origRevoke = urlRef.revokeObjectURL;
    urlRef.createObjectURL = createObjectURL;
    urlRef.revokeObjectURL = vi.fn();
    const open = vi.fn(() => null);
    vi.stubGlobal('open', open);
    try {
      renderWithProviders(<WireframePreview />, {
        route: '/projects/weekly-compass/requirements',
        routePath: '/projects/:projectId/requirements',
        seed: { projects: [PROJECT], wireframe: { 'weekly-compass': WF } },
      });
      fireEvent.click(await screen.findByTestId('wireframe-open-newtab'));
      expect(createObjectURL).toHaveBeenCalledTimes(1);
      const blob = createObjectURL.mock.calls[0]![0];
      expect(blob.type).toBe('text/html');
      // Opens the blob document itself — the standalone wireframe, no app shell.
      expect(open).toHaveBeenCalledWith('blob:mock-wf', '_blank', 'noopener,noreferrer');
    } finally {
      urlRef.createObjectURL = origCreate;
      urlRef.revokeObjectURL = origRevoke;
    }
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
