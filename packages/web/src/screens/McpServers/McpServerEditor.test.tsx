import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { McpServer } from '@harness/shared';
import { McpServerEditor } from './McpServerEditor.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const SERVERS: McpServer[] = [
  {
    name: 'filesystem',
    scope: ORG,
    transport: 'stdio',
    command: 'npx',
    args: ['-y', 'fs'],
    env: { ROOT: '/tmp' },
  },
];

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

/** Body of the last POST to the mcp-servers create/update endpoint. */
function lastServerPost(): Record<string, unknown> | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (req.method === 'POST' && /\/mcp-servers$/.test(req.url.split('?')[0] ?? '')) {
      return req.body ? JSON.parse(String(req.body)) : undefined;
    }
  }
  return undefined;
}

const adminSeed = (extra: Record<string, unknown> = {}) => ({ me: { admin: true }, ...extra });

describe('McpServerEditor', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('defaults to stdio and shows command/args/env fields, not url', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: adminSeed(),
    });
    await screen.findByTestId('mcp-server-editor');

    expect(screen.getByTestId('mcp-stdio-fields')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-command')).toBeInTheDocument();
    expect(screen.queryByTestId('mcp-remote-fields')).not.toBeInTheDocument();
    // No scope selector in the collapsed model.
    expect(screen.queryByTestId('mcp-scope')).not.toBeInTheDocument();
  });

  it('switching transport to http swaps to url/headers and hides command', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: adminSeed(),
    });
    await screen.findByTestId('mcp-server-editor');

    await userEvent.selectOptions(screen.getByTestId('mcp-transport'), 'http');
    expect(screen.getByTestId('mcp-remote-fields')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-url')).toBeInTheDocument();
    expect(screen.queryByTestId('mcp-command')).not.toBeInTheDocument();
  });

  it('saves a stdio server with the correct discriminated payload, no scope', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: adminSeed(),
    });
    await screen.findByTestId('mcp-server-editor');

    await userEvent.type(screen.getByTestId('mcp-name'), 'fs');
    await userEvent.type(screen.getByTestId('mcp-command'), 'npx');
    await userEvent.type(screen.getByTestId('mcp-args'), '-y server');
    await userEvent.click(screen.getByTestId('mcp-save'));

    await waitFor(() => expect(lastServerPost()).toBeDefined());
    const body = lastServerPost()!;
    expect(body).toMatchObject({ name: 'fs', transport: 'stdio', command: 'npx' });
    expect(body.args).toEqual(['-y', 'server']);
    // url/headers must NOT appear on a stdio payload.
    expect(body.url).toBeUndefined();
    // Server forces org scope; the client must not send scope.
    expect(body.scope).toBeUndefined();
  });

  it('saves an http server with url + headers payload', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: adminSeed(),
    });
    await screen.findByTestId('mcp-server-editor');

    await userEvent.type(screen.getByTestId('mcp-name'), 'linear');
    await userEvent.selectOptions(screen.getByTestId('mcp-transport'), 'http');
    await userEvent.type(screen.getByTestId('mcp-url'), 'https://mcp.linear.app/sse');
    await userEvent.click(screen.getByTestId('mcp-headers-add'));
    await userEvent.type(screen.getByTestId('mcp-headers-key-0'), 'Authorization');
    await userEvent.type(screen.getByTestId('mcp-headers-value-0'), 'Bearer x');
    await userEvent.click(screen.getByTestId('mcp-save'));

    await waitFor(() => expect(lastServerPost()).toBeDefined());
    const body = lastServerPost()!;
    expect(body).toMatchObject({
      name: 'linear',
      transport: 'http',
      url: 'https://mcp.linear.app/sse',
    });
    expect(body.headers).toEqual({ Authorization: 'Bearer x' });
    expect(body.command).toBeUndefined();
  });

  it('an invalid url surfaces an error and blocks save', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: adminSeed(),
    });
    await screen.findByTestId('mcp-server-editor');

    await userEvent.type(screen.getByTestId('mcp-name'), 'bad');
    await userEvent.selectOptions(screen.getByTestId('mcp-transport'), 'http');
    await userEvent.type(screen.getByTestId('mcp-url'), 'not a url');

    expect(screen.getByTestId('mcp-url-error')).toBeInTheDocument();
    expect(screen.getByTestId('mcp-save')).toBeDisabled();
    expect(lastServerPost()).toBeUndefined();
  });

  it('seeds the form on edit (name disabled) and exposes delete', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/filesystem/edit',
      routePath: '/mcp-servers/:name/edit',
      seed: adminSeed({ routes: { 'GET mcp-servers': SERVERS } }),
    });
    await screen.findByTestId('mcp-server-editor');

    await waitFor(() => expect(screen.getByTestId('mcp-command')).toHaveValue('npx'));
    expect(screen.getByTestId('mcp-name')).toBeDisabled();
    expect(screen.getByTestId('mcp-delete')).toBeInTheDocument();
  });

  it('non-admin cannot save (control disabled)', async () => {
    renderWithProviders(<McpServerEditor />, {
      route: '/mcp-servers/new',
      routePath: '/mcp-servers/new',
      seed: { me: { admin: false } },
    });
    await screen.findByTestId('mcp-server-editor');

    await userEvent.type(screen.getByTestId('mcp-name'), 'fs');
    await userEvent.type(screen.getByTestId('mcp-command'), 'npx');
    expect(screen.getByTestId('mcp-save')).toBeDisabled();
  });
});
