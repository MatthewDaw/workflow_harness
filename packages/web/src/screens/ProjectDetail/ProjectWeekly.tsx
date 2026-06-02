import { useParams } from 'react-router-dom';
import { useGetWeeklyQuery } from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Project Weekly sub-tab (U19 scaffold for U25): the latest agent-authored
 * weekly update with Done / Plan subsections.
 */
export function ProjectWeekly() {
  const { projectId = '' } = useParams();
  const { data, isLoading } = useGetWeeklyQuery(projectId, { skip: !projectId });
  const latest = (data ?? [])[0];

  return (
    <div className="hq-pad" data-testid="project-weekly">
      <ScreenHeader
        title="Weekly Update"
        subtitle="Authored with a Claude agent — Done (from git) + Plan (validated)."
      />
      <div className="mb-2.5 flex items-center justify-between">
        <div className="text-mut">
          {latest ? `Week ${latest.isoWeek}` : 'No weekly update yet'}{' '}
          {latest?.validated && <Pill variant="good">plan validated</Pill>}
        </div>
        <button type="button" className="hq-btn hq-btn-pri">
          ▶ Draft with agent
        </button>
      </div>
      {isLoading && <div className="text-mut">Loading…</div>}
      {latest && (
        <div className="flex gap-3.5">
          <div className="hq-box flex-1 bg-paper">
            <b>Done last week</b>
            <div className="my-1.5">
              <Bar pct={75} color="#3f7d4e" />
            </div>
            <ul className="text-[12.5px] text-mut">
              {latest.done.map((d, i) => (
                <li key={i}>✓ {d.text}</li>
              ))}
            </ul>
          </div>
          <div className="hq-box flex-1 border-l-[3px] border-l-accent bg-paper">
            <b>Plan next week</b>
            <ul className="mt-1.5 text-[12.5px] text-mut">
              {latest.plan.map((p, i) => (
                <li key={i}>
                  {i + 1}. {p.text}
                </li>
              ))}
            </ul>
          </div>
        </div>
      )}
    </div>
  );
}
