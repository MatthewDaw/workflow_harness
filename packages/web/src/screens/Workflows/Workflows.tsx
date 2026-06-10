import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Workflow } from '@harness/shared';
import { useGetWorkflowsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';
import { WorkflowGraph } from '../../components/WorkflowGraph.js';
import { OverlayModal } from '../../components/OverlayModal.js';
import { useAuthorFilter, AuthorSelect } from '../../components/AuthorFilter.js';
import { authorOf } from '../../lib/catalogUi.js';

/** Workflows registry (collapsed model): a single flat org catalog of DAGs. */
export function Workflows() {
  const { data, isLoading } = useGetWorkflowsQuery();
  const workflows = data ?? [];

  const { author, setAuthor, authors, matches } = useAuthorFilter(workflows, authorOf);

  // Order is stable so the catalog stays steady (mirrors Agents/SkillCatalog).
  const catalog = workflows.filter(matches);

  return (
    <div className="hq-pad" data-testid="workflows-screen">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="Workflows (org catalog)"
          subtitle="The shared org library of agent DAGs. Enable workflows per-project from a project's Workflows tab."
        />
        <Link
          to="/workflows/new"
          className="hq-btn hq-btn-pri no-underline"
          data-testid="new-workflow"
        >
          + New workflow
        </Link>
      </div>
      <div className="mb-3 flex flex-wrap items-center gap-4 text-xs text-mut">
        <AuthorSelect
          author={author}
          onChange={setAuthor}
          authors={authors}
          testid="workflow-author-filter"
        />
      </div>
      {isLoading && <div className="text-mut">Loading workflows…</div>}
      <div className="grid grid-cols-3 gap-3.5" data-testid="workflow-catalog-grid">
        {catalog.map((w) => (
          <WorkflowCard key={w.name} workflow={w} />
        ))}
      </div>
    </div>
  );
}

export function WorkflowCard({ workflow }: { workflow: Workflow }) {
  const count = workflow.nodes.length;
  return (
    <div className="hq-box bg-paper" data-testid={`workflow-card-${workflow.name}`}>
      <div className="flex justify-between">
        <Link
          to={`/workflows/${encodeURIComponent(workflow.name)}/edit`}
          className="text-ink no-underline"
        >
          <b>{workflow.name}</b>
        </Link>
        <Pill>
          {count} node{count === 1 ? '' : 's'}
        </Pill>
      </div>
      {workflow.description && (
        <div
          className="my-1.5 text-xs text-ink"
          data-testid={`workflow-description-${workflow.name}`}
        >
          {workflow.description}
        </div>
      )}
      <WorkflowGraphPreview workflow={workflow} />
      <div
        className="mt-1.5 text-[11px] text-faint"
        data-testid={`workflow-author-${workflow.name}`}
      >
        by {authorOf(workflow)}
      </div>
    </div>
  );
}

/**
 * The card keeps only the workflow's description (rendered by the caller); the
 * full DAG lives behind an Expand button that opens it in a fullscreen overlay,
 * mirroring how an agent card hides its full registration. Renders nothing when
 * the workflow has no nodes to show.
 */
function WorkflowGraphPreview({ workflow }: { workflow: Workflow }) {
  const [open, setOpen] = useState(false);
  if (workflow.nodes.length === 0) return null;

  return (
    <>
      <button
        type="button"
        className="hq-btn mt-1.5"
        data-testid={`workflow-expand-${workflow.name}`}
        onClick={() => setOpen(true)}
      >
        ⤢ Expand
      </button>
      {open && <WorkflowGraphModal workflow={workflow} onClose={() => setOpen(false)} />}
    </>
  );
}

/**
 * Fullscreen overlay rendering a workflow's full DAG through the shared
 * <WorkflowGraph> — the same rendering the editor preview and project cards use.
 */
function WorkflowGraphModal({
  workflow,
  onClose,
}: {
  workflow: Workflow;
  onClose: () => void;
}) {
  return (
    <OverlayModal
      ariaLabel={`${workflow.name} workflow`}
      testid={`workflow-modal-${workflow.name}`}
      closeTestid={`workflow-modal-close-${workflow.name}`}
      onClose={onClose}
      header={
        <span className="flex items-center gap-2">
          <b>{workflow.name}</b>
          <Pill>
            {workflow.nodes.length} node{workflow.nodes.length === 1 ? '' : 's'}
          </Pill>
        </span>
      }
    >
      {workflow.description && <div className="mb-3 text-xs text-mut">{workflow.description}</div>}
      <WorkflowGraph nodes={workflow.nodes} />
    </OverlayModal>
  );
}
