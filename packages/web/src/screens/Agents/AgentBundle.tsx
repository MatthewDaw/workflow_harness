import { useNavigate, useParams } from 'react-router-dom';
import {
  useGetAgentsQuery,
  useAddAgentBundleMemberMutation,
  useRemoveAgentBundleMemberMutation,
  useDissolveAgentBundleMutation,
} from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SkillCombobox } from '../../components/SkillCombobox.js';
import { DissolveControl } from '../../components/DissolveControl.js';
import { AgentCard } from './Agents.js';

/**
 * Agent bundle detail (mirrors SkillBundle): a bundle is an agent made of agents.
 * Add/remove members, or dissolve the whole bundle. Destructive ops surface a
 * blast-radius count (how many members are affected) before firing.
 */
export function AgentBundle() {
  const { name: bundleName = '' } = useParams();
  const navigate = useNavigate();
  const { data } = useGetAgentsQuery();
  const agents = data ?? [];
  const bundle = agents.find((a) => a.name === bundleName && a.kind === 'bundle');

  const [addMember] = useAddAgentBundleMemberMutation();
  const [removeMember] = useRemoveAgentBundleMemberMutation();
  const [dissolve] = useDissolveAgentBundleMutation();

  // Candidate members: any agent not already in the bundle and not the bundle itself.
  const candidates = agents.filter(
    (a) => a.name !== bundleName && !(bundle?.members ?? []).includes(a.name),
  );

  const onAdd = (member: string) => {
    if (!bundle || !member) return;
    addMember({ name: bundle.name, member });
  };

  const onRemove = (member: string) => {
    if (!bundle) return;
    removeMember({ name: bundle.name, member });
  };

  const onDissolve = () => {
    if (!bundle) return;
    dissolve({ name: bundle.name });
    navigate('/agents');
  };

  return (
    <div className="hq-pad" data-testid="agent-bundle">
      <ScreenHeader
        title={`▤ ${bundleName}`}
        subtitle="A bundle made of agents. Add, remove, or eject members; nested bundles drill in."
      />
      {!bundle && <div className="hq-box text-mut">Bundle not found.</div>}
      {bundle && (
        <div className="hq-box bg-paper">
          <div className="flex items-center justify-between">
            <b>
              {bundle.name}{' '}
              <span className="text-xs text-faint">· {bundle.members.length} sub-agents</span>
            </b>
            <div className="flex items-center gap-1.5">
              <SkillCombobox
                testid="add-member"
                placeholder="Search agents to add…"
                buttonLabel="+ Add agent to bundle"
                emptyHint="No matching agents"
                options={candidates.map((c) => ({
                  name: c.name,
                  hint: c.kind === 'bundle' ? 'bundle' : c.model,
                }))}
                onCommit={onAdd}
              />
            </div>
          </div>
          <div className="hq-hr" />
          {/* A bundle is just a grouping: its members render as the SAME catalog
              cards as the top-level Agents grid (nested bundles stay clickable and
              drill in), each with a remove affordance. */}
          <div className="grid grid-cols-3 gap-3.5">
            {bundle.members.map((m) => {
              const member = agents.find((a) => a.name === m);
              return (
                <div key={m} className="flex flex-col gap-1.5" data-testid={`member-${m}`}>
                  {member ? (
                    <AgentCard agent={member} />
                  ) : (
                    // A member whose agent record isn't in the catalog (dangling
                    // reference) still needs a card + a way to eject it.
                    <div
                      className="hq-box bg-paper text-xs text-mut"
                      data-testid={`agent-card-${m}`}
                    >
                      {m} <span className="text-faint">· missing from catalog</span>
                    </div>
                  )}
                  <button
                    type="button"
                    className="hq-btn self-start"
                    data-testid={`remove-member-${m}`}
                    onClick={() => onRemove(m)}
                  >
                    ✕ remove
                  </button>
                </div>
              );
            })}
          </div>

          <div className="hq-hr" />
          <DissolveControl count={bundle.members.length} noun="agent" onConfirm={onDissolve} />
        </div>
      )}
    </div>
  );
}
