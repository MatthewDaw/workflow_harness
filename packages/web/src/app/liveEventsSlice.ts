import { createSlice } from '@reduxjs/toolkit';
import type { Envelope } from '@harness/shared';
import { wsEvent, wsUnsubscribe } from '../ws/liveActions.js';

/** Most recent N events retained per session (older are dropped to bound memory). */
export const MAX_EVENTS_PER_SESSION = 300;

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

      // Dedupe by seq — drop if we've already recorded this monotonic seq.
      if (list.some((e) => e.seq === env.seq)) return;

      // Insert keeping the list ordered by seq (events usually arrive in order,
      // so the common case appends to the tail).
      if (list.length === 0 || env.seq > list[list.length - 1]!.seq) {
        list.push(env);
      } else {
        let i = list.length;
        while (i > 0 && list[i - 1]!.seq > env.seq) i--;
        list.splice(i, 0, env);
      }

      // Cap at the most recent MAX_EVENTS_PER_SESSION (drop oldest).
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
