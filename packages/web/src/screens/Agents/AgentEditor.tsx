import { useMemo, useState, type ReactNode } from 'react';
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
  const agent: Agent = draft ??
    existing ?? {
      name: name ?? '',
      scope: orgScope(org),
      kind: 'agent',
      description: '',
      model: 'claude-sonnet-4',
      prompt: '',
      skills: [],
      tools: [],
      mcpServers: [],
      members: [],
    };

  const set = (patch: Partial<Agent>) => setDraft({ ...agent, ...patch });

  const toggleSkill = (skillName: string) => {
    const has = agent.skills.includes(skillName);
    set({
      skills: has ? agent.skills.filter((s) => s !== skillName) : [...agent.skills, skillName],
    });
  };

  const toggleMcpServer = (name: string) => {
    const has = agent.mcpServers.includes(name);
    set({
      mcpServers: has ? agent.mcpServers.filter((s) => s !== name) : [...agent.mcpServers, name],
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

        <FilterableTogglePills
          label="Skills (catalog)"
          noun="skills"
          items={skills ?? []}
          selected={agent.skills}
          onToggle={toggleSkill}
          testidPrefix="skill"
          filterFields={(s) => [s.name, s.description]}
        />

        <FilterableTogglePills
          label="MCP servers (catalog)"
          noun="MCP servers"
          items={mcpServers ?? []}
          selected={agent.mcpServers}
          onToggle={toggleMcpServer}
          testidPrefix="mcp"
          filterFields={(s) => [s.name, s.transport]}
          renderHint={(s) => <span className="ml-1 text-[11px] text-faint">{s.transport}</span>}
        />

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

/**
 * One filterable toggle-pill picker (the skills and MCP-server catalogs). A text
 * filter narrows the catalog; each entry is a pressed/unpressed toggle button,
 * and the current selection echoes below as pills. `filterFields` keeps the
 * per-picker match fields explicit (skills match description, servers match
 * transport); `testidPrefix` drives `{p}-filter`, `{p}-catalog`,
 * `{p}-filter-empty`, and `catalog-{p}-{name}`.
 */
function FilterableTogglePills<T extends { name: string }>({
  label,
  noun,
  items,
  selected,
  onToggle,
  testidPrefix,
  filterFields,
  renderHint,
}: {
  label: string;
  /** Plural noun for the placeholder and empty-state copy. */
  noun: string;
  items: T[];
  selected: string[];
  onToggle: (name: string) => void;
  testidPrefix: string;
  filterFields: (item: T) => string[];
  renderHint?: (item: T) => ReactNode;
}) {
  const [filter, setFilter] = useState('');
  const q = filter.trim().toLowerCase();
  const catalog = q
    ? items.filter((it) => filterFields(it).some((f) => f.toLowerCase().includes(q)))
    : items;

  return (
    <>
      <div className="mt-3 text-xs font-medium text-mut">{label}</div>
      <input
        className="hq-input mt-1 w-full"
        data-testid={`${testidPrefix}-filter`}
        placeholder={`Filter ${noun}…`}
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="mt-1 flex flex-wrap gap-1.5" data-testid={`${testidPrefix}-catalog`}>
        {items.length === 0 && <span className="text-xs text-faint">No {noun} available.</span>}
        {items.length > 0 && catalog.length === 0 && (
          <span className="text-xs text-faint" data-testid={`${testidPrefix}-filter-empty`}>
            No {noun} match “{filter}”.
          </span>
        )}
        {catalog.map((it) => {
          const on = selected.includes(it.name);
          return (
            <button
              key={it.name}
              type="button"
              className={`hq-btn ${on ? 'hq-btn-pri' : ''}`}
              data-testid={`catalog-${testidPrefix}-${it.name}`}
              aria-pressed={on}
              onClick={() => onToggle(it.name)}
            >
              {on ? '✓ ' : '+ '}
              {it.name}
              {renderHint?.(it)}
            </button>
          );
        })}
      </div>

      {selected.length > 0 && (
        <div className="mt-2">
          <span className="text-[11px] text-faint">selected: </span>
          {selected.map((s) => (
            <Pill key={s} variant="skill" className="mr-1">
              {s}
            </Pill>
          ))}
        </div>
      )}
    </>
  );
}
