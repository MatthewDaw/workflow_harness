import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent, McpServer, Skill } from '@harness/shared';
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
    description: 'Use when building features',
    model: 'claude-sonnet-4',
    kind: 'agent',
    members: [],
    prompt: 'Builds',
    skills: ['gh'],
    tools: [],
    mcpServers: ['fs'],
  },
];

const MCP_SERVERS: McpServer[] = [
  { name: 'fs', scope: ORG, transport: 'stdio', command: 'npx', args: [], env: {} },
  { name: 'linear', scope: ORG, transport: 'http', url: 'https://mcp.linear.app', headers: {} },
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
    await userEvent.type(
      screen.getByTestId('agent-description'),
      'Invoke to distill a session into an agent',
    );
    await userEvent.click(await screen.findByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body).toMatchObject({ name: 'distiller' });
    // Server forces org scope; the client must not send scope on create.
    expect(body.scope).toBeUndefined();
    expect(body.skills).toContain('browse');
    // The new delegation-trigger field reaches the POST payload.
    expect(body.description).toBe('Invoke to distill a session into an agent');
  });

  it('edits an existing agent: seeds the form and saves toggled skills', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/builder/edit',
      routePath: '/agents/:name/edit',
      seed: { skills: SKILLS, agents: AGENTS },
    });
    await screen.findByTestId('agent-editor');

    // Seeded from the existing agent.
    await waitFor(() => expect(screen.getByTestId('agent-model')).toHaveValue('claude-sonnet-4'));
    // Description seeds from the existing record and saves through.
    await waitFor(() =>
      expect(screen.getByTestId('agent-description')).toHaveValue('Use when building features'),
    );
    // gh is already selected; toggle on browse too. The catalog renders once
    // the seeded skills resolve, so wait for the button before clicking.
    await userEvent.click(await screen.findByTestId('catalog-skill-browse'));
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body.name).toBe('builder');
    expect(body.skills).toEqual(expect.arrayContaining(['gh', 'browse']));
    expect(body.description).toBe('Use when building features');
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

  it('toggles an MCP server into the draft and the save payload', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/new',
      routePath: '/agents/new',
      seed: { skills: SKILLS, mcpServers: MCP_SERVERS },
    });
    await screen.findByTestId('agent-editor');

    await userEvent.type(screen.getByTestId('agent-name'), 'distiller');
    // Toggle on an MCP server; it shows as pressed.
    const linear = await screen.findByTestId('catalog-mcp-linear');
    await userEvent.click(linear);
    expect(linear).toHaveAttribute('aria-pressed', 'true');

    await userEvent.click(screen.getByTestId('agent-save'));
    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body.mcpServers).toContain('linear');
  });

  it('edits an agent: seeds mcpServers and removes one before save', async () => {
    renderWithProviders(<AgentEditor />, {
      route: '/agents/builder/edit',
      routePath: '/agents/:name/edit',
      seed: { skills: SKILLS, agents: AGENTS, mcpServers: MCP_SERVERS },
    });
    await screen.findByTestId('agent-editor');

    // The seeded server renders as already selected.
    const fs = await screen.findByTestId('catalog-mcp-fs');
    await waitFor(() => expect(fs).toHaveAttribute('aria-pressed', 'true'));

    // Toggling it off removes it from the draft + saved payload.
    await userEvent.click(fs);
    await userEvent.click(screen.getByTestId('agent-save'));

    await waitFor(() => expect(lastAgentPost()).toBeDefined());
    const body = lastAgentPost()!;
    expect(body.name).toBe('builder');
    expect(body.mcpServers).toEqual([]);
  });
});
