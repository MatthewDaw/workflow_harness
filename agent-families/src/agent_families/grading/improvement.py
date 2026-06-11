"""Improvement-tier grading (plan-005 U3, R9-R11).

Grade "better", not just "same", without corrupting the reward.

**Lexicographic gate (R9, DESIGN §10).** The behavioral pass rate is a HARD GATE.
Below the gate the improvement bonus is exactly zero — never offset by a stellar
performance or design score (the SWE-Perf "gate-then-measure" / constrained-RLHF
gating discipline). The gate threshold is *derived from measured grader noise*
(panel-adjudicated must-tier), never hardcoded: a clone whose must-tier pass rate
is statistically indistinguishable from the target's clears the gate.

**Capped improvement bonuses (R10).** Above the gate, bonuses ride DETERMINISTIC
instruments wherever possible and LLM judgment only where research says it is
reliable:

- performance (R10a): k6/autocannon p95 latency ratio vs the target under an
  IDENTICAL load profile (credit capped at 2x) + Core Web Vitals (LCP, INP) as
  median-of-5 against ABSOLUTE budgets.
- design (R10b): MLLM pairwise screenshot judgment (CoT + a hierarchy / readability
  / layout / typography rubric) through the judge seam, **swap-and-average**,
  invoked only for GROSS differences, uncertain = neutral (no bonus), scored as
  absolute quality — NOT target-similarity.
- code structure (R10c): a CRITERION-SEPARATED LLM rubric + lockfile/lint health,
  explicitly NOT maintainability-index / cyclomatic-complexity folklore.
- automated UX (R10d): axe-core violations, console-error absence, viewport checks.

All measured against a PRODUCTION artifact of the clone (``vite build`` +
``vite preview``, Hono in production mode) — never the dev server, whose overhead
would systematically zero the perf dimension. A build failure is a typed
zero-bonus outcome (:class:`BuildOutcome` with ``built=False`` -> bonus zero, the
reason recorded), distinct from a gate failure.

**Anti-Goodhart composition (R11).** A per-dimension cap (~30% of the bonus pool),
a total bonus <= 15-20% of base, dimension-weight rotation across grading runs
(seeded, deterministic), and human spot-audits sampled on high-bonus episodes.
Every bonus component is itemized in the settlement report.

Offline by construction: the deterministic instruments (k6, CWV, axe, lockfile/
lint, the rubric scores) are injected as typed measurements; the ONE LLM call (the
pairwise design judge) rides the record/replay judge seam exactly like settlement.
Tunables are caller-supplied via :class:`ImprovementParams` with carried PROVENANCE
defaults (the validate.py / settle.py precedent — run-assembly routes the live
values; nothing here reads config directly).
"""

from __future__ import annotations

import logging
import random
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_families.judge import run_judge

logger = logging.getLogger(__name__)


class ImprovementError(Exception):
    """A broken improvement-grading precondition with an actionable message."""


# --- vocabulary -----------------------------------------------------------------

# The four improvement dimensions, in canonical (weight-rotation) order.
DIMENSIONS = ("performance", "design", "code_structure", "ux")

# The pairwise design rubric (R10b): absolute visual-quality criteria, NOT
# similarity-to-target. The judge reasons over these (CoT).
DESIGN_RUBRIC = ("hierarchy", "readability", "layout", "typography")

# The criterion-separated code-structure rubric (R10c). Each is a named quality
# criterion an LLM can reason about — deliberately NOT a complexity metric.
CODE_STRUCTURE_CRITERIA = (
    "naming_clarity",
    "module_cohesion",
    "separation_of_concerns",
    "error_handling",
    "test_presence",
)

# Folklore complexity metrics the code-structure rubric must NEVER ingest (R10c):
# maintainability-index and cyclomatic complexity correlate poorly with real
# maintainability (research: MI/cyclomatic criticisms). A rubric input containing
# any of these tokens is a hard error — the "provably absent" invariant.
FORBIDDEN_CODE_METRICS = (
    "maintainability_index",
    "cyclomatic",
    "halstead",
    "loc_complexity",
)

# The zeroing reasons (R9 / build): a gate failure or a build failure both drive
# the bonus to exactly zero, with the reason recorded for the report.
ZEROED_GATE = "gate_failed"
ZEROED_BUILD = "build_failed"


def assert_no_folklore_metrics(*texts: str) -> None:
    """Hard-fail if any folklore complexity metric appears in rubric inputs (R10c).

    The "MI/cyclomatic provably absent" guard: the code-structure rubric criteria
    and judge inputs are scanned for forbidden tokens before they reach a judge.
    """
    blob = " ".join(texts).lower()
    for metric in FORBIDDEN_CODE_METRICS:
        if metric in blob:
            raise ImprovementError(
                f"folklore complexity metric {metric!r} found in code-structure"
                " rubric inputs — R10c bars MI/cyclomatic from the rubric"
            )


# Belt-and-suspenders: the shipped rubric provably excludes folklore at import.
assert_no_folklore_metrics(*CODE_STRUCTURE_CRITERIA)


# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) -----

# PROVENANCE: R9 / SWE-Perf gate-then-measure — the behavioral gate sits at
# 1 - k·σ(grader). TUNING METRIC: false-gate vs. missed-regression on the
# calibration corpus. The must-tier grader σ comes from the panel-adjudicated
# replicate distribution (validate.BenchmarkOutcome.sigma precedent).
DEFAULT_GATE_SIGMA_MULTIPLIER = 2.0
# PROVENANCE: §10 — the gate is a real bar even when grader noise is large; it
# never drops below half. TUNING METRIC: floor vs. corpus pass-rate distribution.
DEFAULT_GATE_FLOOR = 0.5

# PROVENANCE: R10a — "p95 latency ratio credit capped at 2×". A clone 2× faster
# than the target earns full latency credit; same-speed earns none.
# TUNING METRIC: credit saturation vs. measured latency-ratio distribution.
DEFAULT_P95_RATIO_CAP = 2.0

# PROVENANCE: R10a — Core Web Vitals measured as median-of-5 (Lighthouse practice).
# TUNING METRIC: median stability vs. sample count.
DEFAULT_CWV_SAMPLE_COUNT = 5

# PROVENANCE: R11 — per-dimension cap "~30% of the bonus pool"; total bonus
# "≤ 15-20% of base". TUNING METRIC: reward-hacking incidence on spot-audited
# high-bonus episodes.
DEFAULT_PER_DIMENSION_CAP = 0.30
DEFAULT_TOTAL_BONUS_CAP = 0.20

# PROVENANCE: R11 — dimension-weight rotation across grading runs. The base
# weights are relative emphasis multipliers (NOT a probability simplex): the
# per-dimension and total caps do the bounding, so several dimensions can hit
# their cap and the total cap can bind. Unequal so a cyclic rotation actually
# changes the composition. TUNING METRIC: anti-Goodhart audit findings.
DEFAULT_BASE_WEIGHTS = (
    ("performance", 0.50),
    ("design", 0.45),
    ("code_structure", 0.35),
    ("ux", 0.40),
)

# PROVENANCE: R11 — "human spot-audits sampled on high-bonus episodes". A
# high-bonus episode is one whose bonus reaches at least this fraction of base;
# the sampler flags ``spot_audit_fraction`` of them for human review.
# TUNING METRIC: audit yield vs. reviewer load.
DEFAULT_HIGH_BONUS_THRESHOLD = 0.10
DEFAULT_SPOT_AUDIT_FRACTION = 0.25


@dataclass(frozen=True)
class ImprovementParams:
    """Improvement-grading tunables, caller-supplied (no hidden config reads)."""

    gate_sigma_multiplier: float = DEFAULT_GATE_SIGMA_MULTIPLIER
    gate_floor: float = DEFAULT_GATE_FLOOR
    p95_ratio_cap: float = DEFAULT_P95_RATIO_CAP
    cwv_sample_count: int = DEFAULT_CWV_SAMPLE_COUNT
    per_dimension_cap: float = DEFAULT_PER_DIMENSION_CAP
    total_bonus_cap: float = DEFAULT_TOTAL_BONUS_CAP
    base_weights: tuple[tuple[str, float], ...] = DEFAULT_BASE_WEIGHTS
    high_bonus_threshold: float = DEFAULT_HIGH_BONUS_THRESHOLD
    spot_audit_fraction: float = DEFAULT_SPOT_AUDIT_FRACTION

    def __post_init__(self) -> None:
        if self.gate_sigma_multiplier < 0:
            raise ImprovementError(
                f"gate_sigma_multiplier must be >= 0, got {self.gate_sigma_multiplier}"
            )
        if not (0.0 <= self.gate_floor <= 1.0):
            raise ImprovementError(
                f"gate_floor must be in [0, 1], got {self.gate_floor}"
            )
        if self.p95_ratio_cap <= 1.0:
            raise ImprovementError(
                f"p95_ratio_cap must be > 1.0 (a cap of 1× is no headroom),"
                f" got {self.p95_ratio_cap}"
            )
        if self.cwv_sample_count < 1:
            raise ImprovementError(
                f"cwv_sample_count must be >= 1, got {self.cwv_sample_count}"
            )
        if not (0.0 < self.per_dimension_cap <= 1.0):
            raise ImprovementError(
                f"per_dimension_cap must be in (0, 1], got {self.per_dimension_cap}"
            )
        if not (0.0 < self.total_bonus_cap <= 1.0):
            raise ImprovementError(
                f"total_bonus_cap must be in (0, 1], got {self.total_bonus_cap}"
            )
        names = tuple(name for name, _ in self.base_weights)
        if set(names) != set(DIMENSIONS) or len(names) != len(DIMENSIONS):
            raise ImprovementError(
                f"base_weights must cover exactly {DIMENSIONS}, got {names}"
            )
        for name, weight in self.base_weights:
            if weight <= 0.0:
                raise ImprovementError(
                    f"base weight for {name!r} must be > 0, got {weight}"
                )
        if not (0.0 <= self.high_bonus_threshold <= 1.0):
            raise ImprovementError(
                f"high_bonus_threshold must be in [0, 1], got {self.high_bonus_threshold}"
            )
        if not (0.0 <= self.spot_audit_fraction <= 1.0):
            raise ImprovementError(
                f"spot_audit_fraction must be in [0, 1], got {self.spot_audit_fraction}"
            )

    @property
    def weight_map(self) -> dict[str, float]:
        return {name: weight for name, weight in self.base_weights}


# --- the behavioral gate (R9) ---------------------------------------------------


def derive_gate_threshold(grader_sigma: float, params: ImprovementParams) -> float:
    """The behavioral-gate threshold from measured grader noise (R9).

    ``threshold = clamp(1 - k·σ, gate_floor, 1)`` — a must-tier pass rate within
    ``k`` grader-noise σ of a perfect clone clears the gate; below it the bonus is
    lexicographically zero. ``grader_sigma`` is the panel-adjudicated must-tier
    replicate σ (the validate.BenchmarkOutcome.sigma precedent), never hardcoded.
    """
    if grader_sigma < 0:
        raise ImprovementError(f"grader_sigma must be >= 0, got {grader_sigma}")
    raw = 1.0 - params.gate_sigma_multiplier * grader_sigma
    return min(1.0, max(params.gate_floor, raw))


# --- performance dimension (R10a) -----------------------------------------------


@dataclass(frozen=True)
class LatencyProfile:
    """Identical-load p95 latencies (ms) for the clone and the target (R10a).

    The SAME load profile is applied to both apps (the k6/autocannon harness);
    ``profile`` names it so the report can prove the comparison was fair.
    """

    clone_p95_ms: float
    target_p95_ms: float
    profile: str

    def __post_init__(self) -> None:
        if self.clone_p95_ms <= 0 or self.target_p95_ms <= 0:
            raise ImprovementError(
                "p95 latencies must be positive milliseconds (R10a), got"
                f" clone={self.clone_p95_ms}, target={self.target_p95_ms}"
            )
        if not self.profile.strip():
            raise ImprovementError("latency profile name must be non-empty (R10a)")

    @property
    def ratio(self) -> float:
        """How many times faster the clone is than the target (>1 == faster)."""
        return self.target_p95_ms / self.clone_p95_ms


def latency_credit(profile: LatencyProfile, params: ImprovementParams) -> float:
    """Normalized p95 credit, saturating at the 2× ratio cap (R10a).

    ratio <= 1 (no faster than the target) -> 0; ratio >= cap -> 1. The cap is
    why an absurdly fast clone cannot run away with the reward.
    """
    capped = min(profile.ratio, params.p95_ratio_cap)
    return max(0.0, (capped - 1.0) / (params.p95_ratio_cap - 1.0))


@dataclass(frozen=True)
class WebVital:
    """One Core Web Vital measured as median-of-N against an ABSOLUTE budget (R10a)."""

    name: str  # e.g. "LCP", "INP"
    samples_ms: tuple[float, ...]
    budget_ms: float

    def __post_init__(self) -> None:
        if not self.samples_ms:
            raise ImprovementError(
                f"web vital {self.name!r} needs at least one sample (R10a)"
            )
        if self.budget_ms <= 0:
            raise ImprovementError(
                f"web vital {self.name!r} budget must be positive ms, got {self.budget_ms}"
            )

    @property
    def median_ms(self) -> float:
        return statistics.median(self.samples_ms)

    @property
    def under_budget(self) -> bool:
        return self.median_ms <= self.budget_ms


def cwv_credit(
    vitals: Sequence[WebVital], params: ImprovementParams
) -> float:
    """Fraction of Core Web Vitals whose median-of-N meets its absolute budget.

    Each vital should carry ``cwv_sample_count`` samples (median-of-5 protocol);
    a short sample set is a soft warning, never a silent pass.
    """
    if not vitals:
        return 0.0
    for v in vitals:
        if len(v.samples_ms) < params.cwv_sample_count:
            logger.warning(
                "web vital %s has %d samples (< median-of-%d protocol)",
                v.name,
                len(v.samples_ms),
                params.cwv_sample_count,
            )
    return sum(1 for v in vitals if v.under_budget) / len(vitals)


def performance_credit(
    latency: LatencyProfile,
    vitals: Sequence[WebVital],
    params: ImprovementParams,
) -> float:
    """The performance dimension's raw credit: mean of latency and CWV credit (R10a)."""
    return (latency_credit(latency, params) + cwv_credit(vitals, params)) / 2.0


# --- design dimension (R10b): pairwise screenshot judge through the seam ---------

DESIGN_VERDICTS = ("a_better", "b_better", "equivalent", "uncertain")

DESIGN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {"type": "string", "enum": list(DESIGN_VERDICTS)},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["reasoning", "verdict", "confidence"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class DesignJudgeConfig:
    """Judge plumbing for the pairwise design comparison (the SettleConfig precedent)."""

    model: str
    max_retries: int
    bare: bool = False
    mode: str | None = None
    fixtures_dir: Path | None = None


def design_prompt(app_a_label: str, app_b_label: str) -> str:
    """The pairwise design prompt: absolute quality, CoT, neutral-on-uncertainty.

    Position-relative (A vs B) so the swap call gets a distinct prompt — hence a
    distinct fixture key through the replay seam. No volatile data (judge-seam
    discipline). Deliberately frames ABSOLUTE quality, not similarity to a target.
    """
    rubric = ", ".join(DESIGN_RUBRIC)
    return (
        "You compare the visual design QUALITY of two rebuilt web-app"
        " screenshots, A and B. Judge ABSOLUTE quality on this rubric (not"
        f" similarity to any reference): {rubric}.\n"
        "Reason step by step in 'reasoning' first, then give the verdict:"
        " 'a_better' if A is CLEARLY superior, 'b_better' if B is CLEARLY"
        " superior, 'equivalent' if neither is clearly better, or 'uncertain'"
        " if you cannot tell. When unsure, answer 'uncertain' — it scores"
        " neutral (no bonus). Also report your confidence (high or low).\n\n"
        f"Screenshot A: {app_a_label}\n"
        f"Screenshot B: {app_b_label}"
    )


def _clone_advantage(verdict: str, confidence: str, clone_is_a: bool) -> float:
    """Map one pairwise verdict to clone advantage in [0, 1] (0.5 == neutral).

    Low confidence or an 'uncertain'/'equivalent' verdict is neutral (0.5) — the
    neutral-on-uncertainty rule (R10b).
    """
    if confidence == "low" or verdict in ("uncertain", "equivalent"):
        return 0.5
    if verdict == "a_better":
        return 1.0 if clone_is_a else 0.0
    # b_better
    return 0.0 if clone_is_a else 1.0


@dataclass(frozen=True)
class DesignOutcome:
    """The settled design judgment (R10b)."""

    advantage: float  # [0, 1]; 0.5 == neutral (clone neither better nor worse)
    credit: float  # [0, 1] bonus credit; 0 when neutral / clone not better
    neutral: bool
    invoked: bool  # False when gross-difference gating skipped the judge
    calls: tuple[dict, ...]  # itemized {position, verdict, confidence}

    @staticmethod
    def neutral_outcome(invoked: bool) -> "DesignOutcome":
        return DesignOutcome(
            advantage=0.5, credit=0.0, neutral=True, invoked=invoked, calls=()
        )


def judge_design(
    clone_label: str,
    target_label: str,
    *,
    gross_difference: bool,
    config: DesignJudgeConfig,
) -> DesignOutcome:
    """Pairwise design judgment with swap-and-average (R10b).

    Gross-difference gating: the judge is invoked ONLY when a gross visual
    difference is present (cheap pixel/structure pre-screen upstream) — otherwise
    the dimension is neutral and earns no bonus. When invoked, two judge calls run
    with the apps in swapped positions; their clone advantages are averaged
    (swap-and-average debiases position). Uncertain or low-confidence verdicts
    score neutral. The credit is ``max(0, 2·(advantage − 0.5))`` so a clone that
    is merely equivalent earns zero — this is a bonus for being BETTER.
    """
    if not gross_difference:
        # Not a gross difference: the MLLM judge is unreliable here, so neutral.
        return DesignOutcome.neutral_outcome(invoked=False)

    # Swap-and-average: call 1 puts the clone in position A, call 2 swaps it to B.
    call1 = run_judge(
        design_prompt(clone_label, target_label),
        DESIGN_SCHEMA,
        config.model,
        max_retries=config.max_retries,
        bare=config.bare,
        mode=config.mode,
        fixtures_dir=config.fixtures_dir,
    ).output
    call2 = run_judge(
        design_prompt(target_label, clone_label),
        DESIGN_SCHEMA,
        config.model,
        max_retries=config.max_retries,
        bare=config.bare,
        mode=config.mode,
        fixtures_dir=config.fixtures_dir,
    ).output

    adv1 = _clone_advantage(call1["verdict"], call1["confidence"], clone_is_a=True)
    adv2 = _clone_advantage(call2["verdict"], call2["confidence"], clone_is_a=False)
    advantage = (adv1 + adv2) / 2.0
    credit = max(0.0, 2.0 * (advantage - 0.5))
    return DesignOutcome(
        advantage=advantage,
        credit=credit,
        neutral=credit == 0.0,
        invoked=True,
        calls=(
            {"position": "clone_as_a", "verdict": call1["verdict"], "confidence": call1["confidence"]},
            {"position": "clone_as_b", "verdict": call2["verdict"], "confidence": call2["confidence"]},
        ),
    )


# --- code-structure dimension (R10c) --------------------------------------------


@dataclass(frozen=True)
class CodeStructureMeasurement:
    """Criterion-separated rubric scores + lockfile/lint health (R10c).

    ``rubric_scores`` keys must be a subset of :data:`CODE_STRUCTURE_CRITERIA` —
    a folklore complexity metric (MI/cyclomatic) is a hard error, the "provably
    absent" invariant. Each score is in [0, 1].
    """

    rubric_scores: Mapping[str, float]
    lockfile_present: bool
    lint_clean: bool

    def __post_init__(self) -> None:
        assert_no_folklore_metrics(*self.rubric_scores.keys())
        for crit, score in self.rubric_scores.items():
            if crit not in CODE_STRUCTURE_CRITERIA:
                raise ImprovementError(
                    f"unknown code-structure criterion {crit!r}; allowed:"
                    f" {CODE_STRUCTURE_CRITERIA}"
                )
            if not (0.0 <= score <= 1.0):
                raise ImprovementError(
                    f"code-structure score for {crit!r} must be in [0, 1], got {score}"
                )
        if not self.rubric_scores:
            raise ImprovementError(
                "code-structure measurement needs at least one rubric criterion (R10c)"
            )


def code_structure_credit(
    measurement: CodeStructureMeasurement, params: ImprovementParams
) -> float:
    """The code-structure dimension's raw credit (R10c).

    60% the criterion-separated rubric mean, 20% lockfile presence, 20% lint
    cleanliness — deterministic health weighted alongside the LLM rubric. No
    MI/cyclomatic anywhere (enforced at construction).
    """
    rubric_mean = statistics.fmean(measurement.rubric_scores.values())
    lockfile = 1.0 if measurement.lockfile_present else 0.0
    lint = 1.0 if measurement.lint_clean else 0.0
    return 0.6 * rubric_mean + 0.2 * lockfile + 0.2 * lint


# --- automated-UX dimension (R10d) ----------------------------------------------


@dataclass(frozen=True)
class UxMeasurement:
    """Deterministic UX probes (R10d): axe-core, console errors, viewport checks."""

    axe_violations: int
    console_errors: int
    viewport_ok: bool

    def __post_init__(self) -> None:
        if self.axe_violations < 0 or self.console_errors < 0:
            raise ImprovementError(
                "axe_violations and console_errors must be >= 0 (R10d)"
            )


def ux_credit(measurement: UxMeasurement, params: ImprovementParams) -> float:
    """The UX dimension's raw credit: a clean production artifact earns full (R10d).

    Three deterministic checks, equally weighted: zero axe violations, zero
    console errors, viewport checks passing.
    """
    checks = (
        measurement.axe_violations == 0,
        measurement.console_errors == 0,
        measurement.viewport_ok,
    )
    return sum(1 for ok in checks if ok) / len(checks)


# --- build artifact (the production-build precondition) -------------------------


@dataclass(frozen=True)
class BuildOutcome:
    """Whether the clone's PRODUCTION artifact built (vite build + preview).

    Perf/CWV/design/UX all measure the production artifact, never the dev server
    (R10 approach). A build failure is a typed ZERO-BONUS outcome — distinct from
    a behavioral-gate failure.
    """

    built: bool
    detail: str = ""


# --- anti-Goodhart composition (R11) --------------------------------------------


def rotated_weights(seed: int, params: ImprovementParams) -> dict[str, float]:
    """Cyclically rotate the base weight VALUES across dimensions by ``seed`` (R11).

    The same seed reproduces the same weighting; consecutive seeds shuffle which
    dimension carries which emphasis — the weight-rotation anti-Goodhart guard.
    """
    values = [params.weight_map[d] for d in DIMENSIONS]
    shift = seed % len(DIMENSIONS)
    rotated = values[-shift:] + values[:-shift] if shift else list(values)
    return {d: rotated[i] for i, d in enumerate(DIMENSIONS)}


@dataclass(frozen=True)
class DimensionScore:
    """One dimension's itemized contribution to the bonus (R10/R11 report line)."""

    dimension: str
    credit: float  # [0, 1] raw credit
    weight: float  # rotated weight this run
    contribution: float  # capped contribution to the bonus (fraction of base)
    capped: bool  # the per-dimension cap bound this dimension


def compose_bonus(
    credits: Mapping[str, float],
    weights: Mapping[str, float],
    params: ImprovementParams,
) -> tuple[tuple[DimensionScore, ...], float, bool]:
    """Compose capped per-dimension contributions into the bonus (R11).

    ``contribution = min(credit · weight · pool, per_dim_cap · pool)``; the bonus
    is ``min(Σ contributions, pool)``. Returns the itemized dimensions, the bonus
    (fraction of base), and whether the TOTAL cap bound.
    """
    pool = params.total_bonus_cap
    dim_cap = params.per_dimension_cap * pool
    eps = 1e-12
    dims: list[DimensionScore] = []
    for d in DIMENSIONS:
        pre = credits[d] * weights[d] * pool
        capped_val = min(pre, dim_cap)
        dims.append(
            DimensionScore(
                dimension=d,
                credit=credits[d],
                weight=weights[d],
                contribution=capped_val,
                capped=pre > dim_cap + eps,
            )
        )
    total = sum(x.contribution for x in dims)
    bonus = min(total, pool)
    return tuple(dims), bonus, total > pool + eps


# --- spot-audit sampling (R11) --------------------------------------------------


def spot_audit_sample(
    high_bonus_episode_ids: Sequence[int],
    params: ImprovementParams,
    *,
    seed: int,
) -> tuple[int, ...]:
    """Deterministically flag the configured fraction of high-bonus episodes (R11).

    ``k = round(spot_audit_fraction · N)`` episodes are sampled with a seeded RNG
    over the sorted ids — reproducible per seed, no wall-clock entropy.
    """
    ids = sorted(set(high_bonus_episode_ids))
    if not ids:
        return ()
    k = round(params.spot_audit_fraction * len(ids))
    if k <= 0:
        return ()
    k = min(k, len(ids))
    rng = random.Random(seed)
    return tuple(sorted(rng.sample(ids, k)))


# --- the improvement-tier result ------------------------------------------------


@dataclass(frozen=True)
class ImprovementResult:
    """The settled improvement-tier grade (R9-R11), ready for the settlement report."""

    pass_rate: float
    gate_threshold: float
    gate_passed: bool
    built: bool
    bonus: float  # fraction of base; exactly 0 when zeroed
    total_capped: bool
    zeroed_reason: str | None  # ZEROED_GATE | ZEROED_BUILD | None
    seed: int
    dimensions: tuple[DimensionScore, ...]
    spot_audit_eligible: bool

    @property
    def report(self) -> dict:
        return improvement_report(self)


def grade_improvement(
    *,
    pass_rate: float,
    grader_sigma: float,
    build: BuildOutcome,
    credits: Mapping[str, float],
    seed: int,
    params: ImprovementParams = ImprovementParams(),
) -> ImprovementResult:
    """Grade the improvement tier end to end (R9-R11).

    Lexicographic: a failed behavioral gate OR a failed production build zeroes
    the bonus regardless of stellar components (the reason is recorded). Otherwise
    the capped, weight-rotated bonus is composed. ``credits`` must carry exactly
    the four :data:`DIMENSIONS`, each in [0, 1] (the dimension helpers produce
    them; the design credit comes from :func:`judge_design`).
    """
    if not (0.0 <= pass_rate <= 1.0):
        raise ImprovementError(f"pass_rate must be in [0, 1], got {pass_rate}")
    if set(credits) != set(DIMENSIONS):
        raise ImprovementError(
            f"credits must carry exactly {DIMENSIONS}, got {tuple(sorted(credits))}"
        )
    for d, c in credits.items():
        if not (0.0 <= c <= 1.0):
            raise ImprovementError(f"credit for {d!r} must be in [0, 1], got {c}")

    gate_threshold = derive_gate_threshold(grader_sigma, params)
    gate_passed = pass_rate >= gate_threshold
    weights = rotated_weights(seed, params)

    zeroed_reason: str | None = None
    if not gate_passed:
        zeroed_reason = ZEROED_GATE
    elif not build.built:
        zeroed_reason = ZEROED_BUILD

    if zeroed_reason is not None:
        # Itemize components for transparency, but every contribution is zero.
        dims = tuple(
            DimensionScore(
                dimension=d,
                credit=credits[d],
                weight=weights[d],
                contribution=0.0,
                capped=False,
            )
            for d in DIMENSIONS
        )
        bonus, total_capped = 0.0, False
    else:
        dims, bonus, total_capped = compose_bonus(credits, weights, params)

    spot_eligible = zeroed_reason is None and bonus >= params.high_bonus_threshold
    logger.info(
        "improvement grade: gate=%s(thr=%.4f, rate=%.4f) built=%s bonus=%.4f"
        " zeroed=%s total_capped=%s",
        gate_passed,
        gate_threshold,
        pass_rate,
        build.built,
        bonus,
        zeroed_reason,
        total_capped,
    )
    return ImprovementResult(
        pass_rate=pass_rate,
        gate_threshold=gate_threshold,
        gate_passed=gate_passed,
        built=build.built,
        bonus=bonus,
        total_capped=total_capped,
        zeroed_reason=zeroed_reason,
        seed=seed,
        dimensions=dims,
        spot_audit_eligible=spot_eligible,
    )


def improvement_report(result: ImprovementResult) -> dict:
    """Itemize every bonus component for the settlement report (R10/R11).

    Pure function of the result — no timestamps, byte-stable rendering. The report
    carries the rubric criteria so a reviewer can confirm folklore metrics are
    absent (R10c), and the per-dimension / total caps so the anti-Goodhart bounds
    are auditable.
    """
    return {
        "bonus": result.bonus,
        "dimensions": [
            {
                "capped": d.capped,
                "contribution": d.contribution,
                "credit": d.credit,
                "dimension": d.dimension,
                "weight": d.weight,
            }
            for d in result.dimensions
        ],
        "gate": {
            "passed": result.gate_passed,
            "pass_rate": result.pass_rate,
            "threshold": result.gate_threshold,
        },
        "guards": {
            "code_structure_criteria": list(CODE_STRUCTURE_CRITERIA),
            "design_rubric": list(DESIGN_RUBRIC),
            "forbidden_code_metrics": list(FORBIDDEN_CODE_METRICS),
            "note": "improvement bonuses ride deterministic instruments where"
            " possible; MI/cyclomatic folklore is barred from code-structure"
            " rubric inputs (R10c)",
        },
        "production_build": {"built": result.built},
        "seed": result.seed,
        "spot_audit_eligible": result.spot_audit_eligible,
        "total_capped": result.total_capped,
        "zeroed_reason": result.zeroed_reason,
    }
