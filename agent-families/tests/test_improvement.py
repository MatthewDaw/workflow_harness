"""plan-005 U3: improvement-tier grading (R9-R11).

Offline by construction: the deterministic instruments (perf, UX, lockfile/lint)
take typed measurements; the judge-mediated dimensions (design pairwise, code
rubric) take injected scripted fakes through the module's judge seams — so the
full gate × caps × rotation decision table runs with zero quota and no ``claude``
on PATH. The one live dual-app bonus run (``vite build`` + ``vite preview`` + k6 +
playwright screenshots + axe) is the documented docker-required deliverable.

## Conformance

Test-scenario / invariant (plan-005 U3) -> test:

- gate-fail zeroes the bonus regardless of stellar components:
  ``test_gate_fail_zeroes_bonus_despite_stellar_components``
- per-dimension cap enforced: ``test_per_dimension_cap_enforced``
- total cap enforced: ``test_total_bonus_cap_enforced``
- p95 credit caps at 2×: ``test_p95_credit_caps_at_2x`` (+ ``test_p95_slower_clone_zero_latency_credit``)
- uncertain design judgment scores neutral: ``test_uncertain_design_scores_neutral``,
  ``test_design_swap_disagreement_neutral``, ``test_design_low_confidence_neutral``,
  ``test_design_clone_clearly_better_scores``
- weight rotation changes composition deterministically by seed:
  ``test_weight_rotation_changes_composition_by_seed``,
  ``test_weight_rotation_deterministic_same_seed``
- spot-audit sampling flags the configured fraction:
  ``test_spot_audit_flags_configured_fraction``, ``test_spot_audit_only_high_bonus_episodes``
- folklore metrics (MI/cyclomatic) provably absent from rubric inputs:
  ``test_folklore_metrics_absent_from_rubric``,
  ``test_code_measurement_rejects_folklore_metric_input``,
  ``test_code_measurement_requires_exact_criteria``
- gate threshold derived from measured grader noise:
  ``test_gate_threshold_derived_from_grader_noise``, ``test_gate_threshold_floor``
- build failure is a typed zero-bonus outcome for perf:
  ``test_build_failure_zeroes_perf_dimension``
- the full decision table (gate × caps × rotation) has a 1:1 test:
  ``test_decision_table``
- itemized report carries every component: ``test_report_itemizes_every_component``
- param validation is fail-fast: ``test_params_validation``
- one live dual-app bonus run documented: ``TestLiveDualAppBonus`` (docker-required)
"""

from __future__ import annotations

import pytest

from agent_families.grading.improvement import (
    BASE_WEIGHTS,
    CODE_STRUCTURE_CRITERIA,
    DESIGN_CLONE_BETTER,
    DESIGN_TARGET_BETTER,
    DESIGN_UNCERTAIN,
    DIMENSIONS,
    FORBIDDEN_CODE_METRICS,
    CodeMeasurement,
    ImprovementError,
    ImprovementParams,
    PerfMeasurement,
    UxMeasurement,
    code_score,
    derive_gate_threshold,
    design_pairwise,
    gate_passes,
    grade_improvement,
    perf_score,
    rotate_weights,
    spot_audit_flagged,
    ux_score,
)

# --- fixtures: typed measurements + scripted judge fakes ----------------------------


def _perf(target_p95=200.0, clone_p95=100.0, lcp=1000.0, inp=80.0, build=True):
    return PerfMeasurement(
        build_succeeded=build,
        target_p95_ms=target_p95,
        clone_p95_ms=clone_p95,
        clone_lcp_samples_ms=(lcp, lcp, lcp, lcp, lcp),
        clone_inp_samples_ms=(inp, inp, inp, inp, inp),
    )


def _code(value=1.0, lockfile=True, lint=True):
    return CodeMeasurement(
        rubric={c: value for c in CODE_STRUCTURE_CRITERIA},
        lockfile_healthy=lockfile,
        lint_clean=lint,
    )


def _ux(axe=0, console=0, viewport=True):
    return UxMeasurement(
        axe_violations=axe, console_errors=console, viewport_pass=viewport
    )


def _design_judge(verdict_a, conf_a, verdict_b, conf_b):
    """A scripted pairwise-design judge fake: returns ordering-1 reply when the
    clone is slot A, ordering-2 reply when swapped."""

    def judge(clone_is_a: bool) -> dict:
        if clone_is_a:
            return {"verdict": verdict_a, "confidence": conf_a}
        return {"verdict": verdict_b, "confidence": conf_b}

    return judge


# A clone-clearly-better judge: clone wins in BOTH orderings, high confidence.
_CLONE_BETTER_JUDGE = _design_judge("A_better", "high", "B_better", "high")


def _stellar_grade(**overrides):
    """A maxed-out, gate-passing grade input set; overrides tweak one knob."""
    base = dict(
        must_tier_pass_rate=1.0,
        base_score=1.0,
        gate_threshold=0.95,
        perf=perf_score(_perf(), ImprovementParams()),
        design=design_pairwise(_CLONE_BETTER_JUDGE),
        code=code_score(_code()),
        ux=ux_score(_ux()),
        rotation_seed=0,
        episode_key="ep-1",
    )
    base.update(overrides)
    return base


# --- hard gate (R9) ------------------------------------------------------------------


def test_gate_threshold_derived_from_grader_noise():
    """The gate threshold is 1 - k·σ: more grader noise => more forgiving gate."""
    params = ImprovementParams(gate_floor=0.0, gate_sigma_multiplier=2.0)
    # zero noise => perfect gate
    assert derive_gate_threshold(0.0, params) == 1.0
    # 1% grader σ => 1 - 2*0.01 = 0.98
    assert derive_gate_threshold(0.01, params) == pytest.approx(0.98)
    # noisier grader => lower (more forgiving) gate
    assert derive_gate_threshold(0.05, params) < derive_gate_threshold(0.01, params)
    with pytest.raises(ImprovementError):
        derive_gate_threshold(-0.1, params)


def test_gate_threshold_floor():
    """The derived threshold never drops below the configured floor."""
    params = ImprovementParams(gate_floor=0.90, gate_sigma_multiplier=2.0)
    # huge σ would push 1-k·σ well below the floor; it clamps at the floor
    assert derive_gate_threshold(0.5, params) == 0.90


def test_gate_passes_boundary():
    assert gate_passes(0.95, 0.95) is True
    assert gate_passes(0.949, 0.95) is False
    with pytest.raises(ImprovementError):
        gate_passes(1.5, 0.95)


def test_gate_fail_zeroes_bonus_despite_stellar_components():
    """Lexicographic: below the gate, every stellar component still yields zero bonus."""
    grade = grade_improvement(**_stellar_grade(must_tier_pass_rate=0.80))
    assert grade.gate_passed is False
    assert grade.total_bonus_fraction == 0.0
    assert grade.total_bonus_value == 0.0
    assert grade.final_score == grade.base_score
    # the components are genuinely stellar — the zero is the gate, not the inputs
    assert grade.dimension_scores["performance"]["score"] > 0.0
    assert grade.dimension_scores["design"]["score"] == 1.0
    # and a passing gate over the SAME components yields a real bonus
    passing = grade_improvement(**_stellar_grade(must_tier_pass_rate=1.0))
    assert passing.gate_passed is True
    assert passing.total_bonus_fraction > 0.0


# --- performance dimension (R10a) ----------------------------------------------------


def test_p95_credit_caps_at_2x():
    """p95 latency credit saturates at 2×: a 4×-faster clone scores no more than 2×."""
    params = ImprovementParams()
    at_2x = perf_score(_perf(target_p95=200.0, clone_p95=100.0), params)
    at_4x = perf_score(_perf(target_p95=400.0, clone_p95=100.0), params)
    assert at_2x["latency_score"] == pytest.approx(1.0)
    assert at_4x["latency_score"] == pytest.approx(1.0)
    assert at_4x["p95_ratio_capped"] == pytest.approx(2.0)
    # exactly equal speed => no latency credit; halfway (1.5×) => half credit
    assert perf_score(_perf(200.0, 200.0), params)["latency_score"] == pytest.approx(0.0)
    assert perf_score(_perf(150.0, 100.0), params)["latency_score"] == pytest.approx(0.5)


def test_p95_slower_clone_zero_latency_credit():
    """A clone slower than the target earns zero latency credit, never negative."""
    score = perf_score(_perf(target_p95=100.0, clone_p95=400.0), ImprovementParams())
    assert score["latency_score"] == 0.0


def test_cwv_median_of_five_against_budgets():
    """CWV is median-of-5 against absolute budgets; out-of-budget loses that half."""
    params = ImprovementParams(lcp_budget_ms=2500.0, inp_budget_ms=200.0)
    good = perf_score(_perf(lcp=1000.0, inp=80.0), params)
    assert good["cwv_score"] == pytest.approx(1.0)
    half = perf_score(_perf(lcp=9000.0, inp=80.0), params)  # LCP blown, INP ok
    assert half["cwv_score"] == pytest.approx(0.5)
    assert good["lcp_median_ms"] == 1000.0  # median of 5 identical samples


def test_cwv_requires_five_samples():
    with pytest.raises(ImprovementError):
        PerfMeasurement(
            build_succeeded=True, target_p95_ms=200.0, clone_p95_ms=100.0,
            clone_lcp_samples_ms=(1.0, 2.0), clone_inp_samples_ms=(1.0,) * 5,
        )


def test_build_failure_zeroes_perf_dimension():
    """A failed production build is a typed zero-bonus outcome for perf (R10a)."""
    perf = PerfMeasurement(
        build_succeeded=False, target_p95_ms=0.0, clone_p95_ms=0.0,
        clone_lcp_samples_ms=(), clone_inp_samples_ms=(),
        build_failure_reason="vite build exited 1: type error in App.tsx",
    )
    score = perf_score(perf, ImprovementParams())
    assert score["score"] == 0.0
    assert score["build_succeeded"] is False
    assert "vite build exited 1" in score["build_failure_reason"]
    # and it zeroes only perf — the whole grade still computes from the other dims
    grade = grade_improvement(**_stellar_grade(perf=score))
    assert grade.dimension_scores["performance"]["score"] == 0.0
    assert grade.total_bonus_fraction > 0.0  # design/code/ux still contribute


# --- design dimension (R10b) ---------------------------------------------------------


def test_design_clone_clearly_better_scores():
    """Both orderings agree clone-better at high confidence => full design score."""
    outcome = design_pairwise(_CLONE_BETTER_JUDGE)
    assert outcome.verdict == DESIGN_CLONE_BETTER
    assert outcome.score == 1.0


def test_uncertain_design_scores_neutral():
    """An 'equivalent' verdict is neutral — no bonus (R10b)."""
    judge = _design_judge("equivalent", "high", "equivalent", "high")
    outcome = design_pairwise(judge)
    assert outcome.verdict == DESIGN_UNCERTAIN
    assert outcome.score == 0.0


def test_design_low_confidence_neutral():
    """Any low-confidence reply collapses to neutral (coin-flip when close; §10)."""
    judge = _design_judge("A_better", "low", "B_better", "high")
    outcome = design_pairwise(judge)
    assert outcome.verdict == DESIGN_UNCERTAIN
    assert outcome.score == 0.0


def test_design_swap_disagreement_neutral():
    """Order-position bias: judge always picks slot A. After un-swapping the two
    orderings disagree on who's better => neutral (the swap-and-average guard)."""
    judge = _design_judge("A_better", "high", "A_better", "high")
    outcome = design_pairwise(judge)
    assert outcome.verdict == DESIGN_UNCERTAIN
    assert outcome.score == 0.0


def test_design_target_better_scores_zero_not_negative():
    """A clearly-worse clone scores zero design bonus, never a penalty (bonus only adds)."""
    judge = _design_judge("B_better", "high", "A_better", "high")  # target wins both
    outcome = design_pairwise(judge)
    assert outcome.verdict == DESIGN_TARGET_BETTER
    assert outcome.score == 0.0


def test_design_invalid_judge_reply_rejected():
    with pytest.raises(ImprovementError):
        design_pairwise(_design_judge("maybe", "high", "B_better", "high"))
    with pytest.raises(ImprovementError):
        design_pairwise(_design_judge("A_better", "certain", "B_better", "high"))


# --- code-structure dimension (R10c) -------------------------------------------------


def test_folklore_metrics_absent_from_rubric():
    """MI/cyclomatic are provably not among the rubric criteria (R10c)."""
    for folklore in ("maintainability_index", "cyclomatic_complexity", "halstead"):
        assert folklore not in CODE_STRUCTURE_CRITERIA
    # the criteria are the criterion-separated set the design names
    assert set(CODE_STRUCTURE_CRITERIA) == {
        "modularity", "naming", "dead_code_absence", "dependency_health"
    }


def test_code_measurement_rejects_folklore_metric_input():
    """A measurement smuggling a banned metric name is a hard error (R10c)."""
    for banned in FORBIDDEN_CODE_METRICS:
        with pytest.raises(ImprovementError, match="folklore"):
            CodeMeasurement(rubric={banned: 0.9}, lockfile_healthy=True, lint_clean=True)


def test_code_measurement_requires_exact_criteria():
    with pytest.raises(ImprovementError):
        CodeMeasurement(
            rubric={"modularity": 0.9}, lockfile_healthy=True, lint_clean=True
        )
    with pytest.raises(ImprovementError):
        CodeMeasurement(
            rubric={c: 1.5 for c in CODE_STRUCTURE_CRITERIA},
            lockfile_healthy=True, lint_clean=True,
        )


def test_code_score_blends_rubric_and_health():
    score = code_score(_code(value=1.0, lockfile=True, lint=True))
    assert score["score"] == pytest.approx(1.0)
    half_health = code_score(_code(value=1.0, lockfile=True, lint=False))
    assert half_health["health_score"] == pytest.approx(0.5)
    assert half_health["score"] == pytest.approx(0.75)  # (1.0 + 0.5) / 2
    assert "cyclomatic_complexity" not in score["rubric"]


# --- automated UX dimension (R10d) ---------------------------------------------------


def test_ux_score_combines_three_checks():
    assert ux_score(_ux(0, 0, True))["score"] == pytest.approx(1.0)
    assert ux_score(_ux(3, 0, True))["score"] == pytest.approx(2 / 3)
    assert ux_score(_ux(0, 1, False))["score"] == pytest.approx(1 / 3)
    with pytest.raises(ImprovementError):
        UxMeasurement(axe_violations=-1, console_errors=0, viewport_pass=True)


# --- anti-Goodhart: weight rotation (R11) --------------------------------------------


def test_weight_rotation_deterministic_same_seed():
    """Same seed => identical weight assignment (reproducible audits)."""
    assert rotate_weights(7) == rotate_weights(7)


def test_weight_rotation_changes_composition_by_seed():
    """Consecutive seeds shift which dimension carries which weight (R11)."""
    w0 = rotate_weights(0)
    w1 = rotate_weights(1)
    assert w0 != w1
    # weights are a rotation of the same base multiset, so each run sums to 1.0
    assert sum(w0.values()) == pytest.approx(1.0)
    assert sorted(w0.values()) == sorted(BASE_WEIGHTS)
    assert sorted(w1.values()) == sorted(BASE_WEIGHTS)
    # the full rotation cycles every n seeds back to the start
    assert rotate_weights(len(DIMENSIONS)) == w0
    with pytest.raises(ImprovementError):
        rotate_weights(-1)


def test_rotation_changes_grade_composition():
    """Different seeds change the itemized contributions of a fixed input set."""
    # asymmetric raw scores so the weight->dimension assignment is observable
    perf = perf_score(_perf(), ImprovementParams())  # high
    code = code_score(_code(value=0.0, lockfile=False, lint=False))  # zero
    g0 = grade_improvement(**_stellar_grade(perf=perf, code=code, rotation_seed=0))
    g1 = grade_improvement(**_stellar_grade(perf=perf, code=code, rotation_seed=1))
    assert g0.weights != g1.weights
    assert g0.contributions != g1.contributions


# --- anti-Goodhart: per-dimension and total caps (R11) -------------------------------


def test_per_dimension_cap_enforced():
    """No single dimension's contribution exceeds per_dimension_cap of the pool (R11)."""
    params = ImprovementParams(per_dimension_cap=0.30)
    # construct a run where one dimension has weight 0.30 and raw 1.0 => uncapped 0.30,
    # exactly the cap; push weight higher via rotation is bounded by BASE_WEIGHTS max.
    grade = grade_improvement(**_stellar_grade(params=params))
    for name, contribution in grade.contributions.items():
        assert contribution <= params.per_dimension_cap + 1e-9, name
    # a deliberately tiny cap binds every maxed dimension to exactly the cap
    tight = ImprovementParams(per_dimension_cap=0.05)
    grade2 = grade_improvement(**_stellar_grade(params=tight))
    assert all(c <= 0.05 + 1e-9 for c in grade2.contributions.values())
    assert any(c == pytest.approx(0.05) for c in grade2.contributions.values())


def test_total_bonus_cap_enforced():
    """Total bonus never exceeds total_bonus_cap of the base, even all-maxed (R11)."""
    for cap in (0.15, 0.20):
        params = ImprovementParams(total_bonus_cap=cap, per_dimension_cap=1.0)
        grade = grade_improvement(**_stellar_grade(params=params))
        assert grade.total_bonus_fraction <= cap + 1e-9
        # all dimensions maxed + weights sum to 1 => pool fully claimed => exactly cap
        assert grade.total_bonus_fraction == pytest.approx(cap)
        assert grade.total_bonus_value == pytest.approx(grade.base_score * cap)
        assert grade.final_score == pytest.approx(grade.base_score * (1 + cap))


# --- anti-Goodhart: spot-audit sampling (R11) ----------------------------------------


def test_spot_audit_only_high_bonus_episodes():
    """Low-bonus episodes are never audited, regardless of the key hash (R11)."""
    params = ImprovementParams(spot_audit_fraction=1.0)  # would audit everything eligible
    low = params.high_bonus_threshold * params.total_bonus_cap - 1e-6
    assert spot_audit_flagged(low, "ep-x", params) is False
    high = params.high_bonus_threshold * params.total_bonus_cap
    assert spot_audit_flagged(high, "ep-x", params) is True  # fraction=1.0 => all eligible


def test_spot_audit_flags_configured_fraction():
    """Over many high-bonus episodes, the flagged share matches the configured
    fraction (deterministic, uniform sampling) — R11."""
    params = ImprovementParams(spot_audit_fraction=0.20)
    high_bonus = params.total_bonus_cap  # max bonus => definitely eligible
    n = 4000
    flagged = sum(
        spot_audit_flagged(high_bonus, f"episode-{i}", params) for i in range(n)
    )
    assert flagged / n == pytest.approx(0.20, abs=0.03)
    # deterministic: the same keys flag identically on a re-run
    again = sum(
        spot_audit_flagged(high_bonus, f"episode-{i}", params) for i in range(n)
    )
    assert again == flagged


# --- composition + report (R10/R11) --------------------------------------------------


def test_report_itemizes_every_component():
    """The settlement report section carries every bonus component (R10)."""
    grade = grade_improvement(**_stellar_grade())
    report = grade.report()
    assert set(report["dimensions"]) == set(DIMENSIONS)
    for name in DIMENSIONS:
        dim = report["dimensions"][name]
        assert "raw_score" in dim and "weight" in dim and "capped_contribution" in dim
        assert "details" in dim
    assert report["gate"]["passed"] is True
    assert report["total_bonus"]["fraction_of_base"] > 0.0
    # the perf itemization survives whole into the report details
    assert "p95_ratio" in report["dimensions"]["performance"]["details"]
    # design verdict + both orderings are itemized
    assert report["dimensions"]["design"]["details"]["verdict"] == DESIGN_CLONE_BETTER
    assert len(report["dimensions"]["design"]["details"]["orderings"]) == 2
    # no folklore metric anywhere in the code itemization
    assert "cyclomatic_complexity" not in report["dimensions"]["code_structure"]["details"]["rubric"]


def test_grade_validates_base_score():
    with pytest.raises(ImprovementError):
        grade_improvement(**_stellar_grade(base_score=1.5))


def test_params_validation():
    with pytest.raises(ImprovementError):
        ImprovementParams(per_dimension_cap=1.5)
    with pytest.raises(ImprovementError):
        ImprovementParams(total_bonus_cap=-0.1)
    with pytest.raises(ImprovementError):
        ImprovementParams(p95_credit_cap=1.0)  # 1× is no credit
    with pytest.raises(ImprovementError):
        ImprovementParams(gate_sigma_multiplier=-1.0)
    with pytest.raises(ImprovementError):
        ImprovementParams(lcp_budget_ms=0.0)


# --- the full decision table: gate × caps × rotation (Verification) ------------------


def test_decision_table():
    """Gate × per-dimension-cap × total-cap × rotation: a 1:1 enumerated table.

    Each row fixes the four toggles and asserts the resulting (gate, capped
    contribution shape, total bonus, final) — so a regression in any axis fails a
    named row, not a vague aggregate.
    """
    base = dict(
        base_score=1.0, gate_threshold=0.95,
        perf=perf_score(_perf(), ImprovementParams()),  # perf raw = 1.0
        design=design_pairwise(_CLONE_BETTER_JUDGE),    # design raw = 1.0
        code=code_score(_code()),                       # code raw = 1.0
        ux=ux_score(_ux()),                             # ux raw = 1.0
        episode_key="ep-table",
    )

    # Row 1: gate FAIL -> zero bonus regardless of caps/rotation.
    g = grade_improvement(
        **base, must_tier_pass_rate=0.0, rotation_seed=0,
        params=ImprovementParams(),
    )
    assert (g.gate_passed, g.total_bonus_fraction, g.final_score) == (False, 0.0, 1.0)

    # Row 2: gate PASS, generous caps -> total bonus == total_bonus_cap (pool full,
    # all dims maxed), per-dimension cap not binding.
    p2 = ImprovementParams(per_dimension_cap=1.0, total_bonus_cap=0.20)
    g = grade_improvement(**base, must_tier_pass_rate=1.0, rotation_seed=0, params=p2)
    assert g.gate_passed is True
    assert g.total_bonus_fraction == pytest.approx(0.20)
    assert g.final_score == pytest.approx(1.20)

    # Row 3: gate PASS, tight per-dimension cap -> every maxed dim binds at the cap;
    # pool fraction = sum of caps clamped to 1, total bonus scaled by total_bonus_cap.
    p3 = ImprovementParams(per_dimension_cap=0.10, total_bonus_cap=0.20)
    g = grade_improvement(**base, must_tier_pass_rate=1.0, rotation_seed=0, params=p3)
    assert all(c == pytest.approx(0.10) for c in g.contributions.values())
    # 4 dims × 0.10 = 0.40 of pool; × 0.20 cap = 0.08 bonus of base
    assert g.total_bonus_fraction == pytest.approx(0.40 * 0.20)

    # Row 4: rotation seed changes the per-dimension contribution map but, with all
    # raw scores equal and caps generous, leaves the TOTAL invariant.
    p4 = ImprovementParams(per_dimension_cap=1.0, total_bonus_cap=0.20)
    g0 = grade_improvement(**base, must_tier_pass_rate=1.0, rotation_seed=0, params=p4)
    g1 = grade_improvement(**base, must_tier_pass_rate=1.0, rotation_seed=1, params=p4)
    assert g0.weights != g1.weights
    assert g0.total_bonus_fraction == pytest.approx(g1.total_bonus_fraction)

    # Row 5: rotation DOES change the total when raw scores are asymmetric and a tight
    # cap interacts with which dimension holds the heavy weight.
    asym = dict(base)
    asym["code"] = code_score(_code(value=0.0, lockfile=False, lint=False))  # code raw=0
    pt = ImprovementParams(per_dimension_cap=0.25, total_bonus_cap=0.20)
    seeds = {
        s: grade_improvement(
            **asym, must_tier_pass_rate=1.0, rotation_seed=s, params=pt
        ).total_bonus_fraction
        for s in range(len(DIMENSIONS))
    }
    assert len(set(round(v, 6) for v in seeds.values())) > 1  # rotation matters


# --- live dual-app bonus run (the documented R10/Verification deliverable) -----------


@pytest.mark.docker
@pytest.mark.skipif(
    True,
    reason="live dual-app improvement bonus run: requires Docker (target), Node "
    "(vite build + vite preview of the clone), k6/autocannon, and Playwright+axe. "
    "Documented manual procedure — see the class docstring.",
)
class TestLiveDualAppBonus:
    """One live improvement-bonus run against a built clone (R10/Verification).

    Manual procedure (Docker + Node + k6 + Playwright installed):

    1. Bring up the target and seed it; build the clone for PRODUCTION
       (``npm run build && npm run preview`` in the episode workspace) — NEVER the
       dev server (R10a: dev-mode overhead systematically zeroes perf).
    2. Run k6/autocannon against both apps under one identical load profile; take
       the p95 ratio. Capture LCP + INP median-of-5 against the preview build.
    3. Capture full-page screenshots of both apps for the pairwise design judge
       (swap-and-average through the record-mode judge seam; commit the fixtures).
    4. Run axe-core, collect console errors, run the mobile-viewport check.
    5. Run the criterion-separated code rubric over the clone's source.
    6. Feed the measurements to ``grade_improvement`` and assert: gate passes, the
       bonus is itemized, total ≤ total_bonus_cap of base, perf credit ≤ 2×.
    """

    def test_live_dual_app_bonus(self):  # pragma: no cover - documented manual run
        raise AssertionError("documented manual procedure; see class docstring")
