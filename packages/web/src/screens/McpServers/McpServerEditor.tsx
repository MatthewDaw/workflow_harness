import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { McpServer, McpTransport } from '@harness/shared';
import { MCP_TRANSPORTS, orgScope } from '@harness/shared';
import {
  useGetMcpServersQuery,
  useGetMeQuery,
  useSaveMcpServerMutation,
  useDeleteMcpServerMutation,
} from '../../api/baseApi.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * The editor draft. We hold ALL transport fields flat so switching transport in
 * the <select> never discards what the user already typed; the discriminated
 * `McpServer` payload is assembled per-transport at save time.
 */
interface Draft {
  name: string;
  transport: McpTransport;
  command: string;
  /** Raw space-separated args text; split into the array only at save time so
   * typing a trailing space mid-edit is never collapsed. */
  args: string;
  env: Array<[string, string]>;
  url: string;
  headers: Array<[string, string]>;
}

/** A remote transport (http/sse) is addressed by `url` + `headers`. */
function isRemote(t: McpTransport): boolean {
  return t === 'http' || t === 'sse';
}

function recordToRows(rec: Record<string, string>): Array<[string, string]> {
  return Object.entries(rec);
}

/** Collapse key/value rows into a record, dropping blank-key rows. */
function rowsToRecord(rows: Array<[string, string]>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of rows) {
    const key = k.trim();
    if (key) out[key] = v;
  }
  return out;
}

/** A http/sse URL is required and must be a valid URL; stdio has no URL. */
function urlIsValid(url: string): boolean {
  try {
    new URL(url);
    return true;
  } catch {
    return false;
  }
}

/** Seed an editor draft from an existing server (edit) or a blank skeleton. */
function draftFrom(server: McpServer | undefined, name: string): Draft {
  if (server?.transport === 'stdio') {
    return {
      name: server.name,
      transport: 'stdio',
      command: server.command,
      args: server.args.join(' '),
      env: recordToRows(server.env),
      url: '',
      headers: [],
    };
  }
  if (server && isRemote(server.transport)) {
    return {
      name: server.name,
      transport: server.transport,
      command: '',
      args: '',
      env: [],
      url: server.url,
      headers: recordToRows(server.headers),
    };
  }
  return {
    name,
    transport: 'stdio',
    command: '',
    args: '',
    env: [],
    url: '',
    headers: [],
  };
}

/**
 * MCP-server editor (collapsed model): name + a transport <select> that swaps
 * between the stdio (command/args/env) and remote (url/headers) field sets. There
 * is no scope selector — the server forces org scope. Reached at /mcp-servers/new
 * (create) and /mcp-servers/:name/edit (edit). Admin-gated like the Agents tab.
 */
export function McpServerEditor() {
  const { name } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const org = user?.org ?? '';

  const { data: servers } = useGetMcpServersQuery();
  const { data: me } = useGetMeQuery();
  const isAdmin = Boolean(me?.admin);
  const [saveServer, { isLoading: saving }] = useSaveMcpServerMutation();
  const [deleteServer] = useDeleteMcpServerMutation();

  const existing = useMemo(
    () => (name ? (servers ?? []).find((s) => s.name === name) : undefined),
    [servers, name],
  );
  const isEdit = Boolean(name);

  const [draft, setDraft] = useState<Draft | null>(null);
  // State init runs before servers resolve, so derive the draft lazily from the
  // existing record once it loads (edit) or a blank skeleton (create).
  const d: Draft = draft ?? draftFrom(existing, name ?? '');
  const set = (patch: Partial<Draft>) => setDraft({ ...d, ...patch });

  const remote = isRemote(d.transport);
  const urlInvalid = remote && d.url.trim() !== '' && !urlIsValid(d.url);
  const canSave =
    isAdmin && d.name.trim() !== '' && !saving && (remote ? urlIsValid(d.url) : d.command.trim() !== '');

  const onSave = async () => {
    if (!canSave) return;
    // Assemble the discriminated payload per transport. On create we omit scope
    // (the server forces org) and createdBy (stamped server-side).
    const payload: McpServer = remote
      ? {
          name: d.name.trim(),
          scope: orgScope(org),
          transport: d.transport as 'http' | 'sse',
          url: d.url.trim(),
          headers: rowsToRecord(d.headers),
        }
      : {
          name: d.name.trim(),
          scope: orgScope(org),
          transport: 'stdio',
          command: d.command.trim(),
          args: d.args.split(/\s+/).filter(Boolean),
          env: rowsToRecord(d.env),
        };
    const { scope: _scope, ...rest } = payload;
    await saveServer(rest as McpServer).unwrap();
    navigate('/mcp-servers');
  };

  const onDelete = async () => {
    if (!isEdit || !isAdmin) return;
    if (!window.confirm(`Delete MCP server “${d.name}”?`)) return;
    await deleteServer(d.name).unwrap();
    navigate('/mcp-servers');
  };

  return (
    <div className="hq-pad" data-testid="mcp-server-editor">
      <ScreenHeader
        title={isEdit ? `Edit MCP server · ${d.name}` : 'New MCP server'}
        subtitle="Define the transport and its connection details. Secrets in env/headers are stored as plaintext — do not use for highly sensitive credentials."
      />

      <div className="hq-box bg-paper">
        <label className="block text-xs font-medium text-mut">Name</label>
        <input
          className="hq-input mt-1 w-full"
          data-testid="mcp-name"
          value={d.name}
          disabled={isEdit}
          onChange={(e) => set({ name: e.target.value })}
        />

        <label className="mt-3 block text-xs font-medium text-mut">Transport</label>
        <select
          className="hq-input mt-1 w-full"
          data-testid="mcp-transport"
          value={d.transport}
          onChange={(e) => set({ transport: e.target.value as McpTransport })}
        >
          {MCP_TRANSPORTS.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </select>

        {!remote && (
          <div data-testid="mcp-stdio-fields">
            <label className="mt-3 block text-xs font-medium text-mut">Command</label>
            <input
              className="hq-input mt-1 w-full"
              data-testid="mcp-command"
              value={d.command}
              onChange={(e) => set({ command: e.target.value })}
            />

            <label className="mt-3 block text-xs font-medium text-mut">Arguments</label>
            <input
              className="hq-input mt-1 w-full"
              data-testid="mcp-args"
              placeholder="Space-separated, e.g. -y @scope/server"
              value={d.args}
              onChange={(e) => set({ args: e.target.value })}
            />

            <KeyValueRows
              label="Environment variables"
              testid="env"
              rows={d.env}
              onChange={(env) => set({ env })}
            />
          </div>
        )}

        {remote && (
          <div data-testid="mcp-remote-fields">
            <label className="mt-3 block text-xs font-medium text-mut">URL</label>
            <input
              className="hq-input mt-1 w-full"
              data-testid="mcp-url"
              placeholder="https://example.com/mcp"
              value={d.url}
              onChange={(e) => set({ url: e.target.value })}
            />
            {urlInvalid && (
              <div className="mt-1 text-[11px] text-red-600" role="alert" data-testid="mcp-url-error">
                Enter a valid URL (including scheme, e.g. https://).
              </div>
            )}

            <KeyValueRows
              label="Headers"
              testid="headers"
              rows={d.headers}
              onChange={(headers) => set({ headers })}
            />
          </div>
        )}

        <div className="hq-hr" />
        <div className="flex gap-2">
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="mcp-save"
            disabled={!canSave}
            onClick={onSave}
          >
            Save &amp; sync
          </button>
          <button
            type="button"
            className="hq-btn"
            data-testid="mcp-cancel"
            onClick={() => navigate('/mcp-servers')}
          >
            Cancel
          </button>
          {isEdit && isAdmin && (
            <button
              type="button"
              className="hq-btn ml-auto"
              data-testid="mcp-delete"
              onClick={onDelete}
            >
              Delete
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/**
 * A key/value row editor for env (stdio) and headers (http/sse). Each row is a
 * key+value input pair with a remove button; a trailing "+ Add" appends a blank
 * row. Blank-key rows are dropped when the draft is serialized at save time.
 */
function KeyValueRows({
  label,
  testid,
  rows,
  onChange,
}: {
  label: string;
  testid: string;
  rows: Array<[string, string]>;
  onChange: (rows: Array<[string, string]>) => void;
}) {
  const setRow = (i: number, next: [string, string]) =>
    onChange(rows.map((r, idx) => (idx === i ? next : r)));
  const addRow = () => onChange([...rows, ['', '']]);
  const removeRow = (i: number) => onChange(rows.filter((_, idx) => idx !== i));

  return (
    <div className="mt-3" data-testid={`mcp-${testid}`}>
      <div className="text-xs font-medium text-mut">{label}</div>
      {rows.map(([k, v], i) => (
        <div key={i} className="mt-1 flex gap-2">
          <input
            className="hq-input w-1/3"
            data-testid={`mcp-${testid}-key-${i}`}
            placeholder="key"
            value={k}
            onChange={(e) => setRow(i, [e.target.value, v])}
          />
          <input
            className="hq-input flex-1"
            data-testid={`mcp-${testid}-value-${i}`}
            placeholder="value"
            value={v}
            onChange={(e) => setRow(i, [k, e.target.value])}
          />
          <button
            type="button"
            className="hq-btn"
            data-testid={`mcp-${testid}-remove-${i}`}
            aria-label={`Remove ${label} row ${i + 1}`}
            onClick={() => removeRow(i)}
          >
            ×
          </button>
        </div>
      ))}
      <button
        type="button"
        className="hq-btn mt-1"
        data-testid={`mcp-${testid}-add`}
        onClick={addRow}
      >
        + Add
      </button>
    </div>
  );
}
