"""plan-004 U7: Validation and promotion lifecycle (R15-R18).

Fully offline (the default suite): the benchmark/replay episodes and the
frozen-replay re-judge call are injected as typed results / a scripted ``rejudge``
fake, so the full decision table runs with zero quota and no ``claude`` on PATH.
The promote/revert path drives the real store queue.

## Conformance

Test-scenario / invariant (plan-004 U7) -> test:

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
"""

from __future__ import annotations

import pytest

from agent_families.reflector import validate as v
from agent_families.store import Store


# --- fixtures + builders --------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def _register_quarantined_batch(store: Store, label: str, n: int = 2) -> tuple[int, list[int]]:
    """Insert ``n`` quarantined insights tagged to one batch (Stage B's output)."""
    batch_id = store.ensure_batch(label)
    ids = []
    for i in range(n):
        iid = store.insert_insight(
            precondition=f"when {label} case {i}",
            action=f"do {label} action {i}",
            expected_outcome=f"expect {label} outcome {i}",
            content_hash=f"{label}-hash-{i}",
            status="quarantined",
            batch_id=batch_id,
        )
        ids.append(iid)
    return batch_id, ids


def _bench(candidate: float, *, sigma: float = 0.02, history=(0.90, 0.91, 0.89)):
    return v.BenchmarkOutcome(candidate=candidate, sigma=sigma, history=tuple(history))


# A history of ≥10 clean points to leave the bootstrap regime.
POST_BOOTSTRAP_HISTORY = tuple([0.90] * 12)


def _statuses(store: Store, ids):
    return {iid: store.get_insight(iid)["status"] for iid in ids}


# --- pure decision: bootstrap gate ---------------------------------------------


def test_is_bootstrap_needs_both_gates():
    p = v.ValidateParams(min_benchmark_points=10, min_replay_pairs=20)
    # both short -> bootstrap
    assert v.is_bootstrap(3, 5, p) is True
    # benchmark points ok but replay pairs short -> still bootstrap
    assert v.is_bootstrap(10, 5, p) is True
    # replay pairs ok but benchmark points short -> still bootstrap
    assert v.is_bootstrap(3, 20, p) is True
    # both clear -> out of bootstrap
    assert v.is_bootstrap(10, 20, p) is False


def test_bootstrap_threshold_math():
    """The bootstrap revert threshold is max(2σ, 5pp) (R16)."""
    p = v.ValidateParams(bootstrap_sigma_multiplier=2.0, must_tier_pp_floor=0.05)
    # small σ -> the 5pp must-tier floor dominates
    assert v.bootstrap_revert_threshold(0.01, p) == pytest.approx(0.05)
    # large σ -> 2σ dominates
    assert v.bootstrap_revert_threshold(0.05, p) == pytest.approx(0.10)
    # at the crossover both arms agree
    assert v.bootstrap_revert_threshold(0.025, p) == pytest.approx(0.05)

    # And the decision honours the threshold: a drop just under does NOT revert,
    # just over DOES (σ=0.05 -> threshold 0.10, baseline 0.90).
    just_under = _bench(0.801, sigma=0.05, history=(0.90, 0.90, 0.90))  # drop 0.099
    just_over = _bench(0.799, sigma=0.05, history=(0.90, 0.90, 0.90))  # drop 0.101
    assert v.decide_validation(just_under, bootstrap=True, params=p).promotes is True
    assert v.decide_validation(just_over, bootstrap=True, params=p).promotes is False


# --- pure decision: SPC (post-bootstrap) ----------------------------------------


def test_spc_flags_only_special_cause():
    """≥10-point individuals chart reverts ONLY a special-cause-low point (R16)."""
    p = v.ValidateParams(spc_sigma_multiple=3.0)
    history = POST_BOOTSTRAP_HISTORY  # mean 0.90
    limits = v.spc_limits(history, 0.02, p)
    assert limits.center == pytest.approx(0.90)
    assert limits.lcl == pytest.approx(0.84)
    assert limits.ucl == pytest.approx(0.96)

    # within the limits: common-cause noise -> promote (no over-reaction)
    common = v.BenchmarkOutcome(candidate=0.88, sigma=0.02, history=history)
    d_common = v.decide_validation(common, bootstrap=False, params=p)
    assert d_common.promotes is True
    assert d_common.regressed is False

    # below the LCL: special-cause low -> revert
    special = v.BenchmarkOutcome(candidate=0.80, sigma=0.02, history=history)
    d_special = v.decide_validation(special, bootstrap=False, params=p)
    assert d_special.promotes is False
    assert d_special.regressed is True


# --- benchmark wins all conflicts -----------------------------------------------


def test_replay_pass_benchmark_regress_reverts():
    """replay-pass + benchmark-regress = revert (benchmark wins) (R15b)."""
    p = v.ValidateParams()
    replay = v.ReplayResult(ran=True, passed=True, scen_ids=("SCEN-1",))
    bench = _bench(0.50, sigma=0.02, history=(0.90, 0.90, 0.90))  # big drop
    d = v.decide_validation(bench, bootstrap=True, params=p, replay=replay)
    assert d.verdict == v.VERDICT_REVERT
    assert d.replay_miss is False
    assert "benchmark wins" in d.reason


def test_replay_fail_benchmark_pass_promotes_with_replay_miss():
    """replay-fail + benchmark-pass = promote with replay_miss flag (R15b)."""
    p = v.ValidateParams()
    replay = v.ReplayResult(ran=True, passed=False, scen_ids=("SCEN-1",))
    bench = _bench(0.90, sigma=0.02, history=(0.90, 0.91, 0.89))  # no regress
    d = v.decide_validation(bench, bootstrap=True, params=p, replay=replay)
    assert d.verdict == v.VERDICT_PROMOTE
    assert d.replay_miss is True
    assert "replay_miss" in d.reason


def test_skipped_replay_sets_no_replay_miss():
    """A skipped (quota-tight) replay never sets replay_miss (R15a diagnostic)."""
    p = v.ValidateParams()
    bench = _bench(0.90, sigma=0.02)
    d_skipped = v.decide_validation(
        bench, bootstrap=True, params=p, replay=v.ReplayResult(ran=False)
    )
    d_none = v.decide_validation(bench, bootstrap=True, params=p, replay=None)
    assert d_skipped.replay_miss is False
    assert d_none.replay_miss is False


# --- the full decision table (verification: 1:1 over replay × benchmark × bootstrap)


def test_decision_table():
    """replay × benchmark × bootstrap has a 1:1 covered cell (verification)."""
    p = v.ValidateParams()
    pass_bench_boot = _bench(0.90, sigma=0.02, history=(0.90, 0.90, 0.90))
    regress_bench_boot = _bench(0.50, sigma=0.02, history=(0.90, 0.90, 0.90))
    pass_bench_spc = v.BenchmarkOutcome(0.88, 0.02, POST_BOOTSTRAP_HISTORY)
    regress_bench_spc = v.BenchmarkOutcome(0.80, 0.02, POST_BOOTSTRAP_HISTORY)

    replay_pass = v.ReplayResult(ran=True, passed=True)
    replay_fail = v.ReplayResult(ran=True, passed=False)

    table = {
        # (bootstrap, benchmark-passes, replay) -> (verdict, replay_miss)
        ("boot", "pass", "rpass"): (pass_bench_boot, True, replay_pass, v.VERDICT_PROMOTE, False),
        ("boot", "pass", "rfail"): (pass_bench_boot, True, replay_fail, v.VERDICT_PROMOTE, True),
        ("boot", "regress", "rpass"): (regress_bench_boot, True, replay_pass, v.VERDICT_REVERT, False),
        ("boot", "regress", "rfail"): (regress_bench_boot, True, replay_fail, v.VERDICT_REVERT, False),
        ("spc", "pass", "rpass"): (pass_bench_spc, False, replay_pass, v.VERDICT_PROMOTE, False),
        ("spc", "pass", "rfail"): (pass_bench_spc, False, replay_fail, v.VERDICT_PROMOTE, True),
        ("spc", "regress", "rpass"): (regress_bench_spc, False, replay_pass, v.VERDICT_REVERT, False),
        ("spc", "regress", "rfail"): (regress_bench_spc, False, replay_fail, v.VERDICT_REVERT, False),
    }
    for key, (bench, bootstrap, replay, want_verdict, want_miss) in table.items():
        d = v.decide_validation(bench, bootstrap=bootstrap, params=p, replay=replay)
        assert d.verdict == want_verdict, key
        assert d.replay_miss == want_miss, key


# --- lifecycle: default deny + promote/revert through the queue (R17) ------------


def test_unvalidated_batch_stays_quarantined(store):
    """A registered batch that never cleared the benchmark gate stays quarantined
    and is absent from the active set (default deny, R17)."""
    _, ids = _register_quarantined_batch(store, "reflect-ep1")
    # Registration alone does not promote — nothing active.
    assert v.active_batch_insight_ids(store, "reflect-ep1") == ()
    assert all(st == "quarantined" for st in _statuses(store, ids).values())


def test_promotion_requires_benchmark_pass(store):
    """Promotion to active happens ONLY on benchmark non-regression (R15b/R17)."""
    _, ids = _register_quarantined_batch(store, "reflect-ep2")
    snap = store.current_snapshot_id()
    bench = v.BenchmarkOutcome(0.88, 0.02, POST_BOOTSTRAP_HISTORY)  # post-bootstrap pass
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep2",
        snapshot_id=snap,
        benchmark=bench,
        n_replay_pairs=20,
    )
    assert outcome.promoted is True
    assert outcome.decision.verdict == v.VERDICT_PROMOTE
    assert set(outcome.active_insight_ids) == set(ids)
    assert all(st == "active" for st in _statuses(store, ids).values())
    rec = store.get_batch_validation(outcome.validation_id)
    assert rec["verdict"] == "promote"
    assert rec["snapshot_id"] == snap


def test_benchmark_regression_auto_reverts(store):
    """A benchmark regression auto-reverts with no human action (default deny)."""
    _, ids = _register_quarantined_batch(store, "reflect-ep3")
    snap = store.current_snapshot_id()
    bench = v.BenchmarkOutcome(0.80, 0.02, POST_BOOTSTRAP_HISTORY)  # special-cause low
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep3",
        snapshot_id=snap,
        benchmark=bench,
        n_replay_pairs=20,
        # no cosign_fn: keeping a batch out needs no human action
    )
    assert outcome.promoted is False
    assert outcome.decision.verdict == v.VERDICT_REVERT
    assert v.active_batch_insight_ids(store, "reflect-ep3") == ()
    assert all(st == "retired" for st in _statuses(store, ids).values())


def test_reverted_batch_leaves_no_active_insights(store):
    """A reverted batch leaves no active insights and the record explains why."""
    _register_quarantined_batch(store, "reflect-ep4")
    snap = store.current_snapshot_id()
    replay = v.ReplayResult(ran=True, passed=True)  # replay passed, benchmark wins
    bench = _bench(0.50, sigma=0.02, history=(0.90, 0.90, 0.90))
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep4",
        snapshot_id=snap,
        benchmark=bench,
        n_replay_pairs=20,
        replay=replay,
        cosign_fn=lambda d: "reviewer",  # advisory on a revert, recorded only
    )
    assert outcome.promoted is False
    assert v.active_batch_insight_ids(store, "reflect-ep4") == ()
    rec = store.get_batch_validation(outcome.validation_id)
    assert rec["verdict"] == "revert"
    # the record carries a human-readable reason explaining the revert
    assert "benchmark wins" in rec["detail"] or "drop" in rec["detail"]
    assert rec["cosigned_by"] == "reviewer"


# --- bootstrap co-sign discipline (R16) -----------------------------------------


def test_cosign_required_during_bootstrap_not_after(store):
    """A bootstrap promote requires a co-sign; a post-bootstrap promote does not."""
    snap = store.current_snapshot_id()

    # Bootstrap promote with NO cosign -> refused, batch stays quarantined.
    _, ids1 = _register_quarantined_batch(store, "reflect-ep5")
    bench_boot = _bench(0.90, sigma=0.02, history=(0.90, 0.90, 0.90))  # passes, few points
    with pytest.raises(v.CosignRequiredError):
        v.validate_batch(
            store,
            batch_label="reflect-ep5",
            snapshot_id=snap,
            benchmark=bench_boot,
            n_replay_pairs=3,  # bootstrap regime
        )
    assert v.active_batch_insight_ids(store, "reflect-ep5") == ()
    assert all(st == "quarantined" for st in _statuses(store, ids1).values())

    # Bootstrap promote WITH cosign -> promotes, signer recorded.
    _, ids2 = _register_quarantined_batch(store, "reflect-ep6")
    outcome = v.validate_batch(
        store,
        batch_label="reflect-ep6",
        snapshot_id=store.current_snapshot_id(),
        benchmark=bench_boot,
        n_replay_pairs=3,
        cosign_fn=lambda d: "alice",
    )
    assert outcome.promoted is True
    assert outcome.cosigned_by == "alice"
    rec = store.get_batch_validation(outcome.validation_id)
    assert rec["bootstrap"] == 1
    assert rec["cosigned_by"] == "alice"

    # Post-bootstrap promote needs NO cosign.
    _, ids3 = _register_quarantined_batch(store, "reflect-ep7")
    outcome2 = v.validate_batch(
        store,
        batch_label="reflect-ep7",
        snapshot_id=store.current_snapshot_id(),
        benchmark=v.BenchmarkOutcome(0.88, 0.02, POST_BOOTSTRAP_HISTORY),
        n_replay_pairs=20,
    )
    assert outcome2.promoted is True
    assert outcome2.cosigned_by is None
    assert store.get_batch_validation(outcome2.validation_id)["bootstrap"] == 0


def test_bootstrap_promote_with_refused_cosign_stays_quarantined(store):
    """A co-sign callback that returns None during a bootstrap promote refuses."""
    _, ids = _register_quarantined_batch(store, "reflect-ep8")
    bench_boot = _bench(0.90, sigma=0.02, history=(0.90, 0.90, 0.90))
    with pytest.raises(v.CosignRequiredError):
        v.validate_batch(
            store,
            batch_label="reflect-ep8",
            snapshot_id=store.current_snapshot_id(),
            benchmark=bench_boot,
            n_replay_pairs=3,
            cosign_fn=lambda d: None,  # human declined
        )
    assert all(st == "quarantined" for st in _statuses(store, ids).values())


# --- frozen-replay re-judging harness (R18) -------------------------------------


def _frozen(pairs):
    return {"version": 1, "pairs": list(pairs)}


def _pair(scen_id, verdict):
    return {
        "scen_id": scen_id,
        "verdict": verdict,
        "judge_input": {"diff": f"a11y for {scen_id}"},
        "feat_id": "FEAT-1",
        "episode_id": 1,
        "snapshot_id": 1,
        "tier": "must",
        "judge_mode": "single",
        "verified_by": "human",
    }


def test_rejudge_due_honours_cadence():
    p = v.ValidateParams(rejudge_cadence=5)
    assert v.rejudge_due(4, p) is False
    assert v.rejudge_due(5, p) is True
    assert v.rejudge_due(6, p) is True


def test_rejudge_no_drift_is_clean():
    """Stable verdicts -> no drift -> not instrument_suspect (R18)."""
    p = v.ValidateParams(rejudge_drift_tolerance=0.1)
    frozen = _frozen([_pair("SCEN-1", "pass"), _pair("SCEN-2", "fail")])
    # re-judge reproduces the stored verdicts exactly
    stored = {pp["scen_id"]: pp["verdict"] for pp in frozen["pairs"]}

    def rejudge(judge_input):
        sid = judge_input["diff"].split()[-1]
        return stored[sid]

    res = v.rejudge_frozen_set(frozen, rejudge, p)
    assert res.flips == 0
    assert res.drift == pytest.approx(0.0)
    assert res.instrument_suspect is False


def test_instrument_suspect_propagates_to_scores_since_last_clean():
    """Drift past tolerance raises instrument_suspect and propagates to every
    benchmark score since the last clean replay; SPC excludes them (R18)."""
    p = v.ValidateParams(rejudge_drift_tolerance=0.1)
    # 5 pairs, 2 flip -> drift 0.4 > tolerance 0.1
    pairs = [_pair(f"SCEN-{i}", "pass") for i in range(5)]
    frozen = _frozen(pairs)
    flip = {"SCEN-1", "SCEN-3"}

    def rejudge(judge_input):
        sid = judge_input["diff"].split()[-1]
        return "fail" if sid in flip else "pass"

    res = v.rejudge_frozen_set(frozen, rejudge, p)
    assert res.flips == 2
    assert res.drift == pytest.approx(0.4)
    assert res.instrument_suspect is True
    assert res.flipped_scen_ids == ("SCEN-1", "SCEN-3")

    # Propagate to scores recorded since the last clean replay (snapshot 10).
    points = [
        v.BenchmarkPoint(snapshot_id=10, score=0.90),  # at the clean replay
        v.BenchmarkPoint(snapshot_id=12, score=0.70),  # after -> suspect
        v.BenchmarkPoint(snapshot_id=14, score=0.71),  # after -> suspect
    ]
    flagged = v.flag_scores_since_last_clean(
        points, last_clean_snapshot_id=10, suspect=res.instrument_suspect
    )
    assert flagged[0].instrument_suspect is False
    assert flagged[1].instrument_suspect is True
    assert flagged[2].instrument_suspect is True

    # SPC eligibility excludes the flagged scores.
    assert v.clean_scores(flagged) == (0.90,)


def test_clean_scores_keeps_all_when_no_suspect():
    points = [
        v.BenchmarkPoint(snapshot_id=1, score=0.9),
        v.BenchmarkPoint(snapshot_id=2, score=0.8),
    ]
    assert v.clean_scores(points) == (0.9, 0.8)


# --- guards ---------------------------------------------------------------------


def test_benchmark_outcome_rejects_empty_history():
    with pytest.raises(v.ValidateError):
        v.BenchmarkOutcome(candidate=0.9, sigma=0.02, history=())


def test_validate_params_range_checks():
    with pytest.raises(v.ValidateError):
        v.ValidateParams(rejudge_drift_tolerance=1.5)
    with pytest.raises(v.ValidateError):
        v.ValidateParams(min_benchmark_points=0)
    with pytest.raises(v.ValidateError):
        v.ValidateParams(spc_sigma_multiple=0)


def test_unknown_batch_raises(store):
    with pytest.raises(v.ValidateError):
        v.validate_batch(
            store,
            batch_label="nope",
            snapshot_id=0,
            benchmark=_bench(0.9),
            n_replay_pairs=20,
        )
