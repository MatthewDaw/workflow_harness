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
import {
  CatalogPicker,
  type CatalogRef,
  type CatalogPickerRow,
} from '../../components/CatalogPicker.js';
import { mcpSummary } from '../../lib/catalogUi.js';

/**
 * Project MCP Servers sub-tab (collapsed model): manage the project's
 * enabledMcpServers directly against the org catalog. The primary surface is now
 * the shared CatalogPicker modal ("+ Add to project"), so this tab stages a batch
 * of add/remove toggles in one place instead of one-at-a-time combobox adds; the
 * enabled-servers list below the button still renders the project's current set
 * with a per-row remove for quick single removals. There are no MCP bundles, so
 * every catalog server is a single `{ type: 'mcp', name }` row.
 */
export function ProjectMcpServers() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: catalogData } = useGetMcpServersQuery();
  const catalog = catalogData ?? [];

  const [enableServer] = useEnableProjectMcpServerMutation();
  const [disableServer] = useDisableProjectMcpServerMutation();

  // Local UI state: the modal open flag, an in-flight apply flag (so the picker
  // shows its busy state while we run the sequential mutations), and the error
  // banner reused from the old add flow.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const enabled = project?.enabledMcpServers ?? [];
  const byName = new Map<string, McpServer>(catalog.map((s) => [s.name, s]));

  // Picker rows: one per catalog server. The transport is the hint and the
  // secret-free command/url summary is the description, mirroring the enabled
  // list below so the modal and the list read identically.
  const rows: CatalogPickerRow[] = catalog.map((s) => ({
    ref: { type: 'mcp', name: s.name },
    label: s.name,
    hint: s.transport,
    description: mcpSummary(s),
  }));

  // The refs that are ON when the modal opens: the project's current enabled set.
  const initialSelected: CatalogRef[] = enabled.map((name) => ({ type: 'mcp', name }));

  // Apply the picker diff. CRITICAL: every opt-in mutation read-modify-writes the
  // single project META record, so the enable/disable mutations MUST run
  // SEQUENTIALLY (await each before the next) — never concurrently, or they
  // clobber each other.
  const onApply = async (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => {
    if (!projectId) return;
    setApplying(true);
    setAddError(null);
    try {
      for (const ref of diff.enable) {
        await enableServer({ projectId, name: ref.name }).unwrap();
      }
      for (const ref of diff.disable) {
        await disableServer({ projectId, name: ref.name }).unwrap();
      }
    } catch {
      // Only servers in the org catalog can be enabled; a 404 means a name isn't
      // registered. Surface it instead of failing silently — authoring happens on
      // the MCP Servers tab.
      setAddError(
        "Couldn't apply your changes — a selected server may not be in the org catalog. Create it on the MCP Servers tab first.",
      );
    } finally {
      setApplying(false);
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
          <b>MCP servers</b>
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="enable-mcp-server"
            onClick={() => setPickerOpen(true)}
          >
            + Add to project
          </button>
        </div>
        {addError && (
          <div
            className="mt-2 text-xs text-rose-600"
            role="alert"
            data-testid="add-mcp-server-error"
          >
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
                  <span className="text-xs text-mut">{server ? mcpSummary(server) : ''}</span>
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

      <CatalogPicker
        open={pickerOpen}
        title="Add MCP servers to project"
        rows={rows}
        initialSelected={initialSelected}
        emptyHint="No MCP servers in the org catalog yet — create one on the MCP Servers tab first."
        onClose={() => setPickerOpen(false)}
        onApply={onApply}
        applying={applying}
      />
    </div>
  );
}
