import { Link, useParams } from 'react-router-dom';
import { useGetProjectQuery, useGetSessionsQuery } from '../../api/baseApi.js';
import { Bar, StatusDot, ScreenHeader } from '../../components/primitives.js';

/** Project Overview sub-tab (U21): PRD goal, progress, and session history. */
export function Overview() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: sessions } = useGetSessionsQuery();
  const projectSessions = (sessions ?? []).filter((s) => s.projectId === projectId);

  return (
    <div className="hq-pad" data-testid="project-overview">
      <ScreenHeader title={project?.name ?? projectId} subtitle={project?.repo} />

      <div className="flex gap-4">
        <div className="hq-box flex-1 bg-paper">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            PRD.md — goal &amp; objectives
          </div>
          <div className="mt-1.5 text-[13px] text-mut">
            {project?.prdGoal ?? 'No PRD goal yet.'}
          </div>
          <div className="mt-2.5 text-[11px] uppercase tracking-wide text-faint">
            Owns these Supporting Outcomes
          </div>
          <div className="mt-1.5 text-[11px] text-faint">
            <Link to="/objectives" className="text-accent">
              ↑ link up to Company Objectives
            </Link>
          </div>
        </div>
        <div className="hq-box flex-1 bg-paper">
          <div className="text-[11px] uppercase tracking-wide text-faint">
            PROGRESS.md — done so far · {project?.progressPct ?? 0}%
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
