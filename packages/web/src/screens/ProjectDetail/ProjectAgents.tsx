import { useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import type { Agent, Skill } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetAgentsQuery,
  useGetSkillsQuery,
  useEnableProjectAgentMutation,
  useDisableProjectAgentMutation,
  useEnableProjectAgentBundleMutation,
  useDisableProjectAgentBundleMutation,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { bundleMemberNames } from '../../lib/bundles.js';
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
 * Project Agents sub-tab (collapsed model): the page shows the agents already
 * enabled on this project (as cards, keeping the "brings these skills" pills +
 * notes), and the full org catalog — individual agents AND agent bundles — moves
 * behind a single "+ Add to project" button that opens the shared `CatalogPicker`.
 * The modal stages a selection and hands back a {enable, disable} diff on Apply;
 * we fan that diff out to the per-agent and per-bundle mutations. Enabling an
 * agent (or a bundle of agents) also unions their skills into the project's
 * enabledSkills server-side, so there is nothing extra to do client-side.
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
  const [enableBundle] = useEnableProjectAgentBundleMutation();
  const [disableBundle] = useDisableProjectAgentBundleMutation();

  // Modal open + in-flight apply state. `applying` drives the picker's busy UI
  // (Apply button disabled / "Applying…") while we walk the diff sequentially.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const agents = agentData ?? [];
  const byName = new Map<string, Skill>((skillData ?? []).map((s) => [s.name, s]));
  const enabledNames = project?.enabledAgents ?? [];
  const enabledSet = new Set(enabledNames);
  const enabledBundles = project?.enabledAgentBundles ?? [];

  // The agents currently enabled on this project — the only thing the page body
  // renders now (the full catalog lives in the picker). Bundles never appear here
  // (they aren't in enabledAgents); their member agents do.
  const enabledAgents = agents.filter((a) => a.kind !== 'bundle' && enabledSet.has(a.name));

  // Leaf member names of every catalog agent bundle — a plain agent that belongs
  // to some bundle is only reachable via that bundle in the picker.
  const memberNames = useMemo(() => bundleMemberNames(agents), [agents]);

  // The picker rows: one bundle row per catalog agent bundle (members mapped to
  // agent refs), plus one agent row per standalone agent that is NOT a member of
  // any bundle. hint = model; description folds in the agent's own description
  // plus the skills it brings so the side effect of enabling is visible.
  const rows: CatalogPickerRow[] = useMemo(() => {
    const agentRow = (a: Agent): CatalogPickerRow => {
      const brings = bringsSkills(a, byName);
      const desc = [a.description, brings.length > 0 ? `Brings: ${brings.join(', ')}` : '']
        .filter(Boolean)
        .join(' — ');
      return {
        ref: { type: 'agent', name: a.name },
        label: a.name,
        hint: a.model,
        description: desc || undefined,
      };
    };
    const out: CatalogPickerRow[] = [];
    for (const a of agents) {
      if (a.kind !== 'bundle') continue;
      const members = a.resolvedMembers ?? a.members;
      out.push({
        ref: { type: 'agent-bundle', name: a.name },
        label: a.name,
        description: a.description || undefined,
        hint: 'bundle',
        members: members.map((m) => {
          const member = agents.find((x) => x.name === m);
          return member ? agentRow(member) : { ref: { type: 'agent', name: m }, label: m };
        }),
      });
    }
    for (const a of agents) {
      if (a.kind === 'bundle') continue;
      if (memberNames.has(a.name)) continue;
      out.push(agentRow(a));
    }
    return out;
  }, [agents, byName, memberNames]);

  // The refs ON when the modal opens: each enabled bundle as an agent-bundle ref,
  // plus each enabled agent NOT already covered by an enabled bundle as an agent ref.
  const initialSelected: CatalogRef[] = useMemo(() => {
    const covered = new Set<string>();
    const refs: CatalogRef[] = [];
    for (const name of enabledBundles) {
      refs.push({ type: 'agent-bundle', name });
      const bundle = agents.find((a) => a.kind === 'bundle' && a.name === name);
      if (bundle) for (const m of bundle.resolvedMembers ?? bundle.members) covered.add(m);
    }
    for (const name of enabledNames) {
      if (covered.has(name)) continue;
      refs.push({ type: 'agent', name });
    }
    return refs;
    // enabled arrays are fresh each render; key off contents.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabledNames.join('|'), enabledBundles.join('|'), agents]);

  // Apply the staged diff SEQUENTIALLY (await each) — every mutation read-modify-
  // writes the whole project META record. On ENABLE process bundles before agents;
  // on DISABLE process agents before bundles (mirrors ProjectSkills).
  const onApply = async (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => {
    if (!projectId) return;
    setApplying(true);
    setAddError(null);
    try {
      const enableBundles = diff.enable.filter((r) => r.type === 'agent-bundle');
      const enableAgents = diff.enable.filter((r) => r.type === 'agent');
      const disableAgents = diff.disable.filter((r) => r.type === 'agent');
      const disableBundles = diff.disable.filter((r) => r.type === 'agent-bundle');

      for (const ref of enableBundles) {
        await enableBundle({ projectId, bundleName: ref.name }).unwrap();
      }
      for (const ref of enableAgents) {
        await enableAgent({ projectId, agentName: ref.name }).unwrap();
      }
      for (const ref of disableAgents) {
        await disableAgent({ projectId, agentName: ref.name }).unwrap();
      }
      for (const ref of disableBundles) {
        await disableBundle({ projectId, bundleName: ref.name }).unwrap();
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
