import { describe, it, expect, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import type { Envelope, SessionProjection } from '@harness/shared';
import { LiveWatch } from '../screens/LiveWatch/LiveWatch.js';
import { wsEvent } from '../ws/liveActions.js';
import { applyEventToProjection } from '../ws/liveMiddleware.js';
import { renderWithProviders } from './testUtils.js';

const SESSION: SessionProjection = {
  sessionId: 'a91f',
  projectId: 'weekly-compass',
  name: 'reconcile-variance',
  host: 'matt@mbp',
  agent: 'builder',
  status: 'active',
  tokens: 48000,
  costUsd: 0.62,
  startedAt: 1000,
  lastEventAt: 1000,
  maxSeq: 5,
};

function statusEnvelope(seq: number, to: SessionProjection['status']): Envelope {
  return {
    v: 1,
    instanceId: 'inst-0',
    host: 'matt@mbp',
    ts: 2000,
    seq,
    event: { kind: 'status.change', sessionId: 'a91f', from: 'active', to },
  };
}

describe('live WS middleware', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('folds a status.change into a projection (max-seq wins, stale ignored)', () => {
    const updated = applyEventToProjection(SESSION, statusEnvelope(6, 'needs_input'));
    expect(updated?.status).toBe('needs_input');
    expect(updated?.maxSeq).toBe(6);

    // A stale event (seq <= maxSeq) does not regress the projection.
    const stale = applyEventToProjection({ ...SESSION, maxSeq: 10 }, statusEnvelope(6, 'idle'));
    expect(stale?.status).toBe('active');
    expect(stale?.maxSeq).toBe(10);
  });

  it('updates a subscribed component when a mock WS event arrives (no refetch)', async () => {
    const { store } = renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION] },
    });

    // The session loads via the seeded fetch.
    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    // Dispatch a live status.change as if it came off the socket.
    store.dispatch(wsEvent(statusEnvelope(6, 'needs_input')));

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('needs_input'));
  });
});
