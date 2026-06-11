import { useMemo, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { COMMIT_OUTCOME_STATUSES, type CommitOutcomeStatus } from '@harness/shared';
import {
  useGetObjectivesQuery,
  useGetWeeksQuery,
  useUpdateCommitMutation,
  useStartReconcileMutation,
  useCompleteReconcileMutation,
  type WeekView,
  type WeeklyCommit,
} from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

/**
 * Weekly reconciliation view (U11). The planned-vs-actual surface for a
 * `RECONCILING` week (KTD2): a two-column layout where the LEFT is the frozen
 * plan (title, SO/orphan link, derived priority — read-only) and the RIGHT is
 * the actual outcome the human records (`status` + `actualOutcome`), saved via
 * the `updateCommit` actual-fields path (U3/U4).
 *
 * Lifecycle:
 *  - A `LOCKED` week offers "Start reconciliation" (`POST …/reconcile/start`).
 *  - A `RECONCILING` week shows the editable actual column; "Complete" is
 *    disabled until EVERY commit is terminal (not `planned`) — the same guard
 *    the server re-checks (U4 → 409). Completing runs carry-forward + the
 *    single-source roll-up recompute in one transaction (KTD3/KTD5), so RTK
 *    invalidates `Objective` and the SO `pct` moves on the Objectives screen.
 *  - On complete, the carry-forward summary (how many items carried to next
 *    week, and any line that reached `carryDepth >= 3`) is surfaced as a nudge.
 *
 * A `done` commit shows no carry; a `partial`/`planned` line is flagged as
 * carrying into next week's DRAFT.
 */

/** Terminal = anything but `planned` (the reconciliation-complete guard, U4). */
function isTerminal(c: WeeklyCommit): boolean {
  return c.status !== 'planned';
}

/** Will this reconciled status seed next week's DRAFT (KTD3)? `planned`/`partial` carry. */
function carries(status: CommitOutcomeStatus): boolean {
  return status === 'planned' || status === 'partial';
}

/** The carry-forward summary surfaced after a successful `completeReconcile`. */
interface CarrySummary {
  carriedTo: string;
  carriedCount: number;
  deepCarryNudge?: { commitId: string; carryDepth: number }[];
}

/** A single planned-vs-actual row: frozen plan on the left, editable actual on the right. */
function ReconcileRow({
  commit,
  soTitle,
  onSetActual,
}: {
  commit: WeeklyCommit;
  soTitle?: string;
  onSetActual: (patch: { status?: CommitOutcomeStatus; actualOutcome?: string }) => void;
}) {
  // Local mirror so typing the actual outcome doesn't round-trip on every keystroke;
  // committed on blur. The status select saves immediately (a small discrete choice).
  const [outcome, setOutcome] = useState(commit.actualOutcome ?? '');

  return (
    <li className="hq-box bg-paper2 mt-1.5" data-testid={`reconcile-${commit.id}`}>
      <div className="flex flex-wrap gap-3">
        {/* LEFT — the frozen plan (read-only). */}
        <div className="flex-1 min-w-[220px]">
          <div className="font-medium">{commit.title}</div>
          <div className="mt-1 flex flex-wrap items-center gap-2 text-[12.5px]">
            {commit.supportingOutcomeId ? (
              <Pill variant="good">SO: {soTitle ?? commit.supportingOutcomeId}</Pill>
            ) : (
              <Pill variant="idle">orphan: {commit.orphanReason}</Pill>
            )}
            <span className="text-mut">
              category <b>{commit.category}</b> · priority{' '}
              <b>{commit.priorityNumeric.toFixed(2)}</b>
            </span>
          </div>
        </div>

        {/* RIGHT — the actual outcome the human records. */}
        <div className="flex-1 min-w-[220px]">
          <label className="text-mut text-[12.5px]">
            outcome
            <select
              className="hq-input ml-1 py-0.5"
              data-testid={`reconcile-status-${commit.id}`}
              value={commit.status}
              onChange={(e) =>
                onSetActual({ status: e.target.value as CommitOutcomeStatus })
              }
            >
              {COMMIT_OUTCOME_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <textarea
            className="hq-input mt-1.5 w-full"
            rows={2}
            placeholder="What actually happened?"
            data-testid={`reconcile-outcome-${commit.id}`}
            value={outcome}
            onChange={(e) => setOutcome(e.target.value)}
            onBlur={() => {
              if (outcome !== (commit.actualOutcome ?? '')) onSetActual({ actualOutcome: outcome });
            }}
          />
          {/* A done line is settled; a planned/partial line is flagged as carrying. */}
          {carries(commit.status) ? (
            <span className="text-mut text-[12.5px]" data-testid={`reconcile-carry-${commit.id}`}>
              carries to next week
            </span>
          ) : (
            <span className="text-mut text-[12.5px]" data-testid={`reconcile-settled-${commit.id}`}>
              settled
            </span>
          )}
        </div>
      </div>
    </li>
  );
}

export function WeeklyReconcile() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const { data, isLoading } = useGetWeeksQuery(projectId, { skip: !projectId });
  const { data: objectives } = useGetObjectivesQuery();

  const [updateCommit] = useUpdateCommitMutation();
  const [startReconcile, startState] = useStartReconcileMutation();
  const [completeReconcile, completeState] = useCompleteReconcileMutation();
  /** The carry-forward summary surfaced after a successful complete. */
  const [summary, setSummary] = useState<CarrySummary | undefined>();

  // The newest week (the list arrives oldest-first and isn't guaranteed ordered).
  const latest = useMemo<WeekView | undefined>(() => {
    const weeks = [...(data ?? [])];
    weeks.sort((a, b) => b.plan.isoWeek.localeCompare(a.plan.isoWeek));
    return weeks[0];
  }, [data]);

  // SO id → title so the frozen-plan column can render the linked SO's name.
  const soTitles = useMemo(() => {
    const m = new Map<string, string>();
    for (const o of objectives ?? []) {
      if (o.level === 'supporting_outcome') m.set(o.id, o.title);
    }
    return m;
  }, [objectives]);

  // The commit list self-sorts by derived priority (KTD4), descending leverage.
  const commits = useMemo(
    () => [...(latest?.commits ?? [])].sort((a, b) => b.priorityNumeric - a.priorityNumeric),
    [latest],
  );

  const isReconciling = latest?.plan.status === 'RECONCILING';
  const allTerminal = commits.length > 0 && commits.every(isTerminal);
  const stillPlanned = commits.filter((c) => c.status === 'planned');
  const isoWeek = latest?.plan.isoWeek ?? '';

  async function handleStart() {
    if (!latest) return;
    await startReconcile({ projectId, isoWeek: latest.plan.isoWeek });
  }

  async function handleComplete() {
    if (!latest) return;
    const res = await completeReconcile({ projectId, isoWeek: latest.plan.isoWeek }).unwrap();
    setSummary(res);
  }

  return (
    <div className="hq-pad" data-testid="weekly-reconcile">
      <ScreenHeader
        title="Reconcile Week"
        subtitle="Compare what you planned against what actually shipped, then complete the week."
      />

      <div className="mb-2.5 flex items-center justify-between">
        <div className="text-mut flex items-center gap-2">
          {latest ? `Week ${latest.plan.isoWeek}` : 'No week to reconcile'}
          {latest && <Pill variant="idle">{latest.plan.status.toLowerCase()}</Pill>}
        </div>
        <button
          type="button"
          className="hq-btn"
          data-testid="back-to-weekly"
          onClick={() => navigate(`/projects/${projectId}/weekly`)}
        >
          Back to week
        </button>
      </div>

      {isLoading && <div className="text-mut">Loading…</div>}

      {/* A LOCKED week starts reconciliation; nothing to compare until then. */}
      {latest && latest.plan.status === 'LOCKED' && (
        <div className="hq-box bg-paper">
          <p className="text-[12.5px] text-mut">
            This week is locked. Start reconciliation to record what actually shipped.
          </p>
          <button
            type="button"
            className="hq-btn hq-btn-pri mt-1.5"
            data-testid="start-reconcile"
            disabled={startState.isLoading}
            onClick={handleStart}
          >
            Start reconciliation
          </button>
        </div>
      )}

      {isReconciling && (
        <div className="hq-box bg-paper">
          <div className="flex items-center justify-between text-[12.5px] text-mut">
            <b>Planned</b>
            <b>Actual</b>
          </div>

          {commits.length === 0 ? (
            <p className="mt-1.5 text-[12.5px] text-mut">No commits to reconcile.</p>
          ) : (
            <ul className="mt-1.5 list-none p-0">
              {commits.map((c) => (
                <ReconcileRow
                  key={c.id}
                  commit={c}
                  soTitle={
                    c.supportingOutcomeId ? soTitles.get(c.supportingOutcomeId) : undefined
                  }
                  onSetActual={(patch) =>
                    updateCommit({ projectId, isoWeek, commitId: c.id, ...patch })
                  }
                />
              ))}
            </ul>
          )}

          {/* Complete = the RECONCILING → RECONCILED transition. Disabled (with an
              inline reason) until every commit is terminal — the server re-checks. */}
          <div className="mt-2 flex items-center gap-2">
            <button
              type="button"
              className="hq-btn hq-btn-pri"
              data-testid="complete-reconcile"
              disabled={!allTerminal || completeState.isLoading}
              onClick={handleComplete}
            >
              Complete reconciliation
            </button>
            {!allTerminal && commits.length > 0 && (
              <span className="text-live text-[12.5px]" data-testid="complete-blocked">
                {stillPlanned.length} item{stillPlanned.length === 1 ? '' : 's'} still planned —
                set an outcome on each to complete.
              </span>
            )}
          </div>
        </div>
      )}

      {/* The carry-forward summary surfaced after a successful complete (KTD3). */}
      {summary && (
        <div className="hq-box bg-paper mt-2" data-testid="carry-summary">
          <b>Reconciled.</b>
          <p className="mt-1 text-[12.5px] text-mut" data-testid="carry-count">
            {summary.carriedCount === 0
              ? 'Nothing carried forward — every item settled.'
              : `${summary.carriedCount} item${summary.carriedCount === 1 ? '' : 's'} carried to ${summary.carriedTo}.`}
          </p>
          {summary.deepCarryNudge && summary.deepCarryNudge.length > 0 && (
            <p className="mt-1 text-live text-[12.5px]" data-testid="deep-carry-nudge">
              {summary.deepCarryNudge.length} item
              {summary.deepCarryNudge.length === 1 ? '' : 's'} now at carry depth ≥ 3 — consider
              decomposing or dropping.
            </p>
          )}
        </div>
      )}

      {latest &&
        latest.plan.status !== 'LOCKED' &&
        latest.plan.status !== 'RECONCILING' &&
        !summary && (
          <p className="mt-2 text-[12.5px] text-mut" data-testid="not-reconciling-note">
            This week is {latest.plan.status.toLowerCase()} — nothing to reconcile.
          </p>
        )}
    </div>
  );
}
