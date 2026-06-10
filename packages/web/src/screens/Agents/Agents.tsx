import { useMemo, useState, type ReactNode } from 'react';
import { Link } from 'react-router-dom';
import type { Agent } from '@harness/shared';
import { useGetAgentsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';
import { OverlayModal } from '../../components/OverlayModal.js';
import { useAuthorFilter, AuthorSelect } from '../../components/AuthorFilter.js';
import { authorOf, bundlesFirst } from '../../lib/catalogUi.js';
import { bundleMemberNames } from '../../lib/bundles.js';

/** Agents registry (collapsed model): a single flat org catalog, bundles included. */
export function Agents() {
  const { data, isLoading } = useGetAgentsQuery();
  const agents = data ?? [];

  const [showInBundles, setShowInBundles] = useState(false);
  const { author, setAuthor, authors, matches } = useAuthorFilter(agents, authorOf);
  const memberNames = useMemo(() => bundleMemberNames(agents), [agents]);

  const visible = (a: Agent): boolean => {
    if (!matches(a)) return false;
    if (a.kind === 'bundle') return true;
    if (showInBundles) return true;
    return !memberNames.has(a.name);
  };

  // Bundles lead the grid (entry points that drill into member agents), then plain
  // agents — order is otherwise stable so the catalog stays steady (mirrors SkillCatalog).
  const catalog = bundlesFirst(agents.filter(visible), (a) => a.kind === 'bundle');

  return (
    <div className="hq-pad" data-testid="agents-screen">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="Agents (org catalog)"
          subtitle="The shared org library. Enable agents per-project from a project's Agents tab."
        />
        <Link to="/agents/new" className="hq-btn hq-btn-pri no-underline" data-testid="new-agent">
          + New agent
        </Link>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-4 text-xs text-mut">
        <label className="flex items-center gap-1.5" data-testid="show-in-bundles-toggle">
          <input
            type="checkbox"
            checked={showInBundles}
            onChange={(e) => setShowInBundles(e.target.checked)}
            data-testid="show-in-bundles-checkbox"
          />
          Show agents that are in bundles
        </label>
        <AuthorSelect
          author={author}
          onChange={setAuthor}
          authors={authors}
          testid="agent-author-filter"
        />
      </div>
      {isLoading && <div className="text-mut">Loading agents…</div>}
      <div className="grid grid-cols-3 gap-3.5" data-testid="agent-catalog-grid">
        {catalog.map((a) => (
          <AgentCard key={a.name} agent={a} />
        ))}
      </div>
    </div>
  );
}

export function AgentCard({ agent }: { agent: Agent }) {
  // A bundle is an entry point that drills into its member agents — link to the
  // bundle detail and show its member count instead of a model/prompt (mirrors a
  // skill bundle card).
  if (agent.kind === 'bundle') {
    const count = (agent.resolvedMembers ?? agent.members).length;
    return (
      <div
        className="hq-box bg-paper border-l-[3px] border-l-[#6f8fb5]"
        data-testid={`agent-card-${agent.name}`}
      >
        <div className="flex justify-between">
          <Link
            to={`/agents/${encodeURIComponent(agent.name)}/bundle`}
            className="text-ink no-underline"
          >
            <b>▤ {agent.name}</b>
          </Link>
          <Pill>bundle</Pill>
        </div>
        {agent.description && (
          <div className="my-1.5 text-xs text-ink" data-testid={`agent-description-${agent.name}`}>
            {agent.description}
          </div>
        )}
        <div className="mt-1.5 text-[11px] text-faint">
          {count} member agent{count === 1 ? '' : 's'} · by {authorOf(agent)}
        </div>
      </div>
    );
  }

  return (
    <div className="hq-box bg-paper" data-testid={`agent-card-${agent.name}`}>
      <div className="flex justify-between">
        <Link
          to={`/agents/${encodeURIComponent(agent.name)}/edit`}
          className="text-ink no-underline"
        >
          <b>{agent.name}</b>
        </Link>
        <Pill>{agent.model}</Pill>
      </div>
      {agent.description && (
        <div className="my-1.5 text-xs text-ink" data-testid={`agent-description-${agent.name}`}>
          {agent.description}
        </div>
      )}
      <AgentBodyPreview agent={agent} />
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`agent-author-${agent.name}`}>
        by {authorOf(agent)}
      </div>
    </div>
  );
}

/**
 * The card keeps only the agent's description (rendered by the caller); the full
 * registration — system prompt plus every attached skill, MCP server, and tool —
 * lives behind an Expand button that opens it in a fullscreen overlay, mirroring
 * how a skill card hides its SKILL.md body. Renders nothing when the agent has no
 * prompt and nothing attached to show.
 */
function AgentBodyPreview({ agent }: { agent: Agent }) {
  const [open, setOpen] = useState(false);
  const prompt = (agent.prompt ?? '').trim();
  const hasAttachments =
    agent.skills.length > 0 || agent.mcpServers.length > 0 || agent.tools.length > 0;
  if (!prompt && !hasAttachments) return null;

  return (
    <>
      <button
        type="button"
        className="hq-btn mt-1.5"
        data-testid={`agent-expand-${agent.name}`}
        onClick={() => setOpen(true)}
      >
        ⤢ Expand
      </button>
      {open && <AgentBodyModal agent={agent} prompt={prompt} onClose={() => setOpen(false)} />}
    </>
  );
}

/**
 * A section header for the agent detail overlay. Renders as an unambiguous header:
 * bold, full-strength ink, with a divider rule beneath it so the eye reads it as a
 * section break rather than just faint label text.
 */
function SectionHeader({ children }: { children: ReactNode }) {
  return (
    <div className="mb-2 border-b border-odd pb-1 text-xs font-bold uppercase tracking-wider text-ink">
      {children}
    </div>
  );
}

/**
 * A labelled row of pills (skills / MCP servers / tools). Hidden when empty unless
 * `alwaysShow` is set — used for the skills and MCP servers that ship with the agent,
 * which stay visible (with an explicit empty state) so it's clear nothing is bundled.
 */
function AttachmentList({
  label,
  items,
  variant,
  testid,
  alwaysShow,
  emptyHint,
}: {
  label: string;
  items: string[];
  variant?: 'skill';
  testid: string;
  alwaysShow?: boolean;
  emptyHint?: string;
}) {
  if (items.length === 0 && !alwaysShow) return null;
  return (
    <div className="mb-3" data-testid={testid}>
      <SectionHeader>{label}</SectionHeader>
      {items.length === 0 ? (
        <div className="text-xs italic text-faint">{emptyHint ?? 'None'}</div>
      ) : (
        <div>
          {items.map((i) => (
            <Pill key={i} variant={variant} className="mr-1 mb-1">
              {i}
            </Pill>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Fullscreen overlay rendering an agent's full registration: its attached skills,
 * MCP servers, and tools, followed by the system prompt.
 */
function AgentBodyModal({
  agent,
  prompt,
  onClose,
}: {
  agent: Agent;
  prompt: string;
  onClose: () => void;
}) {
  return (
    <OverlayModal
      ariaLabel={`${agent.name} agent`}
      testid={`agent-modal-${agent.name}`}
      closeTestid={`agent-modal-close-${agent.name}`}
      onClose={onClose}
      header={
        <span className="flex items-center gap-2">
          <b>{agent.name}</b>
          <Pill>{agent.model}</Pill>
        </span>
      }
    >
      {agent.description && <div className="mb-3 text-xs text-mut">{agent.description}</div>}
      <AttachmentList
        label="Skills"
        items={agent.skills}
        variant="skill"
        testid={`agent-skills-${agent.name}`}
        alwaysShow
        emptyHint="No skills ship with this agent"
      />
      <AttachmentList
        label="MCP servers"
        items={agent.mcpServers}
        testid={`agent-mcp-${agent.name}`}
        alwaysShow
        emptyHint="No MCP servers ship with this agent"
      />
      <AttachmentList label="Tools" items={agent.tools} testid={`agent-tools-${agent.name}`} />
      {prompt && (
        <div data-testid={`agent-prompt-${agent.name}`}>
          <SectionHeader>System prompt</SectionHeader>
          <MarkdownView markdown={prompt} />
        </div>
      )}
    </OverlayModal>
  );
}
