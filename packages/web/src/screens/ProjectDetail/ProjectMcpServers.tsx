import { useState } from 'react';
import { useParams } from 'react-router-dom';
import type { McpServer } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetMcpServersQuery,
  useEnableProjectMcpServerMutation,
  useDisableProjectMcpServerMutation,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { SkillCombobox } from '../../components/SkillCombobox.js';

/** Short, secret-free summary line for an enabled server (command or url). */
function summarize(server: McpServer): string {
  if (server.transport === 'stdio') {
    return [server.command, ...server.args].join(' ');
  }
  return server.url;
}

/**
 * Project MCP Servers sub-tab (collapsed model): manage the project's
 * enabledMcpServers directly against the org catalog. Add via a searchable
 * combobox (mirrors the Skills sub-tab; the reused SkillCombobox carries the
 * transport as its hint); remove with a per-row button.
 */
export function ProjectMcpServers() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: catalogData } = useGetMcpServersQuery();
  const catalog = catalogData ?? [];

  const [enableServer] = useEnableProjectMcpServerMutation();
  const [disableServer] = useDisableProjectMcpServerMutation();

  const [addError, setAddError] = useState<string | null>(null);

  const enabled = project?.enabledMcpServers ?? [];
  const byName = new Map<string, McpServer>(catalog.map((s) => [s.name, s]));

  // Candidates: catalog servers not already enabled.
  const candidates = catalog.filter((s) => !enabled.includes(s.name));

  const onAdd = async (name: string) => {
    if (!name || !projectId) return;
    try {
      await enableServer({ projectId, name }).unwrap();
      setAddError(null);
    } catch {
      // Only servers in the org catalog can be enabled; a 404 means the name
      // isn't registered. Surface it instead of failing silently — authoring
      // happens on the MCP Servers tab.
      setAddError(
        `Couldn't add "${name}" — it isn't in the org catalog. Create it on the MCP Servers tab first.`,
      );
    }
  };
  const onRemove = (name: string) => {
    if (!projectId) return;
    disableServer({ projectId, name });
  };

  return (
    <div className="hq-pad" data-testid="project-mcp-servers">
      <ScreenHeader
        title="MCP Servers"
        subtitle="MCP servers enabled for this project. Add directly from the org catalog; agents also auto-add their MCP servers here."
      />

      <div className="hq-box bg-paper">
        <div className="flex items-center justify-between">
          <b>Add an MCP server</b>
          <SkillCombobox
            testid="enable-mcp-server"
            placeholder="Search the org catalog…"
            buttonLabel="+ Add to project"
            options={candidates.map((c) => ({
              name: c.name,
              hint: c.transport,
            }))}
            onCommit={onAdd}
            emptyHint="Not in the org catalog — create it on the MCP Servers tab first."
          />
        </div>
        {addError && (
          <div className="mt-2 text-xs text-rose-600" role="alert" data-testid="add-mcp-server-error">
            {addError}
          </div>
        )}
      </div>

      {enabled.length === 0 && (
        <div className="hq-box mt-3 text-mut" data-testid="project-mcp-servers-empty">
          No MCP servers enabled yet.
        </div>
      )}
      {enabled.length > 0 && (
        <div className="hq-box mt-3 bg-paper">
          {enabled.map((name) => {
            const server = byName.get(name);
            return (
              <div
                key={name}
                className="flex items-center justify-between border-b border-line2 py-1.5 text-[13px]"
                data-testid={`enabled-mcp-server-${name}`}
              >
                <span>
                  <Pill variant="skill" className="mr-1.5">
                    {name}
                  </Pill>
                  {server && (
                    <span className="mr-1.5 text-[11px] text-faint">{server.transport}</span>
                  )}
                  <span className="text-xs text-mut">{server ? summarize(server) : ''}</span>
                </span>
                <button
                  type="button"
                  className="hq-btn"
                  data-testid={`remove-mcp-server-${name}`}
                  onClick={() => onRemove(name)}
                >
                  ✕ remove
                </button>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
