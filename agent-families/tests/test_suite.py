"""plan-005 U1: the held-out benchmark suite and its control charts (R1, R2).

Two layers (the benchmark/target_env precedent):

- **Offline** (the default suite): the suite runner driven by scripted per-target
  benchmark scores AND by the real ``run_benchmark_episode`` over each target's
  frozen slice with ``ConstDriver``/``ResolveFn`` fakes (zero quota, no
  ``claude`` on PATH, no Docker); the held-out constraint; the excluded-channel
  discipline; control-chart recompute-only-on-suite-runs; the generalization
  curves; LinkAce's frozen-slice immutability; and the static compose contract.
- **Docker-required** (live boot/seed/hand-verification of LinkAce's slice + the
  digest re-pin) is the documented pending-docker deliverable, mirroring
  Kanboard's R22 — not gated here because the offline suite stands alone.

## Conformance

Test-scenario / invariant (plan-005 U1) -> tests:

- suite run produces per-target + aggregate scores keyed (target, epoch,
  snapshot, mode=benchmark):
  ``test_suite_run_produces_per_target_and_aggregate_scores``,
  ``test_suite_run_real_benchmark_wiring`` (real slices end-to-end)
- a training-pool target is rejected as a suite target (held-out constraint):
  ``test_build_suite_rejects_a_training_pool_target``
- revisit curve query returns the generalization series:
  ``test_revisit_curve_returns_generalization_series``,
  ``test_two_fixture_epochs_produce_a_coherent_curve`` (Verification)
- control-chart limits recompute only on suite runs:
  ``test_control_charts_recompute_only_on_suite_runs``
- suite writes no ideas/fitness/settlement (excluded channel):
  ``test_suite_writes_no_ideas_or_fitness``
- generalized frozen-slice mechanism (R1):
  ``test_linkace_slice_hash_is_pinned``, ``test_linkace_slice_tamper_breaks_guard``,
  ``test_linkace_slice_size_and_tier_invariant``,
  ``test_frozen_slice_generalizes_run_benchmark_episode``
- static LinkAce target contract (digest pin, named volume, disjoint port):
  ``test_linkace_compose_pins_image_by_digest``,
  ``test_linkace_compose_uses_named_volume``,
  ``test_linkace_port_disjoint_from_other_targets``
- one-per-archetype / duplicate guards:
  ``test_build_suite_rejects_duplicate_archetype``,
  ``test_build_suite_rejects_empty``
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_families.grading.benchmark import (
    BENCHMARK_MODE,
    KANBOARD_PORT,
    BenchmarkScore,
    ExcludedFitnessChannel,
    run_benchmark_episode,
)
from agent_families.grading.scenarios import (
    Observation,
    Resolution,
    ResolutionCache,
)
from agent_families.grading.settle import SettleConfig
from agent_families.grading.suite import (
    AGGREGATE_KEY,
    HELD_OUT_SUITE,
    LINKACE_DIR,
    LINKACE_SLICE,
    LINKACE_SLICE_HASH,
    LINKACE_SLICE_SCENARIOS,
    SuiteError,
    SuiteParams,
    SuiteRun,
    SuiteTarget,
    aggregate_curve,
    build_suite,
    get_chart,
    make_benchmark_run_target,
    revisit_curve,
    run_suite,
    suite_aggregate_key,
    suite_chart_key,
    suite_score_key,
)
from agent_families.grading.target_env import PORT_TABLE
from agent_families.reflector.validate import SpcLimits
from agent_families.store import Store

COMPOSE_FILE = LINKACE_DIR / "docker-compose.yml"
LINKACE_IMAGE_DIGEST = (
    "sha256:b1c2d3e4f5a60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
)
LINKACE_PORT = 8082


# --- helpers -----------------------------------------------------------------


def _mk_score(target: str, epoch: int, snapshot_id: int, overall: float) -> BenchmarkScore:
    """A scripted benchmark score (the inner runner is exercised elsewhere)."""
    total = 12
    passed = round(overall * total)
    return BenchmarkScore(
        target=target,
        epoch=epoch,
        snapshot_id=snapshot_id,
        mode=BENCHMARK_MODE,
        overall=overall,
        passed=passed,
        scoreable=total,
        by_tier={"must": {"passed": passed, "total": total}},
        slice_hash="scripted",
    )


def scripted_run_target(scores: dict):
    """A RunTargetFn returning scripted overalls keyed [epoch][target_name]."""

    def _run(target, snapshot_id, epoch):
        return _mk_score(target.name, epoch, snapshot_id, scores[epoch][target.name])

    return _run


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "lib.db")
    store.migrate()
    return store


# --- held-out constraint (R1/R2) ---------------------------------------------


def test_build_suite_rejects_a_training_pool_target():
    # A held-out suite target that is also in the training pool measures
    # memorization, not generalization — the registry refuses it (R1/R2).
    with pytest.raises(SuiteError, match="in the training pool"):
        build_suite(HELD_OUT_SUITE, training_pool_names={"kanboard"})


def test_build_suite_accepts_a_disjoint_pool():
    suite = build_suite(HELD_OUT_SUITE, training_pool_names={"linkding", "shaarli"})
    assert {t.name for t in suite} == {"kanboard", "linkace"}


def test_build_suite_rejects_duplicate_archetype():
    # linkace re-typed to kanban collides with kanboard's archetype (R1: one
    # instance per archetype). Each SuiteTarget still owns its same-named slice.
    other = SuiteTarget("linkace", "kanban", LINKACE_DIR, LINKACE_SLICE)
    with pytest.raises(SuiteError, match="one instance per archetype"):
        build_suite([HELD_OUT_SUITE[0], other])  # kanboard(kanban)+linkace(kanban)


def test_build_suite_rejects_empty():
    with pytest.raises(SuiteError, match="at least one target"):
        build_suite([])


def test_suite_target_slice_name_must_match():
    with pytest.raises(SuiteError, match="slice of the same name"):
        SuiteTarget("kanboard", "kanban", LINKACE_DIR, LINKACE_SLICE)


# --- the suite runner: keyed scores (R1) -------------------------------------


def test_suite_run_produces_per_target_and_aggregate_scores(tmp_path):
    store = _store(tmp_path)
    scores = {2: {"kanboard": 0.75, "linkace": 0.5}}
    run = run_suite(
        suite=HELD_OUT_SUITE,
        snapshot_id=7,
        epoch=2,
        run_target=scripted_run_target(scores),
        store=store,
        sigma_by_target={"kanboard": 0.05, "linkace": 0.05},
    )
    assert isinstance(run, SuiteRun)
    # per-target scores present, in canonical name order
    assert [p.target for p in run.per_target] == ["kanboard", "linkace"]
    assert run.aggregate == pytest.approx((0.75 + 0.5) / 2)

    # each score persisted, keyed (target, epoch, snapshot, mode=benchmark)
    for name, overall in (("kanboard", 0.75), ("linkace", 0.5)):
        raw = store.get_meta(suite_score_key(name, 2))
        assert raw is not None
        parsed = json.loads(raw)
        assert parsed["target"] == name
        assert parsed["epoch"] == 2
        assert parsed["snapshot_id"] == 7
        assert parsed["mode"] == BENCHMARK_MODE
        assert parsed["overall"] == pytest.approx(overall)

    agg = json.loads(store.get_meta(suite_aggregate_key(2)))
    assert agg["target"] == AGGREGATE_KEY
    assert agg["mode"] == BENCHMARK_MODE
    assert agg["snapshot_id"] == 7
    assert agg["overall"] == pytest.approx(run.aggregate)
    assert agg["n_targets"] == 2
    store.close()


def test_suite_run_rejects_a_mis_keyed_inner_score():
    bad = lambda target, snapshot_id, epoch: _mk_score("WRONG", epoch, snapshot_id, 1.0)
    with pytest.raises(SuiteError, match="keyed to"):
        run_suite(
            suite=HELD_OUT_SUITE[:1],
            snapshot_id=1,
            epoch=0,
            run_target=bad,
        )


# --- the suite runner: real benchmark wiring over both slices (R1) -----------

_KB_URL = "http://app/project/1/task/2/board"
_LA_URL = "http://app/links/2"


def _node(role, name="", *children):
    node = {"role": role, "name": name}
    if children:
        node["children"] = list(children)
    return node


def _kb_tree():
    return _node(
        "document", "Kanboard",
        _node("link", "Engineering"),
        _node("link", "Board"),
        _node("columnheader", "Backlog"),
        _node("link", "Write the runbook"),
        _node("link", "Reset-to-seed runbook"),
        _node("link", "Reset-to-seed runbook v2"),
        _node("link", "Verify column order"),
        _node("text", "Looks good to me"),
        _node("button", "Open this task"),
    )


def _la_tree():
    return _node(
        "document", "LinkAce",
        _node("link", "SQLite WAL mode explained"),
        _node("link", "Django ORM query patterns"),
        _node("link", "Playwright trace viewer v2"),
        _node("link", "The accessibility tree"),
        _node("link", "Reading queue"),
        _node("link", "web-security"),
        _node("button", "Restore link"),
        _node("button", "Unarchive link"),
        _node("text", "Re-read before the grader refactor"),
    )


class _ConstDriver:
    def __init__(self, tree, url):
        self._tree = tree
        self._url = url

    def observe(self) -> Observation:
        return Observation(a11y_tree=self._tree, url=self._url)

    def execute(self, action: dict) -> None:
        pass

    def screenshot(self) -> bytes:
        return b"png"


def _const_resolve(step_text, a11y_tree) -> Resolution:
    return Resolution(
        "resolved", {"action": "click", "selector": "x", "args": []}, "scripted"
    )


def _settle_config() -> SettleConfig:
    return SettleConfig(model="sonnet", max_retries=1, panel_size=2)


def test_suite_run_real_benchmark_wiring(tmp_path):
    # The default per-target runner executes each target's OWN frozen slice
    # through the real benchmark episode — proving the generalized FrozenSlice
    # mechanism (R1), not just the scripted shortcut.
    store = _store(tmp_path)
    kb = _ConstDriver(_kb_tree(), _KB_URL)
    la = _ConstDriver(_la_tree(), _LA_URL)
    run_target = make_benchmark_run_target(
        drivers_by_target={
            "kanboard": {"target": kb, "clone": kb},
            "linkace": {"target": la, "clone": la},
        },
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
    )
    run = run_suite(
        suite=HELD_OUT_SUITE,
        snapshot_id=3,
        epoch=0,
        run_target=run_target,
        store=store,
        sigma_by_target={"kanboard": 0.04, "linkace": 0.04},
    )
    # both slices fully pass on their satisfying fixtures
    assert all(p.score.overall == 1.0 for p in run.per_target)
    assert run.aggregate == 1.0
    # each inner score carries its target's real slice hash, not the other's
    by_name = {p.target: p.score for p in run.per_target}
    assert by_name["linkace"].slice_hash == LINKACE_SLICE_HASH
    store.close()


# --- excluded channel (R1/R19) -----------------------------------------------


def test_suite_writes_no_ideas_or_fitness(tmp_path):
    store = _store(tmp_path)
    for i in range(2):
        store.insert_insight(
            precondition=f"pre{i}", action=f"act{i}", expected_outcome=f"out{i}",
            content_hash=f"hash{i}", status="active",
        )
    before = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w, COALESCE(SUM(losses),0) l,"
        " COALESCE(SUM(retrievals),0) r FROM insights"
    ).fetchone()

    channel = ExcludedFitnessChannel()
    kb = _ConstDriver(_kb_tree(), _KB_URL)
    la = _ConstDriver(_la_tree(), _LA_URL)
    run_target = make_benchmark_run_target(
        drivers_by_target={
            "kanboard": {"target": kb, "clone": kb},
            "linkace": {"target": la, "clone": la},
        },
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
        fitness_channel=channel,
    )
    run_suite(
        suite=HELD_OUT_SUITE, snapshot_id=1, epoch=0,
        run_target=run_target, store=store,
        sigma_by_target={"kanboard": 0.04, "linkace": 0.04},
    )

    after = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w, COALESCE(SUM(losses),0) l,"
        " COALESCE(SUM(retrievals),0) r FROM insights"
    ).fetchone()
    assert tuple(after) == tuple(before), "no insight rows/fitness mutated"
    assert store.conn.execute(
        "SELECT COUNT(*) FROM settlement_reports"
    ).fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM trace_scen").fetchone()[0] == 0
    # benchmark fitness routed to the excluded channel only
    assert channel.events, "the excluded channel collected benchmark fitness"
    assert all(e.kind == BENCHMARK_MODE for e in channel.events)
    store.close()


# --- control charts recompute only on suite runs (R1) ------------------------


def test_control_charts_recompute_only_on_suite_runs(tmp_path):
    store = _store(tmp_path)
    params = SuiteParams(min_chart_points=2)
    sig = {"kanboard": 0.05, "linkace": 0.05}

    # epoch 0: one point per target -> too few to chart, nothing persisted
    run_suite(
        suite=HELD_OUT_SUITE, snapshot_id=1, epoch=0,
        run_target=scripted_run_target({0: {"kanboard": 0.6, "linkace": 0.6}}),
        store=store, params=params, sigma_by_target=sig,
    )
    assert get_chart(store, "kanboard") is None

    # epoch 1: two points -> chart computed and persisted
    run_suite(
        suite=HELD_OUT_SUITE, snapshot_id=2, epoch=1,
        run_target=scripted_run_target({1: {"kanboard": 0.8, "linkace": 0.8}}),
        store=store, params=params, sigma_by_target=sig,
    )
    chart_after_e1 = get_chart(store, "kanboard")
    assert chart_after_e1 is not None
    assert chart_after_e1.center == pytest.approx((0.6 + 0.8) / 2)

    # a STANDALONE benchmark episode (not a suite run) must not move the chart
    kb = _ConstDriver(_kb_tree(), _KB_URL)
    run_benchmark_episode(
        target="kanboard", snapshot_id=9, epoch=99,
        drivers={"target": kb, "clone": kb}, cache=ResolutionCache(),
        resolve=_const_resolve, settle_config=_settle_config(),
        store=store, persist=True,
    )
    assert get_chart(store, "kanboard") == chart_after_e1, (
        "control-chart limits recompute ONLY on suite runs (R1)"
    )

    # epoch 2: another suite run recomputes the chart (center shifts)
    run_suite(
        suite=HELD_OUT_SUITE, snapshot_id=3, epoch=2,
        run_target=scripted_run_target({2: {"kanboard": 1.0, "linkace": 1.0}}),
        store=store, params=params, sigma_by_target=sig,
    )
    chart_after_e2 = get_chart(store, "kanboard")
    assert chart_after_e2.center == pytest.approx((0.6 + 0.8 + 1.0) / 3)
    assert chart_after_e2 != chart_after_e1
    # the aggregate is charted too
    assert get_chart(store, AGGREGATE_KEY) is not None
    store.close()


# --- generalization curves (R2) ----------------------------------------------


def test_revisit_curve_returns_generalization_series(tmp_path):
    store = _store(tmp_path)
    series = {
        0: {"kanboard": 0.5, "linkace": 0.4},
        1: {"kanboard": 0.65, "linkace": 0.55},
        2: {"kanboard": 0.7, "linkace": 0.6},
    }
    # run epochs out of order to prove the curve is sorted by epoch, not write order
    for epoch in (1, 0, 2):
        run_suite(
            suite=HELD_OUT_SUITE, snapshot_id=epoch + 1, epoch=epoch,
            run_target=scripted_run_target({epoch: series[epoch]}),
            store=store, sigma_by_target={"kanboard": 0.05, "linkace": 0.05},
        )
    curve = revisit_curve(store, "kanboard")
    assert curve == ((0, 0.5), (1, 0.65), (2, 0.7))
    assert aggregate_curve(store) == (
        (0, pytest.approx(0.45)),
        (1, pytest.approx(0.6)),
        (2, pytest.approx(0.65)),
    )
    store.close()


def test_two_fixture_epochs_produce_a_coherent_curve(tmp_path):
    """Verification: two fixture epochs produce a coherent generalization curve."""
    store = _store(tmp_path)
    for epoch, overall in ((0, 0.5), (1, 0.7)):
        run_suite(
            suite=HELD_OUT_SUITE, snapshot_id=epoch + 1, epoch=epoch,
            run_target=scripted_run_target(
                {epoch: {"kanboard": overall, "linkace": overall - 0.1}}
            ),
            store=store, params=SuiteParams(min_chart_points=2),
            sigma_by_target={"kanboard": 0.05, "linkace": 0.05},
        )
    curve = revisit_curve(store, "kanboard")
    assert [e for e, _ in curve] == [0, 1]
    # coherent: monotone non-decreasing across the two epochs
    assert curve[1][1] > curve[0][1]
    # the chart exists after the 2nd suite run, centered on the two points
    chart = get_chart(store, "kanboard")
    assert chart is not None and chart.center == pytest.approx(0.6)
    store.close()


# --- generalized frozen-slice mechanism (R1) ---------------------------------


def test_linkace_slice_hash_is_pinned():
    from agent_families.grading.benchmark import slice_hash

    assert slice_hash(LINKACE_SLICE_SCENARIOS) == LINKACE_SLICE_HASH
    assert LINKACE_SLICE.verify() == LINKACE_SLICE_HASH


def test_linkace_slice_tamper_breaks_guard():
    tampered = list(LINKACE_SLICE_SCENARIOS)
    tampered[0] = {**tampered[0], "title": tampered[0]["title"] + " (edited)"}
    from agent_families.grading.benchmark import FrozenSlice

    bad = FrozenSlice("linkace", tuple(tampered), LINKACE_SLICE_HASH)
    with pytest.raises(Exception, match="hash mismatch"):
        bad.verify()


def test_linkace_slice_size_and_tier_invariant():
    assert 12 <= len(LINKACE_SLICE_SCENARIOS) <= 20
    assert all(s["tier"] == "must" for s in LINKACE_SLICE_SCENARIOS)
    ids = [s["scenario_id"] for s in LINKACE_SLICE_SCENARIOS]
    assert len(ids) == len(set(ids)), "scenario ids unique"
    assert all(s["feat_id"].startswith("FEAT-") for s in LINKACE_SLICE_SCENARIOS)
    manifests = LINKACE_SLICE.manifests()
    assert all(m.tier == "must" for m in manifests)


def test_frozen_slice_generalizes_run_benchmark_episode(tmp_path):
    # The default (no frozen_slice) still runs Kanboard; passing LINKACE_SLICE
    # runs LinkAce — one runner, N instruments (R1).
    la = _ConstDriver(_la_tree(), _LA_URL)
    score = run_benchmark_episode(
        target="linkace", snapshot_id=1,
        drivers={"target": la, "clone": la}, cache=ResolutionCache(),
        resolve=_const_resolve, settle_config=_settle_config(),
        frozen_slice=LINKACE_SLICE, persist=False,
    )
    assert score.overall == 1.0
    assert score.slice_hash == LINKACE_SLICE_HASH
    assert score.scoreable == len(LINKACE_SLICE_SCENARIOS)


# --- static LinkAce target contract (R1, inherited R9 digest discipline) -----


def test_linkace_compose_pins_image_by_digest():
    from agent_families.grading.target_env import compose_image_ref

    ref = compose_image_ref(COMPOSE_FILE)
    assert "@sha256:" in ref, "tag-only pins drift silently (R9)"
    assert ref.endswith("@" + LINKACE_IMAGE_DIGEST)


def test_linkace_compose_uses_named_volume():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    mounts = re.findall(r"^\s*-\s*['\"]?([^:'\"\s]+):(/[^\s'\"]+)", text, re.MULTILINE)
    assert mounts, "compose must mount a data dir"
    for src, _dst in mounts:
        assert "/" not in src and "\\" not in src and "." not in src, (
            f"data must live in a named volume, not a bind mount (R5); got {src!r}"
        )


def test_linkace_port_disjoint_from_other_targets():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert f'"{LINKACE_PORT}:80"' in text
    assert LINKACE_PORT not in PORT_TABLE.values(), "disjoint from linkding/clone"
    assert LINKACE_PORT != KANBOARD_PORT, "disjoint from kanboard"
    assert 1 <= LINKACE_PORT <= 65535
