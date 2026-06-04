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
    model: 'claude-sonnet-4',
    prompt: 'Builds features',
    skills: ['gh'],
    tools: [],
    createdBy: { userId: 'u-matt', name: 'Matt' },
  },
  {
    name: 'reviewer',
    scope: ORG,
    model: 'claude-opus-4',
    prompt: '',
    skills: [],
    tools: [],
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

  it('links to the new-agent editor', async () => {
    renderWithProviders(<Agents />, { route: '/agents', seed: { agents: AGENTS } });
    const link = await screen.findByTestId('new-agent');
    expect(link).toHaveAttribute('href', '/agents/new');
  });
});
