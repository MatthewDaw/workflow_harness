import { createAction, createSlice } from '@reduxjs/toolkit';
import type { Envelope } from '@harness/shared';
import { wsEvent, wsUnsubscribe } from '../ws/liveActions.js';

/** Most recent N events retained per session (older are dropped to bound memory). */
export const MAX_EVENTS_PER_SESSION = 300;

/**
 * Seed a session's feed with a backfilled page of stored events (the REST
 * `GET /sessions/:id` history). Dispatched by LiveWatch on open so the feed
 * shows the full conversation immediately instead of "waiting for activity…".
 * The reducer merges these in by `seq` (deduping against anything the live WS
 * has already delivered), so backfill + live coexist with no duplicates.
 */
export const seedSessionEvents = createAction<{ sessionId: string; events: Envelope[] }>(
  'liveEvents/seed',
);

/** Insert one envelope into a seq-ordered list, deduping by seq. */
function insertBySeq(list: Envelope[], env: Envelope): void {
  if (list.some((e) => e.seq === env.seq)) return;
  if (list.length === 0 || env.seq > list[list.length - 1]!.seq) {
    list.push(env);
  } else {
    let i = list.length;
    while (i > 0 && list[i - 1]!.seq > env.seq) i--;
    list.splice(i, 0, env);
  }
}

/**
 * Accumulates the raw per-event activity feed for each live session so the
 * Watch & steer view can render a transcript. The projection cache (RTK Query)
 * only carries aggregates; this slice keeps the individual envelopes.
 */
export interface LiveEventsState {
  bySession: Record<string, Envelope[]>;
}

const initialState: LiveEventsState = { bySession: {} };

const liveEventsSlice = createSlice({
  name: 'liveEvents',
  initialState,
  reducers: {},
  extraReducers: (builder) => {
    builder.addCase(wsEvent, (state, action) => {
      const env = action.payload;
      const sessionId = env.event.sessionId;
      const list = state.bySession[sessionId] ?? (state.bySession[sessionId] = []);

      // Insert keeping the list ordered by seq, deduping by seq (events usually
      // arrive in order, so the common case appends to the tail). This is what
      // makes the feed update LIVE: each inbound WS event lands here and the
      // selector recomputes, re-rendering LiveWatch + auto-scrolling.
      insertBySeq(list, env);

      // Cap at the most recent MAX_EVENTS_PER_SESSION (drop oldest).
      if (list.length > MAX_EVENTS_PER_SESSION) {
        list.splice(0, list.length - MAX_EVENTS_PER_SESSION);
      }
    });

    // Backfill: merge a page of stored history in by seq. Anything the live WS
    // already delivered is deduped, so re-seeding never duplicates rows.
    builder.addCase(seedSessionEvents, (state, action) => {
      const { sessionId, events } = action.payload;
      const list = state.bySession[sessionId] ?? (state.bySession[sessionId] = []);
      for (const env of events) insertBySeq(list, env);
      if (list.length > MAX_EVENTS_PER_SESSION) {
        list.splice(0, list.length - MAX_EVENTS_PER_SESSION);
      }
    });

    builder.addCase(wsUnsubscribe, (state, action) => {
      delete state.bySession[action.payload.sessionId];
    });
  },
});

export const liveEventsReducer = liveEventsSlice.reducer;

/** Select the accumulated event feed for a session (empty array when absent). */
export function selectSessionEvents(
  state: { liveEvents: LiveEventsState },
  sessionId: string,
): Envelope[] {
  return state.liveEvents.bySession[sessionId] ?? EMPTY;
}

const EMPTY: Envelope[] = [];
