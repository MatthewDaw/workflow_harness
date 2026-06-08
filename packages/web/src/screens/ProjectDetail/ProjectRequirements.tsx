import { Link, useParams } from 'react-router-dom';
import { useGetProjectQuery, useGetProjectRequirementsQuery } from '../../api/baseApi.js';
import { Bar, ScreenHeader } from '../../components/primitives.js';
import { MarkdownView } from '../../components/MarkdownView.js';
import { parseCompletion } from '../../lib/frontmatter.js';
import { extractCompliance } from '../../lib/compliance.js';
import { WireframePreview } from './ProjectWireframe.js';

/**
 * Project Requirements sub-tab (U10): the project's HIGH-LEVEL requirements,
 * sourced read-only from GitHub `docs/PRD.md`. The body is rendered markdown,
 * while the progress bar reflects the GitHub-sourced `completion` surfaced as
 * the project's `progressPct`. A full-screen reader lives at the
 * `requirements/full` route for distraction-free reading.
 */
export function ProjectRequirements() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: requirements, isLoading } = useGetProjectRequirementsQuery(projectId, {
    skip: !projectId,
  });

  const markdown = requirements?.markdown ?? '';
  // Prefer the doc's own `completion:` frontmatter so the bar matches the body
  // immediately; fall back to the project's stored progressPct (set on refresh).
  const pct = parseCompletion(markdown) ?? project?.progressPct ?? 0;
  const { report, body } = extractCompliance(markdown);

  return (
    <div className="hq-pad" data-testid="project-requirements">
      <div className="mb-3 flex items-start justify-between gap-3">
        <ScreenHeader title="Project Requirements" subtitle="from GitHub docs/PRD.md (read-only)" />
        <div className="flex shrink-0 gap-2">
          <Link to="full" className="hq-btn">
            ⤢ Full screen
          </Link>
        </div>
      </div>

      <div className="mb-3.5 hq-box bg-paper">
        <div className="text-[11px] uppercase tracking-wide text-faint">
          Completion (from GitHub docs/PRD.md) · {pct}%
        </div>
        <div className="mt-1.5">
          <Bar pct={pct} />
        </div>
      </div>

      {report !== null && (
        <div className="mb-3.5 hq-box bg-paper" data-testid="compliance-panel">
          <div className="mb-1.5 text-[11px] uppercase tracking-wide text-faint">
            Compliance breakdown
          </div>
          <MarkdownView markdown={report} />
        </div>
      )}

      <WireframePreview />

      <div className="hq-box bg-paper">
        {isLoading ? (
          <div className="text-mut">Loading…</div>
        ) : markdown ? (
          <MarkdownView markdown={body} />
        ) : (
          <div className="text-mut">No docs/PRD.md found.</div>
        )}
      </div>
    </div>
  );
}

/**
 * Full-screen reader for the project requirements (U10). Read-only,
 * distraction-free; reached from the `⤢ Full screen` action. Sourced from
 * GitHub `docs/PRD.md`.
 */
export function ProjectRequirementsFull() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data: requirements, isLoading } = useGetProjectRequirementsQuery(projectId, {
    skip: !projectId,
  });
  const markdown = requirements?.markdown ?? '';
  const { report, body } = extractCompliance(markdown);

  return (
    <div className="hq-pad" data-testid="project-requirements-full">
      <div className="mb-3 flex items-center justify-between gap-3">
        <ScreenHeader
          title={`${project?.name ?? projectId} — Requirements`}
          subtitle="Full-screen reader · from GitHub docs/PRD.md (read-only)"
        />
        <Link to=".." relative="path" className="hq-btn shrink-0">
          ← Back
        </Link>
      </div>
      {report !== null && (
        <div className="mb-3.5 hq-box bg-paper" data-testid="compliance-panel">
          <div className="mb-1.5 text-[11px] uppercase tracking-wide text-faint">
            Compliance breakdown
          </div>
          <MarkdownView markdown={report} />
        </div>
      )}
      <div className="hq-box bg-paper">
        {isLoading ? (
          <div className="text-mut">Loading…</div>
        ) : markdown ? (
          <MarkdownView markdown={body} />
        ) : (
          <div className="text-mut">No docs/PRD.md found.</div>
        )}
      </div>
    </div>
  );
}
