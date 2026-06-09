import { useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import type { Workflow } from '@harness/shared';
import {
  useGetProjectQuery,
  useGetWorkflowsQuery,
  useEnableProjectWorkflowMutation,
  useDisableProjectWorkflowMutation,
  useStartWorkflowRunMutation,
  useGetWorkflowRunQuery,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import {
  WorkflowGraph,
  type WorkflowNodeRunState,
} from '../../components/WorkflowGraph.js';
import {
  CatalogPicker,
  type CatalogPickerRow,
  type CatalogRef,
} from '../../components/CatalogPicker.js';

/**
 * The distinct agents a workflow brings into a project (de-duped, input order),
 * for the "agents this workflow brings" display. Enabling a workflow unions every
 * referenced node's agent — and transitively their skills + MCP servers — server-
 * side, so this is purely an informational summary of that side effect.
 */
function bringsAgents(workflow: Workflow): string[] {
  const out = new Set<string>();
  for (const node of workflow.nodes) out.add(node.agent);
  return [...out];
}

/**
 * Project Workflows sub-tab (collapsed model): the page shows the workflows
 * already enabled on this project (as cards, each rendering its DAG via
 * `WorkflowGraph` plus a note of which agents it brings), and the full org
 * workflow catalog moves behind a single "+ Add to project" button that opens the
 * shared `CatalogPicker`. The modal stages a selection and hands back a
 * {enable, disable} diff on Apply; we fan that diff out to the per-workflow
 * mutations. Enabling a workflow also unions its referenced agents (and their
 * skills/MCP servers) into the project's enabled sets server-side, so there is
 * nothing extra to do client-side.
 *
 * CRITICAL: each opt-in mutation does a full read-modify-write of the single
 * project META record, so we apply the diff SEQUENTIALLY (await each before the
 * next) — concurrent writes would clobber one another.
 */
export function ProjectWorkflows() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: workflowData } = useGetWorkflowsQuery();

  const [enableWorkflow] = useEnableProjectWorkflowMutation();
  const [disableWorkflow] = useDisableProjectWorkflowMutation();

  // Modal open + in-flight apply state. `applying` drives the picker's busy UI
  // (Apply button disabled / "Applying…") while we walk the diff sequentially.
  const [pickerOpen, setPickerOpen] = useState(false);
  const [applying, setApplying] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const workflows = workflowData ?? [];
  const enabledNames = project?.enabledWorkflows ?? [];
  const enabledSet = new Set(enabledNames);

  // The workflows currently enabled on this project — the only thing the page
  // body renders now (the full catalog lives in the picker).
  const enabledWorkflows = workflows.filter((w) => enabledSet.has(w.name));

  // The picker rows: one row per catalog workflow. hint = node count; description
  // folds in the workflow's own description plus the agents it brings so the side
  // effect of enabling is visible. Workflows have no bundle/member concept, so
  // each row is a plain (non-expandable) row.
  const rows: CatalogPickerRow[] = useMemo(() => {
    return workflows.map((w) => {
      const brings = bringsAgents(w);
      const desc = [w.description, brings.length > 0 ? `Brings: ${brings.join(', ')}` : '']
        .filter(Boolean)
        .join(' — ');
      return {
        ref: { type: 'workflow', name: w.name },
        label: w.name,
        hint: `${w.nodes.length} node${w.nodes.length === 1 ? '' : 's'}`,
        description: desc || undefined,
      };
    });
  }, [workflows]);

  // The refs ON when the modal opens: each enabled workflow as a workflow ref.
  const initialSelected: CatalogRef[] = useMemo(() => {
    return enabledNames.map((name) => ({ type: 'workflow', name }));
    // enabledNames is a fresh array each render; key off its contents.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabledNames.join('|')]);

  // Apply the staged diff SEQUENTIALLY (await each) — every mutation read-modify-
  // writes the whole project META record. Enables before disables (mirrors
  // ProjectAgents).
  const onApply = async (diff: { enable: CatalogRef[]; disable: CatalogRef[] }) => {
    if (!projectId) return;
    setApplying(true);
    setAddError(null);
    try {
      const enableWorkflows = diff.enable.filter((r) => r.type === 'workflow');
      const disableWorkflows = diff.disable.filter((r) => r.type === 'workflow');

      for (const ref of enableWorkflows) {
        await enableWorkflow({ projectId, workflowName: ref.name }).unwrap();
      }
      for (const ref of disableWorkflows) {
        await disableWorkflow({ projectId, workflowName: ref.name }).unwrap();
      }
    } catch {
      // A failed toggle (e.g. the workflow isn't in the org catalog) surfaces here
      // instead of failing silently. The picker keeps the modal open on a thrown
      // apply, so the user can retry or cancel.
      setAddError("Couldn't update workflows for this project. Please try again.");
      throw new Error('apply-failed');
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="hq-pad" data-testid="project-workflows">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="Workflows"
          subtitle="Workflows enabled for this project. Enabling a workflow also adds its agents (and their skills) to this project."
        />
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="add-workflow"
          onClick={() => {
            setAddError(null);
            setPickerOpen(true);
          }}
        >
          + Add to project
        </button>
      </div>

      {addError && (
        <div className="mt-2 text-xs text-rose-600" role="alert" data-testid="add-workflow-error">
          {addError}
        </div>
      )}

      {enabledWorkflows.length === 0 ? (
        <div className="hq-box mt-3 text-mut" data-testid="project-workflows-empty">
          No workflows enabled yet.
        </div>
      ) : (
        <div className="mt-3 flex flex-col gap-3.5">
          {enabledWorkflows.map((w) => (
            <WorkflowRunCard
              key={w.name}
              workflow={w}
              projectId={projectId}
              onDisable={() => disableWorkflow({ projectId, workflowName: w.name })}
            />
          ))}
        </div>
      )}

      <CatalogPicker
        open={pickerOpen}
        title="Add workflows to this project"
        rows={rows}
        initialSelected={initialSelected}
        emptyHint="No workflows in the catalog."
        applying={applying}
        onClose={() => setPickerOpen(false)}
        onApply={onApply}
      />
    </div>
  );
}

/**
 * One enabled-workflow card with its live run overlay (M5). The card renders the
 * workflow's DAG via `WorkflowGraph` (same single rendering the catalog/editor
 * use) and adds a "Run" button: pressing it POSTs a run
 * (`useStartWorkflowRunMutation` → `POST /workflows/:name/runs` with `{ projectId }`)
 * and holds onto the returned `runId`. While a run is in flight we poll
 * `useGetWorkflowRunQuery` every 2s (RTK `pollingInterval`) until the run leaves
 * `running`, mapping the run's per-node `{ state, runs }` straight onto the
 * graph's `runState` prop so each card is colored by state and rerun nodes show
 * their run count. A done/failed banner closes out the run.
 *
 * Each card owns its own run hooks (the run lifecycle is per-workflow), which is
 * why this is a component rather than inline JSX in the parent's `.map`.
 */
function WorkflowRunCard({
  workflow,
  projectId,
  onDisable,
}: {
  workflow: Workflow;
  projectId: string;
  onDisable: () => void;
}) {
  const brings = bringsAgents(workflow);

  // The active run's id, set when the user presses Run. We keep polling the run
  // until it leaves `running`, but hold the id afterwards so the final
  // done/failed banner (and last per-node colors) stay on screen.
  const [runId, setRunId] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);

  const [startRun, { isLoading: starting }] = useStartWorkflowRunMutation();

  // `active` gates the 2s poll. We flip it on when a run is kicked off and off
  // once the run record reports a terminal status (see the effect below). This
  // is a separate flag (rather than reading the query's own data back into its
  // own options) so there is no self-reference in the hook arguments.
  const [active, setActive] = useState(false);

  // Poll the run while it is active; once it is no longer `running` we stop
  // polling (pollingInterval 0) but keep the last snapshot for the final banner.
  const { data: run } = useGetWorkflowRunQuery(
    { name: workflow.name, runId: runId ?? '', projectId },
    {
      skip: !runId,
      pollingInterval: active ? 2000 : 0,
    },
  );

  // Stop polling as soon as the run leaves `running`; the last snapshot stays in
  // cache so the final per-node colors + done/failed banner remain on screen.
  useEffect(() => {
    if (run && run.status !== 'running') setActive(false);
  }, [run]);

  // Map the run's per-node `{ state, runs, outputTail }` onto the graph's
  // `runState` prop (`{ state, runs }`) so the DAG colors each card by state and
  // rerun nodes show their run count. Undefined when no run has started.
  const runState: Record<string, WorkflowNodeRunState> | undefined = useMemo(() => {
    if (!run) return undefined;
    const out: Record<string, WorkflowNodeRunState> = {};
    for (const [id, n] of Object.entries(run.nodes)) {
      out[id] = { state: n.state, runs: n.runs };
    }
    return out;
  }, [run]);

  // `active` is true from kick-off until the run reaches a terminal status, so
  // it (not the not-yet-arrived poll) drives the button's busy state — the Run
  // button stays disabled/"Running…" across the gap before the first poll lands.
  const running = active;

  const onRun = async () => {
    setRunError(null);
    try {
      const started = await startRun({ name: workflow.name, projectId }).unwrap();
      setRunId(started.runId);
      setActive(true);
    } catch {
      // A failed kick-off (e.g. the executor route isn't deployed yet) surfaces
      // inline instead of failing silently; the user can retry.
      setRunError("Couldn't start a run. Please try again.");
    }
  };

  return (
    <div className="hq-box bg-paper" data-testid={`project-workflow-${workflow.name}`}>
      <div className="flex justify-between">
        <b>{workflow.name}</b>
        <Pill>{`${workflow.nodes.length} node${workflow.nodes.length === 1 ? '' : 's'}`}</Pill>
      </div>
      {workflow.description && (
        <div className="mt-1 text-xs text-mut">{workflow.description}</div>
      )}

      <div className="mt-3">
        <WorkflowGraph nodes={workflow.nodes} runState={runState} />
      </div>

      {run && run.status !== 'running' && (
        <div
          className={`hq-box mt-3 text-xs ${
            run.status === 'failed' ? 'text-rose-600' : 'text-emerald-700'
          }`}
          role="status"
          data-testid={`run-banner-${workflow.name}`}
          data-status={run.status}
        >
          {run.status === 'failed' ? 'Run failed.' : 'Run complete.'}
        </div>
      )}

      {runError && (
        <div
          className="mt-2 text-xs text-rose-600"
          role="alert"
          data-testid={`run-error-${workflow.name}`}
        >
          {runError}
        </div>
      )}

      <div className="mt-3">
        <div className="text-[11px] uppercase tracking-wide text-faint">Brings agents</div>
        <div className="mt-1">
          {brings.map((a) => (
            <Pill key={a} className="mr-1">
              {a}
            </Pill>
          ))}
        </div>
      </div>

      <div className="mt-3 flex items-center justify-between">
        <span className="text-[11px] text-faint">enabled</span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid={`run-workflow-${workflow.name}`}
            disabled={starting || running}
            onClick={onRun}
          >
            {running ? 'Running…' : starting ? 'Starting…' : 'Run'}
          </button>
          <button
            type="button"
            className="hq-btn"
            data-testid={`disable-workflow-${workflow.name}`}
            onClick={onDisable}
          >
            Disable
          </button>
        </div>
      </div>
      <div className="mt-1 text-[11px] text-faint" data-testid={`disable-note-${workflow.name}`}>
        Disabling keeps its agents and skills enabled in this project.
      </div>
    </div>
  );
}
