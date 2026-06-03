import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import type { Envelope, SessionProjection } from '@harness/shared';
import { makeStore } from '../app/store.js';
import { baseApi } from '../api/baseApi.js';
import { wsConnect, wsDisconnect } from '../ws/liveActions.js';

const SESSION: SessionProjection = {
  sessionId: 'a91f',
  projectId: 'p',
  name: 'n',
  host: 'h',
  agent: 'a',
  status: 'active',
  tokens: 0,
  costUsd: 0,
  startedAt: 0,
  lastEventAt: 0,
  maxSeq: 1,
};

/**
 * Verifies the live middleware (H3/H4): wsConnect opens a socket with the auth
 * token on the query string, and an inbound `{ type: 'event', envelope }` frame
 * is unwrapped into a `wsEvent` action (a bare envelope still works too).
 */

const ENVELOPE: Envelope = {
  v: 1,
  instanceId: 'inst-0',
  host: 'matt@mbp',
  ts: 2000,
  seq: 9,
  event: { kind: 'status.change', sessionId: 'a91f', from: 'active', to: 'needs_input' },
};

class MockWebSocket {
  static instances: MockWebSocket[] = [];
  static OPEN = 1;
  readyState = MockWebSocket.OPEN;
  url: string;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onopen: (() => void) | null = null;
  sent: string[] = [];
  closed = false;
  constructor(url: string) {
    this.url = url;
    MockWebSocket.instances.push(this);
  }
  send(data: string) {
    this.sent.push(data);
  }
  close() {
    this.closed = true;
  }
}

describe('live WS connect + event-frame unwrap', () => {
  beforeEach(() => {
    MockWebSocket.instances = [];
    vi.stubGlobal('WebSocket', MockWebSocket as unknown as typeof WebSocket);
  });
  afterEach(() => vi.unstubAllGlobals());

  it('opens the socket with the auth token on the query string', () => {
    const store = makeStore();
    store.dispatch(wsConnect({ url: 'wss://ws.example/stage', token: 'tok 123' }));
    const sock = MockWebSocket.instances.at(-1)!;
    expect(sock.url).toBe('wss://ws.example/stage?token=tok%20123');
    store.dispatch(wsDisconnect());
    expect(sock.closed).toBe(true);
  });

  it('omits the token query param when no token is provided', () => {
    const store = makeStore();
    store.dispatch(wsConnect({ url: 'wss://ws.example/stage' }));
    expect(MockWebSocket.instances.at(-1)!.url).toBe('wss://ws.example/stage');
  });

  it('unwraps a { type: "event", envelope } frame and folds it into the session cache', async () => {
    const store = makeStore();
    await store.dispatch(baseApi.util.upsertQueryData('getSession', 'a91f', SESSION));
    store.dispatch(wsConnect({ url: 'wss://ws.example/stage', token: null }));
    const sock = MockWebSocket.instances.at(-1)!;

    sock.onmessage?.({ data: JSON.stringify({ type: 'event', envelope: ENVELOPE }) });

    const entry = baseApi.endpoints.getSession.select('a91f')(store.getState());
    expect(entry.data?.status).toBe('needs_input');
    expect(entry.data?.maxSeq).toBe(9);
  });

  it('still tolerates a bare envelope (no wrapper)', async () => {
    const store = makeStore();
    await store.dispatch(baseApi.util.upsertQueryData('getSession', 'a91f', SESSION));
    store.dispatch(wsConnect({ url: 'wss://ws.example/stage' }));
    const sock = MockWebSocket.instances.at(-1)!;

    sock.onmessage?.({ data: JSON.stringify(ENVELOPE) });

    const entry = baseApi.endpoints.getSession.select('a91f')(store.getState());
    expect(entry.data?.status).toBe('needs_input');
  });
});
