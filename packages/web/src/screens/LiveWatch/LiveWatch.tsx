import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import type { Envelope, LearningRecord, SessionProjection } from '@harness/shared';
import {
  useGetSessionQuery,
  useGetSessionEventsQuery,
  useGetProjectLearningsQuery,
  useSendControlMutation,
} from '../../api/baseApi.js';
import { wsSubscribe, wsUnsubscribe } from '../../ws/liveActions.js';
import { selectSessionEvents, seedSessionEvents } from '../../app/liveEventsSlice.js';
import type { RootState } from '../../app/store.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Bar, Pill, StatusDot, ScreenHeader } from '../../components/primitives.js';
import { relativeTime, useNow } from '../../lib/time.js';

/** Short HH:MM:SS clock for an event's epoch-ms timestamp. */
function clock(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/**
 * A rendered feed row: a compact header (who/what) plus an optional multi-line
 * body carrying the REAL content — assistant/user text, tool call name+input,
 * tool result output / console logs. The body is rendered monospace + pre-wrap
 * so console output keeps its shape.
 */
interface FeedRow {
  header: string;
  body?: string;
}

/** Map one envelope's event to a feed row with full content. */
function feedRow(env: Envelope): FeedRow | null {
  const e = env.event;
  switch (e.kind) {
    case 'tool.call':
      return { header: `→ ${e.tool}`, body: e.argsSummary || undefined };
    case 'tool.result':
      return { header: `${e.ok ? '✓' : '✗'} ${e.ms}ms`, body: e.summary || undefined };
    case 'user.msg':
      return { header: `▎ you · ${e.tokens} tok`, body: e.text || undefined };
    case 'assistant.msg':
      return { header: `▎ claude · ${e.tokens} tok`, body: e.text || undefined };
    case 'status.change':
      return { header: `● ${e.from} → ${e.to}` };
    case 'session.rename':
      return { header: `✎ renamed → ${e.name}` };
    case 'session.start':
    default:
      return null;
  }
}

/**
 * One row of the within-session topic timeline: a topic segment with the time it
 * opened and a one-line description. A segment opens once; an in-place relabel
 * (judge re-titling the same focus) just updates `label` on the latest row.
 */
interface TopicSegment {
  segmentId: string;
  label: string;
  /** Epoch-ms the segment first appeared (its open time). */
  openedAt: number;
  description?: string;
}

/**
 * Derive the within-session topic timeline (strictly this session — no
 * cross-session grouping). Segments are mined from the session's learnings
 * (each carries the `segmentId` + `topicLabel` it belongs to); the earliest
 * stamp of a segment is its open time and the latest stamp wins the label, so an
 * in-place relabel updates the row rather than appending a new one. The session
 * projection's CURRENT topic is the live latest segment, so it is merged onto the
 * most-recent row (or appended when no learning has landed for it yet).
 */
function topicSegments(
  session: SessionProjection | undefined,
  learnings: LearningRecord[],
): TopicSegment[] {
  const bySegment = new Map<string, TopicSegment>();
  for (const l of learnings) {
    const existing = bySegment.get(l.segmentId);
    if (!existing) {
      bySegment.set(l.segmentId, { segmentId: l.segmentId, label: l.topicLabel, openedAt: l.ts });
      continue;
    }
    if (l.ts < existing.openedAt) existing.openedAt = l.ts; // earliest stamp = open time
    if (l.ts >= existing.openedAt) existing.label = l.topicLabel; // latest stamp wins the label
  }
  const segments = [...bySegment.values()].sort((a, b) => a.openedAt - b.openedAt);

  // Fold the live current topic onto the timeline. The projection has no
  // segmentId, so the current topic is the latest segment: relabel the last row
  // when present, else append a synthetic latest row carrying the description.
  if (session?.topic) {
    const last = segments[segments.length - 1];
    const openedAt = session.summaryUpdatedAt ?? session.lastEventAt;
    if (last) {
      last.label = session.topic;
      last.description = session.description;
    } else {
      segments.push({
        segmentId: 'current',
        label: session.topic,
        openedAt,
        description: session.description,
      });
    }
  }
  return segments;
}

/** The within-session topic timeline (chronological, oldest first). */
function TopicTimeline({
  segments,
  now,
  isLoading,
}: {
  segments: TopicSegment[];
  now: number;
  isLoading: boolean;
}) {
  return (
    <div className="hq-box bg-paper" data-testid="topic-timeline">
      <div className="text-[11px] uppercase tracking-wide text-faint">Topic timeline</div>
      {isLoading ? (
        <div className="mt-1.5 text-xs text-mut" data-testid="topic-timeline-loading">
          Loading topics…
        </div>
      ) : segments.length === 0 ? (
        <div className="mt-1.5 text-xs text-faint" data-testid="topic-timeline-empty">
          No topic segments yet
        </div>
      ) : (
        <ol className="mt-1.5 flex flex-col gap-2">
          {segments.map((seg) => (
            <li
              key={seg.segmentId}
              className="border-l-2 border-line2 pl-2.5"
              data-testid="topic-segment"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-xs font-semibold" data-testid="topic-segment-label">
                  {seg.label}
                </span>
                <span className="shrink-0 text-[11px] text-faint">
                  {relativeTime(seg.openedAt, now)}
                </span>
              </div>
              {seg.description && (
                <div
                  className="truncate text-[11px] text-mut"
                  data-testid="topic-segment-desc"
                  title={seg.description}
                >
                  {seg.description}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/**
 * One labeled learnings section (impl or doc). Both are always rendered — the doc
 * section deliberately shows a zero-state rather than hiding when empty.
 */
function LearningSection({
  title,
  learnings,
  emptyLabel,
  isLoading,
  testid,
}: {
  title: string;
  learnings: LearningRecord[];
  emptyLabel: string;
  isLoading: boolean;
  testid: string;
}) {
  const rows = [...learnings].sort((a, b) => a.ts - b.ts);
  return (
    <div className="hq-box bg-paper" data-testid={testid}>
      <div className="text-[11px] uppercase tracking-wide text-faint">{title}</div>
      {isLoading ? (
        <div className="mt-1.5 text-xs text-mut" data-testid={`${testid}-loading`}>
          Loading…
        </div>
      ) : rows.length === 0 ? (
        <div className="mt-1.5 text-xs text-faint" data-testid={`${testid}-empty`}>
          {emptyLabel}
        </div>
      ) : (
        <ul className="mt-1.5 flex flex-col gap-2">
          {rows.map((l) => (
            <li
              key={`${l.sessionId}#${l.turnId}`}
              className="text-xs"
              data-testid={`${testid}-row`}
            >
              <div className="text-faint">{l.topicLabel}</div>
              <div className="text-mut">{l.text}</div>
              {l.docRef && <div className="font-mono text-[11px] text-faint">{l.docRef}</div>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
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
  const {
    data: session,
    isLoading,
    isFetching: sessionFetching,
    refetch: refetchSession,
  } = useGetSessionQuery(sessionId, { skip: !sessionId });
  // Backfill the stored event history on open so the feed shows the full
  // conversation immediately instead of "waiting for activity…". The live WS
  // stream (below) appends everything thereafter; the slice merges both by seq.
  const {
    data: backfill,
    isFetching: eventsFetching,
    refetch: refetchEvents,
  } = useGetSessionEventsQuery({ id: sessionId, limit: 300 }, { skip: !sessionId });

  // Manual re-pull for when the live WS lags or drops (e.g. a slow-syncing
  // session): re-read both the projection and the stored event page. The seed
  // effect below folds the fresh events in by seq, so this never duplicates rows.
  const refreshing = sessionFetching || eventsFetching;
  const refresh = () => {
    if (!sessionId) return;
    void refetchSession();
    void refetchEvents();
  };
  const [sendControl] = useSendControlMutation();
  const [message, setMessage] = useState('');
  const [sent, setSent] = useState<string | null>(null);
  const [steerError, setSteerError] = useState<string | null>(null);

  // Run a control action and report the REAL outcome. The control POST can fail
  // (502 "daemon offline" when the owning daemon isn't connected, or 500 when the
  // backend can't reach the WS management API) — surface that instead of always
  // claiming success, which previously made a no-op send look like it had worked.
  const runControl = async (action: 'inject' | 'pause' | 'interrupt', text?: string) => {
    setSteerError(null);
    try {
      await sendControl({ sessionId, action, text }).unwrap();
      return true;
    } catch (err) {
      const status = (err as { status?: number | string })?.status;
      setSteerError(
        status === 502
          ? 'daemon offline — the session’s claude+ isn’t connected'
          : `couldn’t reach the session (${status ?? 'error'})`,
      );
      return false;
    }
  };
  // Within-session topic/learnings surfaces. Learnings are keyed by PROJECT, so
  // we read the project's corpus and narrow to THIS session below (strictly
  // within-session; no cross-session grouping). The query is skipped until the
  // projection resolves the owning projectId.
  const projectId = session?.projectId;
  const { data: projectLearnings = [], isLoading: learningsLoading } = useGetProjectLearningsQuery(
    { projectId: projectId ?? '' },
    { skip: !projectId },
  );
  const sessionLearnings = projectLearnings.filter((l) => l.sessionId === sessionId);
  const implLearnings = sessionLearnings.filter((l) => l.stream === 'impl');
  const docLearnings = sessionLearnings.filter((l) => l.stream === 'doc');
  const segments = topicSegments(session, sessionLearnings);
  const now = useNow();

  const events = useSelector((state: RootState) => selectSessionEvents(state, sessionId));

  // Seed the backfilled page into the live slice once it arrives. Re-seeding is
  // safe: the slice dedupes by seq, so live events already received are kept.
  useEffect(() => {
    if (sessionId && backfill && backfill.length > 0) {
      dispatch(seedSessionEvents({ sessionId, events: backfill }));
    }
  }, [dispatch, sessionId, backfill]);

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
              <div className="flex items-center gap-1.5">
                <Pill variant={session.status === 'done' ? 'good' : 'live'}>
                  {session.status === 'done' ? 'replay' : 'streaming'}
                </Pill>
                <button
                  type="button"
                  className="hq-btn"
                  onClick={refresh}
                  disabled={refreshing}
                  aria-label="Refresh session"
                  data-testid="live-refresh"
                >
                  {refreshing ? '↻ syncing…' : '↻ refresh'}
                </button>
              </div>
            </div>
            <div
              ref={feedRef}
              className="max-h-[420px] min-h-[180px] overflow-y-auto rounded-md border border-line2 bg-[#fafafa] p-3 font-mono text-xs"
              data-testid="live-transcript"
            >
              <div data-testid="live-session-name">{session.name}</div>
              <div className="mt-2 text-faint">
                tokens {session.tokens} · status{' '}
                <span data-testid="live-status">{session.status}</span>
              </div>
              <div className="mt-2 whitespace-pre-wrap break-words">
                {events.length === 0 && <div className="text-faint">waiting for activity…</div>}
                {events.map((env) => {
                  const row = feedRow(env);
                  if (row === null) return null;
                  return (
                    <div key={env.seq} data-testid="live-event-row" className="mb-1">
                      <div>
                        <span className="text-faint">{clock(env.ts)}</span> {row.header}
                      </div>
                      {row.body && (
                        <div
                          className="ml-[3ch] whitespace-pre-wrap break-words text-mut"
                          data-testid="live-event-body"
                        >
                          {row.body}
                        </div>
                      )}
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
                    void runControl('inject', text).then((okSend) => {
                      if (okSend) {
                        setSent(text);
                        setMessage('');
                      }
                    });
                  }}
                >
                  send
                </button>
                <button
                  type="button"
                  className="hq-btn"
                  disabled={!canSteer}
                  onClick={() => {
                    void runControl('pause');
                  }}
                >
                  ⏸ pause
                </button>
                <button
                  type="button"
                  className="hq-btn"
                  disabled={!canSteer}
                  onClick={() => {
                    void runControl('interrupt');
                  }}
                >
                  ⤓ interrupt
                </button>
              </div>
              {steerError && (
                <div className="mt-2 text-[11px] text-live" role="alert">
                  {steerError}
                </div>
              )}
              {sent && !steerError && (
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
      {session && (
        <div className="mt-3.5 flex flex-col gap-3" data-testid="topic-focus">
          <TopicTimeline segments={segments} now={now} isLoading={learningsLoading} />
          <LearningSection
            title="Implementation learnings"
            learnings={implLearnings}
            emptyLabel="No corrections logged this session"
            isLoading={learningsLoading}
            testid="impl-learnings"
          />
          <LearningSection
            title="Doc-writing learnings"
            learnings={docLearnings}
            emptyLabel="No doc loaded — doc learnings appear when a correction contradicts a loaded doc"
            isLoading={learningsLoading}
            testid="doc-learnings"
          />
        </div>
      )}
    </div>
  );
}
