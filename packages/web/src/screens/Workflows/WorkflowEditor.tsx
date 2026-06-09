import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import type { Workflow, WorkflowNode } from '@harness/shared';
import { orgScope } from '@harness/shared';
import {
  useGetWorkflowsQuery,
  useGetAgentsQuery,
  useSaveWorkflowMutation,
} from '../../api/baseApi.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { ScreenHeader } from '../../components/primitives.js';
import { SkillCombobox } from '../../components/SkillCombobox.js';
import { WorkflowGraph, layerNodes } from '../../components/WorkflowGraph.js';

/**
 * Workflow editor (collapsed model): name + description + a list of agent nodes
 * forming a DAG. There is no scope selector — the server forces org scope.
 * Reached at /workflows/new (create) and /workflows/:name/edit (edit). A live
 * <WorkflowGraph> preview renders the current draft, and the draft is validated
 * client-side (unique ids, resolvable refs, no cycles) before save.
 */
export function WorkflowEditor() {
  const { name } = useParams();
  const navigate = useNavigate();
  const { user } = useAuth();
  const org = user?.org ?? '';

  const { data: workflows } = useGetWorkflowsQuery();
  const { data: agents } = useGetAgentsQuery();
  const [saveWorkflow, { isLoading: saving }] = useSaveWorkflowMutation();

  const existing = useMemo(
    () => (name ? (workflows ?? []).find((w) => w.name === name) : undefined),
    [workflows, name],
  );
  const isEdit = Boolean(name);

  const [draft, setDraft] = useState<Workflow | null>(null);
  // Seed the draft from the existing workflow once it loads (edit), or a blank
  // skeleton (create). State init runs before workflows resolve, so derive lazily.
  const workflow: Workflow = draft ??
    existing ?? {
      name: name ?? '',
      scope: orgScope(org),
      kind: 'workflow',
      description: '',
      nodes: [],
    };

  const set = (patch: Partial<Workflow>) => setDraft({ ...workflow, ...patch });
  const setNodes = (nodes: WorkflowNode[]) => set({ nodes });

  // The catalog of agents a node can point to (combobox options).
  const agentOptions = useMemo(
    () => (agents ?? []).map((a) => ({ name: a.name, hint: a.model })),
    [agents],
  );

  const addNode = () => {
    // Mint a unique default id (node1, node2, …) that doesn't collide.
    const taken = new Set(workflow.nodes.map((n) => n.id));
    let i = workflow.nodes.length + 1;
    while (taken.has(`node${i}`)) i += 1;
    const node: WorkflowNode = {
      id: `node${i}`,
      agent: '',
      label: '',
      prompt: '',
      dependsOn: [],
    };
    setNodes([...workflow.nodes, node]);
  };

  const removeNode = (idx: number) => {
    const removed = workflow.nodes[idx];
    const next = workflow.nodes
      .filter((_, i) => i !== idx)
      // Drop any references to the removed node so the graph stays resolvable.
      .map((n) => ({
        ...n,
        dependsOn: n.dependsOn.filter((d) => d !== removed?.id),
        rerun:
          n.rerun?.mode === 'declared-by' && n.rerun.declaredBy === removed?.id
            ? { ...n.rerun, declaredBy: undefined }
            : n.rerun,
      }));
    setNodes(next);
  };

  const updateNode = (idx: number, patch: Partial<WorkflowNode>) => {
    setNodes(workflow.nodes.map((n, i) => (i === idx ? { ...n, ...patch } : n)));
  };

  // Client-side validation, mirroring the shared superRefine: unique ids, every
  // dependsOn / declaredBy resolves to a node id, and the dependsOn graph is
  // acyclic. Save is blocked with an inline error while this returns a message.
  const validationError = useMemo(() => validateDraft(workflow), [workflow]);

  const onSave = async () => {
    if (!workflow.name.trim() || validationError) return;
    // On create do not send scope — the server forces org scope and stamps
    // createdBy. Send only the editable fields.
    const { scope: _scope, createdBy: _createdBy, ...rest } = workflow;
    await saveWorkflow(rest as Workflow).unwrap();
    navigate('/workflows');
  };

  return (
    <div className="hq-pad" data-testid="workflow-editor">
      <ScreenHeader
        title={isEdit ? `Edit workflow · ${workflow.name}` : 'New workflow'}
        subtitle="Compose a DAG of catalog agents. Each node runs an agent; edges are dependencies; a node may rerun until done."
      />

      <div className="hq-box bg-paper">
        <label className="block text-xs font-medium text-mut">Name</label>
        <input
          className="hq-input mt-1 w-full"
          data-testid="workflow-name"
          value={workflow.name}
          disabled={isEdit}
          onChange={(e) => set({ name: e.target.value })}
        />

        <label className="mt-3 block text-xs font-medium text-mut">Description</label>
        <textarea
          className="hq-input mt-1 w-full"
          data-testid="workflow-description"
          rows={2}
          value={workflow.description}
          onChange={(e) => set({ description: e.target.value })}
        />

        <div className="mt-4 flex items-center justify-between">
          <div className="text-xs font-medium text-mut">Nodes (the DAG)</div>
          <button
            type="button"
            className="hq-btn"
            data-testid="workflow-add-node"
            onClick={addNode}
          >
            + Add node
          </button>
        </div>

        {workflow.nodes.length === 0 && (
          <div className="mt-2 text-xs text-faint" data-testid="workflow-no-nodes">
            No nodes yet — add an agent node to start the DAG.
          </div>
        )}

        <div className="mt-2 flex flex-col gap-3">
          {workflow.nodes.map((node, idx) => (
            <NodeEditor
              key={idx}
              node={node}
              others={workflow.nodes.filter((_, i) => i !== idx)}
              agentOptions={agentOptions}
              onChange={(patch) => updateNode(idx, patch)}
              onRemove={() => removeNode(idx)}
            />
          ))}
        </div>

        <div className="hq-hr" />
        <div className="text-xs font-medium text-mut">Live preview</div>
        <div className="mt-2" data-testid="workflow-preview">
          <WorkflowGraph nodes={workflow.nodes} />
        </div>

        {validationError && (
          <div
            className="mt-3 rounded border border-[#b5402f] bg-[#f3ddd6] px-2 py-1 text-xs text-[#b5402f]"
            data-testid="workflow-validation-error"
          >
            {validationError}
          </div>
        )}

        <div className="hq-hr" />
        <div className="flex gap-2">
          <button
            type="button"
            className="hq-btn hq-btn-pri"
            data-testid="workflow-save"
            disabled={saving || !workflow.name.trim() || Boolean(validationError)}
            onClick={onSave}
          >
            Save &amp; sync
          </button>
          <button
            type="button"
            className="hq-btn"
            data-testid="workflow-cancel"
            onClick={() => navigate('/workflows')}
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/**
 * One node's editor row: its id, the agent it runs (a combobox over the agent
 * catalog), a label, a prompt, its `dependsOn` upstream nodes (multi-toggle of
 * the OTHER node ids), and an optional rerun rule (mode + end-criteria /
 * declared-by checker + max runs).
 */
function NodeEditor({
  node,
  others,
  agentOptions,
  onChange,
  onRemove,
}: {
  node: WorkflowNode;
  others: WorkflowNode[];
  agentOptions: { name: string; hint?: string }[];
  onChange: (patch: Partial<WorkflowNode>) => void;
  onRemove: () => void;
}) {
  const toggleDep = (depId: string) => {
    const has = node.dependsOn.includes(depId);
    onChange({
      dependsOn: has
        ? node.dependsOn.filter((d) => d !== depId)
        : [...node.dependsOn, depId],
    });
  };

  const setRerunMode = (value: '' | 'self' | 'declared-by') => {
    if (value === '') {
      onChange({ rerun: undefined });
      return;
    }
    const prev = node.rerun;
    onChange({
      rerun: {
        mode: value,
        endCriteria: prev?.endCriteria ?? '',
        declaredBy: prev?.declaredBy,
        maxRuns: prev?.maxRuns ?? 10,
      },
    });
  };

  const setRerun = (patch: Partial<NonNullable<WorkflowNode['rerun']>>) => {
    if (!node.rerun) return;
    onChange({ rerun: { ...node.rerun, ...patch } });
  };

  return (
    <div className="hq-box bg-odd" data-testid={`workflow-node-editor-${node.id}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <label className="text-[11px] text-faint">id</label>
          <input
            className="hq-input w-40 font-mono text-[12px]"
            data-testid={`node-id-${node.id}`}
            value={node.id}
            onChange={(e) => onChange({ id: e.target.value })}
          />
        </div>
        <button
          type="button"
          className="hq-btn"
          data-testid={`node-remove-${node.id}`}
          onClick={onRemove}
        >
          ✕ Remove
        </button>
      </div>

      <div className="mt-2 flex items-center gap-2">
        <label className="text-[11px] text-faint">agent</label>
        <SkillCombobox
          options={agentOptions}
          placeholder={node.agent || 'Search agents…'}
          testid={`node-agent-${node.id}`}
          emptyHint="No matching agents — register one with /hq-add-agent first"
          onSelect={(agent) => onChange({ agent })}
        />
        {node.agent && (
          <span className="font-mono text-[11px] text-mut" data-testid={`node-agent-current-${node.id}`}>
            {node.agent}
          </span>
        )}
      </div>

      <label className="mt-2 block text-[11px] text-faint">label</label>
      <input
        className="hq-input mt-1 w-full"
        data-testid={`node-label-${node.id}`}
        value={node.label}
        onChange={(e) => onChange({ label: e.target.value })}
      />

      <label className="mt-2 block text-[11px] text-faint">prompt</label>
      <textarea
        className="hq-input mt-1 w-full"
        data-testid={`node-prompt-${node.id}`}
        rows={2}
        value={node.prompt}
        onChange={(e) => onChange({ prompt: e.target.value })}
      />

      <div className="mt-2 text-[11px] text-faint">depends on</div>
      <div className="mt-1 flex flex-wrap gap-1.5" data-testid={`node-depends-${node.id}`}>
        {others.length === 0 && (
          <span className="text-xs text-faint">No other nodes to depend on.</span>
        )}
        {others.map((o) => {
          const on = node.dependsOn.includes(o.id);
          return (
            <button
              key={o.id}
              type="button"
              className={`hq-btn ${on ? 'hq-btn-pri' : ''}`}
              data-testid={`node-dep-${node.id}-${o.id}`}
              aria-pressed={on}
              onClick={() => toggleDep(o.id)}
            >
              {on ? '✓ ' : '+ '}
              {o.id}
            </button>
          );
        })}
      </div>

      <div className="mt-2 flex items-center gap-2">
        <label className="text-[11px] text-faint">rerun</label>
        <select
          className="hq-btn normal-case"
          data-testid={`node-rerun-mode-${node.id}`}
          value={node.rerun?.mode ?? ''}
          onChange={(e) => setRerunMode(e.target.value as '' | 'self' | 'declared-by')}
        >
          <option value="">none</option>
          <option value="self">self (end-criteria)</option>
          <option value="declared-by">declared-by (checker)</option>
        </select>
      </div>

      {node.rerun && (
        <div className="mt-2 flex flex-col gap-2">
          {node.rerun.mode === 'self' && (
            <div>
              <label className="block text-[11px] text-faint">end-criteria</label>
              <textarea
                className="hq-input mt-1 w-full"
                data-testid={`node-rerun-criteria-${node.id}`}
                rows={2}
                value={node.rerun.endCriteria}
                onChange={(e) => setRerun({ endCriteria: e.target.value })}
              />
            </div>
          )}
          {node.rerun.mode === 'declared-by' && (
            <div className="flex items-center gap-2">
              <label className="text-[11px] text-faint">declared by</label>
              <select
                className="hq-btn normal-case"
                data-testid={`node-rerun-declaredby-${node.id}`}
                value={node.rerun.declaredBy ?? ''}
                onChange={(e) => setRerun({ declaredBy: e.target.value || undefined })}
              >
                <option value="">— pick checker node —</option>
                {others.map((o) => (
                  <option key={o.id} value={o.id}>
                    {o.id}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="flex items-center gap-2">
            <label className="text-[11px] text-faint">max runs</label>
            <input
              type="number"
              min={1}
              className="hq-input w-24"
              data-testid={`node-rerun-maxruns-${node.id}`}
              value={node.rerun.maxRuns}
              onChange={(e) => setRerun({ maxRuns: Number(e.target.value) })}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * Validate the draft DAG client-side, mirroring the shared `workflowSchema`
 * superRefine so save is blocked before the round-trip. Returns a human-readable
 * message for the first problem found, or `null` when the draft is valid.
 */
function validateDraft(workflow: Workflow): string | null {
  const nodes = workflow.nodes;
  if (nodes.length === 0) return 'Add at least one agent node.';

  const ids = new Set<string>();
  for (const node of nodes) {
    if (!node.id.trim()) return 'Every node needs a non-empty id.';
    if (ids.has(node.id)) return `Duplicate node id '${node.id}'.`;
    ids.add(node.id);
  }

  for (const node of nodes) {
    if (!node.agent.trim()) return `Node '${node.id}' needs an agent.`;
    for (const dep of node.dependsOn) {
      if (!ids.has(dep)) return `Node '${node.id}' depends on unknown node '${dep}'.`;
    }
    if (node.rerun?.mode === 'declared-by') {
      const checker = node.rerun.declaredBy;
      if (!checker || !ids.has(checker)) {
        return `Node '${node.id}' is declared-by an unknown checker node.`;
      }
    }
  }

  // Acyclicity: layerNodes leaves cycle members `unsorted` (it excludes dangling
  // refs / self-loops, which we already flagged above).
  const { unsorted } = layerNodes(nodes);
  if (unsorted.length > 0) return 'The dependsOn graph has a cycle.';

  return null;
}
