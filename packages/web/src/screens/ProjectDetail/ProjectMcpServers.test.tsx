import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { McpServer, Project } from '@harness/shared';
import { ProjectMcpServers } from './ProjectMcpServers.js';
import { ProjectLayout } from './ProjectLayout.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 0,
  liveSessionCount: 0,
  enabledSkills: [],
  enabledAgents: [],
  enabledMcpServers: ['fs'],
};

const SERVERS: McpServer[] = [
  {
    name: 'fs',
    scope: ORG,
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem'],
    env: {},
  },
  {
    name: 'linear',
    scope: ORG,
    transport: 'http',
    url: 'https://mcp.linear.app',
    headers: {},
  },
];

interface StubReq {
  url: string;
  method: string;
}

function lastMatching(pred: (u: string, m: string) => boolean): StubReq | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (pred(req.url, req.method)) return req;
  }
  return undefined;
}

describe('ProjectMcpServers (project opt-in)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists enabled servers with transport + summary', async () => {
    renderWithProviders(<ProjectMcpServers />, {
      route: '/projects/weekly-compass/mcp-servers',
      routePath: '/projects/:projectId/mcp-servers',
      seed: { projects: [PROJECT], mcpServers: SERVERS },
    });
    const row = await screen.findByTestId('enabled-mcp-server-fs');
    // Transport label + a secret-free command summary are shown.
    expect(row).toHaveTextContent('stdio');
    expect(row).toHaveTextContent('npx -y @modelcontextprotocol/server-filesystem');
  });

  it('removes a server via disableProjectMcpServer', async () => {
    renderWithProviders(<ProjectMcpServers />, {
      route: '/projects/weekly-compass/mcp-servers',
      routePath: '/projects/:projectId/mcp-servers',
      seed: { projects: [PROJECT], mcpServers: SERVERS },
    });
    await screen.findByTestId('enabled-mcp-server-fs');

    await userEvent.click(screen.getByTestId('remove-mcp-server-fs'));
    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'DELETE' && u.includes('projects/weekly-compass/mcp-servers/fs'),
        ),
      ).toBeDefined(),
    );
  });

  it('adds a server from the catalog via enableProjectMcpServer', async () => {
    renderWithProviders(<ProjectMcpServers />, {
      route: '/projects/weekly-compass/mcp-servers',
      routePath: '/projects/:projectId/mcp-servers',
      seed: { projects: [PROJECT], mcpServers: SERVERS },
    });
    await screen.findByTestId('enable-mcp-server-input');

    await userEvent.click(screen.getByTestId('enable-mcp-server-input'));
    await userEvent.click(await screen.findByTestId('enable-mcp-server-option-linear'));
    await userEvent.click(screen.getByTestId('enable-mcp-server-commit'));

    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/mcp-servers/linear'),
        ),
      ).toBeDefined(),
    );
  });

  it('shows an empty state when no servers are enabled', async () => {
    renderWithProviders(<ProjectMcpServers />, {
      route: '/projects/weekly-compass/mcp-servers',
      routePath: '/projects/:projectId/mcp-servers',
      seed: {
        projects: [{ ...PROJECT, enabledMcpServers: [] }],
        mcpServers: SERVERS,
      },
    });
    expect(await screen.findByTestId('project-mcp-servers-empty')).toBeInTheDocument();
  });

  it('renders the MCP Servers sub-tab in the project sub-nav', async () => {
    renderWithProviders(<ProjectLayout />, {
      route: '/projects/weekly-compass/mcp-servers',
      routePath: '/projects/:projectId/mcp-servers',
    });
    const link = await screen.findByRole('link', { name: 'MCP Servers' });
    expect(link).toHaveAttribute('href', expect.stringContaining('mcp-servers'));
  });
});
