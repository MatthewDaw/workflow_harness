import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent } from '@harness/shared';
import { Agents } from './Agents.js';
import { renderWithProviders } from '../../test/testUtils.js';

const AGENTS: Agent[] = [
  {
    name: 'builder',
    scope: { tier: 'project', id: 'user-matt' },
    model: 'claude-sonnet-4',
    prompt: 'Builds features',
    skills: ['gh'],
    tools: [],
  },
  {
    name: 'reviewer',
    scope: { tier: 'user', id: 'user-matt' },
    model: 'claude-opus-4',
    prompt: '',
    skills: [],
    tools: [],
  },
];

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

/** Find the last POST to an agents/.../scope endpoint (reads the stubbed Request). */
function lastScopePost(): { url: string; body: unknown } | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (req.method === 'POST' && req.url.includes('/scope')) {
      return { url: req.url, body: req.body ? JSON.parse(String(req.body)) : undefined };
    }
  }
  return undefined;
}

describe('Agents scope controls (U17/U15)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('groups agents by scope tier', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');
    expect(screen.getByTestId('scope-group-project')).toBeInTheDocument();
    expect(screen.getByTestId('scope-group-user')).toBeInTheDocument();
  });

  it('elevates an agent to org via changeAgentScope (mutation fires with the new scope)', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    await userEvent.selectOptions(screen.getByTestId('scope-picker-builder'), 'org');

    await waitFor(() => expect(lastScopePost()).toBeDefined());
    const post = lastScopePost()!;
    expect(post.url).toContain('agents/builder/scope');
    expect(post.body).toMatchObject({ scope: { tier: 'org', id: 'acme' } });
  });

  it('exposes the single-admin promote control and elevates to org', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    const promote = screen.getByTestId('promote-builder');
    await userEvent.click(promote);

    await waitFor(() => expect(lastScopePost()).toBeDefined());
    expect(lastScopePost()!.body).toMatchObject({ scope: { tier: 'org', id: 'acme' } });
  });

  it('hides the promote control once an agent is already at org scope', async () => {
    const orgAgent: Agent = { ...AGENTS[0]!, name: 'org-agent', scope: { tier: 'org', id: 'acme' } };
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: [orgAgent] } });
    await screen.findByTestId('agent-card-org-agent');
    expect(screen.queryByTestId('promote-org-agent')).not.toBeInTheDocument();
  });

  it('links to the new-agent editor', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    const link = await screen.findByTestId('new-agent');
    expect(link).toHaveAttribute('href', '/agents/new');
  });
});
