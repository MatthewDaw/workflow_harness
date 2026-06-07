import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import type { McpServer } from '@harness/shared';
import { useGetMcpServersQuery, useGetMeQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const ANY_AUTHOR = '__any__';

function authorOf(s: { createdBy?: { name: string } }): string {
  return s.createdBy?.name ?? 'Unknown';
}

/**
 * A one-line summary of where the server lives: the spawned command (stdio) or
 * the remote endpoint (http/sse). Mirrors the model badge on the agent card.
 */
function summaryOf(s: McpServer): string {
  return s.transport === 'stdio'
    ? [s.command, ...s.args].join(' ').trim()
    : s.url;
}

/**
 * MCP-server registry (collapsed model): a single flat org catalog, modeled on
 * the Agents tab (structured records, no bundle drill-down). Create/edit/delete
 * are admin-gated — non-admins see a read-only catalog.
 */
export function McpServers() {
  const { data, isLoading } = useGetMcpServersQuery();
  const { data: me } = useGetMeQuery();
  const isAdmin = Boolean(me?.admin);
  const servers = data ?? [];

  const [author, setAuthor] = useState<string>(ANY_AUTHOR);
  const authors = useMemo(() => {
    const set = new Set<string>();
    for (const s of servers) set.add(authorOf(s));
    return [...set].sort();
  }, [servers]);

  const catalog =
    author === ANY_AUTHOR ? servers : servers.filter((s) => authorOf(s) === author);

  return (
    <div className="hq-pad" data-testid="mcp-servers-screen">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="MCP Servers (org catalog)"
          subtitle="The shared org library. Enable servers per-project from a project's MCP Servers tab."
        />
        {isAdmin && (
          <Link
            to="/mcp-servers/new"
            className="hq-btn hq-btn-pri no-underline"
            data-testid="new-mcp-server"
          >
            + New server
          </Link>
        )}
      </div>
      {authors.length > 1 && (
        <div className="mb-3 flex items-center gap-3">
          <label className="flex items-center gap-1.5 text-xs text-mut">
            Author
            <select
              className="hq-btn"
              data-testid="mcp-author-filter"
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
      )}
      {isLoading && <div className="text-mut">Loading MCP servers…</div>}
      {!isLoading && catalog.length === 0 && (
        <div className="hq-box text-mut" data-testid="mcp-empty">
          No MCP servers in the catalog.
        </div>
      )}
      <div className="grid grid-cols-3 gap-3.5" data-testid="mcp-catalog-grid">
        {catalog.map((s) => (
          <McpServerCard key={s.name} server={s} isAdmin={isAdmin} />
        ))}
      </div>
    </div>
  );
}

function McpServerCard({ server, isAdmin }: { server: McpServer; isAdmin: boolean }) {
  const summary = summaryOf(server);
  return (
    <div className="hq-box bg-paper" data-testid={`mcp-card-${server.name}`}>
      <div className="flex justify-between">
        {isAdmin ? (
          <Link
            to={`/mcp-servers/${encodeURIComponent(server.name)}/edit`}
            className="text-ink no-underline"
          >
            <b>{server.name}</b>
          </Link>
        ) : (
          <b>{server.name}</b>
        )}
        <span data-testid={`mcp-transport-${server.name}`}>
          <Pill>{server.transport}</Pill>
        </span>
      </div>
      {summary && (
        <div className="my-1.5 break-all text-xs text-mut" data-testid={`mcp-summary-${server.name}`}>
          {summary}
        </div>
      )}
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`mcp-author-${server.name}`}>
        by {authorOf(server)}
      </div>
    </div>
  );
}
