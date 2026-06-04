import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent, Skill } from '@harness/shared';
import { AgentEditor } from './AgentEditor.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const SKILLS: Skill[] = [
  {
    name: 'gh',
    scope: ORG,
    kind: 'skill',
    description: 'GitHub CLI',
    source: 'built-in',
    members: [],
    body: '',
  },
  {
    name: 'browse',
    scope: ORG,
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
    scope: ORG,
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

  it('creates a new agent: name + skills persist via saveAgent, no scope sent', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/new',
      routePath: '/agents/new',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('agent-editor');

    // No scope selector in the collapsed model.
    expect(screen.queryByTestId('agent-scope')).not.toBeInTheDocument();

    await userEvent.type(screen.getByTestId('agent-name'), 'distiller');
    await userEvent.selectOptions(screen.getByTestId('agent-scope'), 'org');
    await userEvent.click(await screen.findByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body).toMatchObject({ name: 'distiller' });
    // Server forces org scope; the client must not send scope on create.
    expect(body.scope).toBeUndefined();
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
    // gh is already selected; toggle on browse too. The catalog renders once
    // the seeded skills resolve, so wait for the button before clicking.
    await userEvent.click(await screen.findByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body.name).toBe('builder');
    expect(body.skills).toEqual(expect.arrayContaining(['gh', 'browse']));
  });

  it('filters the skill catalog as you type', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/new',
      routePath: '/agents/new',
      seed: { skills: SKILLS },
    });
    await screen.findByTestId('agent-editor');

    // Both skills visible initially.
    expect(await screen.findByTestId('catalog-skill-gh')).toBeInTheDocument();
    expect(screen.getByTestId('catalog-skill-browse')).toBeInTheDocument();

    // Typing filters down to the matching skill.
    await userEvent.type(screen.getByTestId('skill-filter'), 'brow');
    expect(screen.getByTestId('catalog-skill-browse')).toBeInTheDocument();
    expect(screen.queryByTestId('catalog-skill-gh')).not.toBeInTheDocument();
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
