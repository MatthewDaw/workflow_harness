import { useEffect, useRef } from 'react';
import { useParams } from 'react-router-dom';
import { useDispatch, useSelector } from 'react-redux';
import {
  useGetSessionQuery,
  useGetSessionEventsQuery,
  useGetProjectLearningsQuery,
} from '../../api/baseApi.js';
import { wsSubscribe, wsUnsubscribe } from '../../ws/liveActions.js';
import { selectSessionEvents, seedSessionEvents } from '../../app/liveEventsSlice.js';
import type { RootState } from '../../app/store.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Bar, Pill, StatusDot, ScreenHeader } from '../../components/primitives.js';
import { useNow } from '../../lib/time.js';
import { clock, feedRow } from './feed.js';
import { TopicTimeline, LearningSection, topicSegments } from './TopicTimeline.js';
import { SteerPanel } from './SteerPanel.js';

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
            <SteerPanel sessionId={sessionId} canSteer={canSteer} />
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
