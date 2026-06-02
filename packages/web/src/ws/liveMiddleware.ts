import type { Middleware } from '@reduxjs/toolkit';
import { safeParseEnvelope, type Envelope, type SessionProjection } from '@harness/shared';
import { baseApi } from '../api/baseApi.js';
import { wsConnect, wsDisconnect, wsSubscribe, wsUnsubscribe, wsEvent } from './liveActions.js';

/**
 * WebSocket middleware (U19, KTD12). Holds the live socket, sends subscribe
 * frames to the control gateway (U7), and on each inbound event dispatches
 * `wsEvent`. The `wsEvent` handler folds the envelope into the RTK Query session
 * cache (getSession + getSessions) so any subscribed component re-renders live —
 * no refetch. Designed so tests can dispatch `wsEvent` directly with a mock
 * envelope and observe the cache update, without a real socket.
 */

/** Apply one event envelope to a session projection (pure, max-seq wins). */
export function applyEventToProjection(
  prev: SessionProjection | undefined,
  env: Envelope,
): SessionProjection | undefined {
  if (!prev) return prev;
  if (env.seq <= prev.maxSeq) return prev; // ignore stale/duplicate
  const next: SessionProjection = { ...prev, maxSeq: env.seq, lastEventAt: env.ts };
  const e = env.event;
  switch (e.kind) {
    case 'session.rename':
      next.name = e.name;
      break;
    case 'status.change':
      next.status = e.to;
      break;
    case 'cost.tick':
      next.costUsd = e.totalUsd;
      next.tokens = e.tokens;
      break;
    case 'user.msg':
    case 'assistant.msg':
      next.tokens = prev.tokens + e.tokens;
      break;
    default:
      break;
  }
  return next;
}

export const liveMiddleware: Middleware = (store) => {
  let socket: WebSocket | null = null;
  const subscriptions = new Set<string>();
  // The patch thunks from `updateQueryData` carry the api's concrete RootState,
  // which the generic Middleware dispatch can't express; accept them loosely.
  const dispatch = store.dispatch as (thunk: unknown) => unknown;

  const send = (data: unknown) => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify(data));
    }
  };

  const foldEvent = (env: Envelope) => {
    const sessionId = env.event.sessionId;
    // Update the single-session cache entry.
    dispatch(
      baseApi.util.updateQueryData('getSession', sessionId, (draft) => {
        const updated = applyEventToProjection(draft, env);
        if (updated) Object.assign(draft, updated);
      }),
    );
    // Update the matching row in any cached sessions list.
    for (const live of [undefined, { live: true }, { live: false }] as const) {
      dispatch(
        baseApi.util.updateQueryData('getSessions', live, (draft) => {
          const idx = draft.findIndex((s) => s.sessionId === sessionId);
          if (idx === -1) return;
          const updated = applyEventToProjection(draft[idx], env);
          if (updated) draft[idx] = updated;
        }),
      );
    }
  };

  return (next) => (action) => {
    if (wsConnect.match(action)) {
      socket?.close();
      socket = new WebSocket(action.payload.url);
      socket.onmessage = (msg) => {
        const parsed = safeParseEnvelope(safeJson(msg.data));
        if (parsed.success) store.dispatch(wsEvent(parsed.data));
      };
      socket.onopen = () => {
        for (const id of subscriptions) send({ action: 'subscribe', sessionId: id });
      };
      return next(action);
    }

    if (wsDisconnect.match(action)) {
      socket?.close();
      socket = null;
      subscriptions.clear();
      return next(action);
    }

    if (wsSubscribe.match(action)) {
      subscriptions.add(action.payload.sessionId);
      send({ action: 'subscribe', sessionId: action.payload.sessionId });
      return next(action);
    }

    if (wsUnsubscribe.match(action)) {
      subscriptions.delete(action.payload.sessionId);
      send({ action: 'unsubscribe', sessionId: action.payload.sessionId });
      return next(action);
    }

    if (wsEvent.match(action)) {
      foldEvent(action.payload);
      return next(action);
    }

    return next(action);
  };
};

function safeJson(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
