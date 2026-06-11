"""Enforcement activation and annealing (plan-005 U6, R18-R20).

Every shadow detector built in Phases 1-3 has logged in OBSERVE mode since the
day it shipped (DESIGN §17's shadow-first discipline: "no enforcement mode ships
without its shadow-mode data trail; no new detector is invented and enforced in
the same plan"). This module is the consumer that flips those logged detectors
from observation to action — and nothing else: every kill, exclusion, and anneal
here is a *config promotion of an already-logged signal*, never new machinery.

Three activations, one per requirement:

- **R18 — tripwires flip from shadow to kill.** The diff-similarity threshold is
  *derived from the logged distribution* (:func:`derive_similarity_threshold`):
  Phase 1-3's ``tripwire_events`` carry every consecutive-iteration cosine, and a
  high quantile of that distribution is the enforced threshold — so a kill fires
  only in the tail the system has measured, not at a guessed constant. A
  :class:`TripwireEnforcer` then evaluates each iteration; in ``enforce`` mode a
  fired detector ends the Ralph loop with a :class:`TripwireEscalation` carrying
  the **existing** detector kind (:data:`tripwires.DETECTOR_DIFF_SIMILARITY` /
  :data:`~tripwires.DETECTOR_NO_PROGRESS`) — *no new failure shape* is invented;
  the escalation reuses the typed record the detector has logged all along.
  :func:`shadow_vs_enforce` runs both modes over one signal stream to prove the
  detection is byte-identical and only the action differs.

- **R19 — suspect-verdict consumption.** Two exclusions, both reading flags the
  Phase 2/Plan 4 instruments already wrote. (a) A ticket closed by a verifier a
  mutation audit flagged (its closing CHK is in ``calibrate.suspect_chk_ids``) is
  **re-verified before its fitness counts** (:func:`gate_suspect_fitness`): only a
  clean re-verification lets the fitness event land. (b) Episode scores flagged
  ``instrument_suspect`` (Plan 4's frozen-replay drift signal, already excluded
  from Plan 4's own SPC) are **additionally excluded from curriculum decisions**
  here (:func:`curriculum_eligible_scores`) — a suspect score never moves a
  rotation or a control chart.

- **R20 — annealing and persona rotation.** Both are pure config schedules over
  logged telemetry, no new machinery (the plan is explicit). The question budget
  tightens across epochs toward the §17 floor of 3-5 (:class:`AnnealingSchedule`);
  persona rotation fires *only* when logged planner scores plateau
  (:class:`PersonaRotationRule`) — never on a healthy improvement trend.

Discipline, per the validate.py / curriculum.py precedent: tunables are
caller-supplied via small frozen dataclasses whose defaults carry PROVENANCE; the
``thresholds.toml`` routing is run-assembly's job and nothing here reads config
directly. Offline by construction — the re-verification is an injected ``reverify``
seam and every other input is typed data, so the full decision surface runs with
zero quota and no ``claude`` on PATH.

## Conformance (plan-005 U6 test scenarios / Verification -> test, in ``tests/test_enforcement.py``)

- tripwire fires only above the derived threshold (fixture distributions):
  ``test_tripwire_fires_only_above_derived_threshold``
- killed loop escalates with the existing typed record (no new failure shape):
  ``test_killed_loop_escalates_with_existing_typed_record``
- suspect verifier's ticket re-verified before fitness lands:
  ``test_suspect_ticket_reverified_before_fitness``
- suspect episode scores excluded from chart recompute:
  ``test_suspect_episode_scores_excluded_from_chart_recompute``
- annealing schedule steps the budget per epoch:
  ``test_annealing_schedule_steps_budget_per_epoch``
- rotation trigger fires only on the plateau condition:
  ``test_rotation_trigger_fires_only_on_plateau``
- Verification — shadow-vs-enforce A/B shows identical detection, differing only
  in action: ``test_shadow_vs_enforce_ab_identical_detection``
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_families.pipeline.tripwires import (
    DETECTOR_DIFF_SIMILARITY,
    DETECTOR_NO_PROGRESS,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)


class EnforcementError(Exception):
    """An enforcement misconfiguration or invariant breach with an actionable message."""


# The detector kinds enforcement is allowed to act on. They are the EXISTING
# typed records the Phase 1 tripwires have logged since they shipped — R18 flips
# their mode, it never invents a new failure shape. A TripwireEscalation's
# detector_kind is always one of these (asserted by the typed-record test).
EXISTING_TYPED_DETECTORS: tuple[str, ...] = (
    DETECTOR_NO_PROGRESS,
    DETECTOR_DIFF_SIMILARITY,
)

# The order detectors are checked when more than one would fire on the same
# iteration: a confirmed no-progress stall (a repeated non-empty failure set) is
# the unambiguous signal and is reported first; near-duplicate diffs are the
# softer one. Deterministic so the A/B and the escalation record are reproducible.
KILL_DETECTOR_ORDER: tuple[str, ...] = (
    DETECTOR_NO_PROGRESS,
    DETECTOR_DIFF_SIMILARITY,
)

MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
ENFORCEMENT_MODES = (MODE_SHADOW, MODE_ENFORCE)

ACTION_CONTINUE = "continue"
ACTION_KILL = "kill"


# --- R18: tripwire threshold derivation from the logged distribution -------------

# PROVENANCE: DESIGN §17 — kill thresholds are "set from Phase 1-3's logged
# similarity distributions". A high quantile of the observed consecutive-iteration
# cosine is the enforced threshold, so a kill fires only in the measured tail.
# TUNING METRIC: false-kill rate vs. missed-stall rate on the logged corpus.
DEFAULT_SIMILARITY_QUANTILE = 0.95

# PROVENANCE: §17 shadow-first / §6 small-N noise guard — never enforce a
# threshold derived from a thin sample. TUNING METRIC: threshold stability vs. N.
DEFAULT_MIN_OBSERVATIONS = 30


def _quantile(values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile (numpy 'linear' / type-7), deterministic.

    No PRNG, no platform-dependent sort instability (the inputs are floats); the
    same observed distribution yields the same threshold on every machine — the
    KTD byte-stability rule the rest of the system holds to.
    """
    if not values:
        raise EnforcementError("cannot take a quantile of an empty distribution")
    if not (0.0 <= q <= 1.0):
        raise EnforcementError(f"quantile q must be in [0, 1], got {q}")
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    frac = pos - lo
    if lo + 1 >= len(ordered):
        return float(ordered[-1])
    return float(ordered[lo] + frac * (ordered[lo + 1] - ordered[lo]))


@dataclass(frozen=True)
class DerivedThreshold:
    """A kill threshold derived from a logged shadow distribution (R18).

    Carries its own provenance — ``n_observations`` and ``quantile`` make the
    derivation auditable, so changing the value later reveals what it produced
    (the §17 logged-decision discipline).
    """

    detector_kind: str
    threshold: float
    n_observations: int
    quantile: float

    @property
    def provenance(self) -> str:
        return (
            f"derived: {self.quantile:.2f}-quantile of {self.n_observations}"
            f" logged {self.detector_kind} similarities = {self.threshold:.4f}"
        )


def derive_similarity_threshold_from_values(
    similarities: Sequence[float],
    *,
    quantile: float = DEFAULT_SIMILARITY_QUANTILE,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> DerivedThreshold:
    """Derive the diff-similarity kill threshold from a logged distribution (R18).

    ``similarities`` are the observed consecutive-iteration cosines (the non-NULL
    ``tripwire_events.similarity`` values). The threshold is the ``quantile`` of
    that distribution — a kill fires only above the tail the system has measured.
    Refuses a sample below ``min_observations`` (the shadow-first / small-N guard):
    enforcement on thin data is exactly what §17 forbids.
    """
    if min_observations < 1:
        raise EnforcementError(
            f"min_observations must be >= 1, got {min_observations}"
        )
    clean = [float(s) for s in similarities if s is not None]
    if len(clean) < min_observations:
        raise EnforcementError(
            f"refusing to derive a kill threshold from {len(clean)} logged"
            f" similarity observation(s) (< min_observations={min_observations}):"
            " enforce only on a distribution the shadow phase actually measured"
            " (R18 / §17 shadow-first discipline)"
        )
    return DerivedThreshold(
        detector_kind=DETECTOR_DIFF_SIMILARITY,
        threshold=_quantile(clean, quantile),
        n_observations=len(clean),
        quantile=quantile,
    )


def logged_diff_similarities(store: Store) -> tuple[float, ...]:
    """Every non-NULL consecutive-iteration cosine the shadow tripwire logged.

    The first iteration of each ticket logs a NULL-similarity baseline (no
    predecessor); those rows carry no distance and are excluded — the derivation
    measures only real iteration-to-iteration distances.
    """
    rows = store.conn.execute(
        "SELECT similarity FROM tripwire_events"
        " WHERE detector_kind = ? AND similarity IS NOT NULL"
        " ORDER BY id",
        (DETECTOR_DIFF_SIMILARITY,),
    ).fetchall()
    return tuple(float(r["similarity"]) for r in rows)


def derive_similarity_threshold(
    store: Store,
    *,
    quantile: float = DEFAULT_SIMILARITY_QUANTILE,
    min_observations: int = DEFAULT_MIN_OBSERVATIONS,
) -> DerivedThreshold:
    """Derive the diff-similarity kill threshold from the store's shadow log (R18)."""
    return derive_similarity_threshold_from_values(
        logged_diff_similarities(store),
        quantile=quantile,
        min_observations=min_observations,
    )


# --- R18: the tripwire enforcer (shadow detection, mode-gated action) ------------


@dataclass(frozen=True)
class TripwireEscalation:
    """The typed escalation a fired tripwire ends the Ralph loop with (R18).

    ``detector_kind`` is always one of :data:`EXISTING_TYPED_DETECTORS` — the
    record the detector has logged since Phase 1. R18 flips the *action* (the loop
    now ends) but invents NO new failure shape: this is the same typed kind, now
    consumed.
    """

    detector_kind: str
    ralph_iteration: int
    value: float | None
    threshold: float | None
    reason: str

    def __post_init__(self) -> None:
        if self.detector_kind not in EXISTING_TYPED_DETECTORS:
            raise EnforcementError(
                f"tripwire escalation kind {self.detector_kind!r} is not an"
                " existing typed detector record (R18 invents no new failure"
                f" shape); expected one of {EXISTING_TYPED_DETECTORS}"
            )


@dataclass(frozen=True)
class IterationDecision:
    """One iteration's enforcement decision.

    ``would_kill`` is the pure DETECTION result (identical in shadow and enforce);
    ``action`` is the only thing the mode changes — ``kill`` in enforce mode on a
    firing, ``continue`` everywhere else. ``escalation`` is populated whenever a
    detector fires (so shadow can report what it *would* have killed), but only an
    enforce-mode ``kill`` action ends the loop.
    """

    ralph_iteration: int
    would_kill: bool
    detector_kind: str | None
    action: str
    escalation: TripwireEscalation | None


class TripwireEnforcer:
    """Evaluates each Ralph iteration against the armed kill detectors (R18).

    Detection is mode-independent: ``diff_similarity`` fires when the
    consecutive-iteration cosine is at or above the derived ``similarity_threshold``
    (the same ``>=`` predicate the shadow detector logs with), and ``no_progress``
    fires on a confirmed repeated non-empty failure set (the bool the shadow
    detector already computed). The ``mode`` decides only whether a firing ends the
    loop.
    """

    def __init__(
        self,
        *,
        mode: str,
        similarity_threshold: float,
        armed_detectors: Sequence[str] = KILL_DETECTOR_ORDER,
    ) -> None:
        if mode not in ENFORCEMENT_MODES:
            raise EnforcementError(
                f"mode must be one of {ENFORCEMENT_MODES}, got {mode!r}"
            )
        if not (0.0 <= float(similarity_threshold) <= 1.0):
            raise EnforcementError(
                "similarity_threshold must be a cosine in [0.0, 1.0], got"
                f" {similarity_threshold} (derive it from the logged distribution"
                " via derive_similarity_threshold, never a hardcoded constant)"
            )
        unknown = [d for d in armed_detectors if d not in EXISTING_TYPED_DETECTORS]
        if unknown:
            raise EnforcementError(
                f"armed_detectors {unknown} are not existing typed detectors"
                f" (R18 arms only {EXISTING_TYPED_DETECTORS})"
            )
        self.mode = mode
        self.similarity_threshold = float(similarity_threshold)
        # Evaluate in the canonical order, restricted to the armed set.
        self.armed = tuple(d for d in KILL_DETECTOR_ORDER if d in set(armed_detectors))

    def evaluate(
        self,
        *,
        ralph_iteration: int,
        similarity: float | None = None,
        no_progress_fired: bool = False,
    ) -> IterationDecision:
        """Evaluate one completed iteration; return its detection + mode-gated action."""
        for detector in self.armed:
            fired, value, reason = self._fires(
                detector, similarity, no_progress_fired
            )
            if not fired:
                continue
            escalation = TripwireEscalation(
                detector_kind=detector,
                ralph_iteration=ralph_iteration,
                value=value,
                threshold=(
                    self.similarity_threshold
                    if detector == DETECTOR_DIFF_SIMILARITY
                    else None
                ),
                reason=reason,
            )
            action = ACTION_KILL if self.mode == MODE_ENFORCE else ACTION_CONTINUE
            if action == ACTION_KILL:
                logger.warning(
                    "tripwire KILL at iteration %d: %s", ralph_iteration, reason
                )
            return IterationDecision(
                ralph_iteration=ralph_iteration,
                would_kill=True,
                detector_kind=detector,
                action=action,
                escalation=escalation,
            )
        return IterationDecision(
            ralph_iteration=ralph_iteration,
            would_kill=False,
            detector_kind=None,
            action=ACTION_CONTINUE,
            escalation=None,
        )

    def _fires(
        self, detector: str, similarity: float | None, no_progress_fired: bool
    ) -> tuple[bool, float | None, str]:
        if detector == DETECTOR_DIFF_SIMILARITY:
            if similarity is None:
                return (False, None, "")
            fired = similarity >= self.similarity_threshold
            return (
                fired,
                similarity,
                f"diff_similarity {similarity:.4f} >= threshold"
                f" {self.similarity_threshold:.4f}: the worker is reproducing its"
                " own near-identical diff (stuck)",
            )
        # DETECTOR_NO_PROGRESS — binary, no threshold (the shadow detector's bool).
        return (
            bool(no_progress_fired),
            None,
            "no_progress: the same non-empty failure set repeated across"
            " consecutive iterations (stalled)",
        )


@dataclass(frozen=True)
class ModeRun:
    """One mode's pass over a signal stream: per-iteration decisions + the kill."""

    mode: str
    decisions: tuple[IterationDecision, ...]
    killed_at: int | None  # ralph_iteration the loop ended at (enforce only), else None


@dataclass(frozen=True)
class ABReport:
    """The shadow-vs-enforce A/B result (Unit Verification).

    ``detection_identical`` is the core claim: over the prefix both modes process,
    the per-iteration ``would_kill`` flags match exactly. ``action_differs`` is the
    only intended difference — enforce ends the loop at the first firing where
    shadow logs and continues.
    """

    shadow: ModeRun
    enforce: ModeRun
    detection_identical: bool
    action_differs: bool
    first_firing_iteration: int | None


def _run_mode(
    enforcer: TripwireEnforcer, signals: Sequence[dict], stop_on_kill: bool
) -> ModeRun:
    decisions: list[IterationDecision] = []
    killed_at: int | None = None
    for signal in signals:
        decision = enforcer.evaluate(
            ralph_iteration=signal["ralph_iteration"],
            similarity=signal.get("similarity"),
            no_progress_fired=signal.get("no_progress_fired", False),
        )
        decisions.append(decision)
        if decision.action == ACTION_KILL:
            killed_at = decision.ralph_iteration
            if stop_on_kill:
                break
    return ModeRun(
        mode=enforcer.mode, decisions=tuple(decisions), killed_at=killed_at
    )


def shadow_vs_enforce(
    signals: Sequence[dict],
    *,
    similarity_threshold: float,
    armed_detectors: Sequence[str] = KILL_DETECTOR_ORDER,
) -> ABReport:
    """Run the same signal stream through shadow and enforce mode (Verification).

    Each ``signal`` is ``{"ralph_iteration", "similarity"?, "no_progress_fired"?}``.
    Shadow processes every iteration (it never kills); enforce ends at the first
    firing. The report proves DETECTION is identical over the shared prefix and the
    only difference is the ACTION taken.
    """
    shadow = _run_mode(
        TripwireEnforcer(
            mode=MODE_SHADOW,
            similarity_threshold=similarity_threshold,
            armed_detectors=armed_detectors,
        ),
        signals,
        stop_on_kill=False,
    )
    enforce = _run_mode(
        TripwireEnforcer(
            mode=MODE_ENFORCE,
            similarity_threshold=similarity_threshold,
            armed_detectors=armed_detectors,
        ),
        signals,
        stop_on_kill=True,
    )
    # Identical detection over the prefix enforce actually processed.
    prefix = len(enforce.decisions)
    detection_identical = all(
        shadow.decisions[i].would_kill == enforce.decisions[i].would_kill
        and shadow.decisions[i].detector_kind == enforce.decisions[i].detector_kind
        for i in range(prefix)
    )
    action_differs = (
        enforce.killed_at is not None
        and shadow.killed_at is None
        and all(d.action == ACTION_CONTINUE for d in shadow.decisions)
    )
    return ABReport(
        shadow=shadow,
        enforce=enforce,
        detection_identical=detection_identical,
        action_differs=action_differs,
        first_firing_iteration=enforce.killed_at,
    )


# --- R19: suspect-verdict re-verification before fitness counting ----------------

# A re-verification of a ticket's acceptance: ticket_id -> did it pass on a clean
# re-run? The live binding re-executes the verifier; the suite injects a fake.
ReverifyFn = Callable[[str], bool]


@dataclass(frozen=True)
class SuspectTicket:
    """A ticket whose closing verdict used CHK rows, some possibly suspect (R19).

    ``closing_chk_ids`` are the CHK ids that closed the ticket; if any is in the
    audit's accumulated suspect set, the ticket's fitness must not count until a
    clean re-verification.
    """

    ticket_id: str
    closing_chk_ids: tuple[str, ...]


@dataclass(frozen=True)
class FitnessGateResult:
    """Whether one ticket's fitness counts, and why (R19).

    ``reverified`` is ``None`` for a non-suspect ticket (no re-verification was
    needed); ``True``/``False`` records a suspect ticket's clean re-run outcome.
    """

    ticket_id: str
    suspect: bool
    reverified: bool | None
    fitness_counts: bool
    reason: str


def gate_suspect_fitness(
    tickets: Sequence[SuspectTicket],
    suspect_chk_ids: Sequence[str],
    reverify_fn: ReverifyFn,
) -> tuple[FitnessGateResult, ...]:
    """Hold suspect-verifier tickets out of fitness until re-verified (R19).

    A ticket whose closing CHK is in ``suspect_chk_ids`` (a verifier a mutation
    audit flagged false-passed; see ``calibrate.suspect_chk_ids``) is re-verified
    BEFORE its fitness event counts: only a clean re-run (``reverify_fn`` returns
    True) lets the fitness land. A non-suspect ticket counts immediately — no
    re-verification, no cost. The re-verification is the injected seam (the live
    binding re-runs the verifier); this function owns only the gating logic.
    """
    suspect_set = set(suspect_chk_ids)
    results: list[FitnessGateResult] = []
    for ticket in tickets:
        tainted = [c for c in ticket.closing_chk_ids if c in suspect_set]
        if not tainted:
            results.append(
                FitnessGateResult(
                    ticket_id=ticket.ticket_id,
                    suspect=False,
                    reverified=None,
                    fitness_counts=True,
                    reason="not closed by a flagged verifier; fitness counts",
                )
            )
            continue
        reverified = bool(reverify_fn(ticket.ticket_id))
        results.append(
            FitnessGateResult(
                ticket_id=ticket.ticket_id,
                suspect=True,
                reverified=reverified,
                fitness_counts=reverified,
                reason=(
                    f"closed by a flagged verifier (suspect CHK(s) {tainted});"
                    + (
                        " re-verified clean, fitness counts"
                        if reverified
                        else " re-verification FAILED, fitness withheld"
                    )
                ),
            )
        )
    return tuple(results)


# --- R19: instrument_suspect episode-score exclusion from curriculum -------------


@dataclass(frozen=True)
class EpisodeScore:
    """A settled episode score with its instrument-suspect flag (R19).

    ``instrument_suspect`` is the episode-level flag Plan 4's harness wrote (the
    frozen-replay drift signal) — already excluded from Plan 4's SPC; here it is
    ADDITIONALLY excluded from curriculum decisions.
    """

    episode_id: int
    target: str
    epoch: int
    score: float
    instrument_suspect: bool = False


def curriculum_eligible_scores(
    scores: Sequence[EpisodeScore],
) -> tuple[EpisodeScore, ...]:
    """The episode scores curriculum decisions may read — suspect ones removed (R19).

    A suspect score never moves a rotation, a control-chart recompute, or any other
    curriculum decision. Feed the result wherever a clean score series is required
    (the same posture as ``validate.clean_scores`` one level down).
    """
    return tuple(s for s in scores if not s.instrument_suspect)


def eligible_score_values(scores: Sequence[EpisodeScore]) -> tuple[float, ...]:
    """The bare float series of the curriculum-eligible scores (chart-recompute input)."""
    return tuple(s.score for s in curriculum_eligible_scores(scores))


# --- R20: question-budget annealing ----------------------------------------------

# PROVENANCE: DESIGN §17 — "question-budget annealing: budget tightens across
# epochs toward 3-5". The floor sits in the middle of that band; the start is the
# Phase-2 wide budget. TUNING METRIC: elicitation-efficiency vs. unverified-risk
# rate across epochs (the qa_log budget_counted telemetry).
DEFAULT_QUESTION_BUDGET_START = 8
DEFAULT_QUESTION_BUDGET_FLOOR = 4
DEFAULT_QUESTION_BUDGET_STEP = 1

# The §17 target band the floor must land inside — a guard against annealing to a
# budget so tight elicitation collapses, or so loose it never tightens.
QUESTION_BUDGET_TARGET_BAND = (3, 5)


@dataclass(frozen=True)
class AnnealingSchedule:
    """The per-epoch question budget, tightening toward the §17 3-5 floor (R20).

    A pure config schedule over the epoch counter — no new machinery (R20): the
    budget steps down ``step`` per epoch from ``start`` and clamps at ``floor``.
    The floor must sit inside the §17 target band so the anneal converges where the
    design says it should.
    """

    start: int = DEFAULT_QUESTION_BUDGET_START
    floor: int = DEFAULT_QUESTION_BUDGET_FLOOR
    step: int = DEFAULT_QUESTION_BUDGET_STEP

    def __post_init__(self) -> None:
        lo, hi = QUESTION_BUDGET_TARGET_BAND
        if not (lo <= self.floor <= hi):
            raise EnforcementError(
                f"annealing floor must land in the §17 target band [{lo}, {hi}],"
                f" got {self.floor}"
            )
        if self.start < self.floor:
            raise EnforcementError(
                f"annealing start {self.start} must be >= floor {self.floor}"
            )
        if self.step < 0:
            raise EnforcementError(
                f"annealing step must be >= 0, got {self.step}"
            )

    def budget_for_epoch(self, epoch: int) -> int:
        """The question budget at ``epoch`` (0-based): ``max(floor, start - step·epoch)``."""
        if epoch < 0:
            raise EnforcementError(f"epoch must be >= 0, got {epoch}")
        return max(self.floor, self.start - self.step * epoch)


# --- R20: persona-rotation plateau trigger ---------------------------------------

# PROVENANCE: DESIGN §9 — persona rotation fires "only if planner scores plateau".
# A plateau is improvement over the window at or below the tolerance; the window
# and tolerance are the logged-telemetry config. TUNING METRIC: planner-score
# recovery after a rotation vs. churn from rotating on noise.
DEFAULT_PLATEAU_WINDOW = 3
DEFAULT_PLATEAU_MIN_IMPROVEMENT = 0.01


@dataclass(frozen=True)
class PersonaRotationRule:
    """Fires persona rotation only on a planner-score plateau (R20 / §9).

    Reads the logged planner-score series (no new machinery): a plateau is when the
    improvement across the last ``window`` points is at or below
    ``min_improvement``. A healthy upward trend never rotates — rotation is the
    response to a STALLED planner, not a scheduled event.
    """

    window: int = DEFAULT_PLATEAU_WINDOW
    min_improvement: float = DEFAULT_PLATEAU_MIN_IMPROVEMENT

    def __post_init__(self) -> None:
        if self.window < 2:
            raise EnforcementError(
                f"plateau window must be >= 2 (an improvement needs two points),"
                f" got {self.window}"
            )
        if self.min_improvement < 0:
            raise EnforcementError(
                f"min_improvement must be >= 0, got {self.min_improvement}"
            )

    def plateaued(self, planner_scores: Sequence[float]) -> bool:
        """True iff the last ``window`` planner scores improved by <= ``min_improvement``.

        Improvement is ``best_recent - first_recent`` over the window (the best the
        planner reached minus where the window began) — so a late dip inside a
        still-improving window does not falsely read as a plateau, and a flat or
        declining window does. Fewer than ``window`` points is *not* a plateau
        (insufficient evidence — the small-N guard).
        """
        if len(planner_scores) < self.window:
            return False
        recent = planner_scores[-self.window:]
        improvement = max(recent) - recent[0]
        return improvement <= self.min_improvement


def should_rotate_personas(
    planner_scores: Sequence[float], rule: PersonaRotationRule = PersonaRotationRule()
) -> bool:
    """Whether to rotate planner personas now: only on a plateau (R20 / §9)."""
    return rule.plateaued(planner_scores)
