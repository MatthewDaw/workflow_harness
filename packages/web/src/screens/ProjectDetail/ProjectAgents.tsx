import { useState } from 'react';
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
import {
  CatalogPicker,
  type CatalogPickerRow,
  type CatalogRef,
} from '../../components/CatalogPicker.js';

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
 * Project Agents sub-tab (collapsed model): the page now shows ONLY the agents
 * already enabled on this project (as cards, keeping the "brings these skills"
 * pills + notes), and the full org catalog moves behind a single "+ Add to
 * project" button that opens the shared `CatalogPicker` modal. The modal stages
 * a selection of agent refs and hands back a {enable, disable} diff on Apply;
 * we fan that diff out to the per-agent mutations. Enabling an agent also unions
 * its skills into the project's enabledSkills (server-side), so there is nothing
 * extra to do client-side beyond the agent toggle itself.
 *
 * CRITICAL: each opt-in mutation does a full read-modify-write of the single
 * project META record, so we apply the diff SEQUENTIALLY (await each before the
 * next) — concurrent writes would clobber one another.
 */
export function ProjectAgents() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: agentData } = useGetAgentsQuery();
  const { data: skillData } = useGetSkillsQuery();

  const [enableAgent] = useEnableProjectAgentMutation();
  const [disableAgent] = useDisableProjectAgentMutation();

  // Modal open + in-flight apply state. `applying` drives the picker's busy UI
  // (Apply button disabled / "Applying…") while we walk the diff sequentially.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const agents = agentData ?? [];
  const byName = new Map<string, Skill>((skillData ?? []).map((s) => [s.name, s]));
  const enabledNames = project?.enabledAgents ?? [];
  const enabledSet = new Set(enabledNames);

  // The agents currently enabled on this project — the only thing the page body
  // renders now (the full catalog lives in the picker).
  const enabledAgents = agents.filter((a) => enabledSet.has(a.name));

  // One picker row per catalog agent. No bundles in the agent catalog, so every
  // row is a plain `{ type: 'agent' }` ref; hint = model, and the description
  // folds in the agent's own description plus the skills it brings so the user
  // can see the side effect of enabling it.
  const rows: CatalogPickerRow[] = agents.map((a) => {
    const brings = bringsSkills(a, byName);
    const desc = [a.description, brings.length > 0 ? `Brings: ${brings.join(', ')}` : '']
      .filter(Boolean)
      .join(' — ');
    return {
      ref: { type: 'agent', name: a.name } as CatalogRef,
      label: a.name,
      hint: a.model,
      description: desc || undefined,
    };
  });

  const initialSelected: CatalogRef[] = enabledNames.map((name) => ({ type: 'agent', name }));

  // Apply the staged diff. Enabling/disabling each agent is a separate project
  // read-modify-write, so we MUST await each mutation before firing the next —
  // never fire them concurrently or they clobber the project META record.
  const onApply = async (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => {
    if (!projectId) return;
    setApplying(true);
    setAddError(null);
    try {
      for (const ref of diff.enable) {
        await enableAgent({ projectId, agentName: ref.name }).unwrap();
      }
      for (const ref of diff.disable) {
        await disableAgent({ projectId, agentName: ref.name }).unwrap();
      }
    } catch {
      // A failed toggle (e.g. the agent isn't in the org catalog) surfaces here
      // instead of failing silently. The picker keeps the modal open on a thrown
      // apply, so the user can retry or cancel.
      setAddError("Couldn't update agents for this project. Please try again.");
      throw new Error('apply-failed');
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="hq-pad" data-testid="project-agents">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="Agents"
          subtitle="Agents enabled for this project. Enabling an agent also adds its skills to this project."
        />
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="add-agent"
          onClick={() => {
            setAddError(null);
            setPickerOpen(true);
          }}
        >
          + Add to project
        </button>
      </div>

      {addError && (
        <div className="mt-2 text-xs text-rose-600" role="alert" data-testid="add-agent-error">
          {addError}
        </div>
      )}

      {enabledAgents.length === 0 ? (
        <div className="hq-box mt-3 text-mut" data-testid="project-agents-empty">
          No agents enabled yet.
        </div>
      ) : (
        <div className="mt-3 grid grid-cols-3 gap-3.5">
          {enabledAgents.map((a) => {
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
                <div className="mt-2 flex items-center justify-between">
                  <span className="text-[11px] text-faint">enabled</span>
                  <button
                    type="button"
                    className="hq-btn"
                    data-testid={`disable-agent-${a.name}`}
                    onClick={() => disableAgent({ projectId, agentName: a.name })}
                  >
                    Disable
                  </button>
                </div>
                <div className="mt-1 text-[11px] text-faint" data-testid={`disable-note-${a.name}`}>
                  Disabling keeps its skills enabled in this project.
                </div>
              </div>
            );
          })}
        </div>
      )}

      <CatalogPicker
        open={pickerOpen}
        title="Add agents to this project"
        rows={rows}
        initialSelected={initialSelected}
        emptyHint="No agents in the catalog."
        applying={applying}
        onClose={() => setPickerOpen(false)}
        onApply={onApply}
      />
    </div>
  );
}
