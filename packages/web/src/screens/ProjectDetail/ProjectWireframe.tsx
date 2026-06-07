import { Link, useParams } from 'react-router-dom';
import { useGetProjectQuery, useGetProjectWireframeQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * Compact, non-interactive preview of the project's `docs/wireframe.html`,
 * shown on the Project Requirements tab. The wireframe is a full HTML document,
 * so it renders inside a sandboxed iframe (no scripts, no same-origin); the
 * whole card is a link to the full-screen `wireframe` route. Renders nothing
 * when the repo has no wireframe so the tab stays clean.
 */
export function WireframePreview() {
  const { projectId = '' } = useParams();
  const { data } = useGetProjectWireframeQuery(projectId, { skip: !projectId });
  const html = data?.html ?? '';
  if (!html) return null;

  return (
    <div className="mb-3.5 hq-box bg-paper" data-testid="wireframe-preview">
      <div className="mb-1.5 flex items-center justify-between gap-3">
        <div className="text-[11px] uppercase tracking-wide text-faint">
          Wireframe (from GitHub docs/wireframe.html)
        </div>
        <Link to="wireframe" className="hq-btn shrink-0">
          ⤢ Open wireframe
        </Link>
      </div>
      <Link
        to="wireframe"
        className="group relative block overflow-hidden rounded border border-line"
        aria-label="Open the full wireframe"
      >
        {/* The preview is a scaled-down snapshot; the overlay swallows clicks so
            the whole tile behaves as a single link into the full-screen view. */}
        <iframe
          title="Wireframe preview"
          srcDoc={html}
          sandbox=""
          tabIndex={-1}
          aria-hidden="true"
          className="pointer-events-none h-[220px] w-full origin-top-left"
          style={{ width: '250%', height: '550px', transform: 'scale(0.4)' }}
        />
        <span className="absolute inset-0" />
      </Link>
    </div>
  );
}

/**
 * Full-screen reader for the project's `docs/wireframe.html`. Reached from the
 * Project Requirements wireframe preview. The wireframe is rendered in a
 * sandboxed iframe so its own styles apply without leaking into the app shell.
 */
export function ProjectWireframeFull() {
  const { projectId = '' } = useParams();
  const { data: project } = useGetProjectQuery(projectId, { skip: !projectId });
  const { data, isLoading } = useGetProjectWireframeQuery(projectId, { skip: !projectId });
  const html = data?.html ?? '';

  return (
    <div className="hq-pad" data-testid="project-wireframe-full">
      <div className="mb-3 flex items-center justify-between gap-3">
        <ScreenHeader
          title={`${project?.name ?? projectId} — Wireframe`}
          subtitle="Full-screen · from GitHub docs/wireframe.html (read-only)"
        />
        <Link to=".." relative="path" className="hq-btn shrink-0">
          ← Back
        </Link>
      </div>
      <div className="hq-box bg-paper p-0">
        {isLoading ? (
          <div className="hq-pad text-mut">Loading…</div>
        ) : html ? (
          <iframe
            title="Wireframe"
            srcDoc={html}
            sandbox=""
            className="h-[80vh] w-full rounded border-0"
          />
        ) : (
          <div className="hq-pad text-mut">No docs/wireframe.html found.</div>
        )}
      </div>
    </div>
  );
}
