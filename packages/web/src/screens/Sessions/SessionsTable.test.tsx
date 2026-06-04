import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { SessionProjection } from '@harness/shared';
import { SessionsTable } from './SessionsTable.js';
import { renderWithProviders } from '../../test/testUtils.js';

/** Pull the control-frame POSTs the table issued out of the fetch stub. */
function controlCalls() {
  const fetchMock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  return fetchMock.mock.calls
    .map((c) => c[0] as { url: string; method?: string; body?: string })
    .filter((req) => /\/sessions\/[^/]+\/control$/.test(req.url))
    .map((req) => (typeof req.body === 'string' ? JSON.parse(req.body) : req.body));
}

function session(overrides: Partial<SessionProjection> = {}): SessionProjection {
  return {
    sessionId: 'a91f',
    projectId: 'weekly-compass',
    name: 'reconcile-variance',
    host: 'matt@mbp',
    agent: 'builder',
    status: 'active',
    tokens: 1000,
    costUsd: 0.62,
    startedAt: 1000,
    lastEventAt: 1000,
    maxSeq: 5,
    ...overrides,
  };
}

describe('SessionsTable', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('shows how long ago each session last streamed', () => {
    const now = Date.now();
    renderWithProviders(
      <SessionsTable sessions={[session({ lastEventAt: now - 3 * 3_600_000 })]} />,
    );
    expect(screen.getByTestId('session-activity-a91f')).toHaveTextContent('3h ago');
  });

  it('shows the session name and the first-prompt summary (truncated), with an em dash when absent', () => {
    const longPrompt =
      'fix the login bug in the cursor flow that breaks on the second attempt and more text';
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({ sessionId: 'a91f', name: 'fix-login-cursor', summary: longPrompt }),
          session({ sessionId: 'nosum', name: 'untitled', summary: undefined }),
        ]}
      />,
    );
    // name column still renders s.name under the existing testid.
    expect(screen.getByTestId('session-name-a91f')).toHaveTextContent('fix-login-cursor');
    // new first-prompt column renders the (truncated) summary with an ellipsis.
    const summaryCell = screen.getByTestId('session-summary-a91f');
    expect(summaryCell).toHaveTextContent('fix the login bug in the cursor flow');
    expect(summaryCell.textContent?.endsWith('…')).toBe(true);
    // missing summary falls back to an em dash.
    expect(screen.getByTestId('session-summary-nosum')).toHaveTextContent('—');
  });

  it('offers Shut down on live rows but not on done rows', () => {
    renderWithProviders(
      <SessionsTable
        sessions={[
          session({ sessionId: 'live1', status: 'active' }),
          session({ sessionId: 'done1', status: 'done' }),
        ]}
      />,
    );
    expect(screen.getByTestId('session-shutdown-live1')).toBeInTheDocument();
    expect(screen.queryByTestId('session-shutdown-done1')).toBeNull();
  });

  it('confirms, then sends a graceful shutdown control frame', async () => {
    renderWithProviders(<SessionsTable sessions={[session({ sessionId: 'a91f' })]} />);

    // First click only arms the confirm — no frame sent yet.
    await userEvent.click(screen.getByTestId('session-shutdown-a91f'));
    expect(controlCalls()).toHaveLength(0);

    await userEvent.click(screen.getByTestId('session-shutdown-confirm-a91f'));
    await waitFor(() => expect(controlCalls()).toHaveLength(1));
    expect(controlCalls()[0]).toEqual({ action: 'shutdown', payload: {} });
  });

  it('sends a force kill when Force is chosen', async () => {
    renderWithProviders(<SessionsTable sessions={[session({ sessionId: 'a91f' })]} />);

    await userEvent.click(screen.getByTestId('session-shutdown-a91f'));
    await userEvent.click(screen.getByTestId('session-kill-a91f'));

    await waitFor(() => expect(controlCalls()).toHaveLength(1));
    expect(controlCalls()[0]).toEqual({ action: 'kill', payload: {} });
  });

  it('can cancel the confirm without sending anything', async () => {
    renderWithProviders(<SessionsTable sessions={[session({ sessionId: 'a91f' })]} />);

    await userEvent.click(screen.getByTestId('session-shutdown-a91f'));
    await userEvent.click(screen.getByTestId('session-shutdown-cancel-a91f'));

    expect(screen.getByTestId('session-shutdown-a91f')).toBeInTheDocument();
    expect(controlCalls()).toHaveLength(0);
  });
});
