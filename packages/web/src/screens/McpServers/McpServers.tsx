import { useEffect, useMemo, useState } from 'react';
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
  return s.transport === 'stdio' ? [s.command, ...s.args].join(' ').trim() : s.url;
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

  const catalog = author === ANY_AUTHOR ? servers : servers.filter((s) => authorOf(s) === author);

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
        <div
          className="my-1.5 break-all text-xs text-mut"
          data-testid={`mcp-summary-${server.name}`}
        >
          {summary}
        </div>
      )}
      <McpDetailPreview server={server} />
      <div className="mt-1.5 text-[11px] text-faint" data-testid={`mcp-author-${server.name}`}>
        by {authorOf(server)}
      </div>
    </div>
  );
}

/**
 * The card keeps only the one-line summary; the full structured record (command,
 * args, env / url, headers) lives behind an Expand button that opens it in a
 * fullscreen overlay — mirroring the skill and agent cards.
 */
function McpDetailPreview({ server }: { server: McpServer }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className="hq-btn mt-1.5"
        data-testid={`mcp-expand-${server.name}`}
        onClick={() => setOpen(true)}
      >
        ⤢ Expand
      </button>
      {open && <McpServerModal server={server} onClose={() => setOpen(false)} />}
    </>
  );
}

/** A labelled section header inside the detail overlay. */
function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 text-[11px] uppercase tracking-wide text-faint">{label}</div>
      <div className="text-xs text-ink">{children}</div>
    </div>
  );
}

/**
 * Render a secret map (env vars / headers) as KEY rows with the VALUE masked.
 * The values are plaintext secrets in the catalog (see mcpServerSchema's security
 * note), so the detail view shows only the keys plus a fixed mask — enough to know
 * what is configured without printing credentials. Empty → an explicit "none".
 */
function SecretMap({ map, testid }: { map: Record<string, string>; testid: string }) {
  const keys = Object.keys(map);
  if (keys.length === 0) return <span className="italic text-faint">none</span>;
  return (
    <div data-testid={testid}>
      {keys.map((k) => (
        <div key={k} className="break-all font-mono text-[11px]">
          {k} = <span className="text-faint">••••••</span>
        </div>
      ))}
    </div>
  );
}

/** Fullscreen overlay rendering an MCP server's full structured record. */
function McpServerModal({ server, onClose }: { server: McpServer; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <div
      className="fixed inset-0 z-50 flex flex-col bg-black/50 p-4 sm:p-8"
      role="dialog"
      aria-modal="true"
      aria-label={`${server.name} MCP server`}
      data-testid={`mcp-modal-${server.name}`}
      onClick={onClose}
    >
      <div
        className="hq-box mx-auto flex h-full w-full max-w-[700px] flex-col overflow-hidden bg-paper"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-odd pb-2">
          <span className="flex items-center gap-2">
            <b>{server.name}</b>
            <Pill>{server.transport}</Pill>
          </span>
          <button
            type="button"
            className="hq-btn"
            data-testid={`mcp-modal-close-${server.name}`}
            onClick={onClose}
          >
            ✕ Close
          </button>
        </div>
        <div className="mt-3 min-h-0 flex-1 overflow-auto">
          <DetailRow label="Scope">
            {server.scope.tier} · {server.scope.id}
          </DetailRow>
          {server.transport === 'stdio' ? (
            <>
              <DetailRow label="Command">
                <code className="break-all">{server.command}</code>
              </DetailRow>
              <DetailRow label="Args">
                {server.args.length > 0 ? (
                  <code className="break-all">{server.args.join(' ')}</code>
                ) : (
                  <span className="italic text-faint">none</span>
                )}
              </DetailRow>
              <DetailRow label="Environment">
                <SecretMap map={server.env} testid={`mcp-env-${server.name}`} />
              </DetailRow>
            </>
          ) : (
            <>
              <DetailRow label="URL">
                <code className="break-all">{server.url}</code>
              </DetailRow>
              <DetailRow label="Headers">
                <SecretMap map={server.headers} testid={`mcp-headers-${server.name}`} />
              </DetailRow>
            </>
          )}
          <DetailRow label="Author">{authorOf(server)}</DetailRow>
        </div>
      </div>
    </div>
  );
}
