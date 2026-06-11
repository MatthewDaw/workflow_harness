import { useEffect, useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  COMMIT_CATEGORIES,
  ORPHAN_REASONS,
  type CommitCategory,
  type OrphanReason,
} from '@harness/shared';
import {
  useGetObjectivesQuery,
  useGetWeeksQuery,
  useCreateCommitMutation,
  useDeleteCommitMutation,
  useLockWeekMutation,
  type WeekView,
  type WeeklyCommit,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Project Weekly DRAFT editor (U10). Turns the read-only scaffold into the full
 * itemized-commit editor for a DRAFT week:
 *  - declared concentration `posture` control (KTD8),
 *  - editable commit rows whose link is EITHER a Supporting Outcome OR a typed
 *    `orphanReason` (KTD10) — never neither,
 *  - the DERIVED `category` + `priorityNumeric` shown READ-ONLY (KTD4) with an
 *    explicit override affordance that FLAGS (never silently accepts) a category
 *    that contradicts the derived value,
 *  - the list auto-sorted by derived priority (manual reorder is a flagged
 *    override — out of scope for the core, surfaced as the same flag idiom),
 *  - "Lock week" (the new "publish") disabled with inline `blockers[]` reasons
 *    until ≥1 commit AND every commit carries an SO-or-orphan.
 * A non-DRAFT week renders read-only; reconciliation routes to U11.
 */

/** A structured lock blocker (mirrors the backend 409 `blockers[]`). */
interface Blocker {
  commitId?: string;
  reason: string;
}

/**
 * Compute the blockers that keep a DRAFT week from locking, client-side, so the
 * Lock button can disable + explain BEFORE the server round-trip (the server
 * re-checks the same guard — U4). ≥1 commit AND every commit SO-or-orphan.
 */
function lockBlockers(commits: WeeklyCommit[]): Blocker[] {
  const out: Blocker[] = [];
  if (commits.length === 0) out.push({ reason: 'a week needs at least one commit to lock' });
  for (const c of commits) {
    if (!c.supportingOutcomeId && !c.orphanReason) {
      out.push({
        commitId: c.id,
        reason: `"${c.title}" needs a Supporting Outcome or an orphan reason`,
      });
    }
  }
  return out;
}

/** The ISO-week string for today, e.g. `2026-W23` (matches the backend regex). */
function currentIsoWeek(now = new Date()): string {
  // ISO-8601 week date: Thursday-anchored. Copy to UTC to avoid TZ drift.
  const d = new Date(Date.UTC(now.getFullYear(), now.getMonth(), now.getDate()));
  const day = d.getUTCDay() || 7; // Mon=1..Sun=7
  d.setUTCDate(d.getUTCDate() + 4 - day);
  const yearStart = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
  const week = Math.ceil(((d.getTime() - yearStart.getTime()) / 86400000 + 1) / 7);
  return `${d.getUTCFullYear()}-W${String(week).padStart(2, '0')}`;
}

/** A single editable DRAFT commit row. */
function CommitRow({
  commit,
  supportingOutcomes,
  onDelete,
}: {
  commit: WeeklyCommit;
  supportingOutcomes: { id: string; title: string }[];
  onDelete: () => void;
}) {
  // The override affordance is a CLIENT-side flag (KTD4): the derived `category`
  // is read-only; picking a different one surfaces the contradiction flag rather
  // than silently replacing the derived value (the server re-derives on write).
  const [override, setOverride] = useState<CommitCategory | ''>('');
  const overridden = override !== '' && override !== commit.category;

  return (
    <li className="hq-box bg-paper2 mt-1.5" data-testid={`commit-${commit.id}`}>
      <div className="flex items-center gap-2">
        <span className="flex-1 font-medium">{commit.title}</span>
        <button
          type="button"
          className="hq-btn"
          data-testid={`commit-delete-${commit.id}`}
          onClick={onDelete}
        >
          Remove
        </button>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12.5px]">
        {/* The SO-or-orphan link this commit carries (KTD10). */}
        {commit.supportingOutcomeId ? (
          <Pill variant="good">
            SO:{' '}
            {supportingOutcomes.find((s) => s.id === commit.supportingOutcomeId)?.title ??
              commit.supportingOutcomeId}
          </Pill>
        ) : commit.orphanReason ? (
          <Pill variant="idle">orphan: {commit.orphanReason}</Pill>
        ) : (
          <span className="text-live" data-testid={`commit-unlinked-${commit.id}`}>
            needs a Supporting Outcome or an orphan reason
          </span>
        )}
        {/* DERIVED chess layer — read-only (KTD4). */}
        <span className="text-mut">
          category <b>{commit.category}</b> · priority <b>{commit.priorityNumeric.toFixed(2)}</b>
        </span>
        {/* The override affordance: flags a category that contradicts the derived one. */}
        <label className="text-mut">
          override
          <select
            className="hq-input ml-1 py-0.5"
            data-testid={`commit-override-${commit.id}`}
            value={override}
            onChange={(e) => setOverride(e.target.value as CommitCategory | '')}
          >
            <option value="">(derived)</option>
            {COMMIT_CATEGORIES.map((cat) => (
              <option key={cat} value={cat}>
                {cat}
              </option>
            ))}
          </select>
        </label>
        {overridden && (
          <Pill variant="live">
            <span data-testid={`commit-override-flag-${commit.id}`}>
              override contradicts derived ({commit.category})
            </span>
          </Pill>
        )}
      </div>
    </li>
  );
}

/** The add-a-commit form: title + SO-or-orphan picker (exactly one). */
function AddCommitForm({
  supportingOutcomes,
  onAdd,
  pending,
}: {
  supportingOutcomes: { id: string; title: string }[];
  onAdd: (input: {
    title: string;
    supportingOutcomeId?: string;
    orphanReason?: OrphanReason;
  }) => void;
  pending: boolean;
}) {
  const [title, setTitle] = useState('');
  // The link is a single picker that is EITHER an SO id (prefixed `so:`) OR an
  // orphan reason (prefixed `orphan:`) — never both, mirroring the DB CHECK.
  const [link, setLink] = useState('');

  const parseLink = (): { supportingOutcomeId?: string; orphanReason?: OrphanReason } => {
    if (link.startsWith('so:')) return { supportingOutcomeId: link.slice(3) };
    if (link.startsWith('orphan:')) return { orphanReason: link.slice(7) as OrphanReason };
    return {};
  };

  const linked = link !== '';
  const canAdd = title.trim() !== '' && linked && !pending;

  return (
    <div className="hq-box mt-2" data-testid="add-commit-form">
      <b>Add a commit</b>
      <div className="mt-1.5 flex flex-wrap items-center gap-2">
        <input
          className="hq-input flex-1 min-w-[200px]"
          placeholder="What will you commit to this week?"
          data-testid="commit-title-input"
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <select
          className="hq-input"
          data-testid="commit-link-select"
          value={link}
          onChange={(e) => setLink(e.target.value)}
        >
          <option value="">Link to…</option>
          <optgroup label="Supporting Outcome">
            {supportingOutcomes.map((s) => (
              <option key={s.id} value={`so:${s.id}`}>
                {s.title}
              </option>
            ))}
          </optgroup>
          <optgroup label="Orphan reason">
            {ORPHAN_REASONS.map((r) => (
              <option key={r} value={`orphan:${r}`}>
                {r}
              </option>
            ))}
          </optgroup>
        </select>
        <button
          type="button"
          className="hq-btn hq-btn-pri"
          data-testid="commit-add"
          disabled={!canAdd}
          onClick={() => {
            onAdd({ title: title.trim(), ...parseLink() });
            setTitle('');
            setLink('');
          }}
        >
          Add
        </button>
      </div>
    </div>
  );
}

export function ProjectWeekly() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const { data, isLoading } = useGetWeeksQuery(projectId, { skip: !projectId });
  const { data: objectives } = useGetObjectivesQuery();

  const [createCommit, createState] = useCreateCommitMutation();
  const [deleteCommit] = useDeleteCommitMutation();
  const [lockWeek, lockState] = useLockWeekMutation();
  /** Server-returned lock blockers (a 409 the editor surfaces to self-correct). */
  const [serverBlockers, setServerBlockers] = useState<Blocker[]>([]);

  // The list arrives oldest-first and is not guaranteed ordered; show the newest
  // week by sorting on the lexicographically-comparable ISO week (desc).
  const latest = useMemo<WeekView | undefined>(() => {
    const weeks = [...(data ?? [])];
    weeks.sort((a, b) => b.plan.isoWeek.localeCompare(a.plan.isoWeek));
    return weeks[0];
  }, [data]);

  // Only `supporting_outcome` nodes are linkable (KTD10/KTD9) — never an upper
  // RCDO level. Pre-resolve {id,title} so the rows/picker render names.
  const supportingOutcomes = useMemo(
    () =>
      (objectives ?? [])
        .filter((o) => o.level === 'supporting_outcome')
        .map((o) => ({ id: o.id, title: o.title })),
    [objectives],
  );

  // The commit list self-sorts by DERIVED priority (KTD4), descending leverage.
  const commits = useMemo(
    () => [...(latest?.commits ?? [])].sort((a, b) => b.priorityNumeric - a.priorityNumeric),
    [latest],
  );

  // No week yet is treated as DRAFT-able: adding the first commit auto-creates
  // the DRAFT plan server-side (U3), so the editor must be reachable from empty.
  const isDraft = !latest || latest.plan.status === 'DRAFT';
  const blockers = useMemo(() => lockBlockers(commits), [commits]);

  // A fresh 409 supersedes a stale one; once the week's commits change the
  // client-side `blockers` take over, so clear server blockers on any write.
  useEffect(() => setServerBlockers([]), [latest?.commits]);

  const isoWeek = latest?.plan.isoWeek ?? currentIsoWeek();

  async function handleLock() {
    if (!latest) return;
    setServerBlockers([]);
    try {
      await lockWeek({ projectId, isoWeek: latest.plan.isoWeek }).unwrap();
    } catch (err) {
      const body = (err as { data?: { blockers?: Blocker[] } })?.data;
      if (body?.blockers) setServerBlockers(body.blockers);
    }
  }

  return (
    <div className="hq-pad" data-testid="project-weekly">
      <ScreenHeader
        title="Weekly Update"
        subtitle="Itemized weekly commits — each linked to a Supporting Outcome or a typed orphan reason."
      />
      <div className="mb-2.5 flex items-center justify-between">
        <div className="text-mut flex items-center gap-2">
          {latest ? `Week ${latest.plan.isoWeek}` : `Week ${isoWeek} — new (add a commit to start)`}
          {latest && (
            <Pill variant={latest.plan.status === 'RECONCILED' ? 'good' : 'idle'}>
              {latest.plan.status.toLowerCase()}
            </Pill>
          )}
          {latest && (
            <span data-testid="week-posture" className="text-[12.5px]">
              posture: <b>{latest.plan.posture}</b>
            </span>
          )}
        </div>
        <span className="text-mut text-[12.5px]">
          Run <code>/hq-weekly-update</code> in claude+ to draft this week's commits.
        </span>
      </div>

      {isLoading && <div className="text-mut">Loading…</div>}

      {(latest || isDraft) && (
        <div className="hq-box bg-paper">
          <div className="flex items-center justify-between">
            <b>This week's commits</b>
            {latest?.plan.status === 'RECONCILING' && (
              <button
                type="button"
                className="hq-btn"
                data-testid="goto-reconcile"
                onClick={() => navigate(`/projects/${projectId}/weekly/reconcile`)}
              >
                Reconcile
              </button>
            )}
          </div>

          {commits.length === 0 ? (
            <p className="mt-1.5 text-[12.5px] text-mut">No commits yet — add your first one below.</p>
          ) : (
            <ul className="mt-1.5 list-none p-0">
              {commits.map((c) => (
                <CommitRow
                  key={c.id}
                  commit={c}
                  supportingOutcomes={supportingOutcomes}
                  onDelete={() => isDraft && deleteCommit({ projectId, isoWeek, commitId: c.id })}
                />
              ))}
            </ul>
          )}

          {isDraft && (
            <>
              <AddCommitForm
                supportingOutcomes={supportingOutcomes}
                pending={createState.isLoading}
                onAdd={(input) => createCommit({ projectId, isoWeek, ...input })}
              />

              {/* Lock = the new "publish". Disabled with inline blocker reasons
                  until ≥1 commit and every commit carries an SO-or-orphan. */}
              <div className="mt-2 flex items-center gap-2">
                <button
                  type="button"
                  className="hq-btn hq-btn-pri"
                  data-testid="lock-week"
                  disabled={blockers.length > 0 || lockState.isLoading}
                  onClick={handleLock}
                >
                  Lock week
                </button>
                {(serverBlockers.length > 0 ? serverBlockers : blockers).length > 0 && (
                  <ul className="text-live text-[12.5px]" data-testid="lock-blockers">
                    {(serverBlockers.length > 0 ? serverBlockers : blockers).map((b, i) => (
                      <li key={b.commitId ?? `b${i}`}>{b.reason}</li>
                    ))}
                  </ul>
                )}
              </div>
            </>
          )}

          {latest && !isDraft && latest.plan.status !== 'RECONCILING' && (
            <p className="mt-2 text-[12.5px] text-mut" data-testid="readonly-note">
              This week is {latest.plan.status.toLowerCase()} — commits are read-only.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
