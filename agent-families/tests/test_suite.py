"""plan-005 U1: the held-out benchmark suite, epochs, control charts (R1/R2).

Offline (zero quota, no ``claude`` on PATH): the suite runner is driven by
passing/failing ``Driver`` fakes built from each target's frozen slice — both
apps' final trees are constructed so settlement settles deterministically (no
judge call). The live boot/seed/reset of each held-out stack is pending-docker.

## Conformance

(The behavioral map lives in ``grading/suite.py``'s module docstring.)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from agent_families.grading.benchmark import (
    BENCHMARK_MODE,
    ExcludedFitnessChannel,
    benchmark_score_key,
)
from agent_families.grading.scenarios import (
    Observation,
    Resolution,
    ResolutionCache,
)
from agent_families.grading.settle import SettleConfig
from agent_families.grading.target_env import PORT_TABLE, TargetEnvError
from agent_families.grading import suite as S
from agent_families.grading.suite import (
    SUITE_PORTS,
    SUITE_REGISTRY,
    DigestMismatchError,
    SuiteEnvConfig,
    SuiteError,
    SuiteTargetEnv,
    aggregate_curve,
    assert_held_out,
    control_limits,
    recompute_control_limits,
    revisit_curve,
    run_suite,
    suite_run_count,
    suite_target,
)
from agent_families.store import Store

KANBOARD_PORT = 8081  # asserted disjoint; mirrors benchmark.KANBOARD_PORT


# --- driver fakes built from a slice's post-assertions ------------------------


def _assertions(slice_):
    nodes, urls = [], []
    for scn in slice_:
        for step in scn["steps"]:
            if isinstance(step, dict) and step.get("post_assertion"):
                pa = step["post_assertion"]
                if pa["kind"] == "node_present":
                    nodes.append((pa["role"], pa["name"]))
                elif pa["kind"] == "url_contains":
                    urls.append(pa["value"])
    return nodes, urls


def _tree(slice_, omit=frozenset()):
    nodes, _ = _assertions(slice_)
    children = [
        {"role": r, "name": n} for r, n in nodes if n not in omit
    ]
    return {"role": "document", "name": "app", "children": children}


def _url(slice_):
    _, urls = _assertions(slice_)
    return "http://app/" + "/".join(sorted(set(urls))) if urls else "http://app/"


class _Drv:
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


def _drivers_all_pass(t):
    tree, url = _tree(t.frozen_slice), _url(t.frozen_slice)
    return {"target": _Drv(tree, url), "clone": _Drv(tree, url)}


def _drivers_clone_fails_all(t):
    """Target passes every scenario; the clone's DOM is empty and its URL bare,
    so every scenario fails its own post-assertion on the clone — a deterministic
    ``fail`` (clone built wrong) per scenario, never a judge call (the clone
    never reaches a completed-but-different state). Overall = 0.0.

    A partial offline score is unreachable without the judge: a clone that
    *passes* some scenarios necessarily presents a tree that differs from the
    target on the others, which routes through the comparison judge. So the
    regression fixture is the clean all-fail, and the recovery fixture all-pass —
    a rising curve the instrument captures end to end."""
    return {
        "target": _Drv(_tree(t.frozen_slice), _url(t.frozen_slice)),
        "clone": _Drv({"role": "document", "name": "app", "children": []},
                      "http://app/"),
    }


def _store(tmp_path) -> Store:
    store = Store(tmp_path / "lib.db")
    store.migrate()
    return store


# --- frozen suite slices (R1 immutability / must-tier) ------------------------


def test_suite_frozen_slices_are_pinned_and_must_tier():
    assert len(SUITE_REGISTRY) >= 3, "3-5 archetype held-out targets (R1)"
    archetypes = {t.archetype for t in SUITE_REGISTRY}
    assert len(archetypes) == len(SUITE_REGISTRY), "one instance per archetype"
    for t in SUITE_REGISTRY:
        manifests = t.verify()  # asserts hash + must-tier/size invariant
        assert 12 <= len(manifests) <= 20
        assert all(m.tier == "must" for m in manifests)
        ids = [s["scenario_id"] for s in t.frozen_slice]
        assert len(ids) == len(set(ids)), "scenario ids unique within a slice"


def test_suite_slice_tamper_breaks_the_guard():
    from agent_families.grading.benchmark import load_named_slice

    t = suite_target("shaarli")
    tampered = list(t.frozen_slice)
    tampered[0] = {**tampered[0], "title": tampered[0]["title"] + " (edited)"}
    with pytest.raises(TargetEnvError, match="hash mismatch"):
        load_named_slice(tuple(tampered), t.frozen_slice_hash)


# --- generic harness static contract (digest pin, named volume, ports) --------


def test_suite_compose_pins_image_by_digest():
    from agent_families.grading.target_env import compose_image_ref

    for t in SUITE_REGISTRY:
        ref = compose_image_ref(t.compose_file)
        assert "@sha256:" in ref, f"{t.name} must be digest-pinned (R9)"


def test_suite_compose_uses_named_volume():
    for t in SUITE_REGISTRY:
        text = t.compose_file.read_text(encoding="utf-8")
        mounts = re.findall(
            r"^\s*-\s*['\"]?([^:'\"\s]+):(/[^\s'\"]+)", text, re.MULTILINE
        )
        assert mounts, f"{t.name} compose must declare a data mount"
        for src, _dst in mounts:
            assert "/" not in src and "\\" not in src and "." not in src, (
                f"{t.name} data must live in a named volume, not a bind mount (R5)"
            )


def test_suite_ports_disjoint():
    suite_ports = set(SUITE_PORTS.values())
    assert len(suite_ports) == len(SUITE_PORTS), "suite ports unique among themselves"
    assert suite_ports.isdisjoint(set(PORT_TABLE.values())), (
        "suite ports disjoint from linkding=9090, clone=4173"
    )
    assert KANBOARD_PORT not in suite_ports, "disjoint from kanboard=8081"
    # the registry port matches the compose-file host port
    for t in SUITE_REGISTRY:
        text = t.compose_file.read_text(encoding="utf-8")
        assert f'"{t.port}:' in text, f"{t.name} host port must be {t.port}"


# --- the held-out constraint (R2) ---------------------------------------------


def test_assert_held_out_accepts_suite_targets():
    for t in SUITE_REGISTRY:
        assert_held_out(t.name)  # no raise
    assert_held_out("kanboard")  # the micro-benchmark is held-out too


def test_training_pool_target_rejected_as_suite_target():
    # linkding is trained on -> not a held-out set member -> rejected
    with pytest.raises(SuiteError, match="held-out"):
        assert_held_out("linkding")
    # and a target explicitly in the active training pool is rejected even if it
    # were otherwise a suite name (the generalization constraint, both ways)
    with pytest.raises(SuiteError, match="training pool"):
        assert_held_out("shaarli", training_pool=["shaarli", "linkding"])


# --- the suite runner (R1) ----------------------------------------------------


def test_suite_run_produces_keyed_scores(tmp_path):
    store = _store(tmp_path)
    result = run_suite(
        SUITE_REGISTRY,
        epoch=0,
        snapshot_id=5,
        drivers_for=_drivers_all_pass,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
    )
    assert set(result.per_target) == {t.name for t in SUITE_REGISTRY}
    for t in SUITE_REGISTRY:
        score = result.per_target[t.name]
        # keyed (target, epoch, snapshot, mode=benchmark)
        assert score.target == t.name
        assert score.epoch == 0
        assert score.snapshot_id == 5
        assert score.mode == BENCHMARK_MODE
        assert score.slice_hash == t.frozen_slice_hash
        assert score.overall == 1.0  # all-pass drivers
    assert result.aggregate == 1.0
    assert result.generation == 1
    # per-target + aggregate control limits computed on this suite run
    assert set(result.limits) == {t.name for t in SUITE_REGISTRY} | {S.AGGREGATE_KEY}
    store.close()


def test_suite_scores_persist_to_excluded_channel(tmp_path):
    store = _store(tmp_path)
    # plant an insight so we can prove benchmark mode never touches fitness
    store.insert_insight(
        precondition="p", action="a", expected_outcome="o",
        content_hash="h", status="active",
    )
    before = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w FROM insights"
    ).fetchone()
    channel = ExcludedFitnessChannel()
    run_suite(
        [suite_target("shaarli")],
        epoch=1,
        snapshot_id=3,
        drivers_for=_drivers_all_pass,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
        fitness_channel=channel,
    )
    # the score lands in the excluded meta channel, keyed by target/snap/epoch
    raw = store.get_meta(benchmark_score_key("shaarli", 3, 1))
    assert raw is not None
    assert json.loads(raw)["mode"] == BENCHMARK_MODE
    # no settlement/SCEN rows, no fitness mutation (R19 excluded channel)
    after = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w FROM insights"
    ).fetchone()
    assert tuple(after) == tuple(before)
    assert store.conn.execute(
        "SELECT COUNT(*) FROM settlement_reports"
    ).fetchone()[0] == 0
    # benchmark fitness routed to the excluded channel
    assert len(channel.events) == len(suite_target("shaarli").frozen_slice)
    assert all(e.kind == BENCHMARK_MODE for e in channel.events)
    store.close()


def test_suite_run_rejects_trained_on_target(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(SuiteError):
        run_suite(
            [suite_target("shaarli")],
            epoch=0,
            snapshot_id=1,
            drivers_for=_drivers_all_pass,
            resolve=_const_resolve,
            settle_config=_settle_config(),
            store=store,
            training_pool=["shaarli"],  # someone wrongly trained on it
        )
    store.close()


# --- revisit / generalization curve (R2) --------------------------------------


def test_revisit_curve_is_the_generalization_series(tmp_path):
    store = _store(tmp_path)
    for epoch in (0, 1):
        run_suite(
            [suite_target("shaarli")],
            epoch=epoch,
            snapshot_id=10 + epoch,
            drivers_for=_drivers_all_pass,
            resolve=_const_resolve,
            settle_config=_settle_config(),
            store=store,
        )
    curve = revisit_curve(store, "shaarli")
    assert [epoch for epoch, _ in curve] == [0, 1]
    assert all(score == 1.0 for _, score in curve)
    store.close()


def test_aggregate_curve_across_epochs(tmp_path):
    store = _store(tmp_path)
    for epoch in (0, 1):
        run_suite(
            SUITE_REGISTRY,
            epoch=epoch,
            snapshot_id=epoch,
            drivers_for=_drivers_all_pass,
            resolve=_const_resolve,
            settle_config=_settle_config(),
            store=store,
        )
    curve = aggregate_curve(store)
    assert [epoch for epoch, _ in curve] == [0, 1]
    store.close()


# --- control charts recompute only on suite runs (R1) -------------------------


def test_control_limits_recompute_only_on_suite_runs(tmp_path):
    store = _store(tmp_path)
    # one suite run -> generation 1, limits stamped at gen 1
    run_suite(
        [suite_target("shaarli")],
        epoch=0,
        snapshot_id=1,
        drivers_for=_drivers_all_pass,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
    )
    assert suite_run_count(store) == 1
    lim1 = control_limits(store, "shaarli")
    assert lim1 is not None and lim1["generation"] == 1

    # an ORDINARY training episode does NOT touch the chart (R1)
    store.create_episode("linkding", "sha256:deadbeef", 1, mode="training", epoch=0)
    assert suite_run_count(store) == 1, "ordinary episodes never recompute limits"
    assert control_limits(store, "shaarli")["generation"] == 1

    # a second suite run -> generation 2, limits recomputed over the 2-point series
    run_suite(
        [suite_target("shaarli")],
        epoch=1,
        snapshot_id=2,
        drivers_for=_drivers_all_pass,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
    )
    assert suite_run_count(store) == 2
    lim2 = control_limits(store, "shaarli")
    assert lim2["generation"] == 2 and lim2["n"] == 2
    store.close()


def test_two_epochs_produce_a_coherent_generalization_curve(tmp_path):
    """Verification: two fixture epochs produce a coherent generalization curve.

    Epoch 0 the clone is unbuilt (every scenario fails); epoch 1 it passes them
    all — a rising revisit curve, with control limits recomputed at each suite run.
    """
    store = _store(tmp_path)
    run_suite(
        [suite_target("shaarli")],
        epoch=0,
        snapshot_id=1,
        drivers_for=_drivers_clone_fails_all,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
    )
    run_suite(
        [suite_target("shaarli")],
        epoch=1,
        snapshot_id=2,
        drivers_for=_drivers_all_pass,
        resolve=_const_resolve,
        settle_config=_settle_config(),
        store=store,
    )
    curve = revisit_curve(store, "shaarli")
    assert [e for e, _ in curve] == [0, 1]
    s0, s1 = curve[0][1], curve[1][1]
    assert s0 == 0.0, "epoch 0: clone unbuilt, every scenario fails"
    assert s1 == 1.0, "epoch 1 recovered them"
    assert s1 > s0, "the generalization curve rises coherently"
    # control limits reflect the measured replicate σ over the two points
    lim = control_limits(store, "shaarli")
    assert lim["generation"] == 2 and lim["n"] == 2
    assert lim["lcl"] <= lim["center"] <= lim["ucl"]
    store.close()


def test_recompute_control_limits_needs_history(tmp_path):
    store = _store(tmp_path)
    # no suite history yet -> nothing to recompute, empty result, no crash
    out = recompute_control_limits(store, ["shaarli"], generation=0)
    assert out == {}
    assert control_limits(store, "shaarli") is None
    store.close()


# --- generic suite env boot/health branches (offline seams) -------------------


class _FakeRunner:
    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        return self._handler(argv)


def _proc(argv, returncode=0, stdout="", stderr=""):
    import subprocess

    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class _FakeHttp:
    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def __call__(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url))
        return self._handler(method, url)


def _env(target, **kw):
    config = SuiteEnvConfig(
        target=target, readiness_timeout_s=5.0, poll_interval_s=0.01
    )
    return SuiteTargetEnv(config, **kw)


def test_suite_env_up_digest_then_boot_then_ready():
    t = suite_target("shaarli")
    from agent_families.grading.target_env import compose_image_ref

    ref = compose_image_ref(t.compose_file)
    digest = ref.split("@", 1)[1]

    def runner_handler(argv):
        if argv[:3] == ["docker", "image", "inspect"]:
            return _proc(argv, stdout=json.dumps([f"shaarli/shaarli@{digest}"]))
        return _proc(argv)

    def http_handler(method, url):
        return 200, "<html><title>Shaarli</title></html>"

    runner = _FakeRunner(runner_handler)
    env = _env(t, runner=runner, http=_FakeHttp(http_handler))
    env.up()  # no raise: pin ok, boot, digest verified, ready
    assert any(a[:3] == ["docker", "image", "inspect"] for a in runner.calls)


def test_suite_env_digest_mismatch_fails_before_boot(tmp_path):
    t = suite_target("privatebin")
    unpinned = tmp_path / "docker-compose.yml"
    text = t.compose_file.read_text(encoding="utf-8")
    pinned_digest = "@" + text.split("@", 1)[1].split("\n", 1)[0].strip().strip('"')
    unpinned.write_text(
        text.replace(pinned_digest, ""), encoding="utf-8", newline="\n"
    )
    import dataclasses

    bad_target = dataclasses.replace(t, compose_file=unpinned)
    runner = _FakeRunner(lambda argv: pytest.fail("docker must not be invoked"))
    env = _env(bad_target, runner=runner)
    with pytest.raises(DigestMismatchError, match="not digest-pinned"):
        env.up()
    assert runner.calls == [], "the pin check precedes any docker call"
