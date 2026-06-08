import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { McpServer } from '@harness/shared';
import { McpServers } from './McpServers.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const SERVERS: McpServer[] = [
  {
    name: 'filesystem',
    scope: ORG,
    transport: 'stdio',
    command: 'npx',
    args: ['-y', '@modelcontextprotocol/server-filesystem'],
    env: { API_KEY: 'super-secret-value' },
    createdBy: { userId: 'u-matt', name: 'Matt' },
  },
  {
    name: 'linear',
    scope: ORG,
    transport: 'http',
    url: 'https://mcp.linear.app/sse',
    headers: {},
    createdBy: { userId: 'u-sam', name: 'Sam' },
  },
];

/** Seed the org catalog through the route override (the stub has no mcp branch). */
function seedFor(servers: McpServer[], admin = true) {
  return { routes: { 'GET mcp-servers': servers }, me: { admin } };
}

describe('McpServers org catalog (collapsed model)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders a flat org catalog of cards with transport + summary', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS) });
    await screen.findByTestId('mcp-card-filesystem');

    expect(screen.getByTestId('mcp-card-linear')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-transport-filesystem')).toHaveTextContent('stdio');
    expect(screen.getByTestId('mcp-transport-linear')).toHaveTextContent('http');
    expect(screen.getByTestId('mcp-summary-filesystem')).toHaveTextContent(
      'npx -y @modelcontextprotocol/server-filesystem',
    );
    expect(screen.getByTestId('mcp-summary-linear')).toHaveTextContent(
      'https://mcp.linear.app/sse',
    );
  });

  it('expands a stdio server to show command, args, and env (with masked secret values)', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS) });
    await screen.findByTestId('mcp-card-filesystem');

    // Details are hidden until Expand is clicked.
    expect(screen.queryByTestId('mcp-modal-filesystem')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('mcp-expand-filesystem'));

    const modal = screen.getByTestId('mcp-modal-filesystem');
    expect(modal).toHaveTextContent('npx');
    expect(modal).toHaveTextContent('@modelcontextprotocol/server-filesystem');
    // The env KEY shows; the secret VALUE is masked, never printed.
    expect(screen.getByTestId('mcp-env-filesystem')).toHaveTextContent('API_KEY');
    expect(modal).not.toHaveTextContent('super-secret-value');

    await userEvent.click(screen.getByTestId('mcp-modal-close-filesystem'));
    expect(screen.queryByTestId('mcp-modal-filesystem')).not.toBeInTheDocument();
  });

  it('expands an http server to show its URL', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS) });
    await screen.findByTestId('mcp-card-linear');

    await userEvent.click(screen.getByTestId('mcp-expand-linear'));
    expect(screen.getByTestId('mcp-modal-linear')).toHaveTextContent('https://mcp.linear.app/sse');
  });

  it('shows an empty state when the catalog is empty', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor([]) });
    expect(await screen.findByTestId('mcp-empty')).toBeInTheDocument();
  });

  it('filters by author', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS) });
    await screen.findByTestId('mcp-card-filesystem');

    await userEvent.selectOptions(screen.getByTestId('mcp-author-filter'), 'Sam');
    expect(screen.getByTestId('mcp-card-linear')).toBeInTheDocument();
    expect(screen.queryByTestId('mcp-card-filesystem')).not.toBeInTheDocument();
  });

  it('admin sees the new-server link routing to the editor', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS, true) });
    const link = await screen.findByTestId('new-mcp-server');
    expect(link).toHaveAttribute('href', '/mcp-servers/new');
  });

  it('non-admin does not see create or edit write controls', async () => {
    renderWithProviders(<McpServers />, { route: '/mcp-servers', seed: seedFor(SERVERS, false) });
    await screen.findByTestId('mcp-card-filesystem');

    expect(screen.queryByTestId('new-mcp-server')).not.toBeInTheDocument();
    // The card name is plain text, not an edit link, for non-admins.
    expect(screen.queryByRole('link', { name: 'filesystem' })).not.toBeInTheDocument();
  });
});
