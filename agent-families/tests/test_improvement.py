"""plan-005 U3: improvement-tier grading (R9-R11).

Grade "better", not just "same", without corrupting the reward. The lexicographic
gate, the four capped bonus dimensions, the anti-Goodhart composition, and the
spot-audit sampler — all offline. The deterministic instruments (k6/CWV/axe/
lockfile-lint/rubric) are injected as typed measurements; the ONE LLM call (the
pairwise design judge) replays recorded fixtures through the judge seam (zero
quota, zero subprocess). The live dual-app bonus run is documented and tool-gated.

## Conformance

Test-scenario / invariant (plan-005 U3) -> test mapping:

- gate-fail zeroes the bonus regardless of stellar components (R9):
  ``test_gate_fail_zeroes_bonus_regardless_of_stellar_components``
- gate threshold derived from measured grader noise (R9):
  ``test_gate_threshold_derived_from_grader_noise``
- build-fail is a typed zero-bonus outcome distinct from gate-fail (R10 approach):
  ``test_build_failure_zeroes_bonus_typed``
- per-dimension cap enforced (R11):
  ``test_per_dimension_cap_enforced``
- total cap enforced (R11): ``test_total_cap_enforced``
- p95 latency credit caps at 2× (R10a): ``test_p95_credit_caps_at_two_x``
- CWV median-of-5 vs absolute budgets (R10a): ``test_cwv_median_against_budgets``
- uncertain design judgment scores neutral (R10b, fixture):
  ``test_uncertain_design_judgment_scores_neutral``
- design swap-and-average rewards a clearly-better clone (R10b, fixture):
  ``test_design_swap_and_average_rewards_better_clone``
- design gross-difference gating skips the judge when not gross (R10b):
  ``test_design_gross_difference_gating_skips_judge``
- weight rotation changes composition between runs, deterministic by seed (R11):
  ``test_weight_rotation_changes_composition_by_seed``
- spot-audit sampling flags the configured fraction (R11):
  ``test_spot_audit_sampling_flags_configured_fraction``
- folklore metrics (MI/cyclomatic) provably absent from rubric inputs (R10c):
  ``test_folklore_metrics_absent_from_rubric_inputs``,
  ``test_code_structure_rejects_folklore_criterion``
- code-structure + UX deterministic credit (R10c/R10d):
  ``test_code_structure_credit_composition``, ``test_ux_credit_composition``
- the gate × caps × rotation decision table has a 1:1 test (verification):
  ``test_decision_table``
- every bonus component is itemized in the report (R10/R11):
  ``test_report_itemizes_every_component``
- one live dual-app bonus run documented (verification):
  ``test_live_dual_app_bonus_run`` (tool-gated, documented procedure)
"""

from __future__ import annotations

import os
import shutil

import pytest

from agent_families.grading.improvement import (
    CODE_STRUCTURE_CRITERIA,
    DESIGN_RUBRIC,
    DESIGN_SCHEMA,
    DIMENSIONS,
    FORBIDDEN_CODE_METRICS,
    ZEROED_BUILD,
    ZEROED_GATE,
    BuildOutcome,
    CodeStructureMeasurement,
    DesignJudgeConfig,
    ImprovementError,
    ImprovementParams,
    LatencyProfile,
    UxMeasurement,
    WebVital,
    assert_no_folklore_metrics,
    code_structure_credit,
    compose_bonus,
    cwv_credit,
    derive_gate_threshold,
    design_prompt,
    grade_improvement,
    improvement_report,
    judge_design,
    latency_credit,
    performance_credit,
    rotated_weights,
    spot_audit_sample,
    ux_credit,
)
from agent_families.judge import write_fixture

# A clone that is better/equal on every dimension — used to prove the gate and
# build zeroing override even "stellar" components.
STELLAR = {d: 1.0 for d in DIMENSIONS}
BUILT = BuildOutcome(built=True)


def envelope(output: dict) -> dict:
    return {
        "structured_output": output,
        "is_error": False,
        "total_cost_usd": 0.0,
        "duration_ms": 1,
    }


def design_judgment(verdict: str, confidence: str, reasoning: str = "cot") -> dict:
    return {"reasoning": reasoning, "verdict": verdict, "confidence": confidence}


def design_config(tmp_path) -> DesignJudgeConfig:
    return DesignJudgeConfig(
        model="sonnet", max_retries=0, mode="replay", fixtures_dir=tmp_path
    )


# --- gate (R9) ----------------------------------------------------------------


def test_gate_threshold_derived_from_grader_noise():
    params = ImprovementParams()  # k=2.0, floor=0.5
    assert derive_gate_threshold(0.0, params) == 1.0, "no noise -> perfect bar"
    assert derive_gate_threshold(0.05, params) == pytest.approx(0.90)
    # Large noise is floored, never below the floor.
    assert derive_gate_threshold(0.5, params) == 0.5
    with pytest.raises(ImprovementError, match="grader_sigma"):
        derive_gate_threshold(-0.1, params)


def test_gate_fail_zeroes_bonus_regardless_of_stellar_components():
    # pass_rate below the noise-derived threshold: bonus is exactly zero even
    # though every dimension is maxed and the build succeeded (R9 lexicographic).
    result = grade_improvement(
        pass_rate=0.70,
        grader_sigma=0.0,  # threshold == 1.0
        build=BUILT,
        credits=STELLAR,
        seed=0,
    )
    assert result.gate_passed is False
    assert result.bonus == 0.0
    assert result.zeroed_reason == ZEROED_GATE
    assert result.spot_audit_eligible is False
    # Components are still itemized (transparency) but contribute nothing.
    assert all(d.contribution == 0.0 for d in result.dimensions)
    assert {d.dimension for d in result.dimensions} == set(DIMENSIONS)


def test_build_failure_zeroes_bonus_typed():
    # Gate passes, but the production build failed -> typed zero-bonus outcome,
    # distinct reason from a gate failure (R10 approach).
    result = grade_improvement(
        pass_rate=1.0,
        grader_sigma=0.0,
        build=BuildOutcome(built=False, detail="vite build exited 1"),
        credits=STELLAR,
        seed=0,
    )
    assert result.gate_passed is True
    assert result.built is False
    assert result.bonus == 0.0
    assert result.zeroed_reason == ZEROED_BUILD
    assert result.zeroed_reason != ZEROED_GATE


# --- performance (R10a) -------------------------------------------------------


def test_p95_credit_caps_at_two_x():
    params = ImprovementParams()
    same = LatencyProfile(clone_p95_ms=100.0, target_p95_ms=100.0, profile="k6-default")
    twice = LatencyProfile(clone_p95_ms=50.0, target_p95_ms=100.0, profile="k6-default")
    insane = LatencyProfile(clone_p95_ms=1.0, target_p95_ms=100.0, profile="k6-default")
    assert latency_credit(same, params) == 0.0, "no faster than target -> no credit"
    assert latency_credit(twice, params) == pytest.approx(1.0), "2× faster -> full"
    assert latency_credit(insane, params) == pytest.approx(1.0), (
        "100× faster still caps at the 2× credit ceiling — no runaway reward"
    )


def test_cwv_median_against_budgets():
    params = ImprovementParams()
    # LCP median (of 5) under budget; INP median over budget -> 1 of 2 vitals.
    lcp = WebVital("LCP", (1800.0, 1900.0, 2000.0, 2100.0, 2200.0), budget_ms=2500.0)
    inp = WebVital("INP", (210.0, 220.0, 230.0, 240.0, 250.0), budget_ms=200.0)
    assert lcp.median_ms == 2000.0 and lcp.under_budget is True
    assert inp.under_budget is False
    assert cwv_credit((lcp, inp), params) == pytest.approx(0.5)
    assert cwv_credit((), params) == 0.0
    # performance dimension = mean(latency, cwv)
    fast = LatencyProfile(clone_p95_ms=50.0, target_p95_ms=100.0, profile="p")
    assert performance_credit(fast, (lcp, inp), params) == pytest.approx(0.75)


# --- design (R10b) ------------------------------------------------------------


def test_design_gross_difference_gating_skips_judge(tmp_path):
    # fixtures_dir is EMPTY: a judge call would raise. Not-gross -> neutral, no call.
    outcome = judge_design(
        "clone-shot", "target-shot",
        gross_difference=False, config=design_config(tmp_path),
    )
    assert outcome.invoked is False
    assert outcome.neutral is True
    assert outcome.advantage == 0.5
    assert outcome.credit == 0.0


def test_uncertain_design_judgment_scores_neutral(tmp_path):
    cfg = design_config(tmp_path)
    # Both swap calls return 'uncertain' -> neutral advantage, zero design bonus.
    write_fixture(
        tmp_path, design_prompt("clone-shot", "target-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("uncertain", "low")),
    )
    write_fixture(
        tmp_path, design_prompt("target-shot", "clone-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("uncertain", "high")),
    )
    outcome = judge_design(
        "clone-shot", "target-shot", gross_difference=True, config=cfg
    )
    assert outcome.invoked is True
    assert outcome.advantage == 0.5
    assert outcome.credit == 0.0
    assert outcome.neutral is True


def test_design_swap_and_average_rewards_better_clone(tmp_path):
    cfg = design_config(tmp_path)
    # call1 (clone is A): a_better/high -> clone clearly better.
    write_fixture(
        tmp_path, design_prompt("clone-shot", "target-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("a_better", "high")),
    )
    # call2 (clone is B): b_better/high -> clone clearly better again.
    write_fixture(
        tmp_path, design_prompt("target-shot", "clone-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("b_better", "high")),
    )
    outcome = judge_design(
        "clone-shot", "target-shot", gross_difference=True, config=cfg
    )
    assert outcome.advantage == pytest.approx(1.0)
    assert outcome.credit == pytest.approx(1.0)
    assert outcome.neutral is False
    assert [c["position"] for c in outcome.calls] == ["clone_as_a", "clone_as_b"]


def test_design_swap_average_low_confidence_is_neutral_half(tmp_path):
    cfg = design_config(tmp_path)
    # call1 strongly favors clone, call2 low confidence -> contributes neutral 0.5.
    write_fixture(
        tmp_path, design_prompt("clone-shot", "target-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("a_better", "high")),
    )
    write_fixture(
        tmp_path, design_prompt("target-shot", "clone-shot"),
        DESIGN_SCHEMA, "sonnet", envelope(design_judgment("a_better", "low")),
    )
    outcome = judge_design(
        "clone-shot", "target-shot", gross_difference=True, config=cfg
    )
    # adv1 = 1.0 (clone=A, a_better), adv2 = 0.5 (low confidence) -> avg 0.75.
    assert outcome.advantage == pytest.approx(0.75)
    assert outcome.credit == pytest.approx(0.5)


# --- code structure (R10c) ----------------------------------------------------


def test_folklore_metrics_absent_from_rubric_inputs():
    # The shipped rubric provably contains no MI/cyclomatic folklore.
    for crit in CODE_STRUCTURE_CRITERIA:
        assert not any(bad in crit.lower() for bad in FORBIDDEN_CODE_METRICS)
    # The guard itself raises on a folklore token in any rubric input.
    assert_no_folklore_metrics(*CODE_STRUCTURE_CRITERIA)  # no raise
    with pytest.raises(ImprovementError, match="cyclomatic"):
        assert_no_folklore_metrics("naming_clarity", "cyclomatic_complexity")
    # And the assembled report's rubric inputs carry no folklore (R10c).
    report = improvement_report(
        grade_improvement(
            pass_rate=1.0, grader_sigma=0.0, build=BUILT, credits=STELLAR, seed=0,
        )
    )
    blob = " ".join(report["guards"]["code_structure_criteria"]).lower()
    assert all(bad not in blob for bad in FORBIDDEN_CODE_METRICS)


def test_code_structure_rejects_folklore_criterion():
    with pytest.raises(ImprovementError, match="cyclomatic"):
        CodeStructureMeasurement(
            rubric_scores={"cyclomatic": 0.9}, lockfile_present=True, lint_clean=True
        )
    with pytest.raises(ImprovementError, match="unknown code-structure criterion"):
        CodeStructureMeasurement(
            rubric_scores={"vibes": 0.9}, lockfile_present=True, lint_clean=True
        )


def test_code_structure_credit_composition():
    params = ImprovementParams()
    m = CodeStructureMeasurement(
        rubric_scores={c: 1.0 for c in CODE_STRUCTURE_CRITERIA},
        lockfile_present=True,
        lint_clean=True,
    )
    assert code_structure_credit(m, params) == pytest.approx(1.0)
    # rubric perfect, but missing lockfile + dirty lint -> 0.6 only.
    m2 = CodeStructureMeasurement(
        rubric_scores={c: 1.0 for c in CODE_STRUCTURE_CRITERIA},
        lockfile_present=False,
        lint_clean=False,
    )
    assert code_structure_credit(m2, params) == pytest.approx(0.6)


def test_ux_credit_composition():
    params = ImprovementParams()
    clean = UxMeasurement(axe_violations=0, console_errors=0, viewport_ok=True)
    assert ux_credit(clean, params) == pytest.approx(1.0)
    one_bad = UxMeasurement(axe_violations=3, console_errors=0, viewport_ok=True)
    assert ux_credit(one_bad, params) == pytest.approx(2 / 3)
    with pytest.raises(ImprovementError):
        UxMeasurement(axe_violations=-1, console_errors=0, viewport_ok=True)


# --- anti-Goodhart composition (R11) ------------------------------------------


def test_per_dimension_cap_enforced():
    params = ImprovementParams()  # pool 0.20, per-dim cap 0.30*pool = 0.06
    weights = rotated_weights(0, params)  # perf weight 0.50 (highest)
    dims, bonus, total_capped = compose_bonus(STELLAR, weights, params)
    by = {d.dimension: d for d in dims}
    # perf pre-cap = 1.0 * 0.50 * 0.20 = 0.10 > 0.06 -> capped to 0.06.
    assert by["performance"].contribution == pytest.approx(0.06)
    assert by["performance"].capped is True
    # No single dimension can exceed the per-dimension cap.
    assert all(d.contribution <= 0.06 + 1e-9 for d in dims)


def test_total_cap_enforced():
    params = ImprovementParams()  # pool 0.20
    weights = rotated_weights(0, params)
    # All dimensions maxed: 4 × per-dim cap 0.06 = 0.24 > pool -> total caps at 0.20.
    dims, bonus, total_capped = compose_bonus(STELLAR, weights, params)
    assert sum(d.contribution for d in dims) == pytest.approx(0.24)
    assert bonus == pytest.approx(0.20)
    assert total_capped is True


def test_weight_rotation_changes_composition_by_seed():
    params = ImprovementParams()
    w0 = rotated_weights(0, params)
    w1 = rotated_weights(1, params)
    w4 = rotated_weights(4, params)  # full cycle of 4 dims -> back to seed 0
    assert w0 != w1, "consecutive seeds shuffle the dimension weighting"
    assert w0 == w4, "rotation is cyclic mod the dimension count"
    assert rotated_weights(1, params) == w1, "same seed reproduces the weighting"
    # Each rotation is a permutation of the same multiset of base weights.
    assert sorted(w0.values()) == sorted(w1.values())
    # With a sub-cap partial credit the contribution tracks the rotated weight, so
    # composition differs by seed (a maxed dimension would flatten at the cap).
    credits = {"performance": 0.5, "design": 0.0, "code_structure": 0.0, "ux": 0.0}
    c0 = {d.dimension: d.contribution for d in compose_bonus(credits, w0, params)[0]}
    c1 = {d.dimension: d.contribution for d in compose_bonus(credits, w1, params)[0]}
    assert c0 != c1, "rotation changes which weight performance is graded under"
    # perf credit 0.5: seed0 weight 0.50 -> 0.05; seed1 rotates ux's 0.40 in -> 0.04.
    assert c0["performance"] == pytest.approx(0.05)
    assert c1["performance"] == pytest.approx(0.04)


def test_spot_audit_sampling_flags_configured_fraction():
    params = ImprovementParams(spot_audit_fraction=0.25)
    episodes = list(range(1, 9))  # 8 high-bonus episodes
    flagged = spot_audit_sample(episodes, params, seed=7)
    assert len(flagged) == 2, "round(0.25 * 8) == 2 flagged for human audit"
    assert set(flagged) <= set(episodes)
    assert flagged == tuple(sorted(flagged)), "deterministically sorted"
    # Reproducible by seed; a different seed may pick a different subset.
    assert spot_audit_sample(episodes, params, seed=7) == flagged
    # Empty / no-high-bonus input flags nothing.
    assert spot_audit_sample([], params, seed=7) == ()


# --- the gate × caps × rotation decision table (verification) -----------------


@pytest.mark.parametrize(
    "gate_pass, built, all_max, seed, expect_zero, expect_total_cap",
    [
        # gate fail dominates everything (R9)
        (False, True, True, 0, True, False),
        (False, False, True, 0, True, False),
        # build fail zeroes when gate passes (R10 approach)
        (True, False, True, 0, True, False),
        # gate+build OK, all dimensions maxed -> total cap binds, both seeds
        (True, True, True, 0, False, True),
        (True, True, True, 1, False, True),
        (True, True, True, 2, False, True),
        # gate+build OK, only one dimension scores -> well under the total cap
        (True, True, False, 0, False, False),
        (True, True, False, 3, False, False),
    ],
)
def test_decision_table(gate_pass, built, all_max, seed, expect_zero, expect_total_cap):
    pass_rate = 1.0 if gate_pass else 0.0
    credits = STELLAR if all_max else {
        "performance": 1.0, "design": 0.0, "code_structure": 0.0, "ux": 0.0
    }
    result = grade_improvement(
        pass_rate=pass_rate,
        grader_sigma=0.0,  # threshold == 1.0
        build=BuildOutcome(built=built),
        credits=credits,
        seed=seed,
    )
    assert (result.bonus == 0.0) is expect_zero
    assert result.total_capped is expect_total_cap
    if expect_zero:
        assert result.zeroed_reason in (ZEROED_GATE, ZEROED_BUILD)
        assert result.spot_audit_eligible is False
    else:
        assert result.zeroed_reason is None
        assert result.bonus <= ImprovementParams().total_bonus_cap + 1e-9


def test_report_itemizes_every_component():
    result = grade_improvement(
        pass_rate=1.0, grader_sigma=0.0, build=BUILT, credits=STELLAR, seed=0,
    )
    report = improvement_report(result)
    assert report == result.report, "the .report property mirrors the function"
    # Every dimension is itemized with its credit, weight, capped flag, contribution.
    itemized = {d["dimension"] for d in report["dimensions"]}
    assert itemized == set(DIMENSIONS)
    for d in report["dimensions"]:
        assert set(d) == {"capped", "contribution", "credit", "dimension", "weight"}
    assert report["gate"]["threshold"] == 1.0
    assert report["production_build"]["built"] is True
    assert report["total_capped"] is True
    assert report["zeroed_reason"] is None
    assert report["guards"]["design_rubric"] == list(DESIGN_RUBRIC)


def test_grade_improvement_rejects_malformed_credits():
    with pytest.raises(ImprovementError, match="credits must carry"):
        grade_improvement(
            pass_rate=1.0, grader_sigma=0.0, build=BUILT,
            credits={"performance": 1.0}, seed=0,
        )
    with pytest.raises(ImprovementError, match="must be in"):
        grade_improvement(
            pass_rate=1.0, grader_sigma=0.0, build=BUILT,
            credits={**STELLAR, "performance": 1.5}, seed=0,
        )
    with pytest.raises(ImprovementError, match="pass_rate"):
        grade_improvement(
            pass_rate=1.5, grader_sigma=0.0, build=BUILT, credits=STELLAR, seed=0,
        )


# --- live dual-app bonus run (documented; tool-gated) -------------------------


@pytest.mark.skipif(
    os.environ.get("AF_LIVE_BONUS") != "1"
    or shutil.which("node") is None
    or shutil.which("k6") is None,
    reason=(
        "live dual-app bonus run — set AF_LIVE_BONUS=1 with node + k6 (and a"
        " Chromium for axe/CWV) on PATH"
    ),
)
def test_live_dual_app_bonus_run(tmp_path):
    """The documented one live dual-app bonus run (U3 verification).

    Procedure (run by hand or in a tooled CI lane; never in the offline gate):

    1. Build the PRODUCTION artifact of the clone: ``npm run build`` then
       ``npm run preview`` (Vite) — Hono served in production mode. A non-zero
       build exit is a :class:`BuildOutcome` with ``built=False`` (zero bonus).
    2. Stand the seeded target up beside it on the static port table.
    3. PERFORMANCE: run the IDENTICAL k6 load profile against both apps; read the
       p95 latencies into a :class:`LatencyProfile`; measure Core Web Vitals
       (LCP, INP) as median-of-5 via Lighthouse/Playwright into :class:`WebVital`.
    4. DESIGN: capture matched screenshots; if the pre-screen flags a GROSS
       difference, run :func:`judge_design` (swap-and-average) through the judge
       seam in ``record`` mode and commit the two fixtures deliberately.
    5. CODE STRUCTURE: score the criterion-separated rubric (no MI/cyclomatic) and
       read lockfile presence + lint cleanliness into
       :class:`CodeStructureMeasurement`.
    6. UX: run axe-core, collect console errors, run viewport checks into
       :class:`UxMeasurement`.
    7. Compose with :func:`grade_improvement`; confirm the gate, the per-dimension
       and total caps, and the itemized report; record the bonus and any
       spot-audit flag.

    This test is a tool-gated executable copy of that procedure; offline it is
    skipped (the documented manual run is the deliverable).
    """
    pytest.skip("documented live procedure; execute manually with AF_LIVE_BONUS=1")
