import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { SessionProjection } from '@harness/shared';
import { StatusDot } from '../../components/primitives.js';
import { useSendControlMutation } from '../../api/baseApi.js';
import { relativeTime, useNow } from '../../lib/time.js';

/** Sort by most recent activity first (newest `lastEventAt` at the top). */
function sortByRecentActivity(sessions: SessionProjection[]): SessionProjection[] {
  return [...sessions].sort((a, b) => b.lastEventAt - a.lastEventAt);
}

/** Shared sessions table used by the cross-project list and project sub-tab. */
export function SessionsTable({ sessions }: { sessions: SessionProjection[] }) {
  const rows = sortByRecentActivity(sessions);
  // One ticking "now" for every row so the "last activity" labels age in place
  // without each row owning its own interval.
  const now = useNow();
  if (rows.length === 0) {
    return <div className="hq-box text-mut">No sessions.</div>;
  }
  return (
    <table className="w-full border-collapse text-[13px]">
      <thead>
        <tr className="text-[11px] uppercase tracking-wide text-faint">
          <th className="border-b border-line2 p-2 text-left">status</th>
          <th className="border-b border-line2 p-2 text-left">project</th>
          <th className="border-b border-line2 p-2 text-left">agent</th>
          <th className="border-b border-line2 p-2 text-left">name</th>
          <th className="border-b border-line2 p-2 text-left">topic</th>
          <th className="border-b border-line2 p-2 text-left">host</th>
          <th className="border-b border-line2 p-2 text-left">last activity</th>
          <th className="border-b border-line2 p-2" />
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <SessionRow key={s.sessionId} session={s} now={now} />
        ))}
      </tbody>
    </table>
  );
}

/**
 * One session row. Owns the small confirm state for the destructive shut-down
 * control so an accidental click can't terminate a running session; the actual
 * frame (graceful `shutdown` or force `kill`) is sent through the shared control
 * mutation, which the backend authorizes against session ownership.
 */
function SessionRow({ session: s, now }: { session: SessionProjection; now: number }) {
  const [sendControl, { isLoading }] = useSendControlMutation();
  const [confirming, setConfirming] = useState(false);
  const isDone = s.status === 'done';

  const terminate = (action: 'shutdown' | 'kill') => {
    void sendControl({ sessionId: s.sessionId, action });
    setConfirming(false);
  };

  return (
    <tr data-testid={`session-row-${s.sessionId}`}>
      <td className="border-b border-line2 p-2">
        <StatusDot variant={isDone ? 'good' : 'live'} />
        {s.status === 'needs_input' ? (
          <span className="text-warn">needs input</span>
        ) : (
          <span className="text-mut">{s.status}</span>
        )}
      </td>
      <td className="border-b border-line2 p-2">{s.projectId}</td>
      <td className="border-b border-line2 p-2">{s.agent ?? '—'}</td>
      <td
        className="border-b border-line2 p-2 text-mut"
        data-testid={`session-name-${s.sessionId}`}
      >
        {s.name}
      </td>
      <td
        className="max-w-[26ch] border-b border-line2 p-2"
        data-testid={`session-topic-${s.sessionId}`}
      >
        {s.topic ? (
          <>
            <div className="truncate" data-testid={`session-topic-label-${s.sessionId}`}>
              {s.topic}
              {/* The stable slug stays visible (muted) so a user who navigated by
                  slug isn't disoriented when the evolving topic takes the lead. */}
              <span className="ml-1.5 text-faint" data-testid={`session-topic-slug-${s.sessionId}`}>
                {s.name}
              </span>
            </div>
            {s.description && (
              <div
                className="line-clamp-2 text-[11px] text-mut"
                data-testid={`session-topic-desc-${s.sessionId}`}
                title={s.description}
              >
                {s.description}
              </div>
            )}
          </>
        ) : (
          <span className="text-faint" data-testid={`session-topic-untitled-${s.sessionId}`}>
            Untitled
          </span>
        )}
      </td>
      <td className="border-b border-line2 p-2 font-mono text-faint">{s.host}</td>
      <td
        className="border-b border-line2 p-2 text-mut"
        data-testid={`session-activity-${s.sessionId}`}
      >
        {relativeTime(s.lastEventAt, now)}
      </td>
      <td className="border-b border-line2 p-2">
        <div className="flex items-center justify-end gap-1.5">
          <Link to={`/sessions/${s.sessionId}`} className={`hq-btn ${isDone ? '' : 'hq-btn-pri'}`}>
            {s.status === 'needs_input' ? 'reply' : isDone ? 'replay' : 'watch'}
          </Link>
          {!isDone && !confirming && (
            <button
              type="button"
              className="hq-btn"
              data-testid={`session-shutdown-${s.sessionId}`}
              onClick={() => setConfirming(true)}
            >
              shut down
            </button>
          )}
          {!isDone && confirming && (
            <>
              <span className="text-[11px] text-mut">end?</span>
              <button
                type="button"
                className="hq-btn hq-btn-pri"
                disabled={isLoading}
                data-testid={`session-shutdown-confirm-${s.sessionId}`}
                onClick={() => terminate('shutdown')}
              >
                end
              </button>
              <button
                type="button"
                className="hq-btn"
                disabled={isLoading}
                title="Force-kill if it won't exit gracefully"
                data-testid={`session-kill-${s.sessionId}`}
                onClick={() => terminate('kill')}
              >
                force
              </button>
              <button
                type="button"
                className="hq-btn"
                data-testid={`session-shutdown-cancel-${s.sessionId}`}
                onClick={() => setConfirming(false)}
              >
                cancel
              </button>
            </>
          )}
        </div>
      </td>
    </tr>
  );
}
