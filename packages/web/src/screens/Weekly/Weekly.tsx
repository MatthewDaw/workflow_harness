import { Link } from 'react-router-dom';
import { useGetProjectsQuery } from '../../api/baseApi.js';
import { ScreenHeader } from '../../components/primitives.js';

/**
 * Top-level Weekly entry (U19): weekly updates are project-scoped, so this lists
 * projects and links into each project's Weekly sub-tab (U25).
 */
export function Weekly() {
  const { data } = useGetProjectsQuery();
  const projects = data ?? [];

  return (
    <div className="hq-pad" data-testid="weekly-screen">
      <ScreenHeader
        title="Weekly Updates"
        subtitle="Weekly updates are authored per project. Pick a project to view or draft its update."
      />
      {projects.length === 0 && <div className="hq-box text-mut">No projects yet.</div>}
      <div className="grid grid-cols-3 gap-3.5">
        {projects.map((p) => (
          <Link
            key={p.id}
            to={`/projects/${p.id}/weekly`}
            className="hq-box bg-paper no-underline text-ink"
          >
            <b>{p.name}</b>
            <div className="mt-1.5 text-xs text-mut">Open weekly update ›</div>
          </Link>
        ))}
      </div>
    </div>
  );
}
