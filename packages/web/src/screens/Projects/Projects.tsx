import { useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useCreateProjectMutation,
  useDeleteProjectMutation,
  useGetProjectsQuery,
  useRefreshProjectMutation,
} from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Normalize whatever the user pastes (a `owner/repo` slug, a `gh/owner/repo`,
 * or a full `https://github.com/owner/repo(.git)` URL) into the bare
 * `owner/repo` form the backend's GitHub client expects.
 */
export function normalizeRepoSlug(input: string): string {
  return input
    .trim()
    .replace(/^https?:\/\/github\.com\//i, '')
    .replace(/^gh\//, '')
    .replace(/\.git$/, '')
    .replace(/\/+$/, '');
}

/** A stable project id derived from the repo slug (`acme/Weekly-Compass` → `acme-weekly-compass`). */
export function projectIdFor(slug: string): string {
  return slug
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '');
}

/** Projects overview (U21): a card per repo, framed by PRD goal + progress. */
export function Projects() {
  const { data, isLoading, isError } = useGetProjectsQuery();
  const projects = data ?? [];

  const [createProject, { isLoading: connecting, isError: connectError }] =
    useCreateProjectMutation();
  const [refreshProject] = useRefreshProjectMutation();
  const [deleteProject] = useDeleteProjectMutation();

  const [showConnect, setShowConnect] = useState(false);
  const [repoInput, setRepoInput] = useState('');
  const [nameInput, setNameInput] = useState('');
  // The project id pending a delete confirmation (two-click confirm so the whole
  // card's Link navigation is never hijacked by an accidental single click).
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  const onDelete = async (id: string) => {
    setConfirmingDelete(null);
    await deleteProject(id).unwrap();
  };

  const slug = normalizeRepoSlug(repoInput);
  const canConnect = slug.includes('/') && !connecting;

  const resetForm = () => {
    setShowConnect(false);
    setRepoInput('');
    setNameInput('');
  };

  const onConnect = async () => {
    if (!canConnect) return;
    const id = projectIdFor(slug);
    const name = nameInput.trim() || (slug.split('/').pop() ?? slug);
    await createProject({ id, name, repo: `gh/${slug}` }).unwrap();
    // Best-effort: pull GitHub framing so the new card shows real progress.
    // A failed/unreachable refresh degrades to "stale" server-side — never fatal.
    refreshProject(id);
    resetForm();
  };

  return (
    <div className="hq-pad" data-testid="projects-screen">
      <ScreenHeader
        title="Projects"
        subtitle="A high-level viewer over every project (= GitHub repo)."
      />
      <div className="mb-3 flex items-center justify-between">
        <div className="text-mut">{projects.length} projects</div>
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="connect-repo-toggle"
          aria-expanded={showConnect}
          onClick={() => setShowConnect((v) => !v)}
        >
          + Connect repo
        </button>
      </div>

      {showConnect && (
        <div className="hq-box bg-paper mb-3" data-testid="connect-repo-form">
          <label className="block text-xs font-medium text-mut" htmlFor="connect-repo">
            GitHub repo
          </label>
          <input
            id="connect-repo"
            className="hq-input mt-1 w-full"
            data-testid="connect-repo-input"
            placeholder="owner/repo"
            value={repoInput}
            onChange={(e) => setRepoInput(e.target.value)}
          />

          <label className="mt-3 block text-xs font-medium text-mut" htmlFor="connect-name">
            Display name <span className="text-faint">(optional)</span>
          </label>
          <input
            id="connect-name"
            className="hq-input mt-1 w-full"
            data-testid="connect-name-input"
            placeholder={slug ? (slug.split('/').pop() ?? '') : 'weekly-compass'}
            value={nameInput}
            onChange={(e) => setNameInput(e.target.value)}
          />

          {connectError && (
            <div className="hq-note mt-2" role="alert">
              Could not connect that repo.
            </div>
          )}

          <div className="hq-hr" />
          <div className="flex gap-2">
            <button
              type="button"
              className="hq-btn hq-btn-pri"
              data-testid="connect-repo-submit"
              disabled={!canConnect}
              onClick={onConnect}
            >
              {connecting ? 'Connecting…' : 'Connect'}
            </button>
            <button type="button" className="hq-btn" onClick={resetForm}>
              Cancel
            </button>
          </div>
        </div>
      )}

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
            className="hq-box bg-paper no-underline text-ink relative"
            data-testid={`project-card-${p.id}`}
          >
            <div className="flex items-center justify-between gap-2">
              <b className="min-w-0 truncate">{p.name}</b>
              <div className="flex shrink-0 items-center gap-2">
                {p.liveSessionCount > 0 ? (
                  <Pill variant="live">{p.liveSessionCount} live</Pill>
                ) : (
                  <Pill variant="idle">idle</Pill>
                )}
                {confirmingDelete === p.id ? (
                  <span className="flex items-center gap-1">
                    <button
                      type="button"
                      className="hq-btn text-[11px] text-live"
                      data-testid={`project-delete-confirm-${p.id}`}
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        void onDelete(p.id);
                      }}
                    >
                      Confirm
                    </button>
                    <button
                      type="button"
                      className="hq-btn text-[11px]"
                      onClick={(e) => {
                        e.preventDefault();
                        e.stopPropagation();
                        setConfirmingDelete(null);
                      }}
                    >
                      Cancel
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="hq-btn text-[11px] text-faint"
                    aria-label={`Remove ${p.name}`}
                    title="Remove project"
                    data-testid={`project-delete-${p.id}`}
                    onClick={(e) => {
                      e.preventDefault();
                      e.stopPropagation();
                      setConfirmingDelete(p.id);
                    }}
                  >
                    ✕
                  </button>
                )}
              </div>
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
