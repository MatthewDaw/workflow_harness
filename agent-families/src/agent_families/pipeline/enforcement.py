"""Enforcement activation and annealing (plan-005 U6, R18-R20).

Phase 1 logged tripwires in shadow; Phase 2 logged mutation-audit suspects;
Plan 4 logged ``instrument_suspect`` episode flags and the once-per-episode
fitness write. **Nothing was invented here** — this module is the consumer that
finally acts on those logged detectors (DESIGN §17 shadow-first discipline:
"every kill/exclusion mode activated here has been logging in shadow since
Phase 1; no detector is invented and enforced in the same plan").

The three activations, each a config promotion of a logged detector:

R18 — **tripwire kill-mode.** The step-repetition threshold is derived from the
logged similarity distribution of *productive* iterations (DESIGN §17/§7): a
high quantile, so a productive iteration almost never trips it. In ``shadow``
mode detection is logged and nothing dies (the Phase 1 dataset keeps growing);
in ``enforce`` mode a fired detector ENDS the Ralph loop with an **existing**
typed failure record — ``step_repetition`` / ``termination_unaware`` from the
MAST taxonomy the tripwires have always mapped to (no new failure shape). The
firing computation is identical in both modes; only the action differs — that
A/B equivalence is the unit Verification.

R19 — **suspect-verdict consumption.** A ticket closed by a verifier flagged in
a mutation audit (Phase 2 U8) is re-verified BEFORE its fitness events count: a
re-failing ticket is folded into ``settle_fitness``'s implicated set so it can
never win. Episode scores flagged ``instrument_suspect`` (Plan 4 U7, already
excluded from that plan's SPC) are additionally excluded from this plan's
curriculum / chart recompute — Plan 4's deferred half of the contract.

R20 — **question-budget annealing and persona rotation.** Both are pure config
schedules consuming logged telemetry, no new machinery: the budget tightens
across epochs toward 3–5 (§17); persona rotation fires only when planner scores
plateau (§9).

Tunables follow the ``validate.py`` precedent: caller-supplied via
:class:`EnforcementParams` / :class:`AnnealingSchedule`, the carried defaults
recording PROVENANCE; the run-assembly wiring routes live values from
``thresholds.toml``. Nothing here reads config directly, and nothing hardcodes a
tunable at a decision site.

Offline by construction: the embedder is the Phase 0 injected-encoder seam, the
re-verifier and the productive-similarity distribution are caller-supplied, so
the full decision table runs with zero quota and no ``claude`` on PATH.

## Conformance

Test-scenario / invariant (plan-005 U6) -> test (in ``tests/test_enforcement.py``):

- tripwire fires only above the derived threshold (fixture distributions):
  ``test_kill_threshold_derived_from_productive_distribution`` and
  ``test_enforce_fires_only_above_derived_threshold``
- killed loop escalates with the existing typed record (no new failure shape):
  ``test_killed_loop_escalates_with_existing_typed_record`` and
  ``test_detector_failure_kinds_are_existing_shapes``
- suspect verifier's ticket re-verified before fitness lands:
  ``test_suspect_ticket_reverified_before_fitness_lands`` and the calibrate
  bridge ``test_flagged_suspect_chk_ids_reads_audit_log``
- suspect episode scores excluded from chart recompute:
  ``test_suspect_episode_scores_excluded_from_recompute``
- annealing schedule steps the budget per epoch:
  ``test_annealing_steps_budget_per_epoch``
- rotation trigger fires only on the plateau condition:
  ``test_persona_rotation_fires_only_on_plateau``

Unit Verification — "shadow-vs-enforce A/B on fixture data shows identical
detection, differing only in action" — is
``test_shadow_vs_enforce_identical_detection_differing_action``.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_families.grading import calibrate
from agent_families.pipeline.tripwires import (
    DETECTOR_DIFF_SIMILARITY,
    DETECTOR_NO_PROGRESS,
    IterationObservation,
    ShadowTripwires,
)
from agent_families.store import FAILURE_KINDS, Store

logger = logging.getLogger(__name__)

SHADOW = "shadow"
ENFORCE = "enforce"
MODES = (SHADOW, ENFORCE)

# The MAST failure kind each tripwire detector escalates with when it kills a
# loop (DESIGN §7): step-repetition for an iteration that barely changed the
# diff, termination-unawareness for a loop reproducing the identical failure set.
# Both are EXISTING shapes — the kill mode reuses the typed record the detectors
# have mapped to all along; it never invents a new failure kind.
DETECTOR_FAILURE_KIND: dict[str, str] = {
    DETECTOR_DIFF_SIMILARITY: "step_repetition",
    DETECTOR_NO_PROGRESS: "termination_unaware",
}
# Enforced at import: a kill must never coin a new failure shape (R18).
assert set(DETECTOR_FAILURE_KIND.values()) <= set(FAILURE_KINDS), (
    "tripwire kill-mode must escalate with EXISTING typed failure kinds"
    " (R18: no new failure shape)"
)


class EnforcementError(Exception):
    """A broken enforcement precondition with an actionable message."""


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) ----

# PROVENANCE: DESIGN §17 / §7 — "the step-repetition threshold is set from the
# logged similarity distribution of productive iterations"; the quantile is the
# false-kill guard: at q=0.95 at most ~5% of productive iterations would trip the
# kill. TUNING METRIC: false-kill rate on productive iterations vs. iterations
# saved by killing a stalled loop early.
DEFAULT_KILL_THRESHOLD_QUANTILE = 0.95

# PROVENANCE: R20 / DESIGN §17 — the question budget "tightens across epochs
# toward 3–5". Start loose, step down one per epoch, clamp at a floor inside the
# target band. TUNING METRIC: planner answer-quality vs. quota burned on Q&A.
DEFAULT_QUESTION_BUDGET_START = 8
DEFAULT_QUESTION_BUDGET_FLOOR = 4
DEFAULT_QUESTION_BUDGET_STEP = 1

# PROVENANCE: R20 / DESIGN §9 — persona rotation activates only on a planner-score
# PLATEAU. The window is how many recent episodes define "recent"; the floor is
# the improvement below which the curve is judged flat. TUNING METRIC: rotation's
# effect on the planner-score curve vs. churn from rotating too eagerly.
DEFAULT_PLATEAU_WINDOW = 3
DEFAULT_PLATEAU_MIN_IMPROVEMENT = 0.02


@dataclass(frozen=True)
class EnforcementParams:
    """Enforcement tunables, caller-supplied (no hidden config reads)."""

    kill_threshold_quantile: float = DEFAULT_KILL_THRESHOLD_QUANTILE
    plateau_window: int = DEFAULT_PLATEAU_WINDOW
    plateau_min_improvement: float = DEFAULT_PLATEAU_MIN_IMPROVEMENT

    def __post_init__(self) -> None:
        if not (0.0 < self.kill_threshold_quantile < 1.0):
            raise EnforcementError(
                "kill_threshold_quantile must be in (0, 1), got"
                f" {self.kill_threshold_quantile}"
            )
        if self.plateau_window < 2:
            raise EnforcementError(
                f"plateau_window must be >= 2, got {self.plateau_window}"
            )
        if self.plateau_min_improvement < 0:
            raise EnforcementError(
                "plateau_min_improvement must be >= 0, got"
                f" {self.plateau_min_improvement}"
            )


@dataclass(frozen=True)
class AnnealingSchedule:
    """The question-budget annealing schedule (R20). ``budget_at`` is a pure
    function of the epoch, so a resumed campaign recomputes the same value."""

    start: int = DEFAULT_QUESTION_BUDGET_START
    floor: int = DEFAULT_QUESTION_BUDGET_FLOOR
    step: int = DEFAULT_QUESTION_BUDGET_STEP

    def __post_init__(self) -> None:
        if self.floor < 1:
            raise EnforcementError(f"floor must be >= 1, got {self.floor}")
        if self.start < self.floor:
            raise EnforcementError(
                f"start ({self.start}) must be >= floor ({self.floor}) —"
                " annealing only tightens"
            )
        if self.step < 0:
            raise EnforcementError(f"step must be >= 0, got {self.step}")

    def budget_at(self, epoch: int) -> int:
        """The question budget for ``epoch`` (0-based): start, stepped down each
        epoch, clamped at the floor (R20 'toward 3–5')."""
        if epoch < 0:
            raise EnforcementError(f"epoch must be >= 0, got {epoch}")
        return max(self.floor, self.start - self.step * epoch)


# --- R18: tripwire kill-mode -----------------------------------------------------


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolation quantile over a non-empty sample (robust at small N
    where ``statistics.quantiles`` needs n>=2)."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    frac = pos - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


@dataclass(frozen=True)
class KillThreshold:
    """A tripwire kill threshold derived from logged productive-iteration
    similarities, carrying its own derivation provenance (§17)."""

    value: float
    quantile: float
    n_samples: int
    derivation: str


def derive_kill_threshold(
    productive_similarities: Sequence[float], params: EnforcementParams
) -> KillThreshold:
    """Derive the diff-similarity kill threshold from the logged distribution of
    PRODUCTIVE iterations (R18, DESIGN §17).

    Productive iterations change the diff, so they sit at low similarity; the
    threshold is a high quantile of that distribution, placed just above where
    productive work lives so a productive iteration almost never trips the kill.
    The result is a valid cosine in ``[0, 1]`` (clamped) — it is fed straight to
    :class:`ShadowTripwires` as ``similarity_threshold``.
    """
    sims = [float(s) for s in productive_similarities]
    if not sims:
        raise EnforcementError(
            "cannot derive a kill threshold from an empty productive-iteration"
            " distribution — accumulate shadow tripwire_events first (R18/§17)"
        )
    if any(not (0.0 <= s <= 1.0) for s in sims):
        raise EnforcementError(
            "productive similarities must be cosines in [0, 1]"
        )
    raw = _quantile(sims, params.kill_threshold_quantile)
    value = min(1.0, max(0.0, raw))
    return KillThreshold(
        value=value,
        quantile=params.kill_threshold_quantile,
        n_samples=len(sims),
        derivation=(
            f"q{params.kill_threshold_quantile:.2f} of {len(sims)} productive"
            f" diff-similarities = {value:.4f}; a productive iteration trips the"
            " kill at most ~"
            f"{(1.0 - params.kill_threshold_quantile) * 100:.0f}% of the time"
            " (§17 shadow-first false-kill guard)"
        ),
    )


@dataclass(frozen=True)
class TripwireDecision:
    """One detector's enforcement outcome for a single iteration. ``fired`` is the
    detection (identical across modes); ``killed`` and ``failure_record_id`` are
    the action (populated only in ``enforce`` mode)."""

    detector_kind: str
    fired: bool
    killed: bool
    failure_kind: str | None
    failure_record_id: int | None
    reason: str


@dataclass(frozen=True)
class EnforcementObservation:
    """The enforcement consumer's per-iteration result: the underlying shadow
    observation (always logged), the per-detector decisions, and whether the loop
    must end now."""

    shadow: IterationObservation
    decisions: tuple[TripwireDecision, ...]
    killed: bool

    @property
    def fired_detectors(self) -> tuple[str, ...]:
        return tuple(d.detector_kind for d in self.decisions if d.fired)


class EnforcementTripwires:
    """Shadow logging + an enforcement decision on top (R18).

    Wraps a :class:`ShadowTripwires` whose ``similarity_threshold`` is the DERIVED
    kill threshold. Every iteration is still logged to ``tripwire_events`` (the
    Phase 1 dataset never stops growing); the detection is read from the shadow
    observation, so ``shadow`` and ``enforce`` mode detect identically. Only the
    ACTION differs: ``enforce`` writes the existing typed failure record and
    signals a kill; ``shadow`` writes nothing and never kills.
    """

    def __init__(self, shadow: ShadowTripwires, *, mode: str) -> None:
        if mode not in MODES:
            raise EnforcementError(
                f"mode must be one of {MODES}, got {mode!r}"
            )
        self.shadow = shadow
        self.mode = mode

    @property
    def enforcing(self) -> bool:
        return self.mode == ENFORCE

    def observe_iteration(
        self,
        *,
        run_id: int,
        ticket_id: str,
        ralph_iteration: int,
        diff_summary: str,
        failures=(),
        span_id: str | None = None,
    ) -> EnforcementObservation:
        """Log the shadow detectors and, in ``enforce`` mode, kill on a fired
        detector with an existing typed escalation (R18)."""
        obs = self.shadow.observe_iteration(
            run_id=run_id,
            ticket_id=ticket_id,
            ralph_iteration=ralph_iteration,
            diff_summary=diff_summary,
            failures=failures,
            span_id=span_id,
        )
        fired = (
            (DETECTOR_DIFF_SIMILARITY, obs.diff_would_have_fired),
            (DETECTOR_NO_PROGRESS, obs.no_progress_would_have_fired),
        )
        decisions: list[TripwireDecision] = []
        killed = False
        for detector_kind, did_fire in fired:
            if not did_fire:
                continue
            failure_kind = DETECTOR_FAILURE_KIND[detector_kind]
            failure_record_id: int | None = None
            if self.enforcing:
                with self.shadow.store.transaction():
                    failure_record_id = (
                        self.shadow.store.insert_failure_record(
                            failure_kind=failure_kind,
                            location=f"{ticket_id}@iter{ralph_iteration}",
                            expected="loop makes progress per iteration",
                            observed=(
                                f"{detector_kind} fired"
                                + (
                                    f" (similarity {obs.similarity:.4f})"
                                    if obs.similarity is not None
                                    else ""
                                )
                            ),
                            run_id=run_id,
                            ticket_id=ticket_id,
                            span_id=span_id,
                        )
                    )
                killed = True
                logger.warning(
                    "tripwire %s KILLED run=%d ticket=%s iter=%d -> typed %s",
                    detector_kind, run_id, ticket_id, ralph_iteration,
                    failure_kind,
                )
            decisions.append(
                TripwireDecision(
                    detector_kind=detector_kind,
                    fired=True,
                    killed=self.enforcing,
                    failure_kind=failure_kind if self.enforcing else None,
                    failure_record_id=failure_record_id,
                    reason=(
                        f"{detector_kind} fired in {self.mode} mode"
                        + (
                            f"; killed with typed {failure_kind}"
                            if self.enforcing
                            else "; observed only (shadow)"
                        )
                    ),
                )
            )
        return EnforcementObservation(
            shadow=obs, decisions=tuple(decisions), killed=killed
        )


# --- R19: suspect-verdict consumption -------------------------------------------


@dataclass(frozen=True)
class SuspectReverification:
    """The re-verification pass over tickets closed by suspect verdicts (R19).

    ``now_failing`` is the set folded into ``settle_fitness``'s implicated set so
    a suspect-closed ticket that no longer verifies can never win.
    """

    reverified_ticket_ids: tuple[str, ...]
    still_passing: tuple[str, ...]
    now_failing: tuple[str, ...]


# A re-verification of one ticket: ticket id in, did-it-still-pass out. The live
# binding re-runs the verifier on the ticket's artifact; the suite injects a fake.
ReverifyFn = Callable[[str], bool]


def suspect_closed_tickets(
    store: Store, chk_ids: Sequence[str]
) -> tuple[str, ...]:
    """The tickets a set of suspect CHK verdicts closed, via CHK -> AC -> ticket
    (R19). Empty in -> empty out; duplicates collapse; order is stable."""
    ids = [c for c in dict.fromkeys(chk_ids)]
    if not ids:
        return ()
    placeholders = ",".join("?" for _ in ids)
    rows = store.conn.execute(
        "SELECT DISTINCT ac.ticket_id AS ticket_id"
        " FROM trace_chk chk JOIN trace_ac ac ON chk.ac_id = ac.id"
        f" WHERE chk.id IN ({placeholders})"
        " ORDER BY ac.ticket_id",
        ids,
    ).fetchall()
    return tuple(r["ticket_id"] for r in rows)


def flagged_suspect_chk_ids(
    store: Store, verifier_id: str | None = None
) -> tuple[str, ...]:
    """Bridge to the Phase 2 mutation-audit log (R19): the union of CHK ids
    marked suspect across flagged verifiers (or one verifier if named). This is
    the data the shadow audit recorded all along — enforcement only consumes it."""
    health = calibrate.instrument_health(store, verifier_id)
    flagged = set(health["flagged_verifiers"])
    suspect_map = health["suspect_chk_ids"]
    out: list[str] = []
    seen: set[str] = set()
    for vid in sorted(suspect_map):
        if verifier_id is None and vid not in flagged:
            continue
        for cid in suspect_map[vid]:
            if cid not in seen:
                seen.add(cid)
                out.append(cid)
    return tuple(out)


def reverify_before_fitness(
    store: Store,
    *,
    chk_ids: Sequence[str],
    reverify_fn: ReverifyFn,
) -> SuspectReverification:
    """Re-verify every ticket closed by a suspect verdict BEFORE its fitness
    events count (R19).

    Returns the partition of the suspect-closed tickets into those that still
    pass and those that now fail; fold ``now_failing`` into
    ``settle_fitness(implicated_ticket_ids=...)`` so a tainted close cannot earn
    a win. The re-verification runs first — the caller must call this before
    settling fitness, which the ``before fitness lands`` test asserts.
    """
    tickets = suspect_closed_tickets(store, chk_ids)
    still_passing: list[str] = []
    now_failing: list[str] = []
    for ticket_id in tickets:
        if reverify_fn(ticket_id):
            still_passing.append(ticket_id)
        else:
            now_failing.append(ticket_id)
            logger.warning(
                "suspect-closed ticket %s re-verified as FAILING; excluded"
                " from fitness wins (R19)",
                ticket_id,
            )
    return SuspectReverification(
        reverified_ticket_ids=tickets,
        still_passing=tuple(still_passing),
        now_failing=tuple(now_failing),
    )


# --- R19: instrument_suspect episode-score exclusion ----------------------------


@dataclass(frozen=True)
class EpisodeScore:
    """A persisted episode score with its Plan 4 ``instrument_suspect`` flag.
    Plan 4 excluded these from its own SPC; this plan also excludes them from
    curriculum / chart recompute (R19, the deferred half of the §17 contract)."""

    target: str
    epoch: int | None
    snapshot_id: int
    score: float
    instrument_suspect: bool = False


def curriculum_eligible_scores(
    scores: Sequence[EpisodeScore],
) -> tuple[EpisodeScore, ...]:
    """The non-instrument-suspect scores — the only ones a curriculum decision or
    a control-chart recompute may read (R19)."""
    return tuple(s for s in scores if not s.instrument_suspect)


# --- R20: persona rotation trigger ----------------------------------------------


def planner_scores_plateaued(
    scores: Sequence[float], params: EnforcementParams
) -> bool:
    """True when the recent planner-score window shows no real improvement (R20,
    DESIGN §9). Below ``plateau_window`` points there is not enough history to
    call a plateau, so it returns False (rotation needs evidence)."""
    if len(scores) < params.plateau_window:
        return False
    window = list(scores[-params.plateau_window:])
    improvement = window[-1] - window[0]
    return improvement < params.plateau_min_improvement


def should_rotate_personas(
    scores: Sequence[float], params: EnforcementParams
) -> bool:
    """Persona rotation fires ONLY on the plateau condition (R20) — never on a
    still-improving planner-score curve."""
    return planner_scores_plateaued(scores, params)
