import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { Agent, ScopeRef, ScopeTier } from '@harness/shared';
import { SCOPE_TIERS } from '@harness/shared';
import {
  useGetAgentsQuery,
  useGetSkillsQuery,
  useSaveAgentMutation,
} from '../../api/baseApi.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const SCOPE_LABEL: Record<ScopeTier, string> = {
  org: 'Org · all users',
  user: 'My global · all my repos',
  project: 'Project',
};

/** Resolve a scope ref for a chosen tier against the viewer context. */
function scopeForTier(tier: ScopeTier, ctx: { org: string; userId: string }): ScopeRef {
  if (tier === 'org') return { tier: 'org', id: ctx.org };
  if (tier === 'project') return { tier: 'project', id: ctx.userId };
  return { tier: 'user', id: ctx.userId };
}

/**
 * Agent editor (U17): scope selector + skill catalog picker, Save persists via
 * `saveAgent`. Reached at /agents/new (create) and /agents/:name/edit (edit).
 */
export function AgentEditor() {
  const { name } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const ctx = { org: user?.org ?? '', userId: user?.userId ?? '' };

  const { data: agents } = useGetAgentsQuery();
  const { data: skills } = useGetSkillsQuery();
  const [saveAgent, { isLoading: saving }] = useSaveAgentMutation();

  const existing = useMemo(
    () => (name ? (agents ?? []).find((a) => a.name === name) : undefined),
    [agents, name],
  );
  const isEdit = Boolean(name);

  const [draft, setDraft] = useState<Agent | null>(null);
  // Seed the draft from the existing agent once it loads (edit), or a blank
  // skeleton (create). State init runs before agents resolve, so derive lazily.
  const agent: Agent =
    draft ??
    existing ?? {
      name: name ?? '',
      scope: scopeForTier('user', ctx),
      model: 'claude-sonnet-4',
      prompt: '',
      skills: [],
      tools: [],
    };

  const set = (patch: Partial<Agent>) => setDraft({ ...agent, ...patch });

  const toggleSkill = (skillName: string) => {
    const has = agent.skills.includes(skillName);
    set({ skills: has ? agent.skills.filter((s) => s !== skillName) : [...agent.skills, skillName] });
  };

  const onSave = async () => {
    if (!agent.name.trim()) return;
    await saveAgent(agent).unwrap();
    navigate('/agents');
  };

  const catalog = skills ?? [];

  return (
    <div className="hq-pad" data-testid="agent-editor">
      <ScreenHeader
        title={isEdit ? `Edit agent · ${agent.name}` : 'New agent'}
        subtitle="Define the prompt, model, scope, and the skills this agent draws from."
      />

      <div className="hq-box bg-paper">
        <label className="block text-xs font-medium text-mut">Name</label>
        <input
          className="hq-input mt-1 w-full"
          data-testid="agent-name"
          value={agent.name}
          disabled={isEdit}
          onChange={(e) => set({ name: e.target.value })}
        />

        <label className="mt-3 block text-xs font-medium text-mut">Model</label>
        <input
          className="hq-input mt-1 w-full"
          data-testid="agent-model"
          value={agent.model}
          onChange={(e) => set({ model: e.target.value })}
        />

        <label className="mt-3 block text-xs font-medium text-mut">Scope</label>
        <select
          className="hq-btn mt-1"
          data-testid="agent-scope"
          value={agent.scope.tier}
          onChange={(e) => set({ scope: scopeForTier(e.target.value as ScopeTier, ctx) })}
        >
          {SCOPE_TIERS.map((t) => (
            <option key={t} value={t}>
              {SCOPE_LABEL[t]}
            </option>
          ))}
        </select>

        <label className="mt-3 block text-xs font-medium text-mut">Prompt</label>
        <textarea
          className="hq-input mt-1 w-full"
          data-testid="agent-prompt"
          rows={4}
          value={agent.prompt}
          onChange={(e) => set({ prompt: e.target.value })}
        />

        <div className="mt-3 text-xs font-medium text-mut">Skills (catalog)</div>
        <div className="mt-1 flex flex-wrap gap-1.5" data-testid="skill-catalog">
          {catalog.length === 0 && <span className="text-xs text-faint">No skills available.</span>}
          {catalog.map((s) => {
            const on = agent.skills.includes(s.name);
            return (
              <button
                key={`${s.scope.tier}-${s.name}`}
                type="button"
                className={`hq-btn ${on ? 'hq-btn-pri' : ''}`}
                data-testid={`catalog-skill-${s.name}`}
                aria-pressed={on}
                onClick={() => toggleSkill(s.name)}
              >
                {on ? '✓ ' : '+ '}
                {s.name}
              </button>
            );
          })}
        </div>

        {agent.skills.length > 0 && (
          <div className="mt-2">
            <span className="text-[11px] text-faint">selected: </span>
            {agent.skills.map((s) => (
              <Pill key={s} variant="skill" className="mr-1">
                {s}
              </Pill>
            ))}
          </div>
        )}

        <div className="hq-hr" />
        <div className="flex gap-2">
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="agent-save"
            disabled={saving || !agent.name.trim()}
            onClick={onSave}
          >
            Save &amp; sync
          </button>
          <button
            type="button"
            className="hq-btn"
            data-testid="agent-cancel"
            onClick={() => navigate('/agents')}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}
