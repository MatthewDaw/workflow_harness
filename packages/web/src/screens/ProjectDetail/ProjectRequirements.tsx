import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  useGetProjectQuery,
  useGetProjectRequirementsQuery,
  usePutProjectRequirementsMutation,
} from '../../api/baseApi.js';
import { Bar, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';

/**
 * Project Requirements sub-tab (U10): the project's HIGH-LEVEL, HQ-owned
 * requirements. The body is HQ-owned markdown (edited here, stored in HQ — NOT
 * written back to GitHub), while the progress bar reflects the GitHub-sourced
 * `completion` surfaced as the project's `progressPct`.
 *
 * `✎ Edit` flips to an inline HQ-owned editor that saves via
 * PUT /projects/:id/requirements. A full-screen reader lives at the
 * `requirements/full` route for distraction-free reading.
 */
export function ProjectRequirements() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: requirements, isLoading } = useGetProjectRequirementsQuery(projectId, {
    skip: !projectId,
  });
  const [save, { isLoading: isSaving }] = usePutProjectRequirementsMutation();

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const markdown = requirements?.markdown ?? '';
  const pct = project?.progressPct ?? 0;

  function startEdit() {
    setDraft(markdown);
    setEditing(true);
  }

  async function onSave() {
    await save({ projectId, markdown: draft }).unwrap();
    setEditing(false);
  }

  return (
    <div className="hq-pad" data-testid="project-requirements">
      <div className="mb-3 flex items-start justify-between gap-3">
        <ScreenHeader
          title="Project Requirements"
          subtitle="High-level, HQ-owned requirements for this project."
        />
        <div className="flex shrink-0 gap-2">
          <Link to="full" className="hq-btn">
            ⤢ Full screen
          </Link>
          {!editing && (
            <button type="button" className="hq-btn" onClick={startEdit}>
              ✎ Edit
            </button>
          )}
        </div>
      </div>

      <div className="mb-3.5 hq-box bg-paper">
        <div className="text-[11px] uppercase tracking-wide text-faint">
          Completion (from GitHub) · {pct}%
        </div>
        <div className="mt-1.5">
          <Bar pct={pct} />
        </div>
      </div>

      {editing ? (
        <div className="hq-box bg-paper" data-testid="requirements-editor">
          <textarea
            className="hq-textarea h-80 w-full resize-y bg-paper2 p-2 font-mono text-[12.5px] text-ink"
            aria-label="Requirements markdown"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="mt-2 flex gap-2">
            <button
              type="button"
              className="hq-btn hq-btn-pri"
              onClick={onSave}
              disabled={isSaving}
            >
              {isSaving ? 'Saving…' : 'Save'}
            </button>
            <button type="button" className="hq-btn" onClick={() => setEditing(false)}>
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <div className="hq-box bg-paper">
          {isLoading ? (
            <div className="text-mut">Loading…</div>
          ) : markdown ? (
            <MarkdownView markdown={markdown} />
          ) : (
            <div className="text-mut">No requirements yet. Click ✎ Edit to add them.</div>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * Full-screen reader for the HQ-owned project requirements (U10). Read-only,
 * distraction-free; reached from the `⤢ Full screen` action.
 */
export function ProjectRequirementsFull() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: requirements, isLoading } = useGetProjectRequirementsQuery(projectId, {
    skip: !projectId,
  });
  const markdown = requirements?.markdown ?? '';

  return (
    <div className="hq-pad" data-testid="project-requirements-full">
      <div className="mb-3 flex items-center justify-between gap-3">
        <ScreenHeader
          title={`${project?.name ?? projectId} — Requirements`}
          subtitle="Full-screen reader (read-only)."
        />
        <Link to=".." relative="path" className="hq-btn shrink-0">
          ← Back
        </Link>
      </div>
      <div className="hq-box bg-paper">
        {isLoading ? (
          <div className="text-mut">Loading…</div>
        ) : markdown ? (
          <MarkdownView markdown={markdown} />
        ) : (
          <div className="text-mut">No requirements yet.</div>
        )}
      </div>
    </div>
  );
}
