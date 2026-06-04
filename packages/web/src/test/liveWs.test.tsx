import { describe, it, expect, afterEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Envelope, SessionProjection } from '@harness/shared';
import { LiveWatch } from '../screens/LiveWatch/LiveWatch.js';
import { wsEvent } from '../ws/liveActions.js';
import { applyEventToProjection } from '../ws/liveMiddleware.js';
import { renderWithProviders } from './testUtils.js';

/** Pull the control-frame POSTs the component issued out of the fetch stub. */
function controlCalls() {
  const fetchMock = globalThis.fetch as unknown as { mock: { calls: unknown[][] } };
  return fetchMock.mock.calls
    .map((c) => c[0] as { url: string; method?: string; body?: string })
    .filter((req) => /\/sessions\/[^/]+\/control$/.test(req.url))
    .map((req) => ({
      url: req.url,
      method: req.method,
      body: typeof req.body === 'string' ? JSON.parse(req.body) : req.body,
    }));
}

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

  it('renders an activity feed row per live event', async () => {
    const { store } = renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION] },
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));
    // Empty feed shows the waiting placeholder.
    expect(screen.getByText(/waiting for activity/)).toBeInTheDocument();

    store.dispatch(
      wsEvent({
        v: 1,
        instanceId: 'inst-0',
        host: 'matt@mbp',
        ts: 2000,
        seq: 10,
        event: { kind: 'tool.call', sessionId: 'a91f', tool: 'grep', argsSummary: 'foo' },
      }),
    );
    store.dispatch(wsEvent(statusEnvelope(11, 'needs_input')));

    await waitFor(() => expect(screen.getAllByTestId('live-event-row')).toHaveLength(2));
    const rows = screen.getAllByTestId('live-event-row');
    expect(rows[0]).toHaveTextContent('→ grep foo');
    expect(rows[1]).toHaveTextContent('● active → needs_input');
    // Streaming caret shows while the session is active/needs_input with events.
    expect(screen.getByText(/▌ streaming/)).toBeInTheDocument();
  });

  it('renders the REAL assistant/user/tool content carried by events', async () => {
    const ev = (seq: number, event: Envelope['event']): Envelope => ({
      v: 1,
      instanceId: 'inst-0',
      host: 'matt@mbp',
      ts: 2000 + seq,
      seq,
      event,
    });
    const { store } = renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION] },
    });
    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    store.dispatch(
      wsEvent(ev(20, { kind: 'user.msg', sessionId: 'a91f', tokens: 4, text: 'fix the live feed' })),
    );
    store.dispatch(
      wsEvent(
        ev(21, { kind: 'assistant.msg', sessionId: 'a91f', tokens: 9, text: 'On it — reading the tailer.' }),
      ),
    );
    store.dispatch(
      wsEvent(ev(22, { kind: 'tool.call', sessionId: 'a91f', tool: 'Bash', argsSummary: 'go test ./...' })),
    );
    store.dispatch(
      wsEvent(ev(23, { kind: 'tool.result', sessionId: 'a91f', ok: true, ms: 12, summary: 'ok  all tests pass' })),
    );

    // The real content (not just token counts) appears in the feed.
    await waitFor(() => expect(screen.getByText(/fix the live feed/)).toBeInTheDocument());
    expect(screen.getByText(/On it — reading the tailer\./)).toBeInTheDocument();
    expect(screen.getByText(/go test \.\/\.\.\./)).toBeInTheDocument();
    expect(screen.getByText(/all tests pass/)).toBeInTheDocument();
  });

  it('backfills stored event history on open (no waiting placeholder)', async () => {
    const backfill: Envelope[] = [
      {
        v: 1,
        instanceId: 'inst-0',
        host: 'matt@mbp',
        ts: 1500,
        seq: 3,
        event: { kind: 'user.msg', sessionId: 'a91f', tokens: 6, text: 'backfilled prompt' },
      },
      {
        v: 1,
        instanceId: 'inst-0',
        host: 'matt@mbp',
        ts: 1600,
        seq: 4,
        event: { kind: 'assistant.msg', sessionId: 'a91f', tokens: 8, text: 'backfilled reply' },
      },
    ];
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION], sessionEvents: { a91f: backfill } },
    });

    // History is shown immediately from the REST backfill — not "waiting…".
    await waitFor(() => expect(screen.getByText(/backfilled prompt/)).toBeInTheDocument());
    expect(screen.getByText(/backfilled reply/)).toBeInTheDocument();
    expect(screen.queryByText(/waiting for activity/)).not.toBeInTheDocument();
  });
});

describe('LiveWatch steer → sendControl', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('dispatches control{inject} with the session id + text on send', async () => {
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION] },
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    const box = screen.getByLabelText('Inject a message into the live session');
    await userEvent.type(box, 'ship it');
    await userEvent.click(screen.getByRole('button', { name: 'send' }));

    await waitFor(() => expect(controlCalls()).toHaveLength(1));
    const call = controlCalls()[0]!;
    expect(call.method).toBe('POST');
    expect(call.url).toMatch(/\/sessions\/a91f\/control$/);
    // sendControl maps `text` into `payload.text` with action 'inject'.
    expect(call.body).toEqual({ action: 'inject', payload: { text: 'ship it' } });
  });

  it('dispatches control{pause} and control{interrupt}', async () => {
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [SESSION] },
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    await userEvent.click(screen.getByRole('button', { name: /pause/ }));
    await userEvent.click(screen.getByRole('button', { name: /interrupt/ }));

    await waitFor(() => expect(controlCalls()).toHaveLength(2));
    const actions = controlCalls().map((c) => c.body.action);
    expect(actions).toEqual(['pause', 'interrupt']);
    // pause/interrupt carry no text payload.
    expect(controlCalls()[0]!.body).toEqual({ action: 'pause', payload: {} });
    expect(controlCalls()[1]!.body).toEqual({ action: 'interrupt', payload: {} });
  });

  it('disables steer controls for a session the user does not own (403 path)', async () => {
    // Mock auth user is `user-matt`; this session is owned by someone else.
    renderWithProviders(<LiveWatch />, {
      route: '/sessions/a91f',
      routePath: '/sessions/:sessionId',
      seed: { sessions: [{ ...SESSION, ownerUserId: 'user-other' }] },
    });

    await waitFor(() => expect(screen.getByTestId('live-status')).toHaveTextContent('active'));

    expect(screen.getByLabelText('Inject a message into the live session')).toBeDisabled();
    expect(screen.getByRole('button', { name: /pause/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /interrupt/ })).toBeDisabled();

    // Clicking a disabled control fires nothing.
    await userEvent.click(screen.getByRole('button', { name: /pause/ }));
    expect(controlCalls()).toHaveLength(0);
  });
});
