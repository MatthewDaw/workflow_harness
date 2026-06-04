import { describe, it, expect } from 'vitest';
import type { Envelope } from '@harness/shared';
import {
  liveEventsReducer,
  selectSessionEvents,
  MAX_EVENTS_PER_SESSION,
  type LiveEventsState,
} from '../app/liveEventsSlice.js';
import { wsEvent, wsUnsubscribe } from '../ws/liveActions.js';

function ev(seq: number, sessionId = 'a91f'): Envelope {
  return {
    v: 1,
    instanceId: 'inst-0',
    host: 'matt@mbp',
    ts: 1000 + seq,
    seq,
    event: { kind: 'assistant.msg', sessionId, tokens: seq },
  };
}

const empty: LiveEventsState = { bySession: {} };

describe('liveEventsSlice', () => {
  it('appends events to the session feed in order', () => {
    let state = liveEventsReducer(empty, wsEvent(ev(1)));
    state = liveEventsReducer(state, wsEvent(ev(2)));
    state = liveEventsReducer(state, wsEvent(ev(3)));
    const list = selectSessionEvents({ liveEvents: state }, 'a91f');
    expect(list.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('orders an out-of-order event by seq', () => {
    let state = liveEventsReducer(empty, wsEvent(ev(1)));
    state = liveEventsReducer(state, wsEvent(ev(3)));
    state = liveEventsReducer(state, wsEvent(ev(2)));
    const list = selectSessionEvents({ liveEvents: state }, 'a91f');
    expect(list.map((e) => e.seq)).toEqual([1, 2, 3]);
  });

  it('dedupes by seq (drops a repeat)', () => {
    let state = liveEventsReducer(empty, wsEvent(ev(1)));
    state = liveEventsReducer(state, wsEvent(ev(1)));
    const list = selectSessionEvents({ liveEvents: state }, 'a91f');
    expect(list).toHaveLength(1);
  });

  it('caps the feed at the most recent MAX_EVENTS_PER_SESSION', () => {
    let state = empty;
    for (let seq = 1; seq <= MAX_EVENTS_PER_SESSION + 50; seq++) {
      state = liveEventsReducer(state, wsEvent(ev(seq)));
    }
    const list = selectSessionEvents({ liveEvents: state }, 'a91f');
    expect(list).toHaveLength(MAX_EVENTS_PER_SESSION);
    expect(list[0]!.seq).toBe(51);
    expect(list[list.length - 1]!.seq).toBe(MAX_EVENTS_PER_SESSION + 50);
  });

  it('clears a session feed on unsubscribe', () => {
    let state = liveEventsReducer(empty, wsEvent(ev(1)));
    state = liveEventsReducer(state, wsEvent(ev(1, 'other')));
    state = liveEventsReducer(state, wsUnsubscribe({ sessionId: 'a91f' }));
    expect(selectSessionEvents({ liveEvents: state }, 'a91f')).toEqual([]);
    // Other sessions are untouched.
    expect(selectSessionEvents({ liveEvents: state }, 'other')).toHaveLength(1);
  });

  it('returns a stable empty array for an unknown session', () => {
    const a = selectSessionEvents({ liveEvents: empty }, 'nope');
    const b = selectSessionEvents({ liveEvents: empty }, 'nope');
    expect(a).toEqual([]);
    expect(a).toBe(b);
  });
});
