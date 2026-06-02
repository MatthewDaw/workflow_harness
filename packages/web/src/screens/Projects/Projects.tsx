import { Link } from 'react-router-dom';
import { useGetProjectsQuery } from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/** Projects overview (U21): a card per repo, framed by PRD goal + progress. */
export function Projects() {
  const { data, isLoading, isError } = useGetProjectsQuery();
  const projects = data ?? [];

  return (
    <div className="hq-pad" data-testid="projects-screen">
      <ScreenHeader
        title="Projects"
        subtitle="A high-level viewer over every project (= GitHub repo)."
      />
      <div className="mb-3 flex items-center justify-between">
        <div className="text-mut">{projects.length} projects</div>
        <button type="button" className="hq-btn hq-btn-pri">
          + Connect repo
        </button>
      </div>

      {isLoading && <div className="text-mut">Loading projects…</div>}
      {isError && (
        <div className="hq-note" role="alert">
          Could not load projects.
        </div>
      )}
      {!isLoading && projects.length === 0 && (
        <div className="hq-box text-mut">No projects connected yet.</div>
      )}

      <div className="grid grid-cols-3 gap-3.5">
        {projects.map((p) => (
          <Link
            key={p.id}
            to={`/projects/${p.id}`}
            className="hq-box bg-paper no-underline text-ink"
          >
            <div className="flex items-center justify-between">
              <b>{p.name}</b>
              {p.liveSessionCount > 0 ? (
                <Pill variant="live">{p.liveSessionCount} live</Pill>
              ) : (
                <Pill variant="idle">idle</Pill>
              )}
            </div>
            <div className="my-2 font-mono text-[11px] text-faint">{p.repo}</div>
            <div className="min-h-[48px] text-xs text-mut">{p.prdGoal ?? 'No PRD goal yet.'}</div>
            <div className="hq-hr" />
            <div className="flex justify-between text-[11px] text-mut">
              <span>progress</span>
              <span>{p.progressPct ?? 0}%</span>
            </div>
            <Bar
              pct={p.progressPct ?? 0}
              color={(p.progressPct ?? 0) >= 90 ? '#3f7d4e' : undefined}
            />
          </Link>
        ))}
      </div>
    </div>
  );
}
