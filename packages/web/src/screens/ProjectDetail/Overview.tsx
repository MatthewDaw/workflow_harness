import { Link, useParams } from 'react-router-dom';
import {
  useGetProjectQuery,
  useGetProjectRequirementsQuery,
  useGetSessionsQuery,
} from '../../api/baseApi.js';
import { Bar, StatusDot, ScreenHeader } from '../../components/primitives.js';

/** First non-empty line of the requirements markdown, stripped of `#` markers. */
function summarize(markdown: string): string {
  for (const raw of markdown.split('\n')) {
    const line = raw.replace(/^#+\s*/, '').trim();
    if (line) return line;
  }
  return '';
}

/**
 * Project Overview sub-tab (U21/U12): a requirements summary plus the
 * GitHub-sourced completion, and the session history.
 */
export function Overview() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: requirements } = useGetProjectRequirementsQuery(projectId, { skip: !projectId });
  const { data: sessions } = useGetSessionsQuery();
  const projectSessions = (sessions ?? []).filter((s) => s.projectId === projectId);
  const summary = summarize(requirements?.markdown ?? '');

  return (
    <div className="hq-pad" data-testid="project-overview">
      <ScreenHeader title={project?.name ?? projectId} subtitle={project?.repo} />

      <div className="flex gap-4">
        <div className="hq-box flex-1 bg-paper">
          <div className="text-[11px] uppercase tracking-wide text-faint">Requirements</div>
          <div className="mt-1.5 text-[13px] text-mut">
            {summary || 'No requirements yet.'}
          </div>
          <div className="mt-2.5 flex gap-3 text-[11px]">
            <Link to="requirements" className="text-accent">
              Project Requirements →
            </Link>
            <Link to="detailed-requirements" className="text-accent">
              Detailed Requirements →
            </Link>
          </div>
        </div>
        <div className="hq-box flex-1 bg-paper">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            Completion (from GitHub) · {project?.progressPct ?? 0}%
          </div>
          <div className="my-2">
            <Bar pct={project?.progressPct ?? 0} />
          </div>
        </div>
      </div>

      <div className="hq-box mt-3.5 bg-paper">
        <b>Sessions</b>
        <table className="mt-1.5 w-full border-collapse text-[13px]">
          <thead>
            <tr className="text-[11px] uppercase tracking-wide text-faint">
              <th className="border-b border-line2 p-2 text-left">status</th>
              <th className="border-b border-line2 p-2 text-left">agent</th>
              <th className="border-b border-line2 p-2 text-left">doing</th>
              <th className="border-b border-line2 p-2 text-left">tokens / $</th>
              <th className="border-b border-line2 p-2" />
            </tr>
          </thead>
          <tbody>
            {projectSessions.length === 0 && (
              <tr>
                <td className="p-2 text-mut" colSpan={5}>
                  No sessions yet.
                </td>
              </tr>
            )}
            {projectSessions.map((s) => (
              <tr key={s.sessionId}>
                <td className="border-b border-line2 p-2">
                  <StatusDot variant={s.status === 'done' ? 'good' : 'live'} />
                  {s.status}
                </td>
                <td className="border-b border-line2 p-2">{s.agent ?? '—'}</td>
                <td className="border-b border-line2 p-2 text-mut">{s.name}</td>
                <td className="border-b border-line2 p-2">
                  {s.tokens} · ${s.costUsd.toFixed(2)}
                </td>
                <td className="border-b border-line2 p-2">
                  <Link to={`/sessions/${s.sessionId}`} className="hq-btn">
                    {s.status === 'done' ? 'replay' : 'watch'}
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
