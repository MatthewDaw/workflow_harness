import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import type { UnassignedIdea } from '../../api/baseApi.js';
import { BinTable } from './BinTable.js';
import { Bin } from './Bin.js';
import { renderWithProviders } from '../../test/testUtils.js';

/** Build an unassigned-bin entry with `freq` distinct sessions. */
function entry(over: Partial<UnassignedIdea> = {}): UnassignedIdea {
  const frequency = over.frequency ?? 1;
  const sources =
    over.sources ??
    Array.from({ length: frequency }, (_, i) => ({
      sessionId: `${over.entryId ?? 'e'}-sess-${i}`,
      segmentId: 'seg',
      seq: i,
      snippet: '',
      projectId: 'weekly-compass',
    }));
  return {
    entryId: 'e-1',
    org: 'acme',
    text: 'How to wire a multi-region failover',
    sources,
    createdAt: 1000,
    updatedAt: 1000,
    frequency,
    ...over,
  };
}

describe('BinTable', () => {
  it('lists each bin entry with its topic text and frequency', () => {
    renderWithProviders(
      <BinTable
        entries={[
          entry({ entryId: 'a', text: 'Failover wiring', frequency: 3 }),
          entry({ entryId: 'b', text: 'Cron drift', frequency: 1 }),
        ]}
      />,
    );
    expect(screen.getByTestId('bin-topic-a')).toHaveTextContent('Failover wiring');
    expect(screen.getByTestId('bin-frequency-a')).toHaveTextContent('3×');
    expect(screen.getByTestId('bin-topic-b')).toHaveTextContent('Cron drift');
    expect(screen.getByTestId('bin-frequency-b')).toHaveTextContent('1×');
  });

  it('sorts recurring topics most-frequent-first', () => {
    renderWithProviders(
      <BinTable
        entries={[
          entry({ entryId: 'rare', frequency: 1 }),
          entry({ entryId: 'common', frequency: 5 }),
          entry({ entryId: 'mid', frequency: 3 }),
        ]}
      />,
    );
    const order = screen
      .getAllByTestId(/^bin-row-/)
      .map((el) => el.getAttribute('data-testid'));
    expect(order).toEqual(['bin-row-common', 'bin-row-mid', 'bin-row-rare']);
  });

  it('renders the empty state when the bin is clear', () => {
    renderWithProviders(<BinTable entries={[]} />);
    expect(screen.getByTestId('bin-empty')).toBeInTheDocument();
  });

  it('shows the create-skill action only to admins (non-admins read but cannot action)', () => {
    const { rerender } = renderWithProviders(
      <BinTable entries={[entry({ entryId: 'a' })]} isAdmin={false} />,
    );
    // Non-admin: the row is visible (read) but no action affordance.
    expect(screen.getByTestId('bin-row-a')).toBeInTheDocument();
    expect(screen.queryByTestId('bin-create-skill-a')).toBeNull();

    rerender(<BinTable entries={[entry({ entryId: 'a' })]} isAdmin />);
    // The action is a disabled stub pointing at /skill-idea-iterate, not a no-op.
    expect(screen.getByTestId('bin-create-skill-a')).toBeDisabled();
  });
});

describe('Bin screen', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the bin entries served by the backlog endpoint', async () => {
    renderWithProviders(<Bin />, {
      seed: {
        me: { admin: true },
        bin: [entry({ entryId: 'a', text: 'Multi-region failover', frequency: 2 })],
      },
    });
    await waitFor(() => expect(screen.getByTestId('bin-topic-a')).toHaveTextContent('Multi-region failover'));
    expect(screen.getByTestId('bin-frequency-a')).toHaveTextContent('2×');
    // Admin sees the (disabled stub) action.
    expect(screen.getByTestId('bin-create-skill-a')).toBeDisabled();
  });

  it('renders the empty state when the org bin is clear', async () => {
    renderWithProviders(<Bin />, { seed: { bin: [] } });
    await waitFor(() => expect(screen.getByTestId('bin-empty')).toBeInTheDocument());
  });

  it('hides the create-skill action from non-admin readers', async () => {
    renderWithProviders(<Bin />, {
      seed: { me: { admin: false }, bin: [entry({ entryId: 'a' })] },
    });
    await waitFor(() => expect(screen.getByTestId('bin-row-a')).toBeInTheDocument());
    expect(screen.queryByTestId('bin-create-skill-a')).toBeNull();
  });
});
