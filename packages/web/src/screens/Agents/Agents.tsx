import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { Agent } from '@harness/shared';
import { useGetAgentsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const ANY_AUTHOR = '__any__';

function authorOf(a: { createdBy?: { name: string } }): string {
  return a.createdBy?.name ?? 'Unknown';
}

/** Agents registry (collapsed model): a single flat org catalog. */
export function Agents() {
  const { data, isLoading } = useGetAgentsQuery();
  const agents = data ?? [];

  const [author, setAuthor] = useState<string>(ANY_AUTHOR);
  const authors = useMemo(() => {
    const set = new Set<string>();
    for (const a of agents) set.add(authorOf(a));
    return [...set].sort();
  }, [agents]);

  const catalog =
    author === ANY_AUTHOR ? agents : agents.filter((a) => authorOf(a) === author);

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
      <div className="mb-3 flex items-center gap-3">
        <label className="flex items-center gap-1.5 text-xs text-mut">
          Author
          <select
            className="hq-btn"
            data-testid="agent-author-filter"
            value={author}
            onChange={(e) => setAuthor(e.target.value)}
          >
            <option value={ANY_AUTHOR}>any author</option>
            {authors.map((a) => (
              <option key={a} value={a}>
                {a}
              </option>
            ))}
          </select>
        </label>
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

function AgentCard({ agent }: { agent: Agent }) {
  return (
    <div className="hq-box bg-paper" data-testid={`agent-card-${agent.name}`}>
      <div className="flex justify-between">
        <Link to={`/agents/${encodeURIComponent(agent.name)}/edit`} className="text-ink no-underline">
          <b>{agent.name}</b>
        </Link>
        <Pill>{agent.model}</Pill>
      </div>
      {agent.description && (
        <div
          className="my-1.5 text-xs text-ink"
          data-testid={`agent-description-${agent.name}`}
        >
          {agent.description}
        </div>
      )}
      {agent.prompt && <div className="my-1.5 text-xs text-mut">{agent.prompt}</div>}
      <div className="my-1.5">
        {agent.skills.map((s) => (
          <Pill key={s} variant="skill" className="mr-1">
            {s}
          </Pill>
        ))}
      </div>
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`agent-author-${agent.name}`}>
        by {authorOf(agent)}
      </div>
    </div>
  );
}
