import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import type { Envelope } from '@harness/shared';
import { useGetSessionQuery, useSendControlMutation } from '../../api/baseApi.js';
import { wsSubscribe, wsUnsubscribe } from '../../ws/liveActions.js';
import { selectSessionEvents } from '../../app/liveEventsSlice.js';
import type { RootState } from '../../app/store.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Bar, Pill, StatusDot, ScreenHeader } from '../../components/primitives.js';

/** Short HH:MM:SS clock for an event's epoch-ms timestamp. */
function clock(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** Render one envelope's event as a compact one-line activity string. */
function activityLine(env: Envelope): string | null {
  const e = env.event;
  switch (e.kind) {
    case 'tool.call':
      return `→ ${e.tool}  ${e.argsSummary}`.trimEnd();
    case 'tool.result':
      return `${e.ok ? '✓' : '✗'} ${e.ms}ms  ${e.summary}`.trimEnd();
    case 'user.msg':
      return `▎ you · ${e.tokens} tok`;
    case 'assistant.msg':
      return `▎ claude · ${e.tokens} tok`;
    case 'cost.tick':
      return `$ +${e.deltaUsd} (total ${e.totalUsd})`;
    case 'status.change':
      return `● ${e.from} → ${e.to}`;
    case 'session.rename':
      return `✎ renamed → ${e.name}`;
    case 'session.start':
    default:
      return null;
  }
}

/**
 * Watch & steer a live session (U23). Subscribes over the live-WS on mount so
 * the session projection updates in place as events arrive (folded into the RTK
 * Query cache by the live middleware). The steer panel posts control frames
 * through the same gateway the terminal uses.
 */
export function LiveWatch() {
  const { sessionId = '' } = useParams();
  const dispatch = useDispatch();
  const { user } = useAuth();
  const { data: session, isLoading } = useGetSessionQuery(sessionId, { skip: !sessionId });
  // Backfill the stored event history so the feed shows real content on open,
  // not "waiting for activity…". The live WS stream takes over from here.
  const { data: backfill } = useGetSessionEventsQuery(
    { id: sessionId, limit: 300 },
    { skip: !sessionId },
  );
  const [sendControl] = useSendControlMutation();
  const [message, setMessage] = useState('');
  const [sent, setSent] = useState<string | null>(null);
  const events = useSelector((state: RootState) => selectSessionEvents(state, sessionId));

  // Auto-scroll the transcript to the newest event as the feed grows.
  const feedRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = feedRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [events.length]);

  useEffect(() => {
    if (!sessionId) return;
    dispatch(wsSubscribe({ sessionId }));
    return () => {
      dispatch(wsUnsubscribe({ sessionId }));
    };
  }, [dispatch, sessionId]);

  // The control gateway authorizes that the requester owns the session (the
  // server returns 403 otherwise). When the projection carries an owner that
  // isn't the current user, disable the steer controls up front.
  const canSteer = !session?.ownerUserId || session.ownerUserId === user?.userId;

  return (
    <div className="hq-pad" data-testid="livewatch-screen">
      <ScreenHeader title="Watch & steer a live session" subtitle={`#${sessionId}`} />
      {isLoading && <div className="text-mut">Loading session…</div>}
      {!isLoading && !session && (
        <div className="hq-box text-mut">Session not found or not accessible.</div>
      )}
      {session && (
        <div className="flex gap-3.5">
          <div className="hq-box flex-[2] bg-paper">
            <div className="mb-1.5 flex items-center justify-between">
              <div>
                <StatusDot variant={session.status === 'done' ? 'good' : 'live'} />
                <b>{session.agent ?? 'session'}</b>{' '}
                <span className="text-faint">
                  · {session.projectId} · #{session.sessionId}
                </span>
              </div>
              <Pill variant={session.status === 'done' ? 'good' : 'live'}>
                {session.status === 'done' ? 'replay' : 'streaming'}
              </Pill>
            </div>
            <div
              ref={feedRef}
              className="max-h-[420px] min-h-[180px] overflow-y-auto rounded-md border border-line2 bg-[#fafafa] p-3 font-mono text-xs"
              data-testid="live-transcript"
            >
              <div data-testid="live-session-name">{session.name}</div>
              <div className="mt-2 text-faint">
                tokens {session.tokens} · ${session.costUsd.toFixed(2)} · status{' '}
                <span data-testid="live-status">{session.status}</span>
              </div>
              <div className="mt-2 whitespace-pre-wrap break-words">
                {events.length === 0 && <div className="text-faint">waiting for activity…</div>}
                {events.map((env) => {
                  const line = activityLine(env);
                  if (line === null) return null;
                  return (
                    <div key={env.seq} data-testid="live-event-row">
                      <span className="text-faint">{clock(env.ts)}</span> {line}
                    </div>
                  );
                })}
                {events.length > 0 &&
                  (session.status === 'active' || session.status === 'needs_input') && (
                    <div className="text-faint">▌ streaming…</div>
                  )}
              </div>
            </div>
          </div>
          <div className="flex-1">
            <div className="hq-box mb-3 bg-paper">
              <div className="text-[11px] uppercase tracking-wide text-faint">Steer</div>
              <textarea
                aria-label="Inject a message into the live session"
                value={message}
                onChange={(e) => setMessage(e.target.value)}
                placeholder="type a message to inject into the live session…"
                disabled={!canSteer}
                className="my-2 min-h-[54px] w-full rounded-md border border-line p-2 text-xs disabled:opacity-50"
              />
              <div className="flex gap-1.5">
                <button
                  type="button"
                  className="hq-btn hq-btn-pri"
                  disabled={!canSteer || message.trim() === ''}
                  onClick={() => {
                    const text = message;
                    void sendControl({ sessionId, action: 'inject', text });
                    setSent(text);
                    setMessage('');
                  }}
                >
                  send
                </button>
                <button
                  type="button"
                  className="hq-btn"
                  disabled={!canSteer}
                  onClick={() => {
                    void sendControl({ sessionId, action: 'pause' });
                  }}
                >
                  ⏸ pause
                </button>
                <button
                  type="button"
                  className="hq-btn"
                  disabled={!canSteer}
                  onClick={() => {
                    void sendControl({ sessionId, action: 'interrupt' });
                  }}
                >
                  ⤓ interrupt
                </button>
              </div>
              {sent && (
                <div className="mt-2 text-[11px] text-good" role="status">
                  injected: {sent}
                </div>
              )}
            </div>
            <div className="hq-box bg-paper">
              <div className="text-[11px] uppercase tracking-wide text-faint">Session</div>
              <div className="mt-1.5 text-xs text-mut">
                host <span className="font-mono">{session.host}</span>
              </div>
              <div className="my-2">
                <Bar pct={Math.min(100, session.tokens / 1000)} />
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
