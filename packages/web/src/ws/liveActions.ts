import { createAction } from '@reduxjs/toolkit';
import type { Envelope } from '@harness/shared';

/**
 * Live-WS action contract (U19). Components dispatch `wsSubscribe`/`wsUnsubscribe`
 * to follow a session; the live middleware opens the socket and, on each inbound
 * event, dispatches `wsEvent` — which the middleware folds into the RTK Query
 * session cache so subscribers update without refetching.
 */
export const wsConnect = createAction<{ url: string }>('ws/connect');
export const wsDisconnect = createAction('ws/disconnect');
export const wsSubscribe = createAction<{ sessionId: string }>('ws/subscribe');
export const wsUnsubscribe = createAction<{ sessionId: string }>('ws/unsubscribe');

/** A live event arriving from the socket (or injected by a test). */
export const wsEvent = createAction<Envelope>('ws/event');
