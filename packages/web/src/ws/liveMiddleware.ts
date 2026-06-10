import type { Middleware } from '@reduxjs/toolkit';
import { safeParseEnvelope, type Envelope, type SessionProjection } from '@harness/shared';
import { baseApi, SESSION_LIST_ARGS } from '../api/baseApi.js';
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
  // Bootstrap a projection from a session.start we have no prior state for, so a
  // session that goes live mid-session (after the lists were fetched) becomes a
  // real, countable row instead of being dropped. Other event kinds still need a
  // prior projection — they carry no identity to synthesize one from.
  if (!prev) {
    if (env.event.kind === 'session.start') {
      return {
        sessionId: env.event.sessionId,
        projectId: env.event.projectId,
        name: env.event.name,
        host: env.host,
        agent: env.event.agent,
        status: 'active',
        tokens: 0,
        startedAt: env.ts,
        lastEventAt: env.ts,
        maxSeq: env.seq,
      };
    }
    return prev;
  }
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

  const isLiveStatus = (status: string): boolean => status === 'active' || status === 'needs_input';

  const foldEvent = (env: Envelope) => {
    const sessionId = env.event.sessionId;
    // Update the single-session cache entry.
    dispatch(
      baseApi.util.updateQueryData('getSession', sessionId, (draft) => {
        const updated = applyEventToProjection(draft, env);
        if (updated) Object.assign(draft, updated);
      }),
    );
    // A full projection we can ADD to the {live:true} list if this session is
    // newly live and not yet present there (otherwise the header "N live" count
    // under-reports it). Prefer the single-session cache (richest), else any
    // cached list row, else a bootstrap from a session.start event.
    const state = store.getState() as never;
    const knownProjection: SessionProjection | undefined =
      (baseApi.endpoints.getSession.select(sessionId)(state).data as
        | SessionProjection
        | undefined) ??
      findInLists(state, sessionId) ??
      applyEventToProjection(undefined, env);

    // Update the matching row in any cached sessions list.
    for (const live of SESSION_LIST_ARGS) {
      dispatch(
        baseApi.util.updateQueryData('getSessions', live, (draft) => {
          const idx = draft.findIndex((s) => s.sessionId === sessionId);
          if (idx !== -1) {
            const updated = applyEventToProjection(draft[idx], env);
            if (updated) draft[idx] = updated;
            return;
          }
          // Session not yet in this cached list. For the {live:true} list, a
          // session that becomes live AFTER the list was fetched must be ADDED,
          // or the header "N live" count under-reports it.
          if (
            live &&
            live.live === true &&
            knownProjection &&
            isLiveStatus(knownProjection.status)
          ) {
            draft.push(knownProjection);
          }
        }),
      );
    }
  };

  return (next) => (action) => {
    if (wsConnect.match(action)) {
      socket?.close();
      // The WS authorizer reads the device/id token off the handshake query
      // string (`?token=...`) — browsers cannot set headers on a WS upgrade.
      socket = new WebSocket(withToken(action.payload.url, action.payload.token));
      socket.onmessage = (msg) => {
        // The backend wraps live events as { type: 'event', envelope } (ws/event.ts);
        // unwrap that, while still tolerating a bare envelope for safety.
        const parsed = safeParseEnvelope(unwrapEventFrame(safeJson(msg.data)));
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

/** Find a session's projection in any cached getSessions list. */
function findInLists(state: never, sessionId: string): SessionProjection | undefined {
  for (const arg of SESSION_LIST_ARGS) {
    const list = baseApi.endpoints.getSessions.select(arg)(state).data as
      | SessionProjection[]
      | undefined;
    const hit = list?.find((s) => s.sessionId === sessionId);
    if (hit) return hit;
  }
  return undefined;
}

function safeJson(raw: unknown): unknown {
  if (typeof raw !== 'string') return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}

/**
 * The backend's ws/event.ts posts `{ type: 'event', envelope }` to clients.
 * Pull the inner envelope out of that frame; pass anything else (e.g. a bare
 * envelope) through unchanged so the parser can still validate it.
 */
function unwrapEventFrame(msg: unknown): unknown {
  if (msg && typeof msg === 'object' && (msg as { type?: unknown }).type === 'event') {
    return (msg as { envelope?: unknown }).envelope;
  }
  return msg;
}

/** Append the auth token to the WS URL as `?token=` (or `&token=`) for the authorizer. */
function withToken(url: string, token?: string | null): string {
  if (!token) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(token)}`;
}
