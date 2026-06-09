import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Agent } from '@harness/shared';
import { Agents } from './Agents.js';
import { renderWithProviders } from '../../test/testUtils.js';

const ORG = { tier: 'org', id: 'acme' } as const;

const AGENTS: Agent[] = [
  {
    name: 'builder',
    scope: ORG,
    description: 'Use when building features',
    model: 'claude-sonnet-4',
    kind: 'agent',
    members: [],
    prompt: 'Builds features',
    skills: ['gh'],
    tools: ['Bash'],
    mcpServers: ['linear'],
    createdBy: { userId: 'u-matt', name: 'Matt' },
  },
  {
    name: 'reviewer',
    scope: ORG,
    description: '',
    model: 'claude-opus-4',
    kind: 'agent',
    members: [],
    prompt: '',
    skills: [],
    tools: [],
    mcpServers: [],
    createdBy: { userId: 'u-sam', name: 'Sam' },
  },
];

describe('Agents org catalog (collapsed model)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders a flat org catalog without scope groups, pickers, or promote', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    expect(screen.queryByTestId('scope-group-org')).not.toBeInTheDocument();
    expect(screen.queryByTestId('scope-picker-builder')).not.toBeInTheDocument();
    expect(screen.queryByTestId('promote-builder')).not.toBeInTheDocument();
    expect(screen.getByTestId('agent-card-reviewer')).toBeInTheDocument();
  });

  it('filters by author', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    await userEvent.selectOptions(screen.getByTestId('agent-author-filter'), 'Sam');
    expect(screen.getByTestId('agent-card-reviewer')).toBeInTheDocument();
    expect(screen.queryByTestId('agent-card-builder')).not.toBeInTheDocument();
  });

  it('keeps the prompt and attachments off the card, behind Expand', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    // Card shows the summary (description) but not the prompt or attachment lists.
    expect(screen.getByTestId('agent-description-builder')).toHaveTextContent(
      'Use when building features',
    );
    expect(screen.queryByTestId('agent-prompt-builder')).not.toBeInTheDocument();
    expect(screen.queryByTestId('agent-skills-builder')).not.toBeInTheDocument();
  });

  it('expands to show skills, MCP servers, tools, and the prompt', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-builder');

    await userEvent.click(screen.getByTestId('agent-expand-builder'));

    expect(screen.getByTestId('agent-modal-builder')).toBeInTheDocument();
    expect(screen.getByTestId('agent-skills-builder')).toHaveTextContent('gh');
    expect(screen.getByTestId('agent-mcp-builder')).toHaveTextContent('linear');
    expect(screen.getByTestId('agent-tools-builder')).toHaveTextContent('Bash');
    expect(screen.getByTestId('agent-prompt-builder')).toHaveTextContent('Builds features');

    await userEvent.click(screen.getByTestId('agent-modal-close-builder'));
    expect(screen.queryByTestId('agent-modal-builder')).not.toBeInTheDocument();
  });

  it('shows no Expand button for an agent with no prompt or attachments', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    await screen.findByTestId('agent-card-reviewer');

    expect(screen.queryByTestId('agent-expand-reviewer')).not.toBeInTheDocument();
  });

  it('links to the new-agent editor', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    const link = await screen.findByTestId('new-agent');
    expect(link).toHaveAttribute('href', '/agents/new');
  });
});
