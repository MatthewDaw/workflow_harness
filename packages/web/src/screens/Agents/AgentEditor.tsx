import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { Agent } from '@harness/shared';
import { orgScope } from '@harness/shared';
import {
  useGetAgentsQuery,
  useGetSkillsQuery,
  useGetMcpServersQuery,
  useSaveAgentMutation,
} from '../../api/baseApi.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Agent editor (collapsed model): model + name + prompt + skill picker. There is
 * no scope selector — the server forces org scope. Reached at /agents/new
 * (create) and /agents/:name/edit (edit).
 */
export function AgentEditor() {
  const { name } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const org = user?.org ?? '';

  const { data: agents } = useGetAgentsQuery();
  const { data: skills } = useGetSkillsQuery();
  const { data: mcpServers } = useGetMcpServersQuery();
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
      scope: orgScope(org),
      description: '',
      model: 'claude-sonnet-4',
      prompt: '',
      skills: [],
      tools: [],
      mcpServers: [],
    };

  const set = (patch: Partial<Agent>) => setDraft({ ...agent, ...patch });

  const toggleSkill = (skillName: string) => {
    const has = agent.skills.includes(skillName);
    set({ skills: has ? agent.skills.filter((s) => s !== skillName) : [...agent.skills, skillName] });
  };

  const toggleMcpServer = (name: string) => {
    const has = agent.mcpServers.includes(name);
    set({
      mcpServers: has
        ? agent.mcpServers.filter((s) => s !== name)
        : [...agent.mcpServers, name],
    });
  };

  const onSave = async () => {
    if (!agent.name.trim()) return;
    // On create do not send scope — the server forces org scope and stamps
    // createdBy. Send only the editable fields.
    const { scope: _scope, createdBy: _createdBy, ...rest } = agent;
    await saveAgent(rest as Agent).unwrap();
    navigate('/agents');
  };

  const [skillFilter, setSkillFilter] = useState('');
  const allSkills = skills ?? [];
  const q = skillFilter.trim().toLowerCase();
  const catalog = q
    ? allSkills.filter(
        (s) => s.name.toLowerCase().includes(q) || s.description.toLowerCase().includes(q),
      )
    : allSkills;

  const [mcpFilter, setMcpFilter] = useState('');
  const allMcpServers = mcpServers ?? [];
  const mq = mcpFilter.trim().toLowerCase();
  const mcpCatalog = mq
    ? allMcpServers.filter(
        (s) => s.name.toLowerCase().includes(mq) || s.transport.toLowerCase().includes(mq),
      )
    : allMcpServers;

  return (
    <div className="hq-pad" data-testid="agent-editor">
      <ScreenHeader
        title={isEdit ? `Edit agent · ${agent.name}` : 'New agent'}
        subtitle="Define the prompt, model, and the skills this agent draws from."
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

        <label className="mt-3 block text-xs font-medium text-mut">
          Description — when should this agent be invoked? (the delegation trigger)
        </label>
        <textarea
          className="hq-input mt-1 w-full"
          data-testid="agent-description"
          rows={2}
          value={agent.description}
          onChange={(e) => set({ description: e.target.value })}
        />

        <label className="mt-3 block text-xs font-medium text-mut">Model</label>
        <input
          className="hq-input mt-1 w-full"
          data-testid="agent-model"
          value={agent.model}
          onChange={(e) => set({ model: e.target.value })}
        />

        <div className="mt-3 flex items-center justify-between">
          <label className="block text-xs font-medium text-mut">Prompt</label>
          <span className="text-[11px] text-faint" data-testid="optimize-hint">
            Run /optimize-agent in claude+ to refine this prompt.
          </span>
        </div>
        <textarea
          className="hq-input mt-1 w-full"
          data-testid="agent-prompt"
          rows={4}
          value={agent.prompt}
          onChange={(e) => set({ prompt: e.target.value })}
        />

        <div className="mt-3 text-xs font-medium text-mut">Skills (catalog)</div>
        <input
          className="hq-input mt-1 w-full"
          data-testid="skill-filter"
          placeholder="Filter skills…"
          value={skillFilter}
          onChange={(e) => setSkillFilter(e.target.value)}
        />
        <div className="mt-1 flex flex-wrap gap-1.5" data-testid="skill-catalog">
          {allSkills.length === 0 && (
            <span className="text-xs text-faint">No skills available.</span>
          )}
          {allSkills.length > 0 && catalog.length === 0 && (
            <span className="text-xs text-faint" data-testid="skill-filter-empty">
              No skills match “{skillFilter}”.
            </span>
          )}
          {catalog.map((s) => {
            const on = agent.skills.includes(s.name);
            return (
              <button
                key={s.name}
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

        <div className="mt-3 text-xs font-medium text-mut">MCP servers (catalog)</div>
        <input
          className="hq-input mt-1 w-full"
          data-testid="mcp-filter"
          placeholder="Filter MCP servers…"
          value={mcpFilter}
          onChange={(e) => setMcpFilter(e.target.value)}
        />
        <div className="mt-1 flex flex-wrap gap-1.5" data-testid="mcp-catalog">
          {allMcpServers.length === 0 && (
            <span className="text-xs text-faint">No MCP servers available.</span>
          )}
          {allMcpServers.length > 0 && mcpCatalog.length === 0 && (
            <span className="text-xs text-faint" data-testid="mcp-filter-empty">
              No MCP servers match “{mcpFilter}”.
            </span>
          )}
          {mcpCatalog.map((s) => {
            const on = agent.mcpServers.includes(s.name);
            return (
              <button
                key={s.name}
                type="button"
                className={`hq-btn ${on ? 'hq-btn-pri' : ''}`}
                data-testid={`catalog-mcp-${s.name}`}
                aria-pressed={on}
                onClick={() => toggleMcpServer(s.name)}
              >
                {on ? '✓ ' : '+ '}
                {s.name}
                <span className="ml-1 text-[11px] text-faint">{s.transport}</span>
              </button>
            );
          })}
        </div>

        {agent.mcpServers.length > 0 && (
          <div className="mt-2">
            <span className="text-[11px] text-faint">selected: </span>
            {agent.mcpServers.map((s) => (
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
