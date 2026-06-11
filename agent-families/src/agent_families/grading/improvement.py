"""Improvement-tier grading: grade "better," not just "same" (plan-005 U3, R9-R11).

Plan 3's settlement grades *equivalence* — does the clone behave like the target.
This module adds the second, lexicographic tier the design (§10) specifies: above a
behavioral hard gate, **capped multi-dimensional bonuses** that reward exceeding the
target — faster APIs, nicer design, cleaner structure, better UX — without ever
letting quality offset correctness or letting any one dimension be hill-climbed.

The shape, lexicographic with capped bonuses (the one form the literature agrees on
— SWE-Perf's gate-then-measure, constrained-RLHF on why optimizing a secondary
reward past a threshold destroys the primary):

1. **Hard gate (R9).** The must-tier behavioral pass rate must clear a threshold
   *derived from measured grader noise* (:func:`derive_gate_threshold`): a clone
   within grader noise of perfect clears it, grader variance alone never fails a
   genuinely-equivalent clone. Must-tier failures are panel-adjudicated upstream
   (Plan 3 settlement). Below the gate the entire bonus is **zeroed** — quality can
   never buy back correctness (:func:`gate_passes`).

2. **Capped bonuses above the gate (R10).** Four dimensions, each a raw quality
   score in ``[0, 1]``:

   - **Performance** — k6/autocannon p95 latency ratio vs. the target under an
     identical load profile, **credit capped at 2×** (a clone 2× faster maxes the
     latency credit; 4× faster scores the same), plus Core Web Vitals (LCP, INP)
     **median-of-5** against absolute budgets. Measured against a **production
     artifact** (``vite build`` + ``vite preview`` / Hono in production mode), never
     the dev server — dev-mode overhead would systematically zero the dimension; a
     build failure is a typed zero-bonus outcome (:class:`PerfMeasurement`).
   - **Visual design** — MLLM pairwise screenshot judgment, **swap-and-average**,
     invoked only for *gross* differences, **uncertain = neutral** (pairwise UI
     judging is ~90% accurate when designs clearly differ and coin-flip when close),
     scored as *absolute quality* not target-similarity (:func:`design_pairwise`).
   - **Code structure** — a criterion-separated LLM rubric (modularity, naming,
     dead-code absence, dependency health) + deterministic lockfile/lint health.
     Explicitly **not** Maintainability Index or cyclomatic complexity — documented
     folklore metrics teams satisfy while degrading real maintainability; their names
     are a hard-rejected input (:data:`FORBIDDEN_CODE_METRICS`, :class:`CodeMeasurement`).
   - **Automated UX** — axe-core accessibility, console-error absence, viewport.

3. **Anti-Goodhart guards (R11).** Each dimension is capped at ``per_dimension_cap``
   (~30%) of the bonus pool; the total bonus is ``≤ total_bonus_cap`` (~15-20%) of the
   base score; dimension weights **rotate across grading runs** deterministically by
   seed (:func:`rotate_weights`); high-bonus episodes are **spot-audit sampled** at a
   configured fraction (:func:`spot_audit_flagged`). Every gaming pattern the design
   names (verbosity bias, metric hill-climbing, test-editing) motivates one of these.

Every bonus component is itemized in the grade's report (:meth:`ImprovementGrade.report`)
— the settlement report embeds it whole, so a reviewer sees exactly what each
dimension contributed and why.

Tunables are caller-supplied via :class:`ImprovementParams` (the established
Plan 3/4 ``SettleConfig``/``ValidateParams`` precedent — the carried defaults record
PROVENANCE; the run-assembly wiring routes the live values; nothing here reads config
directly or hardcodes a tunable in logic). The judge-mediated dimensions (design,
code rubric) take an injectable judge seam (:data:`DesignJudgeFn` / :data:`CodeRubricFn`,
the ``validate.RejudgeFn`` precedent): the live binding routes :func:`run_judge`, the
offline suite injects scripted fakes, so the full decision table runs with zero quota
and no ``claude`` on PATH. The deterministic instruments (perf, UX, lockfile/lint) take
typed measurements, so the same offline run exercises every gate/cap/rotation branch.

## Conformance

Test-scenario / invariant (plan-005 U3) -> test (in ``tests/test_improvement.py``):

- gate-fail zeroes the bonus regardless of stellar components:
  ``test_gate_fail_zeroes_bonus_despite_stellar_components``
- per-dimension cap enforced: ``test_per_dimension_cap_enforced``
- total cap enforced: ``test_total_bonus_cap_enforced``
- p95 credit caps at 2×: ``test_p95_credit_caps_at_2x``
- uncertain design judgment scores neutral:
  ``test_uncertain_design_scores_neutral`` (+ ``test_design_swap_disagreement_neutral``,
  ``test_design_clone_clearly_better_scores``)
- weight rotation changes composition between runs deterministically by seed:
  ``test_weight_rotation_changes_composition_by_seed``,
  ``test_weight_rotation_deterministic_same_seed``
- spot-audit sampling flags the configured fraction:
  ``test_spot_audit_flags_configured_fraction``,
  ``test_spot_audit_only_high_bonus_episodes``
- folklore metrics (MI/cyclomatic) provably absent from the rubric inputs:
  ``test_folklore_metrics_absent_from_rubric``,
  ``test_code_measurement_rejects_folklore_metric_input``
- gate threshold derived from measured grader noise:
  ``test_gate_threshold_derived_from_grader_noise``
- build failure is a typed zero-bonus outcome for perf:
  ``test_build_failure_zeroes_perf_dimension``
- the full decision table (gate × caps × rotation) has a 1:1 test:
  ``test_decision_table``
- itemized report carries every component: ``test_report_itemizes_every_component``
- one live dual-app bonus run documented (docker+playwright+node):
  ``TestLiveDualAppBonus`` (docker-required, skipped offline)
"""

from __future__ import annotations

import hashlib
import logging
import statistics
from collections.abc import Callable, Mapping
from dataclasses import dataclass

logger = logging.getLogger(__name__)


class ImprovementError(Exception):
    """A broken improvement-grading precondition with an actionable message."""


# --- dimensions ---------------------------------------------------------------------

# The four bonus dimensions (DESIGN §10). Order is the canonical weight-rotation
# axis (:func:`rotate_weights`) — a protocol constant, not a tunable.
DIMENSION_PERFORMANCE = "performance"
DIMENSION_DESIGN = "design"
DIMENSION_CODE = "code_structure"
DIMENSION_UX = "ux"
DIMENSIONS = (
    DIMENSION_PERFORMANCE,
    DIMENSION_DESIGN,
    DIMENSION_CODE,
    DIMENSION_UX,
)

# Base weights, one per dimension in :data:`DIMENSIONS` order. Deliberately
# UNEQUAL so the anti-Goodhart rotation (R11) actually changes composition run to
# run; the per-dimension cap (R11) — not the weights — is the real dominance guard,
# so the exact split is uncritical. Sums to 1.0 (asserted at import).
BASE_WEIGHTS = (0.30, 0.30, 0.20, 0.20)

# Code-structure rubric criteria (R10c): criterion-separated, modularity/naming/
# dead-code/dependency-health. NOTE the deliberate ABSENCE of Maintainability Index
# and cyclomatic complexity — those are documented folklore gates (§10).
CODE_STRUCTURE_CRITERIA = (
    "modularity",
    "naming",
    "dead_code_absence",
    "dependency_health",
)

# The folklore metrics R10c explicitly bans from the code-structure rubric inputs.
# A measurement that smuggles one of these names in is rejected (`CodeMeasurement`),
# so the ban is enforced, not merely documented.
FORBIDDEN_CODE_METRICS = (
    "maintainability_index",
    "mi",
    "cyclomatic_complexity",
    "cyclomatic",
    "halstead",
)

# Design pairwise verdicts (R10b). The judge speaks A_better/B_better/equivalent on
# each ordering; swap-and-average collapses to one of these CANONICAL outcomes.
DESIGN_CLONE_BETTER = "clone_better"
DESIGN_TARGET_BETTER = "target_better"
DESIGN_UNCERTAIN = "uncertain"
DESIGN_OUTCOMES = (DESIGN_CLONE_BETTER, DESIGN_TARGET_BETTER, DESIGN_UNCERTAIN)

# Absolute-quality bonus scores per design outcome (R10b): only a clone that is
# *clearly* better earns design bonus; target-better and uncertain are NEUTRAL — no
# bonus, never a penalty (the bonus tier only ever adds to the base).
DESIGN_SCORE = {
    DESIGN_CLONE_BETTER: 1.0,
    DESIGN_TARGET_BETTER: 0.0,
    DESIGN_UNCERTAIN: 0.0,
}

# Raw verdict tokens the pairwise judge returns on one ordering.
JUDGE_A_BETTER = "A_better"
JUDGE_B_BETTER = "B_better"
JUDGE_EQUIVALENT = "equivalent"
JUDGE_VERDICTS = (JUDGE_A_BETTER, JUDGE_B_BETTER, JUDGE_EQUIVALENT)
JUDGE_CONFIDENCES = ("high", "low")

# Core Web Vitals are measured median-of-5 (R10a) — never single-run, never the
# Lighthouse composite (§10). Protocol constant.
CWV_SAMPLE_COUNT = 5

assert abs(sum(BASE_WEIGHTS) - 1.0) < 1e-9, "BASE_WEIGHTS must sum to 1.0"
assert len(BASE_WEIGHTS) == len(DIMENSIONS)


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) ---------


# PROVENANCE: §10 / R9 — "behavioral pass rate ≥ threshold (e.g. 95%)"; the threshold
# is DERIVED from grader noise (`derive_gate_threshold`) so noise never fails an
# equivalent clone. This is the floor the derivation never drops below.
DEFAULT_GATE_FLOOR = 0.90

# PROVENANCE: validate.DEFAULT_BOOTSTRAP_SIGMA_MULTIPLIER — a 2σ noise band is the
# established "within grader noise" margin. TUNING METRIC: false-gate-fail rate on
# the calibration corpus.
DEFAULT_GATE_SIGMA_MULTIPLIER = 2.0

# PROVENANCE: R11 — "each dimension capped (~30% of the bonus pool)". TUNING METRIC:
# per-dimension bonus-share distribution on high-bonus episodes (no dimension should
# dominate the pool).
DEFAULT_PER_DIMENSION_CAP = 0.30

# PROVENANCE: R11 — "total bonus ≤ 15-20% of the base score". TUNING METRIC: bonus-as-
# fraction-of-base distribution; spot-audit confirmation rate on high-bonus episodes.
DEFAULT_TOTAL_BONUS_CAP = 0.20

# PROVENANCE: R10a / §10 — "p95 latency ratio ... capped at 2× credit". TUNING METRIC:
# perf-bonus saturation rate (fraction of episodes already at the cap).
DEFAULT_P95_CREDIT_CAP = 2.0

# PROVENANCE: §10 — CWV "absolute budgets". Lighthouse "good" thresholds (LCP ≤ 2.5s,
# INP ≤ 200ms). TUNING METRIC: per-project CWV headroom; re-pin per deployment target.
DEFAULT_LCP_BUDGET_MS = 2500.0
DEFAULT_INP_BUDGET_MS = 200.0

# PROVENANCE: R11 — "human spot-audits on a SAMPLE of HIGH-bonus episodes". The high-
# bonus threshold gates which episodes are eligible (fraction of the total cap); the
# audit fraction is the sampled share. TUNING METRIC: audit yield (confirmed-gaming
# rate) vs. human reviewer load.
DEFAULT_HIGH_BONUS_THRESHOLD = 0.50  # as a fraction of total_bonus_cap reached
DEFAULT_SPOT_AUDIT_FRACTION = 0.20


@dataclass(frozen=True)
class ImprovementParams:
    """Improvement-grading tunables, caller-supplied (no hidden config reads)."""

    gate_floor: float = DEFAULT_GATE_FLOOR
    gate_sigma_multiplier: float = DEFAULT_GATE_SIGMA_MULTIPLIER
    per_dimension_cap: float = DEFAULT_PER_DIMENSION_CAP
    total_bonus_cap: float = DEFAULT_TOTAL_BONUS_CAP
    p95_credit_cap: float = DEFAULT_P95_CREDIT_CAP
    lcp_budget_ms: float = DEFAULT_LCP_BUDGET_MS
    inp_budget_ms: float = DEFAULT_INP_BUDGET_MS
    high_bonus_threshold: float = DEFAULT_HIGH_BONUS_THRESHOLD
    spot_audit_fraction: float = DEFAULT_SPOT_AUDIT_FRACTION

    def __post_init__(self) -> None:
        for name in ("gate_floor", "per_dimension_cap", "total_bonus_cap",
                     "high_bonus_threshold", "spot_audit_fraction"):
            val = getattr(self, name)
            if not (0.0 <= val <= 1.0):
                raise ImprovementError(f"{name} must be in [0, 1], got {val}")
        if self.gate_sigma_multiplier < 0:
            raise ImprovementError(
                f"gate_sigma_multiplier must be >= 0, got {self.gate_sigma_multiplier}"
            )
        if self.p95_credit_cap <= 1.0:
            raise ImprovementError(
                "p95_credit_cap must be > 1.0 (a cap of 1× is no credit at all),"
                f" got {self.p95_credit_cap}"
            )
        if self.lcp_budget_ms <= 0 or self.inp_budget_ms <= 0:
            raise ImprovementError(
                "CWV budgets must be positive milliseconds, got"
                f" lcp={self.lcp_budget_ms}, inp={self.inp_budget_ms}"
            )
        # The per-dimension cap is meaningless if it exceeds the whole pool.
        if self.per_dimension_cap > 1.0:
            raise ImprovementError(
                f"per_dimension_cap is a fraction of the pool; got"
                f" {self.per_dimension_cap}"
            )


# --- hard gate (R9) ------------------------------------------------------------------


def derive_gate_threshold(grader_sigma: float, params: ImprovementParams) -> float:
    """The behavioral hard-gate threshold, derived from measured grader noise (R9).

    The gate must be high (near-perfect must-tier), but set so grader VARIANCE alone
    never fails a genuinely-equivalent clone: a clone within ``gate_sigma_multiplier``
    σ of perfect clears it. So the threshold is ``1 - k·σ`` floored at ``gate_floor``
    — more grader noise ⇒ a more forgiving gate, exactly the gate-then-measure
    discipline (SWE-Perf). σ is the grader-replay σ (Plan 4 calibration), not build
    variance.
    """
    if grader_sigma < 0:
        raise ImprovementError(f"grader_sigma must be >= 0, got {grader_sigma}")
    derived = 1.0 - params.gate_sigma_multiplier * grader_sigma
    return max(params.gate_floor, min(1.0, derived))


def gate_passes(must_tier_pass_rate: float, gate_threshold: float) -> bool:
    """Whether the lexicographic hard gate is cleared (R9).

    Below the gate the entire bonus tier is zeroed — quality can never offset
    correctness (§10). The pass rate is the must-tier behavioral pass rate (must-tier
    failures are panel-adjudicated upstream in Plan 3 settlement).
    """
    if not (0.0 <= must_tier_pass_rate <= 1.0):
        raise ImprovementError(
            f"must_tier_pass_rate must be in [0, 1], got {must_tier_pass_rate}"
        )
    return must_tier_pass_rate >= gate_threshold


# --- performance dimension (R10a) ----------------------------------------------------


@dataclass(frozen=True)
class PerfMeasurement:
    """Performance instruments measured against the clone's PRODUCTION artifact (R10a).

    ``build_succeeded`` records the ``vite build`` + ``vite preview`` settlement-time
    step (NEVER the dev server — dev-mode overhead would systematically zero the
    dimension); a build failure is a typed **zero-bonus** outcome for perf. p95s are
    milliseconds under an identical load profile on both apps. CWV samples are the
    raw **median-of-5** runs (LCP, INP) — never single-run (§10).
    """

    build_succeeded: bool
    target_p95_ms: float
    clone_p95_ms: float
    clone_lcp_samples_ms: tuple[float, ...]
    clone_inp_samples_ms: tuple[float, ...]
    build_failure_reason: str = ""

    def __post_init__(self) -> None:
        if self.build_succeeded:
            if self.target_p95_ms <= 0 or self.clone_p95_ms <= 0:
                raise ImprovementError(
                    "p95 latencies must be positive ms on a successful build, got"
                    f" target={self.target_p95_ms}, clone={self.clone_p95_ms}"
                )
            for label, samples in (
                ("clone_lcp_samples_ms", self.clone_lcp_samples_ms),
                ("clone_inp_samples_ms", self.clone_inp_samples_ms),
            ):
                if len(samples) != CWV_SAMPLE_COUNT:
                    raise ImprovementError(
                        f"{label} must be median-of-{CWV_SAMPLE_COUNT} (R10a), got"
                        f" {len(samples)} sample(s)"
                    )


def perf_score(perf: PerfMeasurement, params: ImprovementParams) -> dict:
    """Performance raw score in ``[0, 1]`` plus its itemization (R10a).

    Two sub-instruments, averaged: latency (p95 ratio vs. target, **credit capped at
    2×**) and CWV (LCP+INP median-of-5 within absolute budgets). A failed production
    build zeroes the dimension with a typed reason.
    """
    if not perf.build_succeeded:
        return {
            "score": 0.0,
            "build_succeeded": False,
            "build_failure_reason": perf.build_failure_reason or "build failed",
            "latency_score": 0.0,
            "cwv_score": 0.0,
            "note": "production build failed — perf dimension is a typed zero-bonus"
            " outcome (R10a); never measured against the dev server",
        }

    # Latency: ratio>1 means the clone is faster than the target. Credit caps at the
    # configured multiple (2×): a clone 2× faster maxes out, 4× faster scores the same.
    ratio = perf.target_p95_ms / perf.clone_p95_ms
    capped = min(ratio, params.p95_credit_cap)
    latency_score = max(0.0, (capped - 1.0) / (params.p95_credit_cap - 1.0))

    lcp_median = statistics.median(perf.clone_lcp_samples_ms)
    inp_median = statistics.median(perf.clone_inp_samples_ms)
    within = (lcp_median <= params.lcp_budget_ms) + (inp_median <= params.inp_budget_ms)
    cwv_score = within / 2.0

    return {
        "score": (latency_score + cwv_score) / 2.0,
        "build_succeeded": True,
        "latency_score": latency_score,
        "p95_ratio": ratio,
        "p95_ratio_capped": capped,
        "cwv_score": cwv_score,
        "lcp_median_ms": lcp_median,
        "inp_median_ms": inp_median,
    }


# --- design dimension (R10b): pairwise, swap-and-average, neutral-on-uncertainty -----

# The judge seam (the validate.RejudgeFn precedent): one ordering -> {verdict,
# confidence}. The live binding wraps `run_judge` over a screenshot-pair prompt
# through the standard record/replay fixtures; the offline suite injects a scripted
# fake. ``clone_is_a`` tells the fake which slot the clone occupies this ordering.
DesignJudgeFn = Callable[[bool], dict]


@dataclass(frozen=True)
class DesignOutcome:
    """The swap-and-averaged pairwise design verdict (R10b)."""

    verdict: str  # DESIGN_OUTCOMES
    score: float
    note: str
    orderings: tuple[dict, ...]


def _validate_judge_reply(reply: Mapping) -> tuple[str, str]:
    verdict = reply.get("verdict")
    confidence = reply.get("confidence")
    if verdict not in JUDGE_VERDICTS:
        raise ImprovementError(
            f"design judge verdict must be one of {JUDGE_VERDICTS}, got {verdict!r}"
        )
    if confidence not in JUDGE_CONFIDENCES:
        raise ImprovementError(
            f"design judge confidence must be one of {JUDGE_CONFIDENCES}, got"
            f" {confidence!r}"
        )
    return verdict, confidence


def _clone_won(verdict: str, clone_is_a: bool) -> str:
    """Re-express one ordering's raw verdict as clone-better/target-better/equiv."""
    if verdict == JUDGE_EQUIVALENT:
        return DESIGN_UNCERTAIN
    clone_better_token = JUDGE_A_BETTER if clone_is_a else JUDGE_B_BETTER
    return DESIGN_CLONE_BETTER if verdict == clone_better_token else DESIGN_TARGET_BETTER


def design_pairwise(judge: DesignJudgeFn) -> DesignOutcome:
    """Run the pairwise design judge twice with swapped slots and average (R10b).

    Gross-difference gating + neutral-on-uncertainty: the verdict is the agreed
    direction ONLY when both orderings agree AND both are high-confidence; a
    disagreement, an ``equivalent``, or any low-confidence reply collapses to
    **uncertain** (neutral — no bonus, never a penalty). Scored as absolute quality,
    not target-similarity: a clone that improves an ugly target wins.
    """
    reply_a = judge(True)   # ordering 1: clone is slot A
    reply_b = judge(False)  # ordering 2: clone is slot B (swapped)
    verdict_a, conf_a = _validate_judge_reply(reply_a)
    verdict_b, conf_b = _validate_judge_reply(reply_b)
    orderings = (
        {"clone_is_a": True, "verdict": verdict_a, "confidence": conf_a},
        {"clone_is_a": False, "verdict": verdict_b, "confidence": conf_b},
    )

    if conf_a == "low" or conf_b == "low":
        return DesignOutcome(
            DESIGN_UNCERTAIN, DESIGN_SCORE[DESIGN_UNCERTAIN],
            "low-confidence reply — neutral (pairwise UI judging is coin-flip when"
            " designs are close; §10)",
            orderings,
        )
    dir_a = _clone_won(verdict_a, clone_is_a=True)
    dir_b = _clone_won(verdict_b, clone_is_a=False)
    if dir_a != dir_b or dir_a == DESIGN_UNCERTAIN:
        return DesignOutcome(
            DESIGN_UNCERTAIN, DESIGN_SCORE[DESIGN_UNCERTAIN],
            "swap disagreement or equivalent — neutral (only gross, order-stable"
            " differences score; R10b)",
            orderings,
        )
    return DesignOutcome(
        dir_a, DESIGN_SCORE[dir_a],
        f"both orderings agree: {dir_a} (high confidence)",
        orderings,
    )


# --- code-structure dimension (R10c): rubric + lockfile/lint, NO folklore metrics ----

# The code-structure rubric seam: criterion name -> score in [0, 1]. The live binding
# wraps `run_judge` (criterion-separated CoT rubric); the offline suite injects a
# scripted fake. Mirrors DesignJudgeFn — a judge seam, not a config read.
CodeRubricFn = Callable[[], Mapping[str, float]]


@dataclass(frozen=True)
class CodeMeasurement:
    """Code-structure inputs (R10c): a criterion-separated rubric + deterministic
    lockfile/lint health. The rubric keys MUST be exactly :data:`CODE_STRUCTURE_CRITERIA`,
    and any :data:`FORBIDDEN_CODE_METRICS` name (MI, cyclomatic, ...) in the inputs is a
    hard error — the folklore ban is enforced at the boundary, not just documented."""

    rubric: Mapping[str, float]
    lockfile_healthy: bool
    lint_clean: bool

    def __post_init__(self) -> None:
        keys = set(self.rubric)
        forbidden = keys & set(FORBIDDEN_CODE_METRICS)
        if forbidden:
            raise ImprovementError(
                f"code-structure rubric must not use folklore metrics (R10c):"
                f" {sorted(forbidden)} — Maintainability Index / cyclomatic complexity"
                " are documented folklore gates and are banned inputs"
            )
        if keys != set(CODE_STRUCTURE_CRITERIA):
            raise ImprovementError(
                f"code-structure rubric keys must be exactly {CODE_STRUCTURE_CRITERIA},"
                f" got {sorted(keys)}"
            )
        for crit, val in self.rubric.items():
            if not (0.0 <= val <= 1.0):
                raise ImprovementError(
                    f"rubric criterion {crit!r} must score in [0, 1], got {val}"
                )


def code_score(code: CodeMeasurement) -> dict:
    """Code-structure raw score in ``[0, 1]`` plus itemization (R10c).

    The criterion-separated rubric mean and the deterministic lockfile/lint health,
    averaged. No Maintainability Index, no cyclomatic complexity (CodeMeasurement
    rejects them outright).
    """
    rubric_mean = statistics.fmean(code.rubric.values())
    health = (int(code.lockfile_healthy) + int(code.lint_clean)) / 2.0
    return {
        "score": (rubric_mean + health) / 2.0,
        "rubric": {k: code.rubric[k] for k in CODE_STRUCTURE_CRITERIA},
        "rubric_mean": rubric_mean,
        "lockfile_healthy": code.lockfile_healthy,
        "lint_clean": code.lint_clean,
        "health_score": health,
        "criteria": list(CODE_STRUCTURE_CRITERIA),
    }


# --- automated UX dimension (R10d): axe-core, console, viewport ----------------------


@dataclass(frozen=True)
class UxMeasurement:
    """Automated UX instruments (R10d): axe-core violation count, console-error count,
    and a mobile-viewport pass flag. All deterministic — no judge."""

    axe_violations: int
    console_errors: int
    viewport_pass: bool

    def __post_init__(self) -> None:
        if self.axe_violations < 0 or self.console_errors < 0:
            raise ImprovementError(
                "axe_violations and console_errors must be >= 0, got"
                f" axe={self.axe_violations}, console={self.console_errors}"
            )


def ux_score(ux: UxMeasurement) -> dict:
    """Automated-UX raw score in ``[0, 1]`` plus itemization (R10d)."""
    axe_ok = ux.axe_violations == 0
    console_ok = ux.console_errors == 0
    parts = (int(axe_ok), int(console_ok), int(ux.viewport_pass))
    return {
        "score": sum(parts) / 3.0,
        "axe_ok": axe_ok,
        "axe_violations": ux.axe_violations,
        "console_ok": console_ok,
        "console_errors": ux.console_errors,
        "viewport_pass": ux.viewport_pass,
    }


# --- anti-Goodhart guards (R11) ------------------------------------------------------


def rotate_weights(rotation_seed: int) -> dict[str, float]:
    """Dimension weights for one grading run, rotated deterministically by seed (R11).

    The base weights cyclically rotate across the dimensions by ``seed % n`` — the
    same seed always yields the same assignment (reproducible), consecutive seeds
    shift which dimension carries which weight (composition changes run to run, so no
    dimension is permanently the heaviest target to hill-climb). The per-dimension
    cap is the real dominance guard; rotation prevents a fixed gaming target.
    """
    if rotation_seed < 0:
        raise ImprovementError(f"rotation_seed must be >= 0, got {rotation_seed}")
    n = len(DIMENSIONS)
    k = rotation_seed % n
    return {
        DIMENSIONS[i]: BASE_WEIGHTS[(i + k) % n] for i in range(n)
    }


def spot_audit_flagged(
    total_bonus_fraction: float, episode_key: str, params: ImprovementParams
) -> bool:
    """Whether a high-bonus episode is sampled for human spot-audit (R11).

    Only HIGH-bonus episodes are eligible (the bonus reached ≥
    ``high_bonus_threshold`` of the total cap); among those, a deterministic per-key
    hash samples the configured ``spot_audit_fraction``. Deterministic so a re-run
    audits the same episodes; uniform so the sampled share matches the configured
    fraction over many episodes.
    """
    high_bonus_floor = params.high_bonus_threshold * params.total_bonus_cap
    if total_bonus_fraction < high_bonus_floor:
        return False
    digest = hashlib.sha256(episode_key.encode("utf-8")).hexdigest()
    draw = int(digest[:8], 16) / 0xFFFFFFFF
    return draw < params.spot_audit_fraction


# --- composition (R10/R11): lexicographic gate + capped, rotated, itemized bonus -----


@dataclass(frozen=True)
class ImprovementGrade:
    """One episode's improvement-tier grade: the gate verdict, the four itemized
    dimensions, the rotated weights, the capped per-dimension contributions, the
    total bonus, the final score, and the spot-audit flag. Pure function of its
    inputs, no timestamps — the settlement report embeds :meth:`report` whole and
    renders byte-deterministically (the Plan 3 R23 discipline)."""

    gate_passed: bool
    gate_threshold: float
    must_tier_pass_rate: float
    base_score: float
    weights: dict[str, float]
    dimension_scores: dict[str, dict]
    contributions: dict[str, float]  # fraction-of-pool, capped, per dimension
    total_bonus_fraction: float  # of base, in [0, total_bonus_cap]
    total_bonus_value: float  # base_score * total_bonus_fraction
    final_score: float  # base_score + total_bonus_value
    spot_audit_flagged: bool
    episode_key: str = ""
    rotation_seed: int = 0

    def report(self) -> dict:
        """The itemized improvement-tier section for the settlement report (R10) —
        every bonus component visible, sorted keys, no volatile data."""
        return {
            "base_score": self.base_score,
            "dimensions": {
                name: {
                    "capped_contribution": self.contributions[name],
                    "details": self.dimension_scores[name],
                    "raw_score": self.dimension_scores[name]["score"],
                    "weight": self.weights[name],
                }
                for name in sorted(self.dimension_scores)
            },
            "final_score": self.final_score,
            "gate": {
                "must_tier_pass_rate": self.must_tier_pass_rate,
                "passed": self.gate_passed,
                "threshold": self.gate_threshold,
            },
            "rotation_seed": self.rotation_seed,
            "spot_audit_flagged": self.spot_audit_flagged,
            "total_bonus": {
                "fraction_of_base": self.total_bonus_fraction,
                "value": self.total_bonus_value,
            },
        }


def grade_improvement(
    *,
    must_tier_pass_rate: float,
    base_score: float,
    gate_threshold: float,
    perf: dict,
    design: DesignOutcome,
    code: dict,
    ux: dict,
    rotation_seed: int,
    episode_key: str,
    params: ImprovementParams = ImprovementParams(),
) -> ImprovementGrade:
    """Compose the lexicographic improvement grade (R9-R11).

    ``perf``/``code``/``ux`` are the itemized dicts from :func:`perf_score`/
    :func:`code_score`/:func:`ux_score`; ``design`` is a :class:`DesignOutcome`. The
    gate is checked first (R9): a fail zeroes the entire bonus regardless of how
    stellar the components are. Above the gate, each dimension's raw score is weighted
    by the rotated weight (R11), its fraction-of-pool contribution **capped at
    ``per_dimension_cap``** (R11), the capped contributions summed and clamped to the
    pool, and the **total bonus capped at ``total_bonus_cap`` of the base** (R11). The
    result is fully itemized (R10) and spot-audit-sampled if high-bonus (R11).
    """
    if not (0.0 <= base_score <= 1.0):
        raise ImprovementError(f"base_score must be in [0, 1], got {base_score}")
    for label, d in (("perf", perf), ("code", code), ("ux", ux)):
        if "score" not in d or not (0.0 <= d["score"] <= 1.0):
            raise ImprovementError(
                f"{label} itemization must carry a 'score' in [0, 1], got {d.get('score')}"
            )

    weights = rotate_weights(rotation_seed)
    dimension_scores = {
        DIMENSION_PERFORMANCE: perf,
        DIMENSION_DESIGN: {"score": design.score, "verdict": design.verdict,
                           "note": design.note, "orderings": list(design.orderings)},
        DIMENSION_CODE: code,
        DIMENSION_UX: ux,
    }

    gate_passed = gate_passes(must_tier_pass_rate, gate_threshold)

    contributions: dict[str, float] = {}
    for name in DIMENSIONS:
        raw = dimension_scores[name]["score"]
        # Fraction-of-pool this dimension claims, capped so no single dimension can
        # dominate the pool even when rotation hands it the heaviest weight (R11).
        uncapped = weights[name] * raw
        contributions[name] = min(uncapped, params.per_dimension_cap)

    pool_fraction = min(sum(contributions.values()), 1.0)  # of the bonus pool
    total_bonus_fraction = pool_fraction * params.total_bonus_cap  # of the base
    if not gate_passed:
        # Lexicographic: below the behavioral gate the bonus is zeroed entirely (R9).
        total_bonus_fraction = 0.0
    total_bonus_value = base_score * total_bonus_fraction
    final_score = base_score + total_bonus_value

    flagged = spot_audit_flagged(total_bonus_fraction, episode_key, params)

    logger.info(
        "improvement grade for %s: gate=%s base=%.3f bonus=%.4f (of base) final=%.3f"
        " seed=%d audit=%s",
        episode_key, gate_passed, base_score, total_bonus_fraction, final_score,
        rotation_seed, flagged,
    )
    return ImprovementGrade(
        gate_passed=gate_passed,
        gate_threshold=gate_threshold,
        must_tier_pass_rate=must_tier_pass_rate,
        base_score=base_score,
        weights=weights,
        dimension_scores=dimension_scores,
        contributions=contributions,
        total_bonus_fraction=total_bonus_fraction,
        total_bonus_value=total_bonus_value,
        final_score=final_score,
        spot_audit_flagged=flagged,
        episode_key=episode_key,
        rotation_seed=rotation_seed,
    )
