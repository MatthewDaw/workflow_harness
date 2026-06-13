"""Reflector validation + promotion lifecycle (plan-004 U7, R15-R18).

Stage B leaves a quarantined batch behind. This module is the gate that decides
whether that batch ever reaches the active set — and the discipline that keeps a
plausible-but-wrong reflection out by default (DESIGN §5, §17).

The contract, in the order the loop runs it:

1. **Optional trial replay** (R15a) — for each failing increment, re-execute the
   originating SCENs with the batch active (a ``mode=trial`` worktree branched
   from the increment-base ref). Pass = the source SCENs pass on re-execution
   (verifier-pass alone is too weak — the AC may itself be the bug). The replay
   is **diagnostic only**, budgeted by a per-batch validation cost ceiling, and
   **skippable** when quota is tight. The live worktree bring-up / SCEN
   re-execution is the docker-required deliverable; this module consumes a typed
   :class:`ReplayResult` so the decision logic is exercised offline.

2. **Required benchmark gate** (R15b) — the held-out Kanboard micro-benchmark
   (U4) must not regress. **Benchmark wins all conflicts**: replay-pass +
   benchmark-regress reverts; replay-fail + benchmark-pass promotes with a
   ``replay_miss`` telemetry flag.

3. **Decision rule** — bootstrap vs. post-bootstrap:

   - **Bootstrap** (R16, until ≥``min_benchmark_points`` benchmark points AND the
     ≥``min_replay_pairs`` frozen replay set is measured): revert on a score drop
     greater than ``max(2× benchmark-instrument σ, 5pp on must-tier)``, and a
     human **co-signs every promote** (the affirmative, risky action). A revert
     is default-deny and never blocks on a co-sign — keeping a batch out requires
     no human action.
   - **Post-bootstrap** (R16): an individuals control chart over the benchmark
     scores (σ from the replicate distribution). Act on **special-cause only** —
     a point below the lower control limit reverts; a point inside the limits is
     common-cause noise and promotes. Scores flagged ``instrument_suspect`` are
     excluded from the chart (R18).

4. **Promote / auto-revert through the single-writer queue** (R17, Phase 0
   mechanics): default deny, one minted snapshot per decision, the record keyed
   ``(batch, snapshot)``. N=1 batches in this plan — the per-batch keying is the
   Plan 5 parallel-merge seam.

5. **Frozen-replay re-judging harness** (R18, deferred from Plan 3): on a cadence,
   re-judge the frozen replay set's stored judge inputs; when verdict drift
   exceeds the tolerance, raise ``instrument_suspect`` and propagate it to every
   benchmark score recorded since the last clean replay. This plan **excludes
   flagged scores from its own SPC computations**; curriculum-level consumption is
   Plan 5's half of the contract.

Tunables are caller-supplied via :class:`ValidateParams` (the U2/U3/U5/U6
precedent — the carried defaults record PROVENANCE, the run-assembly wiring routes
the live values from ``thresholds.toml``; nothing here reads config directly).

Offline by construction: the benchmark/replay episodes and the re-judge call are
injected (typed results / a ``rejudge_fn`` seam), so the full decision table runs
with zero quota and no ``claude`` on PATH. The promote/revert path drives the real
store queue.

## Conformance

Test-scenario / invariant (plan-004 U7) -> test (in ``tests/test_validate.py``):

- a registered batch that has not passed the benchmark gate stays quarantined and
  absent from the active set: ``test_unvalidated_batch_stays_quarantined``
- a benchmark regression auto-reverts (default-deny, no human action):
  ``test_benchmark_regression_auto_reverts``
- promotion happens ONLY on benchmark non-regression (benchmark wins all
  conflicts): ``test_promotion_requires_benchmark_pass``
- replay-pass + benchmark-regress reverts:
  ``test_replay_pass_benchmark_regress_reverts``
- replay-fail + benchmark-pass promotes with a replay_miss flag:
  ``test_replay_fail_benchmark_pass_promotes_with_replay_miss``
- bootstrap threshold math (2σ vs 5pp max): ``test_bootstrap_threshold_math``
- co-sign required during bootstrap and not after:
  ``test_cosign_required_during_bootstrap_not_after``
- SPC limits over ≥10 points flag only special-cause:
  ``test_spc_flags_only_special_cause``
- reverted batch leaves no active insights, the record explains why:
  ``test_reverted_batch_leaves_no_active_insights``
- instrument_suspect propagates to scores since the last clean replay:
  ``test_instrument_suspect_propagates_to_scores_since_last_clean``
- the full decision table (replay × benchmark × bootstrap) has a 1:1 test:
  ``test_decision_table``

Required acceptance test / invariant (plan-007 U10) -> test (in
``tests/test_provenance_telemetry.py``):

- ``test_elicitation_batch_quarantined_pre_substrate`` — before U13b's substrate
  exists, an ``elicitation``-class batch stays quarantined with a TYPED hold
  reason (default-deny; never silently validated on the wrong instrument).
  [enforced by :func:`route_substrates` + the :func:`validate_batch` guard]

Required acceptance test / invariant (plan-008 U8, R19) -> test (in
``tests/test_stage_b.py``):

- ``test_validation_record_names_retired_incumbent`` — promoting a reflector batch
  that retires a contradicted incumbent (the R3 deferred-supersede) surfaces
  ``LifecycleResult.retired_incumbent_ids`` on the :class:`BatchValidationOutcome`
  AND names them in the persisted ``batch_validations.detail`` — the retirement is
  named, never silent. [enforced by :func:`validate_batch` forwarding the NLI seam
  to :func:`lifecycle.promote_batch` and threading its result through]
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace

from agent_families import lifecycle
from agent_families.store import VALIDATION_CLASSES, Store

logger = logging.getLogger(__name__)

VERDICT_PROMOTE = "promote"
VERDICT_REVERT = "revert"


class ValidateError(Exception):
    """A broken validation precondition with an actionable message."""


class CosignRequiredError(ValidateError):
    """A bootstrap promotion was attempted without a human co-sign (R16).

    Raised BEFORE any active-set mutation — the batch stays quarantined, so a
    missing co-sign defaults to deny (nothing is written).
    """


class SubstrateHeldError(ValidateError):
    """An elicitation-class batch was validated before its greenfield substrate
    exists (plan-007 U13b).

    Raised BEFORE any active-set mutation — the batch stays quarantined
    (default-deny), never validated on the brownfield instrument alone (007 KTD5).
    """


# --- validation-class substrate routing (007 U10, KTD5) -------------------------
#
# One shared library serves every family by retrieval, so a greenfield-optimal
# insight ("always propose options") can silently degrade brownfield (where the
# explorer answers open questions perfectly), and per-batch rollback attribution
# is gone by the time SPC curves show it. The fix is dual-substrate validation
# keyed off ``batches.validation_class`` (007 U1):
#
#   code        -> the existing brownfield frozen benchmark only
#   elicitation -> BOTH the greenfield episode benchmark (U13b) AND the brownfield
#                  frozen benchmark, non-negative on EACH
#   general     -> BOTH, non-negative on each (when both exist)
#
# The greenfield substrate does not exist until plan-007 U13b. Until then an
# elicitation batch CANNOT be validated honestly — brownfield-alone is the WRONG
# instrument for an insight whose training gradient is greenfield-specific — so it
# is HELD in quarantine (default-deny) with a typed reason rather than silently
# passed on the wrong instrument. ``code``/``general`` keep a valid brownfield
# instrument and proceed; ``general`` additionally routes through greenfield once
# U13b wires it. U13b flips ``greenfield_available`` true and the hold lifts.

BROWNFIELD_SUBSTRATE = "brownfield_frozen_benchmark"
GREENFIELD_SUBSTRATE = "greenfield_episode_benchmark"

# The substrates each class must clear, non-negative on EACH (007 KTD5).
_REQUIRED_SUBSTRATES: dict[str, tuple[str, ...]] = {
    "code": (BROWNFIELD_SUBSTRATE,),
    "elicitation": (GREENFIELD_SUBSTRATE, BROWNFIELD_SUBSTRATE),
    "general": (GREENFIELD_SUBSTRATE, BROWNFIELD_SUBSTRATE),
}

# Classes for which the greenfield substrate is LOAD-BEARING: validating them on
# the brownfield instrument alone would be the wrong instrument, so they are held
# until U13b. ``code``/``general`` have a valid brownfield instrument and are not
# held (``general`` opportunistically gains greenfield once it exists).
_GREENFIELD_REQUIRED = frozenset({"elicitation"})


@dataclass(frozen=True)
class SubstrateRouting:
    """How a batch's ``validation_class`` routes to substrates (007 KTD5)."""

    validation_class: str
    substrates: tuple[str, ...]  # the available substrates to validate against
    held: bool  # True -> stays quarantined (a required substrate is missing)
    hold_reason: str  # the typed hold reason when held, else ""


def route_substrates(
    validation_class: str, *, greenfield_available: bool
) -> SubstrateRouting:
    """Resolve a ``validation_class`` to the substrates it must clear (007 KTD5).

    When the greenfield substrate is unavailable and the class load-bears on it
    (``elicitation``), the routing is HELD: the batch stays quarantined rather
    than validate on the brownfield instrument alone. ``code``/``general`` resolve
    to the substrates that currently exist (``general`` gains greenfield at U13b).
    """
    if validation_class not in VALIDATION_CLASSES:
        raise ValidateError(
            f"unknown validation_class '{validation_class}'"
            f" (expected one of {VALIDATION_CLASSES})"
        )
    if not greenfield_available and validation_class in _GREENFIELD_REQUIRED:
        return SubstrateRouting(
            validation_class=validation_class,
            substrates=(),
            held=True,
            hold_reason=(
                f"validation_class '{validation_class}' requires the greenfield"
                " episode benchmark substrate (plan-007 U13b), which does not"
                " exist yet; the batch stays quarantined (default-deny) rather"
                " than validate on the brownfield instrument alone (KTD5)"
            ),
        )
    substrates = tuple(
        s
        for s in _REQUIRED_SUBSTRATES[validation_class]
        if s != GREENFIELD_SUBSTRATE or greenfield_available
    )
    return SubstrateRouting(
        validation_class=validation_class,
        substrates=substrates,
        held=False,
        hold_reason="",
    )


def _batch_validation_class(store: Store, batch_label: str) -> str:
    row = store.conn.execute(
        "SELECT validation_class FROM batches WHERE label = ?", (batch_label,)
    ).fetchone()
    if row is None:
        raise ValidateError(f"unknown batch '{batch_label}'")
    return row["validation_class"]


def route_batch_substrates(
    store: Store, batch_label: str, *, greenfield_available: bool
) -> SubstrateRouting:
    """Substrate routing for a stored batch, read off its ``validation_class``."""
    return route_substrates(
        _batch_validation_class(store, batch_label),
        greenfield_available=greenfield_available,
    )


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) ----

# PROVENANCE: R16 — bootstrap lasts "until ≥10 benchmark points". TUNING METRIC:
# benchmark-instrument σ stability vs. number of replicate points.
DEFAULT_MIN_BENCHMARK_POINTS = 10

# PROVENANCE: R16 / calibrate.FROZEN_SET_SIZE_FLOOR — "the ≥20-pair frozen replay
# set is measured". TUNING METRIC: grader-replay σ stability vs. frozen-set N.
DEFAULT_MIN_REPLAY_PAIRS = 20

# PROVENANCE: R16 — bootstrap revert at "score drop > max(2× σ, 5pp on must-tier)".
# TUNING METRIC: false-revert vs. false-promote rate on the calibration corpus.
DEFAULT_BOOTSTRAP_SIGMA_MULTIPLIER = 2.0
DEFAULT_MUST_TIER_PP_FLOOR = 0.05

# PROVENANCE: DESIGN §17 SPC discipline — individuals chart, 3σ control limits.
# TUNING METRIC: special-cause detection rate vs. false-alarm rate.
DEFAULT_SPC_SIGMA_MULTIPLE = 3.0

# PROVENANCE: R15a — replay is "budgeted by a per-batch validation cost ceiling,
# skippable when quota-tight". 0.0 disables the ceiling (caller meters spend).
# TUNING METRIC: replay diagnostic value vs. validation quota burn.
DEFAULT_VALIDATION_COST_CEILING_USD = 0.0

# PROVENANCE: R18 — frozen-replay re-judging "cadence" (benchmark points between
# re-judgings) and "drift tolerance" (fraction of verdict flips that raises
# instrument_suspect). TUNING METRIC: drift detection lead time vs. re-judge cost.
DEFAULT_REJUDGE_CADENCE = 5
DEFAULT_REJUDGE_DRIFT_TOLERANCE = 0.1


@dataclass(frozen=True)
class ValidateParams:
    """The validation tunables, caller-supplied (no hidden config reads)."""

    min_benchmark_points: int = DEFAULT_MIN_BENCHMARK_POINTS
    min_replay_pairs: int = DEFAULT_MIN_REPLAY_PAIRS
    bootstrap_sigma_multiplier: float = DEFAULT_BOOTSTRAP_SIGMA_MULTIPLIER
    must_tier_pp_floor: float = DEFAULT_MUST_TIER_PP_FLOOR
    spc_sigma_multiple: float = DEFAULT_SPC_SIGMA_MULTIPLE
    validation_cost_ceiling_usd: float = DEFAULT_VALIDATION_COST_CEILING_USD
    rejudge_cadence: int = DEFAULT_REJUDGE_CADENCE
    rejudge_drift_tolerance: float = DEFAULT_REJUDGE_DRIFT_TOLERANCE

    def __post_init__(self) -> None:
        if self.min_benchmark_points < 1:
            raise ValidateError(
                f"min_benchmark_points must be >= 1, got {self.min_benchmark_points}"
            )
        if self.min_replay_pairs < 1:
            raise ValidateError(
                f"min_replay_pairs must be >= 1, got {self.min_replay_pairs}"
            )
        if self.bootstrap_sigma_multiplier < 0:
            raise ValidateError(
                "bootstrap_sigma_multiplier must be >= 0, got"
                f" {self.bootstrap_sigma_multiplier}"
            )
        if not (0.0 <= self.must_tier_pp_floor <= 1.0):
            raise ValidateError(
                f"must_tier_pp_floor must be in [0, 1], got {self.must_tier_pp_floor}"
            )
        if self.spc_sigma_multiple <= 0:
            raise ValidateError(
                f"spc_sigma_multiple must be > 0, got {self.spc_sigma_multiple}"
            )
        if self.validation_cost_ceiling_usd < 0:
            raise ValidateError(
                "validation_cost_ceiling_usd must be >= 0, got"
                f" {self.validation_cost_ceiling_usd}"
            )
        if self.rejudge_cadence < 1:
            raise ValidateError(
                f"rejudge_cadence must be >= 1, got {self.rejudge_cadence}"
            )
        if not (0.0 <= self.rejudge_drift_tolerance <= 1.0):
            raise ValidateError(
                "rejudge_drift_tolerance must be in [0, 1], got"
                f" {self.rejudge_drift_tolerance}"
            )


# --- typed validation inputs ----------------------------------------------------


@dataclass(frozen=True)
class ReplayResult:
    """The optional failed-slice replay's diagnostic outcome (R15a).

    ``ran=False`` means the replay was skipped (quota-tight); ``passed`` is only
    meaningful when ``ran`` is true. The replay never decides the verdict on its
    own — the benchmark wins all conflicts (R15b).
    """

    ran: bool
    passed: bool = False
    scen_ids: tuple[str, ...] = ()
    cost_usd: float = 0.0
    detail: str = ""


@dataclass(frozen=True)
class BenchmarkOutcome:
    """The required benchmark gate's measured inputs (R15b/R16).

    ``candidate`` is the new benchmark score measured with the batch under test;
    ``history`` is the **clean** (non-instrument-suspect) prior benchmark scores
    at the validating snapshot — the SPC chart and the bootstrap baseline both
    read it. ``sigma`` is the benchmark-instrument σ from the replicate
    distribution (U4 ``benchmark_sigma``), not computed from ``history``.
    """

    candidate: float
    sigma: float
    history: tuple[float, ...]

    def __post_init__(self) -> None:
        if not self.history:
            raise ValidateError(
                "benchmark history is empty — the validating snapshot must carry"
                " at least one prior benchmark replicate to measure regression"
                " against (R16 replicate distribution)"
            )
        if self.sigma < 0:
            raise ValidateError(f"benchmark sigma must be >= 0, got {self.sigma}")

    @property
    def baseline(self) -> float:
        """The reference score: the mean of the clean benchmark history."""
        return statistics.fmean(self.history)

    @property
    def n_points(self) -> int:
        return len(self.history)

    @property
    def drop(self) -> float:
        """Regression magnitude: how far the candidate fell below the baseline."""
        return self.baseline - self.candidate


# --- pure decision layer (R15b/R16) ---------------------------------------------


def is_bootstrap(
    n_benchmark_points: int, n_replay_pairs: int, params: ValidateParams
) -> bool:
    """True while still in the bootstrap regime (R16).

    Bootstrap ends only when BOTH gates clear: ≥``min_benchmark_points`` benchmark
    points AND a ≥``min_replay_pairs`` frozen replay set measured.
    """
    return not (
        n_benchmark_points >= params.min_benchmark_points
        and n_replay_pairs >= params.min_replay_pairs
    )


def bootstrap_revert_threshold(sigma: float, params: ValidateParams) -> float:
    """The bootstrap revert threshold: ``max(2× σ, 5pp on must-tier)`` (R16)."""
    return max(
        params.bootstrap_sigma_multiplier * sigma, params.must_tier_pp_floor
    )


@dataclass(frozen=True)
class SpcLimits:
    """Individuals-chart control limits (R16): center ± k·σ."""

    center: float
    lcl: float
    ucl: float


def spc_limits(
    history: Sequence[float], sigma: float, params: ValidateParams
) -> SpcLimits:
    """Individuals control chart over the clean benchmark history (R16).

    Center is the history mean; the limits are ``center ± spc_sigma_multiple·σ``
    with σ from the replicate distribution (NOT estimated from the moving range
    of these few points). The caller must already have excluded
    instrument-suspect scores (R18).
    """
    if not history:
        raise ValidateError("SPC limits need a non-empty benchmark history (R16)")
    center = statistics.fmean(history)
    half = params.spc_sigma_multiple * sigma
    return SpcLimits(center=center, lcl=center - half, ucl=center + half)


@dataclass(frozen=True)
class ValidationDecision:
    """The benchmark gate's verdict plus the telemetry that explains it."""

    verdict: str  # VERDICT_PROMOTE | VERDICT_REVERT
    bootstrap: bool
    regressed: bool
    replay_miss: bool
    threshold: float | None  # bootstrap revert threshold, or the SPC LCL
    limits: SpcLimits | None  # post-bootstrap only
    reason: str

    @property
    def promotes(self) -> bool:
        return self.verdict == VERDICT_PROMOTE


def decide_validation(
    benchmark: BenchmarkOutcome,
    *,
    bootstrap: bool,
    params: ValidateParams,
    replay: ReplayResult | None = None,
) -> ValidationDecision:
    """Decide promote vs. revert from the benchmark gate (R15b/R16).

    Benchmark wins all conflicts: the verdict is a pure function of the benchmark
    measurement; the replay contributes only the ``replay_miss`` telemetry flag
    (replay-fail but benchmark-pass → promote-with-flag).
    """
    if bootstrap:
        threshold = bootstrap_revert_threshold(benchmark.sigma, params)
        regressed = benchmark.drop > threshold
        limits = None
        reason = (
            f"bootstrap: drop {benchmark.drop:.4f} "
            f"{'>' if regressed else '<='} threshold {threshold:.4f}"
            f" (max(2σ={benchmark.sigma:.4f}, {params.must_tier_pp_floor:.4f}));"
            f" baseline {benchmark.baseline:.4f}, candidate {benchmark.candidate:.4f}"
        )
    else:
        limits = spc_limits(benchmark.history, benchmark.sigma, params)
        regressed = benchmark.candidate < limits.lcl
        threshold = limits.lcl
        reason = (
            f"spc: candidate {benchmark.candidate:.4f} "
            f"{'<' if regressed else '>='} LCL {limits.lcl:.4f}"
            f" (center {limits.center:.4f}, σ {benchmark.sigma:.4f});"
            f" {'special-cause low' if regressed else 'common-cause'}"
        )

    verdict = VERDICT_REVERT if regressed else VERDICT_PROMOTE
    replay_miss = bool(
        replay is not None and replay.ran and not replay.passed and not regressed
    )
    if replay_miss:
        reason += " | replay_miss: replay-fail but benchmark-pass (benchmark wins)"
    elif replay is not None and replay.ran and replay.passed and regressed:
        reason += " | replay-pass overridden by benchmark regression (benchmark wins)"

    return ValidationDecision(
        verdict=verdict,
        bootstrap=bootstrap,
        regressed=regressed,
        replay_miss=replay_miss,
        threshold=threshold,
        limits=limits,
        reason=reason,
    )


# --- promote / auto-revert lifecycle (R17) --------------------------------------


@dataclass(frozen=True)
class BatchValidationOutcome:
    """One validated batch: the decision, the record, and the resulting state."""

    batch_label: str
    decision: ValidationDecision
    validation_id: int
    lifecycle_snapshot_id: int
    cosigned_by: str | None
    promoted: bool
    active_insight_ids: tuple[int, ...]
    # R3 deferred-supersede (008 R16/U8): incumbents this promotion retired (a
    # validated challenger beat a contradicted incumbent), and contradiction-flag
    # rows a revert closed. Surfaced so the batch_validations record NAMES them.
    retired_incumbent_ids: tuple[int, ...] = ()
    closed_contradiction_ids: tuple[int, ...] = ()


CosignFn = Callable[[ValidationDecision], "str | None"]


def _batch_id(store: Store, batch_label: str) -> int:
    row = store.conn.execute(
        "SELECT id FROM batches WHERE label = ?", (batch_label,)
    ).fetchone()
    if row is None:
        raise ValidateError(f"unknown batch '{batch_label}'")
    return row["id"]


def active_batch_insight_ids(store: Store, batch_label: str) -> tuple[int, ...]:
    """The batch's currently-active insight ids (empty while quarantined).

    The default-deny probe: a batch that never cleared the benchmark gate has no
    active members (R17).
    """
    row = store.conn.execute(
        "SELECT id FROM batches WHERE label = ?", (batch_label,)
    ).fetchone()
    if row is None:
        return ()
    rows = store.conn.execute(
        "SELECT id FROM insights WHERE batch_id = ? AND status = 'active'"
        " ORDER BY id ASC",
        (row["id"],),
    ).fetchall()
    return tuple(r["id"] for r in rows)


def validate_batch(
    store: Store,
    *,
    batch_label: str,
    snapshot_id: int,
    benchmark: BenchmarkOutcome,
    n_replay_pairs: int,
    replay: ReplayResult | None = None,
    cosign_fn: CosignFn | None = None,
    params: ValidateParams = ValidateParams(),
    trial_episode_id: int | None = None,
    benchmark_episode_id: int | None = None,
    greenfield_available: bool = False,
    nli_model: str | None = None,
    nli_mode: str | None = None,
    nli_fixtures_dir: object | None = None,
    nli_confidence_threshold: float = (
        lifecycle.SUPERSEDE_RECHECK_CONFIDENCE_DEFAULT
    ),
    _nli_encoder: object | None = None,
) -> BatchValidationOutcome:
    """Run the validation gate for one quarantined batch and enact the verdict.

    ``snapshot_id`` is the active snapshot the batch validates against (the
    benchmark baseline's snapshot); the promote/revert mints a NEW snapshot, and
    the ``batch_validations`` record is keyed by the validating snapshot (R17).

    Substrate routing runs first (007 KTD5): an ``elicitation``-class batch whose
    greenfield substrate does not exist yet (``greenfield_available=False`` until
    plan-007 U13b) raises :class:`SubstrateHeldError` BEFORE any mutation — the
    batch stays quarantined (default-deny), never validated on the brownfield
    instrument alone. ``code``/``general`` proceed on the existing instrument.

    During bootstrap a **promote** requires a human co-sign (``cosign_fn`` must
    return a signer id) — a missing or refused co-sign raises
    :class:`CosignRequiredError` BEFORE any mutation, so the batch stays
    quarantined (default deny). A **revert** never blocks on a co-sign (keeping a
    batch out needs no human action); a provided signer is still recorded.
    """
    batch_id = _batch_id(store, batch_label)

    routing = route_batch_substrates(
        store, batch_label, greenfield_available=greenfield_available
    )
    if routing.held:
        # Default-deny: nothing is written, so the batch stays quarantined until
        # its substrate exists (007 KTD5 / U13b).
        raise SubstrateHeldError(routing.hold_reason)

    bootstrap = is_bootstrap(benchmark.n_points, n_replay_pairs, params)
    decision = decide_validation(
        benchmark, bootstrap=bootstrap, params=params, replay=replay
    )

    cosigned_by: str | None = None
    if bootstrap and cosign_fn is not None:
        cosigned_by = cosign_fn(decision)
    if bootstrap and decision.promotes and not cosigned_by:
        # The risky affirmative action needs a human in the loop (R16). Nothing
        # has been written yet, so the batch simply stays quarantined.
        raise CosignRequiredError(
            f"batch '{batch_label}' promotion is in the bootstrap regime"
            f" ({benchmark.n_points} benchmark point(s), {n_replay_pairs} replay"
            " pair(s)) and requires a human co-sign (R16); none was provided —"
            " the batch stays quarantined (default deny)"
        )

    if decision.promotes:
        # Forward the NLI seam so the promotion-time deferred-supersede re-check
        # (008 R16) replays offline; a contradiction-free batch never loads the
        # model, so contradiction-free callers can omit these entirely.
        result = lifecycle.promote_batch(
            store,
            batch_label,
            nli_model=nli_model,
            nli_mode=nli_mode,
            nli_fixtures_dir=nli_fixtures_dir,
            nli_confidence_threshold=nli_confidence_threshold,
            _nli_encoder=_nli_encoder,
        )
    else:
        result = lifecycle.revert_batch(store, batch_label)

    retired = result.retired_incumbent_ids
    closed = result.closed_contradiction_ids
    detail = decision.reason
    if retired:
        # Name the deferred-supersede retirements in the persisted record (008 U8).
        detail += f" | deferred-supersede retired incumbents: {list(retired)}"
    if closed:
        detail += f" | closed contradictions: {list(closed)}"

    validation_id = store.insert_batch_validation(
        batch_id,
        snapshot_id=snapshot_id,
        trial_episode_id=trial_episode_id,
        benchmark_episode_id=benchmark_episode_id,
        verdict=decision.verdict,
        replay_miss=decision.replay_miss,
        bootstrap=bootstrap,
        cosigned_by=cosigned_by,
        detail=detail,
    )

    active = active_batch_insight_ids(store, batch_label)
    logger.info(
        "batch '%s' validation: %s (bootstrap=%s, replay_miss=%s, snapshot %d)",
        batch_label,
        decision.verdict,
        bootstrap,
        decision.replay_miss,
        result.snapshot_id,
    )
    return BatchValidationOutcome(
        batch_label=batch_label,
        decision=decision,
        validation_id=validation_id,
        lifecycle_snapshot_id=result.snapshot_id,
        cosigned_by=cosigned_by,
        promoted=decision.promotes,
        active_insight_ids=active,
        retired_incumbent_ids=retired,
        closed_contradiction_ids=closed,
    )


# --- frozen-replay re-judging harness (R18) -------------------------------------

# A re-judge call: the frozen pair's stored judge-input payload (R22) -> the
# re-judged verdict string. The live binding routes ``run_judge`` over the stored
# inputs; the suite injects a scripted fake.
RejudgeFn = Callable[[dict], str]


@dataclass(frozen=True)
class BenchmarkPoint:
    """A persisted benchmark score with its instrument-suspect flag (R18)."""

    snapshot_id: int
    score: float
    instrument_suspect: bool = False


@dataclass(frozen=True)
class RejudgeResult:
    """One frozen-replay re-judging pass (R18)."""

    pairs_checked: int
    flips: int
    drift: float
    instrument_suspect: bool
    flipped_scen_ids: tuple[str, ...]


def rejudge_due(points_since_last_replay: int, params: ValidateParams) -> bool:
    """True once ``rejudge_cadence`` benchmark points accrued since the last
    frozen-replay re-judging (R18 cadence)."""
    return points_since_last_replay >= params.rejudge_cadence


def rejudge_frozen_set(
    frozen: dict, rejudge_fn: RejudgeFn, params: ValidateParams
) -> RejudgeResult:
    """Re-judge a persisted frozen replay set and measure verdict drift (R18).

    ``frozen`` is a ``calibrate.load_frozen_set`` document. Each pair's stored
    ``judge_input`` is re-judged; a verdict that flips relative to the
    hand-verified ``verdict`` is drift. ``instrument_suspect`` is raised when the
    drift fraction exceeds ``rejudge_drift_tolerance`` — the grader has moved
    under the frozen instrument and the scores it produced are suspect.
    """
    pairs = frozen.get("pairs", [])
    if not pairs:
        raise ValidateError(
            "frozen replay set has no pairs to re-judge (R18) — persist the"
            " hand-verified set first (calibrate.persist_frozen_set)"
        )
    flipped: list[str] = []
    for pair in pairs:
        rejudged = rejudge_fn(pair["judge_input"])
        if rejudged != pair["verdict"]:
            flipped.append(pair["scen_id"])
    drift = len(flipped) / len(pairs)
    return RejudgeResult(
        pairs_checked=len(pairs),
        flips=len(flipped),
        drift=drift,
        instrument_suspect=drift > params.rejudge_drift_tolerance,
        flipped_scen_ids=tuple(sorted(flipped)),
    )


def flag_scores_since_last_clean(
    points: Sequence[BenchmarkPoint],
    last_clean_snapshot_id: int,
    *,
    suspect: bool = True,
) -> tuple[BenchmarkPoint, ...]:
    """Propagate ``instrument_suspect`` to every score since the last clean replay
    (R18). Points at or before ``last_clean_snapshot_id`` are untouched; later
    points are flagged when ``suspect`` is true."""
    if not suspect:
        return tuple(points)
    return tuple(
        replace(p, instrument_suspect=True)
        if p.snapshot_id > last_clean_snapshot_id
        else p
        for p in points
    )


def clean_scores(points: Sequence[BenchmarkPoint]) -> tuple[float, ...]:
    """The non-instrument-suspect scores — the SPC chart's eligible input (R18).

    This plan excludes flagged scores from its own SPC computations; pass the
    result as :pyattr:`BenchmarkOutcome.history`.
    """
    return tuple(p.score for p in points if not p.instrument_suspect)
