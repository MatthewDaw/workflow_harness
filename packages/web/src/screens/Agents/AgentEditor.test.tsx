import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent, Skill } from '@harness/shared';
import { AgentEditor } from './AgentEditor.js';
import { renderWithProviders } from '../../test/testUtils.js';

const SKILLS: Skill[] = [
  {
    name: 'gh',
    scope: { tier: 'org', id: 'acme' },
    kind: 'skill',
    description: 'GitHub CLI',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'browse',
    scope: { tier: 'user', id: 'user-matt' },
    kind: 'skill',
    description: 'Headless browser',
    source: 'local',
    members: [],
    body: '',
  },
];

const AGENTS: Agent[] = [
  {
    name: 'builder',
    scope: { tier: 'user', id: 'user-matt' },
    model: 'claude-sonnet-4',
    prompt: 'Builds',
    skills: ['gh'],
    tools: [],
  },
];

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

/** Find the body of the last POST to the agents create/update endpoint. */
function lastAgentPost(): Record<string, unknown> | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (req.method === 'POST' && /\/agents$/.test(req.url.split('?')[0] ?? '')) {
      return req.body ? JSON.parse(String(req.body)) : undefined;
    }
  }
  return undefined;
}

describe('AgentEditor (U17)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('creates a new agent: name + scope + skills persist via saveAgent', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/new',
      routePath: '/agents/new',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('agent-editor');

    await userEvent.type(screen.getByTestId('agent-name'), 'distiller');
    await userEvent.selectOptions(screen.getByTestId('agent-scope'), 'org');
    await userEvent.click(screen.getByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body).toMatchObject({
      name: 'distiller',
      scope: { tier: 'org', id: 'acme' },
    });
    expect(body.skills).toContain('browse');
  });

  it('edits an existing agent: seeds the form and saves toggled skills', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/builder/edit',
      routePath: '/agents/:name/edit',
      seed: { skills: SKILLS, agents: AGENTS },
    });
    await screen.findByTestId('agent-editor');

    // Seeded from the existing agent.
    await waitFor(() =>
      expect(screen.getByTestId('agent-model')).toHaveValue('claude-sonnet-4'),
    );
    // gh is already selected; toggle on browse too.
    await userEvent.click(screen.getByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body.name).toBe('builder');
    expect(body.skills).toEqual(expect.arrayContaining(['gh', 'browse']));
  });

  it('shows a muted hint to refine the prompt via the claude+ skill', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/builder/edit',
      routePath: '/agents/:name/edit',
      seed: { skills: SKILLS, agents: AGENTS },
    });
    await screen.findByTestId('agent-editor');
    expect(screen.getByTestId('optimize-hint')).toHaveTextContent(
      'Run /optimize-agent in claude+ to refine this prompt.',
    );
  });
});
