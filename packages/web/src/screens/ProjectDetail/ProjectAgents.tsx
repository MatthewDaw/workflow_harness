import { useParams } from 'react-router-dom';
import type { Agent, Skill } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetAgentsQuery,
  useGetSkillsQuery,
  useEnableProjectAgentMutation,
  useDisableProjectAgentMutation,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Flatten an agent's declared skills, expanding any bundles to their leaf
 * member skills (de-duped), for the "skills this agent brings" display.
 */
function bringsSkills(agent: Agent, byName: Map<string, Skill>): string[] {
  const out = new Set<string>();
  const visit = (name: string) => {
    const s = byName.get(name);
    if (s?.kind === 'bundle') {
      for (const m of s.resolvedMembers ?? s.members) visit(m);
    } else {
      out.add(name);
    }
  };
  for (const s of agent.skills) visit(s);
  return [...out];
}

/**
 * Project Agents sub-tab (collapsed model): enable/disable org-catalog agents
 * for this project. Enabling unions the agent's skills into the project's
 * enabledSkills (server-side); disabling leaves enabledSkills intact.
 */
export function ProjectAgents() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: agentData } = useGetAgentsQuery();
  const { data: skillData } = useGetSkillsQuery();

  const [enableAgent] = useEnableProjectAgentMutation();
  const [disableAgent] = useDisableProjectAgentMutation();

  const agents = agentData ?? [];
  const byName = new Map<string, Skill>((skillData ?? []).map((s) => [s.name, s]));
  const enabled = new Set(project?.enabledAgents ?? []);

  return (
    <div className="hq-pad" data-testid="project-agents">
      <ScreenHeader
        title="Agents"
        subtitle="Enable org agents for this project. Enabling an agent adds its skills to this project."
      />
      {agents.length === 0 && <div className="hq-box text-mut">No agents in the catalog.</div>}
      <div className="grid grid-cols-3 gap-3.5">
        {agents.map((a) => {
          const on = enabled.has(a.name);
          const brings = bringsSkills(a, byName);
          return (
            <div key={a.name} className="hq-box bg-paper" data-testid={`project-agent-${a.name}`}>
              <div className="flex justify-between">
                <b>{a.name}</b>
                <Pill>{a.model}</Pill>
              </div>
              <div className="mt-1.5">
                {brings.map((s) => (
                  <Pill key={s} variant="skill" className="mr-1">
                    {s}
                  </Pill>
                ))}
              </div>
              {!on && brings.length > 0 && (
                <div className="mt-1.5 text-[11px] text-faint" data-testid={`brings-note-${a.name}`}>
                  Enabling adds these skills to this project.
                </div>
              )}
              <div className="mt-2 flex items-center justify-between">
                {on ? (
                  <>
                    <span className="text-[11px] text-faint">enabled</span>
                    <button
                      type="button"
                      className="hq-btn"
                      data-testid={`disable-agent-${a.name}`}
                      onClick={() => disableAgent({ projectId, agentName: a.name })}
                    >
                      Disable
                    </button>
                  </>
                ) : (
                  <button
                    type="button"
                    className="hq-btn hq-btn-pri"
                    data-testid={`enable-agent-${a.name}`}
                    onClick={() => enableAgent({ projectId, agentName: a.name })}
                  >
                    Enable
                  </button>
                )}
              </div>
              {on && (
                <div className="mt-1 text-[11px] text-faint" data-testid={`disable-note-${a.name}`}>
                  Disabling keeps its skills enabled in this project.
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
