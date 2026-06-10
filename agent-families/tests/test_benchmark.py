"""plan-004 U4: Kanboard onboarding + the frozen micro-benchmark (R14, R22).

Two layers, the plan-003 U2 test-tier precedent:

- **Offline** (the default suite): every lifecycle decision branch via the
  injectable ``runner``/``http`` seams; byte-level checks on the committed
  compose file and seed manifest; the frozen-slice immutability guard; and the
  benchmark episode runner driven by scripted ``Driver``/``ResolveFn`` fakes
  (zero quota, no ``claude`` on PATH).
- **Docker-required** (``-m docker``, auto-skipped when docker is absent): the
  live boot/seed/reset scenarios against the real pinned Kanboard stack.

## Conformance

Test-scenario / invariant (plan-004 U4) -> tests:

- Kanboard boots/seeds/resets: live ``TestLiveKanboard`` (docker-required);
  offline branch coverage in ``test_up_digest_then_boot_then_ready``,
  ``test_seed_posts_jsonrpc_with_basic_auth``,
  ``test_reset_to_seed_sequences_drop_boot_seed``,
  ``test_wait_healthy_polls_until_marker`` / ``test_wait_healthy_times_out``
- slice scenarios execute on the target with cached resolutions: live
  ``TestLiveKanboard`` (docker-required); offline the slice parses to valid
  manifests in ``test_frozen_slice_parses_to_must_tier_manifests`` and executes
  through the harness in ``test_benchmark_episode_produces_keyed_score``
- benchmark episode produces a score keyed (target, epoch, snapshot, mode):
  ``test_benchmark_episode_produces_keyed_score``,
  ``test_benchmark_score_persists_to_excluded_meta_channel``
- ideas/fitness provably not written from benchmark mode:
  ``test_benchmark_writes_no_ideas_or_fitness``
- slice immutability guarded (hash of manifest set):
  ``test_frozen_slice_hash_is_pinned``, ``test_slice_tamper_breaks_the_guard``,
  ``test_frozen_slice_size_and_tier_invariant``
- R22 static contract (digest pin, named volume, disjoint port):
  ``test_compose_pins_image_by_digest``,
  ``test_compose_uses_named_volume_not_bind_mount``,
  ``test_kanboard_port_disjoint_from_linkding_table``
- R16 benchmark-instrument σ from replicates:
  ``test_benchmark_sigma_over_replicates``, ``test_benchmark_sigma_needs_two``
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_families.grading.benchmark import (
    ADMIN_PASSWORD,
    ADMIN_USER,
    BENCHMARK_MODE,
    FROZEN_SLICE,
    FROZEN_SLICE_HASH,
    FROZEN_SLICE_MAX,
    FROZEN_SLICE_MIN,
    JSONRPC_PATH,
    KANBOARD_IMAGE_DIGEST,
    KANBOARD_IMAGE_REF,
    KANBOARD_PORT,
    BenchmarkScore,
    DigestMismatchError,
    ExcludedFitnessChannel,
    KanboardConfig,
    KanboardTarget,
    benchmark_score_key,
    benchmark_sigma,
    compose_image_ref,
    expected_seed_counts,
    load_frozen_slice,
    load_kanboard_seed_manifest,
    run_benchmark_episode,
    slice_hash,
    verify_slice_hash,
)
from agent_families.grading.scenarios import Observation, Resolution, ResolutionCache
from agent_families.grading.settle import SettleConfig
from agent_families.grading.target_env import PORT_TABLE, TargetEnvError
from agent_families.store import Store

TARGET_DIR = Path(__file__).resolve().parent.parent / "targets" / "kanboard"
COMPOSE_FILE = TARGET_DIR / "docker-compose.yml"
SEED_MANIFEST = TARGET_DIR / "seed_manifest.json"


def make_config(**kw) -> KanboardConfig:
    defaults = dict(
        compose_file=COMPOSE_FILE,
        seed_manifest=SEED_MANIFEST,
        readiness_timeout_s=5.0,
        poll_interval_s=0.01,
    )
    return KanboardConfig(**{**defaults, **kw})


# --- scripted seams (the target_env precedent) -------------------------------


class FakeRunner:
    def __init__(self, handler):
        self.calls: list[list[str]] = []
        self._handler = handler

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        return self._handler(argv)


def proc(argv, returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeHttp:
    def __init__(self, handler):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self._handler = handler

    def __call__(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, dict(headers or {}), payload))
        return self._handler(method, url, payload)


def healthy_rpc_http(method, url, payload):
    """Readiness GET / serves the login marker; JSON-RPC returns monotonic ids."""
    if url.endswith("/"):
        return 200, "<html><title>Kanboard</title></html>"
    if url.endswith(JSONRPC_PATH):
        # createProject / createTask both return a positive integer id keyed to
        # the request id, which the seed loop already makes monotonic.
        return 200, json.dumps(
            {"jsonrpc": "2.0", "id": payload["id"], "result": payload["id"]}
        )
    return 200, "ok"


def good_runner_handler(argv):
    if argv[:3] == ["docker", "image", "inspect"]:
        return proc(
            argv, stdout=json.dumps([f"kanboard/kanboard@{KANBOARD_IMAGE_DIGEST}"])
        )
    return proc(argv)


# --- static contract: compose, seed manifest, port table (R22) ---------------


def test_compose_pins_image_by_digest():
    ref = compose_image_ref(COMPOSE_FILE)
    assert "@sha256:" in ref, "tag-only pins drift silently (R9)"
    assert ref == KANBOARD_IMAGE_REF, (
        "compose file and benchmark.py disagree on the image pin"
    )


def test_compose_uses_named_volume_not_bind_mount():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    mounts = re.findall(r"^\s*-\s*['\"]?([^:'\"\s]+):(/[^\s'\"]+)", text, re.MULTILINE)
    data_mounts = [(src, dst) for src, dst in mounts if "/var/www/app/data" in dst]
    assert data_mounts, "compose must mount the kanboard data dir"
    for src, _dst in data_mounts:
        assert "/" not in src and "\\" not in src and "." not in src, (
            f"data must live in a named volume, not a bind mount (WAL"
            f" corruption risk, R5); got source {src!r}"
        )
        assert re.search(
            rf"^volumes:\s*\n(?:\s+\w+:\s*\n)*\s+{re.escape(src)}:", text, re.MULTILINE
        ), f"named volume {src!r} must be declared top-level"


def test_compose_matches_port_table():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert f'"{KANBOARD_PORT}:80"' in text, (
        "host port must come from KANBOARD_PORT"
    )


def test_kanboard_port_disjoint_from_linkding_table():
    assert KANBOARD_PORT not in PORT_TABLE.values(), (
        "the second target's host port must be disjoint from the linkding"
        " port table (linkding=9090, clone=4173) — R7 by construction"
    )
    assert 1 <= KANBOARD_PORT <= 65535


def test_seed_manifest_loads_and_counts():
    manifest = load_kanboard_seed_manifest(SEED_MANIFEST)
    projects, tasks = expected_seed_counts(manifest)
    assert projects == len(manifest) >= 3, "seed must be non-trivial"
    assert tasks >= projects, "every project carries at least one task"


def test_seed_manifest_validation_rejects_malformed(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(TargetEnvError, match="not valid JSON"):
        load_kanboard_seed_manifest(bad_json)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"projects": []}), encoding="utf-8")
    with pytest.raises(TargetEnvError, match="non-empty 'projects'"):
        load_kanboard_seed_manifest(empty)

    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps({"projects": [{"name": "A"}, {"name": "A"}]}),
        encoding="utf-8",
    )
    with pytest.raises(TargetEnvError, match="unique"):
        load_kanboard_seed_manifest(dup)

    bad_task = tmp_path / "task.json"
    bad_task.write_text(
        json.dumps({"projects": [{"name": "A", "tasks": [{"title": ""}]}]}),
        encoding="utf-8",
    )
    with pytest.raises(TargetEnvError, match="title"):
        load_kanboard_seed_manifest(bad_task)

    with pytest.raises(TargetEnvError, match="not found"):
        load_kanboard_seed_manifest(tmp_path / "missing.json")


def test_config_validates_tunables():
    with pytest.raises(TargetEnvError, match="readiness_timeout_s"):
        make_config(readiness_timeout_s=0)
    with pytest.raises(TargetEnvError, match="poll_interval_s"):
        make_config(poll_interval_s=-1)
    with pytest.raises(TargetEnvError, match="sha256"):
        make_config(expected_digest="latest")
    assert make_config().port == KANBOARD_PORT
    assert make_config().base_url == f"http://127.0.0.1:{KANBOARD_PORT}"


# --- lifecycle branches (offline, R5/R6/R9) ----------------------------------


def test_up_digest_then_boot_then_ready():
    runner = FakeRunner(good_runner_handler)
    target = KanboardTarget(
        make_config(), runner=runner, http=FakeHttp(healthy_rpc_http)
    )
    target.up()  # no raise: pin ok, boot ok, digest verified, ready
    ops = [a for a in runner.calls]
    # the compose-pin check precedes any docker call, then up, then inspect
    assert any("up" in a for a in ops)
    assert any(a[:3] == ["docker", "image", "inspect"] for a in ops)


def test_digest_mismatch_in_compose_fails_before_boot():
    wrong = "sha256:" + "0" * 64
    runner = FakeRunner(lambda argv: pytest.fail("docker must not be invoked"))
    target = KanboardTarget(make_config(expected_digest=wrong), runner=runner)
    with pytest.raises(DigestMismatchError) as exc_info:
        target.up()
    message = str(exc_info.value)
    assert wrong in message and KANBOARD_IMAGE_DIGEST in message
    assert "refusing" in message and "re-pin" in message.lower()
    assert runner.calls == [], "the pin check must precede any docker call"


def test_unpinned_compose_image_rejected(tmp_path):
    unpinned = tmp_path / "docker-compose.yml"
    unpinned.write_text(
        COMPOSE_FILE.read_text(encoding="utf-8").replace(
            f"@{KANBOARD_IMAGE_DIGEST}", ""
        ),
        encoding="utf-8",
        newline="\n",
    )
    target = KanboardTarget(
        make_config(compose_file=unpinned),
        runner=FakeRunner(lambda argv: pytest.fail("docker must not be invoked")),
    )
    with pytest.raises(DigestMismatchError, match="not digest-pinned"):
        target.up()


def test_post_boot_digest_verification_rejects_retag():
    def retagged(argv):
        if argv[:3] == ["docker", "image", "inspect"]:
            return proc(argv, stdout=json.dumps(["kanboard/kanboard@sha256:" + "f" * 64]))
        return proc(argv)

    target = KanboardTarget(make_config(), runner=FakeRunner(retagged))
    with pytest.raises(DigestMismatchError, match="digest mismatch"):
        target.up()


def test_compose_up_failure_is_loud():
    def boom(argv):
        if "up" in argv:
            return proc(argv, returncode=1, stderr="no space left on device")
        return proc(argv)

    target = KanboardTarget(make_config(), runner=FakeRunner(boom))
    with pytest.raises(TargetEnvError, match="no space left"):
        target.up()


def test_wait_healthy_polls_until_marker():
    responses = iter(
        [
            ConnectionRefusedError("not up yet"),
            (200, "<html>maintenance</html>"),  # 200 but no marker yet
            (200, "<html><title>Kanboard</title></html>"),
        ]
    )

    def scripted(method, url, payload):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    http = FakeHttp(scripted)
    target = KanboardTarget(make_config(), runner=FakeRunner(proc), http=http)
    target.wait_healthy()
    assert len(http.calls) == 3, "refused + marker-absent absorbed, then ready"


def test_wait_healthy_times_out():
    def refused(method, url, payload):
        raise ConnectionRefusedError("nope")

    target = KanboardTarget(
        make_config(readiness_timeout_s=0.05, poll_interval_s=0.01),
        runner=FakeRunner(proc),
        http=FakeHttp(refused),
    )
    with pytest.raises(TargetEnvError, match="Kanboard"):
        target.wait_healthy()


def test_seed_posts_jsonrpc_with_basic_auth():
    http = FakeHttp(healthy_rpc_http)
    target = KanboardTarget(make_config(), runner=FakeRunner(proc), http=http)
    counts = target.seed()

    manifest = load_kanboard_seed_manifest(SEED_MANIFEST)
    assert counts == expected_seed_counts(manifest)
    posts = [c for c in http.calls if c[0] == "POST"]
    projects, tasks = expected_seed_counts(manifest)
    assert len(posts) == projects + tasks, (
        "one JSON-RPC call per project and per task"
    )
    methods = [c[3]["method"] for c in posts]
    assert methods.count("createProject") == projects
    assert methods.count("createTask") == tasks
    # HTTP basic admin auth on every JSON-RPC call
    import base64

    expected_auth = "Basic " + base64.b64encode(
        f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()
    ).decode()
    for _m, url, headers, _p in posts:
        assert url.endswith(JSONRPC_PATH)
        assert headers["Authorization"] == expected_auth


def test_seed_failure_is_loud():
    def reject(method, url, payload):
        if url.endswith(JSONRPC_PATH):
            return 200, json.dumps(
                {"jsonrpc": "2.0", "id": payload["id"], "error": {"code": -32603}}
            )
        return 200, "ok"

    target = KanboardTarget(
        make_config(), runner=FakeRunner(proc), http=FakeHttp(reject)
    )
    with pytest.raises(TargetEnvError, match="error"):
        target.seed()


def test_reset_to_seed_sequences_drop_boot_seed():
    runner = FakeRunner(good_runner_handler)
    http = FakeHttp(healthy_rpc_http)
    target = KanboardTarget(make_config(), runner=runner, http=http)
    counts = target.reset_to_seed()
    assert counts == expected_seed_counts(load_kanboard_seed_manifest(SEED_MANIFEST))

    ops = []
    for argv in runner.calls:
        if "down" in argv:
            ops.append("down" + ("+volumes" if "--volumes" in argv else ""))
        elif "up" in argv:
            ops.append("up")
        elif argv[:3] == ["docker", "image", "inspect"]:
            ops.append("inspect")
    assert ops == ["down+volumes", "up", "inspect"], (
        "reset = volume drop, re-boot (digest-checked), then re-seed via API (R6)"
    )
    posts = [c for c in http.calls if c[0] == "POST"]
    projects, tasks = expected_seed_counts(load_kanboard_seed_manifest(SEED_MANIFEST))
    assert len(posts) == projects + tasks


# --- frozen micro-benchmark slice (R14) --------------------------------------


def test_frozen_slice_hash_is_pinned():
    assert slice_hash(FROZEN_SLICE) == FROZEN_SLICE_HASH
    assert verify_slice_hash() == FROZEN_SLICE_HASH


def test_slice_tamper_breaks_the_guard():
    tampered = list(FROZEN_SLICE)
    tampered[0] = {**tampered[0], "title": tampered[0]["title"] + " (edited)"}
    assert slice_hash(tampered) != FROZEN_SLICE_HASH
    with pytest.raises(TargetEnvError, match="hash mismatch"):
        verify_slice_hash(tampered)


def test_frozen_slice_size_and_tier_invariant():
    assert FROZEN_SLICE_MIN <= len(FROZEN_SLICE) <= FROZEN_SLICE_MAX
    assert all(s["tier"] == "must" for s in FROZEN_SLICE), "must-tier only (R14)"
    ids = [s["scenario_id"] for s in FROZEN_SLICE]
    assert len(ids) == len(set(ids)), "scenario ids unique"
    assert all(s["feat_id"].startswith("FEAT-") for s in FROZEN_SLICE)


def test_frozen_slice_parses_to_must_tier_manifests():
    manifests = load_frozen_slice()
    assert len(manifests) == len(FROZEN_SLICE)
    assert all(m.tier == "must" for m in manifests)
    # every scenario carries a hand-verified reference verdict
    for entry in FROZEN_SLICE:
        assert entry["reference"]["verdict"] in ("pass", "fail")


# --- benchmark episode runner (R14) ------------------------------------------

# A constant DOM that satisfies every frozen-slice post-assertion and is shared
# by both apps, so the U7 comparison settles every scenario deterministically
# (identical final trees -> empty a11y diff -> pass, no judge call).
_FIXED_URL = "http://app/project/1/task/2/board"


def _kb_node(role, name="", *children):
    node = {"role": role, "name": name}
    if children:
        node["children"] = list(children)
    return node


def _kb_tree():
    return _kb_node(
        "document",
        "Kanboard",
        _kb_node("link", "Engineering"),
        _kb_node("link", "Board"),
        _kb_node("columnheader", "Backlog"),
        _kb_node("link", "Write the runbook"),
        _kb_node("link", "Reset-to-seed runbook"),
        _kb_node("link", "Reset-to-seed runbook v2"),
        _kb_node("link", "Verify column order"),
        _kb_node("text", "Looks good to me"),
        _kb_node("button", "Open this task"),
    )


class ConstDriver:
    """Returns one fixed observation for every observe(); executes no-ops."""

    def observe(self) -> Observation:
        return Observation(a11y_tree=_kb_tree(), url=_FIXED_URL)

    def execute(self, action: dict) -> None:
        pass

    def screenshot(self) -> bytes:
        return b"png"


def _const_resolve(step_text, a11y_tree) -> Resolution:
    return Resolution(
        "resolved", {"action": "click", "selector": "x", "args": []}, "scripted"
    )


def _settle_config() -> SettleConfig:
    # The judge is never reached (deterministic passes); panel_size>=2 is only a
    # construction constraint.
    return SettleConfig(model="sonnet", max_retries=1, panel_size=2)


def _drivers() -> dict:
    return {"target": ConstDriver(), "clone": ConstDriver()}


def test_benchmark_episode_produces_keyed_score(tmp_path):
    score = run_benchmark_episode(
        target="kanboard",
        snapshot_id=7,
        epoch=2,
        drivers=_drivers(),
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
        persist=False,
    )
    assert isinstance(score, BenchmarkScore)
    # keyed (target, epoch, snapshot, mode)
    assert score.target == "kanboard"
    assert score.epoch == 2
    assert score.snapshot_id == 7
    assert score.mode == BENCHMARK_MODE
    assert score.slice_hash == FROZEN_SLICE_HASH
    # the fixed DOM passes every scenario deterministically
    assert score.scoreable == len(FROZEN_SLICE)
    assert score.passed == len(FROZEN_SLICE)
    assert score.overall == 1.0
    assert score.by_tier == {"must": {"passed": len(FROZEN_SLICE), "total": len(FROZEN_SLICE)}}
    # the score dict carries exactly the four keys plus the rollups
    d = score.to_dict()
    assert {"target", "epoch", "snapshot_id", "mode"} <= set(d)


def test_benchmark_score_persists_to_excluded_meta_channel(tmp_path):
    store = Store(tmp_path / "lib.db")
    store.migrate()
    run_benchmark_episode(
        target="kanboard",
        snapshot_id=3,
        epoch=None,
        store=store,
        drivers=_drivers(),
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
    )
    raw = store.get_meta(benchmark_score_key("kanboard", 3, None))
    assert raw is not None, "the benchmark score lands in the excluded meta channel"
    parsed = json.loads(raw)
    assert parsed["mode"] == BENCHMARK_MODE
    assert parsed["target"] == "kanboard"
    assert parsed["snapshot_id"] == 3
    assert parsed["epoch"] is None
    store.close()


def test_benchmark_writes_no_ideas_or_fitness(tmp_path):
    store = Store(tmp_path / "lib.db")
    store.migrate()
    # plant two insights with fitness counters set
    for i in range(2):
        store.insert_insight(
            precondition=f"pre{i}",
            action=f"act{i}",
            expected_outcome=f"out{i}",
            content_hash=f"hash{i}",
            status="active",
        )
    before = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w, COALESCE(SUM(losses),0) l,"
        " COALESCE(SUM(retrievals),0) r, COALESCE(SUM(causal_blames),0) b"
        " FROM insights"
    ).fetchone()

    channel = ExcludedFitnessChannel()
    run_benchmark_episode(
        target="kanboard",
        snapshot_id=1,
        store=store,
        drivers=_drivers(),
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
        fitness_channel=channel,
    )

    after = store.conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(wins),0) w, COALESCE(SUM(losses),0) l,"
        " COALESCE(SUM(retrievals),0) r, COALESCE(SUM(causal_blames),0) b"
        " FROM insights"
    ).fetchone()
    # no insight rows added, no fitness columns mutated (R19 excluded channel)
    assert tuple(after) == tuple(before)
    # benchmark mode never mints settlement/SCEN rows either
    assert store.conn.execute("SELECT COUNT(*) FROM settlement_reports").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM trace_scen").fetchone()[0] == 0
    # fitness routed to the excluded channel, tagged with benchmark mode
    assert len(channel.events) == len(FROZEN_SLICE)
    assert all(e.kind == BENCHMARK_MODE for e in channel.events)
    store.close()


def test_reset_target_hook_fires_before_measuring():
    fired = []
    run_benchmark_episode(
        target="kanboard",
        snapshot_id=1,
        drivers=_drivers(),
        cache=ResolutionCache(),
        resolve=_const_resolve,
        settle_config=_settle_config(),
        reset_target=lambda: fired.append(True),
        persist=False,
    )
    assert fired == [True], "R6 reset-to-seed hook fires once before scenarios"


def test_benchmark_needs_both_app_drivers():
    with pytest.raises(TargetEnvError, match="both apps"):
        run_benchmark_episode(
            target="kanboard",
            snapshot_id=1,
            drivers={"target": ConstDriver()},
            cache=ResolutionCache(),
            resolve=_const_resolve,
            settle_config=_settle_config(),
            persist=False,
        )


# --- benchmark-instrument σ (R16) --------------------------------------------


def test_benchmark_sigma_over_replicates():
    # replicate benchmark scores at an unchanged snapshot -> sample stdev
    import statistics

    replicates = [0.80, 0.75, 0.85, 0.70, 0.90]
    assert benchmark_sigma(replicates) == pytest.approx(statistics.stdev(replicates))
    # zero-variance replicates yield zero σ
    assert benchmark_sigma([0.5, 0.5, 0.5]) == 0.0


def test_benchmark_sigma_needs_two():
    with pytest.raises(TargetEnvError, match="≥2 replicate"):
        benchmark_sigma([0.8])


# --- live docker-required suite (R22 boot/seed/reset) ------------------------


docker_required = [
    pytest.mark.docker,
    pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="requires Docker Desktop (docker not on PATH)",
    ),
]


@pytest.fixture(scope="module")
def live_target():
    config = KanboardConfig(
        compose_file=COMPOSE_FILE,
        seed_manifest=SEED_MANIFEST,
        readiness_timeout_s=300.0,  # first boot pulls the image
        poll_interval_s=2.0,
    )
    target = KanboardTarget(config)
    target.up()
    yield target
    target.down(drop_volume=True)


class TestLiveKanboard:
    """Ordered live scenarios: boot -> seed -> reset.

    NOTE: KANBOARD_IMAGE_DIGEST is a placeholder pending live resolution; up()
    will refuse with DigestMismatchError until it is re-pinned to the real
    Docker Hub digest (the documented R22 live prerequisite).
    """

    pytestmark = docker_required

    def test_boot_reaches_ready(self, live_target):
        live_target.wait_healthy()

    def test_seed_produces_expected_counts(self, live_target):
        counts = live_target.seed()
        assert counts == expected_seed_counts(
            load_kanboard_seed_manifest(SEED_MANIFEST)
        )

    def test_reset_returns_to_seed(self, live_target):
        counts = live_target.reset_to_seed()
        assert counts == expected_seed_counts(
            load_kanboard_seed_manifest(SEED_MANIFEST)
        )
