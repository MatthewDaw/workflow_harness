import { useEffect, useMemo, useState, type FormEvent } from 'react';
import {
  OBJECTIVE_LEVELS,
  type DefinitionOfDone,
  type ObjectiveLevel,
  type ObjectiveNode,
} from '@harness/shared';
import { useAuth } from '../../auth/AuthProvider.js';
import {
  useGetObjectivesQuery,
  useCreateObjectiveMutation,
  useDeleteObjectiveMutation,
  useGetDodQuery,
  usePutDodMutation,
} from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/** Group nodes by parent so the RCDO tree can render recursively. */
function childrenOf(nodes: ObjectiveNode[], parentId: string | undefined): ObjectiveNode[] {
  return nodes.filter((n) => n.parentId === parentId);
}

const LEVEL_LABEL: Record<ObjectiveLevel, string> = {
  rally_cry: '◆ Rally Cry',
  defining_objective: 'Defining Objective',
  outcome: 'Outcome',
  supporting_outcome: 'Supporting Outcome',
};

function Node({
  node,
  all,
  isAdmin,
  onEdit,
  onDelete,
}: {
  node: ObjectiveNode;
  all: ObjectiveNode[];
  isAdmin: boolean;
  onEdit: (node: ObjectiveNode) => void;
  onDelete: (node: ObjectiveNode) => void;
}) {
  const kids = childrenOf(all, node.id);
  const label = LEVEL_LABEL[node.level];
  return (
    <div className="my-2.5" data-testid={`objective-node-${node.id}`}>
      <div className="flex items-center justify-between">
        <span>
          <Pill className="mr-2">{label}</Pill>
          <span className="text-sm">{node.title}</span>
        </span>
        <span className="flex items-center gap-2">
          {isAdmin && (
            <>
              <button
                type="button"
                className="text-xs text-mut hover:text-cream"
                data-testid={`objective-edit-${node.id}`}
                onClick={() => onEdit(node)}
              >
                edit
              </button>
              <button
                type="button"
                className="text-xs text-mut hover:text-live"
                data-testid={`objective-delete-${node.id}`}
                onClick={() => onDelete(node)}
              >
                delete
              </button>
            </>
          )}
          <span className="text-xs text-faint">{node.pct ?? 0}%</span>
        </span>
      </div>
      <div className="my-2">
        <Bar pct={node.pct ?? 0} />
      </div>
      {kids.length > 0 && (
        <div className="ml-3.5 border-l border-line2 pl-3.5">
          {kids.map((k) => (
            <Node
              key={k.id}
              node={k}
              all={all}
              isAdmin={isAdmin}
              onEdit={onEdit}
              onDelete={onDelete}
            />
          ))}
        </div>
      )}
    </div>
  );
}

/** Admin form to add a new objective node: pick level + title + parent. */
function AddNodeForm({ all }: { all: ObjectiveNode[] }) {
  const [level, setLevel] = useState<ObjectiveLevel>('rally_cry');
  const [title, setTitle] = useState('');
  const [parentId, setParentId] = useState('');
  const [createObjective, { isLoading }] = useCreateObjectiveMutation();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const t = title.trim();
    if (!t) return;
    await createObjective({
      id: `obj-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 7)}`,
      level,
      title: t,
      parentId: parentId || undefined,
    });
    setTitle('');
    setParentId('');
  };

  return (
    <form
      className="hq-box mb-3 flex flex-wrap items-end gap-2 bg-paper"
      data-testid="objective-add-form"
      onSubmit={submit}
    >
      <label className="flex flex-col text-xs text-mut">
        Level
        <select
          className="hq-input mt-1"
          data-testid="objective-level"
          value={level}
          onChange={(e) => setLevel(e.target.value as ObjectiveLevel)}
        >
          {OBJECTIVE_LEVELS.map((l) => (
            <option key={l} value={l}>
              {LEVEL_LABEL[l]}
            </option>
          ))}
        </select>
      </label>
      <label className="flex flex-1 flex-col text-xs text-mut">
        Title
        <input
          className="hq-input mt-1"
          data-testid="objective-title"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="New objective title"
        />
      </label>
      <label className="flex flex-col text-xs text-mut">
        Parent
        <select
          className="hq-input mt-1"
          data-testid="objective-parent"
          value={parentId}
          onChange={(e) => setParentId(e.target.value)}
        >
          <option value="">(none — root)</option>
          {all.map((n) => (
            <option key={n.id} value={n.id}>
              {LEVEL_LABEL[n.level]}: {n.title}
            </option>
          ))}
        </select>
      </label>
      <button
        type="submit"
        className="hq-btn"
        data-testid="objective-add"
        disabled={isLoading || !title.trim()}
      >
        Add node
      </button>
    </form>
  );
}

/**
 * The org-wide Definition of Done (plan-mapping feature 1). Shows the current
 * DoD read-only, and — for admins — an inline editor (two checkboxes + notes)
 * that saves via `putDod`. It is ADVISORY: it declares what `/update-progress`
 * verifies before marking work done; it never blocks a progress push.
 */
function DefinitionOfDoneCard({ isAdmin }: { isAdmin: boolean }) {
  const { data: dod, isLoading } = useGetDodQuery();
  const [putDod, { isLoading: isSaving }] = usePutDodMutation();

  const [requiresUnitTests, setRequiresUnitTests] = useState(true);
  const [requiresProdE2E, setRequiresProdE2E] = useState(false);
  const [notes, setNotes] = useState('');

  // Hydrate the editor from the served DoD once it loads (and on refetch).
  useEffect(() => {
    if (!dod) return;
    setRequiresUnitTests(dod.requiresUnitTests);
    setRequiresProdE2E(dod.requiresProdE2E);
    setNotes(dod.notes ?? '');
  }, [dod]);

  const save = async (e: FormEvent) => {
    e.preventDefault();
    const next: DefinitionOfDone = {
      requiresUnitTests,
      requiresProdE2E,
      ...(notes.trim() ? { notes: notes.trim() } : {}),
    };
    await putDod(next);
  };

  return (
    <div className="hq-box mb-4 bg-paper" data-testid="dod-card">
      <div className="mb-1 flex items-center gap-2">
        <Pill>Definition of Done</Pill>
        <span className="text-xs text-faint">advisory — never blocks a progress push</span>
      </div>
      {isLoading && <div className="text-mut">Loading Definition of Done…</div>}
      {dod && (
        <>
          {/* Read-only summary of the current DoD. */}
          <div className="my-2 text-sm" data-testid="dod-display">
            <span className="text-mut">To mark work complete: </span>
            <span data-testid="dod-summary">
              {dod.requiresUnitTests ? 'unit tests passing' : 'no unit-test gate'}
              {dod.requiresProdE2E ? ' + prod-E2E verified' : ''}
            </span>
            {dod.notes && <div className="mt-1 text-xs text-mut">{dod.notes}</div>}
          </div>

          {isAdmin && (
            <form
              className="mt-3 flex flex-col gap-2 border-t border-line2 pt-3"
              data-testid="dod-form"
              onSubmit={save}
            >
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid="dod-unit-tests"
                  checked={requiresUnitTests}
                  onChange={(e) => setRequiresUnitTests(e.target.checked)}
                />
                Require unit tests passing (org-wide floor)
              </label>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  data-testid="dod-prod-e2e"
                  checked={requiresProdE2E}
                  onChange={(e) => setRequiresProdE2E(e.target.checked)}
                />
                Require prod-E2E verified
              </label>
              <label className="flex flex-col text-xs text-mut">
                Notes (optional)
                <textarea
                  className="hq-input mt-1"
                  data-testid="dod-notes"
                  rows={2}
                  value={notes}
                  onChange={(e) => setNotes(e.target.value)}
                  placeholder="e.g. how to find the prod E2E suite"
                />
              </label>
              <button
                type="submit"
                className="hq-btn self-start"
                data-testid="dod-save"
                disabled={isSaving}
              >
                Save Definition of Done
              </button>
            </form>
          )}
        </>
      )}
    </div>
  );
}

export function Objectives() {
  const { user } = useAuth();
  // v1 single-admin model (mirrors Agents): the signed-in user is the admin.
  const isAdmin = Boolean(user);
  const { data, isLoading, isError } = useGetObjectivesQuery();
  const all = data ?? [];
  const roots = useMemo(() => childrenOf(all, undefined), [all]);
  const [createObjective] = useCreateObjectiveMutation();
  const [deleteObjective] = useDeleteObjectiveMutation();

  const onEdit = async (node: ObjectiveNode) => {
    const next = window.prompt('Edit objective title', node.title);
    if (next === null) return;
    const title = next.trim();
    if (!title || title === node.title) return;
    // Re-posting with the node's existing id updates it in place.
    await createObjective({ id: node.id, level: node.level, title, parentId: node.parentId });
  };

  const onDelete = async (node: ObjectiveNode) => {
    if (!window.confirm(`Delete “${node.title}”?`)) return;
    await deleteObjective(node.id);
  };

  return (
    <div className="hq-pad" data-testid="objectives-screen">
      <ScreenHeader
        title="Company Objectives (RCDO)"
        subtitle="Rally Cries → Defining Objectives → Outcomes → Supporting Outcomes. Roll-ups are computed bottom-up from linked work."
      />
      <DefinitionOfDoneCard isAdmin={isAdmin} />
      {isAdmin && <AddNodeForm all={all} />}
      {isLoading && <div className="text-mut">Loading objectives…</div>}
      {isError && (
        <div className="hq-note" role="alert">
          Could not load objectives. Showing an empty tree.
        </div>
      )}
      {!isLoading && roots.length === 0 && (
        <div className="hq-box text-mut">No objectives yet. Add a Rally Cry to get started.</div>
      )}
      {roots.map((r) => (
        <div key={r.id} className="hq-box mb-3 border-l-[3px] border-l-[#8a6fb5] bg-paper">
          <Node node={r} all={all} isAdmin={isAdmin} onEdit={onEdit} onDelete={onDelete} />
        </div>
      ))}
    </div>
  );
}
