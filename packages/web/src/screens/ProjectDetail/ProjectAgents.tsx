import { useParams } from 'react-router-dom';
import { useGetAgentsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/** Project Agents sub-tab (U19 scaffold for U24): agents effective for this repo. */
export function ProjectAgents() {
  const { projectId = '' } = useParams();
  const { data } = useGetAgentsQuery({ projectId });
  const agents = data ?? [];

  return (
    <div className="hq-pad" data-testid="project-agents">
      <ScreenHeader
        title="Agents"
        subtitle="Agents effective for this project (org + user + project)."
      />
      {agents.length === 0 && <div className="hq-box text-mut">No agents resolved.</div>}
      <div className="grid grid-cols-3 gap-3.5">
        {agents.map((a) => (
          <div key={a.name} className="hq-box bg-paper">
            <div className="flex justify-between">
              <b>{a.name}</b>
              <Pill>{a.model}</Pill>
            </div>
            <div className="mt-1.5">
              {a.skills.map((s) => (
                <Pill key={s} variant="skill" className="mr-1">
                  {s}
                </Pill>
              ))}
            </div>
            <div className="mt-1.5 text-[11px] text-faint">scope: {a.scope.tier}</div>
          </div>
        ))}
      </div>
    </div>
  );
}
