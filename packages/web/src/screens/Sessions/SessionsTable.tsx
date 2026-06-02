import { Link } from 'react-router-dom';
import type { SessionProjection } from '@harness/shared';
import { StatusDot } from '../../components/primitives.js';

/** Sort live/needs_input first, then by most recent activity. */
function sortLiveFirst(sessions: SessionProjection[]): SessionProjection[] {
  const rank: Record<string, number> = { needs_input: 0, active: 1, idle: 2, done: 3 };
  return [...sessions].sort((a, b) => {
    const r = (rank[a.status] ?? 9) - (rank[b.status] ?? 9);
    return r !== 0 ? r : b.lastEventAt - a.lastEventAt;
  });
}

/** Shared sessions table used by the cross-project list and project sub-tab. */
export function SessionsTable({ sessions }: { sessions: SessionProjection[] }) {
  const rows = sortLiveFirst(sessions);
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
          <th className="border-b border-line2 p-2 text-left">summary</th>
          <th className="border-b border-line2 p-2 text-left">host</th>
          <th className="border-b border-line2 p-2 text-left">$</th>
          <th className="border-b border-line2 p-2" />
        </tr>
      </thead>
      <tbody>
        {rows.map((s) => (
          <tr key={s.sessionId} data-testid={`session-row-${s.sessionId}`}>
            <td className="border-b border-line2 p-2">
              <StatusDot variant={s.status === 'done' ? 'good' : 'live'} />
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
            <td className="border-b border-line2 p-2 font-mono text-faint">{s.host}</td>
            <td className="border-b border-line2 p-2">{s.costUsd.toFixed(2)}</td>
            <td className="border-b border-line2 p-2">
              <Link
                to={`/sessions/${s.sessionId}`}
                className={`hq-btn ${s.status === 'done' ? '' : 'hq-btn-pri'}`}
              >
                {s.status === 'needs_input' ? 'reply' : s.status === 'done' ? 'replay' : 'watch'}
              </Link>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
