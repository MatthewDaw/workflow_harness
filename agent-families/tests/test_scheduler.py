"""plan-005 U5: parallel episodes and batch merging (R15-R17).

Fully offline (the default suite): episode bodies, the registration step, and
the joint confirmation are all injected, and the merge path drives the REAL
``add_idea`` registration spine (so the cosine prefilter that absorbs a planted
near-duplicate is genuinely exercised — only the judge VERDICT is scripted via
record/replay fixtures). Concurrency is demonstrated with real threads + a
barrier; zero quota, no ``claude`` on PATH.

## Conformance

Test-scenario / invariant (plan-005 U5) -> test:

- two concurrent episodes never contend on the library (read-only verified) and
  serialize at the queue: ``test_concurrent_episodes_are_read_only_on_library``
  (+ enforcement: ``test_library_mutation_during_fanout_is_rejected``)
- at most one in-flight episode per target: ``test_same_target_episodes_serialize``
- quota-aware admission throttles concurrency: ``test_quota_aware_admission_throttles``
- port/compose namespaces disjoint: ``test_episode_namespaces_are_disjoint``
  (+ wiring: ``test_compose_project_threads_into_docker_calls``)
- two batches merge with the prefilter absorbing a planted near-duplicate:
  ``test_merge_absorbs_planted_near_duplicate``
- joint-confirmation failure blocks promotion and records the telemetry:
  ``test_joint_confirmation_failure_blocks_promotion``
- per-batch revert after a merged promotion removes exactly one batch's
  insights: ``test_per_batch_revert_removes_one_batch``
- a batch that fails independent validation never reaches registration:
  ``test_independent_validation_drops_a_batch``
- validation runs execute concurrently: ``test_parallel_validation_runs_concurrently``
- canonical-order determinism (parallel == sequential post-merge state):
  ``test_canonical_order_determinism``
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families import judge
from agent_families.config import (
    Config,
    EmbeddingConfig,
    JudgeConfig,
    LifecycleConfig,
    MergeConfig,
    RetrievalConfig,
    StoreConfig,
)
from agent_families.embedding import EmbeddingService
from agent_families.grading.target_env import (
    PORT_TABLE,
    EpisodeNamespace,
    LinkdingTarget,
    TargetEnvConfig,
    episode_namespace,
)
from agent_families.judge import write_fixture
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    add_idea,
    build_idea_text,
    build_merge_prompt,
    build_placement_prompt,
    build_taxonomy_prompt,
    content_hash,
)
from agent_families.pipeline.scheduler import (
    CandidateIdea,
    EpisodeBatch,
    EpisodeScheduler,
    EpisodeSpec,
    JointConfirm,
    SchedulerError,
    SchedulerParams,
    joint_confirm_failure_rate,
    merge_batches,
    merge_state_digest,
    parallel_map,
    revert_one_batch,
)
from agent_families.reflector.validate import BenchmarkOutcome
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 8
CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
)

# Unit vectors with hand-chosen cosines: A and A_NEAR coincide (cosine 1.0 -> the
# 0.92 merge prefilter fires); B/C are orthogonal to A (cosine 0 < the 0.5
# relevance floor -> the cold-start taxonomy branch, a fresh skill).
VEC_A = [1.0] + [0.0] * (DIM - 1)
VEC_B = [0.0, 1.0] + [0.0] * (DIM - 2)
VEC_C = [0.0, 0.0, 1.0] + [0.0] * (DIM - 3)

# The post-bootstrap clean history (mean 0.90, LCL 0.84 at 3σ=0.06): 0.90 passes,
# 0.80 is special-cause low (reverts its batch in independent validation).
POST_BOOTSTRAP_HISTORY = tuple([0.90] * 12)


def _pass_bench() -> BenchmarkOutcome:
    return BenchmarkOutcome(candidate=0.90, sigma=0.02, history=POST_BOOTSTRAP_HISTORY)


def _fail_bench() -> BenchmarkOutcome:
    return BenchmarkOutcome(candidate=0.80, sigma=0.02, history=POST_BOOTSTRAP_HISTORY)


# --- judge/embedder fakes (the real prefilter, a scripted always-accept judge) --


class MappingEncoder:
    """Returns a fixed vector per idea text (keyed by the idea-text suffix)."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = dict(mapping)

    def encode(self, text: str):
        for key, vector in self.mapping.items():
            if text.endswith(key):
                return list(vector)
        raise KeyError(f"no vector mapped for {text!r}")


def envelope_for(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 950,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": output,
    }


def out(outcome: str, **extra) -> dict:
    base = {
        "outcome": outcome,
        "scope_tag": {"value": "universal", "justification": "any web target"},
        "lint": {"verdict": "pass"},
        "confidence": 0.9,
    }
    base.update(extra)
    return base


@pytest.fixture(autouse=True)
def clean_judge_env(monkeypatch):
    monkeypatch.delenv(judge.MODE_ENV, raising=False)
    monkeypatch.delenv(judge.FIXTURES_ENV, raising=False)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def make_env(tmp_path, name: str):
    """A registration environment: store + vec + a single worker agent."""
    store = Store(tmp_path / f"{name}.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist", description="generic")
    return SimpleNamespace(
        store=store,
        vec=vec,
        agent_id=agent_id,
        fixtures=tmp_path / f"{name}-fixtures",
    )


def make_register(env, embedder):
    """An always-accept registration seam that drives the REAL add_idea spine.

    The branch (merge / placement / taxonomy) is decided by add_idea's own cosine
    math against the live vec index; this wrapper only records the matching
    scripted judge verdict just before each call, so the planted near-duplicate
    is absorbed by the genuine prefilter, never by a fake.
    """

    def register(idea: CandidateIdea, label: str):
        fields = dict(
            precondition=idea.precondition,
            action=idea.action,
            expected_outcome=idea.expected_outcome,
        )
        idea_hash = content_hash(**fields)
        # content-hash fast paths take no judge call
        if env.store.find_insight_by_hash(idea_hash) or env.store.find_merge_log_by_hash(
            idea_hash
        ):
            return add_idea(
                env.store, env.vec, embedder, CFG, **fields,
                batch_label=label, judge_fixtures_dir=env.fixtures,
            )
        idea_text = build_idea_text(**fields)
        vector = embedder.embed_document(idea_text)
        neighbors = env.vec.knn(
            vector, CFG.retrieval.ann_top_k, statuses=None, on="retrieval"
        )
        merge = [n for n in neighbors if 1.0 - n.distance >= CFG.merge.cosine_threshold]
        if merge:
            prompt = build_merge_prompt(env.store, idea_text, merge)
            output = out("merge_discard", duplicate_of=merge[0].insight_id)
        else:
            place = [
                n for n in neighbors
                if 1.0 - n.distance >= CFG.retrieval.relevance_floor
            ]
            if place:
                prompt = build_placement_prompt(env.store, idea_text, place, None)
                row = env.store.conn.execute(
                    "SELECT skill_id FROM skill_members WHERE insight_id = ? LIMIT 1",
                    (place[0].insight_id,),
                ).fetchone()
                output = out("append_to_skill", target_skill_id=row["skill_id"])
            else:
                prompt = build_taxonomy_prompt(env.store, idea_text, None)
                output = out(
                    "new_skill",
                    new_skill={
                        "agent_id": env.agent_id,
                        "name": "skill-" + idea_hash[:12],
                        "description": "auto",
                    },
                )
        write_fixture(env.fixtures, prompt, JUDGE_SCHEMA, "sonnet", envelope_for(output))
        return add_idea(
            env.store, env.vec, embedder, CFG, **fields,
            batch_label=label, judge_fixtures_dir=env.fixtures,
        )

    return register


# Canonical idea set: A (ep1), A_NEAR (near-dup, ep2), B (distinct, ep2).
IDEA_A = CandidateIdea(
    precondition="A web target is probed for requirements",
    action="ask about role-gated admin areas during elicitation alpha",
    expected_outcome="hidden admin features surface as requirements",
)
IDEA_A_NEAR = CandidateIdea(
    precondition="A web target is probed for requirements",
    action="ask about role-gated admin areas during elicitation beta",
    expected_outcome="hidden admin features surface as requirements",
)
IDEA_B = CandidateIdea(
    precondition="A dataset is being imported",
    action="verify the importer rejects malformed rows gamma",
    expected_outcome="malformed rows are reported, not silently dropped",
)


def vectors_for(*ideas_and_vecs) -> dict[str, list[float]]:
    return {
        build_idea_text(
            precondition=idea.precondition,
            action=idea.action,
            expected_outcome=idea.expected_outcome,
        ): vec
        for idea, vec in ideas_and_vecs
    }


def standard_embedder():
    mapping = vectors_for(
        (IDEA_A, VEC_A), (IDEA_A_NEAR, VEC_A), (IDEA_B, VEC_B)
    )
    return EmbeddingService(CFG.embedding, encoder=MappingEncoder(mapping))


# ============================ R15: namespacing =================================


def test_episode_namespaces_are_disjoint():
    """Per-episode compose projects and host ports are disjoint by construction."""
    nss = [episode_namespace(e) for e in range(1, 6)]
    projects = [ns.compose_project for ns in nss]
    assert len(set(projects)) == len(projects), "compose projects must be unique"

    all_ports: list[int] = []
    for ns in nss:
        assert set(ns.ports) == set(PORT_TABLE), "every service gets a namespaced port"
        ports = list(ns.ports.values())
        assert len(set(ports)) == len(ports), "ports within an episode are distinct"
        all_ports.extend(ports)
    assert len(set(all_ports)) == len(all_ports), "ports across episodes never collide"
    assert all(1 <= p <= 65535 for p in all_ports)


def test_episode_namespace_rejects_bad_ids_and_overflow():
    from agent_families.grading.target_env import TargetEnvError

    with pytest.raises(TargetEnvError, match="positive 1-based"):
        episode_namespace(0)
    with pytest.raises(TargetEnvError, match="port range"):
        episode_namespace(2, port_base=65000, port_stride=1000)
    with pytest.raises(TargetEnvError, match="too small"):
        episode_namespace(1, services=("a", "b", "c"), port_stride=2)


def test_episode_namespace_is_immutable_and_stable():
    ns = episode_namespace(3)
    assert isinstance(ns, EpisodeNamespace)
    assert ns.compose_project == "af-ep3"
    with pytest.raises(TypeError):
        ns.ports["linkding"] = 1  # MappingProxyType is read-only
    assert episode_namespace(3).ports == ns.ports  # deterministic


def test_compose_project_threads_into_docker_calls(tmp_path):
    """A configured compose_project scopes every docker call with -p (R15);
    the default (None) leaves the static single-stack argv unchanged."""
    target_dir = Path(__file__).resolve().parent.parent / "targets" / "linkding"
    calls: list[list[str]] = []

    def runner(argv):
        calls.append(list(argv))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    cfg = TargetEnvConfig(
        compose_file=target_dir / "docker-compose.yml",
        seed_manifest=target_dir / "seed_manifest.json",
        readiness_timeout_s=5.0,
        poll_interval_s=0.01,
        compose_project="af-ep7",
    )
    LinkdingTarget(cfg, runner=runner).down()
    assert calls[0][:6] == [
        "docker", "compose", "-f", str(cfg.compose_file), "-p", "af-ep7",
    ]

    # default: no -p inserted (backward-compatible with the Phase 2 contract)
    calls.clear()
    cfg_default = TargetEnvConfig(
        compose_file=target_dir / "docker-compose.yml",
        seed_manifest=target_dir / "seed_manifest.json",
        readiness_timeout_s=5.0,
        poll_interval_s=0.01,
    )
    LinkdingTarget(cfg_default, runner=runner).down()
    assert "-p" not in calls[0]
    assert calls[0][:4] == ["docker", "compose", "-f", str(cfg_default.compose_file)]


# ============================ R15: scheduler ===================================


class ConcurrencyProbe:
    """Tracks peak overall and per-target concurrency observed inside runners."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.per_target: dict[str, int] = {}
        self.peak_per_target = 0

    def enter(self, target: str) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            c = self.per_target.get(target, 0) + 1
            self.per_target[target] = c
            self.peak_per_target = max(self.peak_per_target, c)

    def exit(self, target: str) -> None:
        with self.lock:
            self.active -= 1
            self.per_target[target] -= 1


def test_concurrent_episodes_are_read_only_on_library(store):
    """Two different-target episodes run concurrently against one frozen
    snapshot; the library is never mutated mid-fan-out (R15)."""
    barrier = threading.Barrier(2, timeout=5)
    probe = ConcurrencyProbe()

    def runner(spec: EpisodeSpec):
        probe.enter(spec.target)
        try:
            barrier.wait()  # both must be in flight at once or this times out
            return f"done-{spec.episode_id}"
        finally:
            probe.exit(spec.target)

    specs = [
        EpisodeSpec(episode_id=1, target="linkding"),
        EpisodeSpec(episode_id=2, target="kanboard"),
    ]
    sched = EpisodeScheduler(SchedulerParams(max_concurrent=2))
    result = sched.run(store, specs, runner)

    assert probe.peak == 2, "the two episodes genuinely overlapped"
    assert result.peak_concurrency == 2
    assert result.library_snapshot_id == 0
    assert store.current_snapshot_id() == 0, "library stayed read-only"
    assert {r.outcome for r in result.runs} == {"done-1", "done-2"}
    # each run carries its disjoint namespace
    assert result.runs[0].namespace.compose_project != result.runs[1].namespace.compose_project


def test_same_target_episodes_serialize(store):
    """At most one in-flight episode per target — same-target episodes never
    overlap even with slots free (R15: per-target serial)."""
    probe = ConcurrencyProbe()

    def runner(spec: EpisodeSpec):
        probe.enter(spec.target)
        time.sleep(0.05)
        probe.exit(spec.target)
        return spec.episode_id

    specs = [
        EpisodeSpec(episode_id=1, target="linkding"),
        EpisodeSpec(episode_id=2, target="linkding"),
        EpisodeSpec(episode_id=3, target="linkding"),
    ]
    sched = EpisodeScheduler(SchedulerParams(max_concurrent=3))
    result = sched.run(store, specs, runner)

    assert probe.peak_per_target == 1, "one target serialized to one in flight"
    assert probe.peak == 1
    assert result.max_concurrent_per_target == 1
    assert sorted(result.outcomes) == [1, 2, 3]


def test_quota_aware_admission_throttles(store):
    """The quota callback caps concurrency below max_concurrent (R15)."""
    probe = ConcurrencyProbe()

    def runner(spec: EpisodeSpec):
        probe.enter(spec.target)
        time.sleep(0.05)
        probe.exit(spec.target)
        return spec.episode_id

    specs = [
        EpisodeSpec(episode_id=1, target="a"),
        EpisodeSpec(episode_id=2, target="b"),
        EpisodeSpec(episode_id=3, target="c"),
    ]
    # max_concurrent=3 would allow 3, but the quota allows only 1 in flight.
    sched = EpisodeScheduler(
        SchedulerParams(max_concurrent=3), quota_fn=lambda in_flight: in_flight < 1
    )
    result = sched.run(store, specs, runner)

    assert probe.peak == 1, "quota throttled concurrency to 1"
    assert result.peak_concurrency == 1
    assert sorted(result.outcomes) == [1, 2, 3]


def test_quota_total_denial_raises_instead_of_deadlocking(store):
    sched = EpisodeScheduler(
        SchedulerParams(max_concurrent=2), quota_fn=lambda in_flight: False
    )
    with pytest.raises(SchedulerError, match="cannot make progress"):
        sched.run(store, [EpisodeSpec(1, "a")], lambda spec: 1)


def test_library_mutation_during_fanout_is_rejected(store, monkeypatch):
    """If a write escapes the queue mid-fan-out (snapshot moves), the scheduler
    refuses to return — the read-only invariant is enforced, not just asserted."""
    seq = iter([0, 7])  # frozen=0, after=7

    monkeypatch.setattr(store, "current_snapshot_id", lambda: next(seq))
    sched = EpisodeScheduler(SchedulerParams(max_concurrent=1))
    with pytest.raises(SchedulerError, match="read-only"):
        sched.run(store, [EpisodeSpec(1, "a")], lambda spec: "ok")


# ============================ R17: parallel validation =========================


def test_parallel_validation_runs_concurrently():
    """Read-only validation runs execute concurrently and preserve order (R17)."""
    barrier = threading.Barrier(3, timeout=5)

    def validate(item):
        barrier.wait()  # all three must run at once or this times out
        return item * 10

    results = parallel_map(validate, [1, 2, 3], max_workers=3)
    assert results == [10, 20, 30], "results returned in input order"


def test_parallel_map_empty_is_noop():
    assert parallel_map(lambda x: x, []) == []


# ============================ R16: batch merging ===============================


def test_merge_absorbs_planted_near_duplicate(tmp_path):
    """Two batches validated against the same snapshot merge with the cosine
    prefilter absorbing the planted near-duplicate (R16)."""
    env = make_env(tmp_path, "merge")
    register = make_register(env, standard_embedder())
    batches = [
        EpisodeBatch(1, "reflect-ep1", (IDEA_A,), _pass_bench(), 20),
        EpisodeBatch(2, "reflect-ep2", (IDEA_A_NEAR, IDEA_B), _pass_bench(), 20),
    ]
    result = merge_batches(
        batches,
        store=env.store,
        shared_snapshot_id=env.store.current_snapshot_id(),
        register=register,
        joint_confirm=lambda labels: JointConfirm(passed=True),
    )

    assert result.canonical_order == (1, 2)
    codes = {(r.label, r.idea_index): r.code for r in result.registrations}
    assert codes[("reflect-ep1", 0)] == "registered"
    assert codes[("reflect-ep2", 0)] == "merged", "the near-duplicate was absorbed"
    assert codes[("reflect-ep2", 1)] == "registered"

    # exactly two distinct insights survived (A and B), plus one merge-log row
    n_insights = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    n_merges = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM merge_log"
    ).fetchone()["n"]
    assert n_insights == 2
    assert n_merges == 1
    assert result.promoted is True
    assert len(result.active_insight_ids) == 2
    env.store.close()


def test_joint_confirmation_failure_blocks_promotion(tmp_path):
    """A planted interaction fails joint confirmation: promotion is blocked, the
    union stays quarantined (default deny), telemetry recorded (R16)."""
    env = make_env(tmp_path, "jcfail")
    register = make_register(env, standard_embedder())
    batches = [
        EpisodeBatch(1, "reflect-ep1", (IDEA_A,), _pass_bench(), 20),
        EpisodeBatch(2, "reflect-ep2", (IDEA_B,), _pass_bench(), 20),
    ]
    result = merge_batches(
        batches,
        store=env.store,
        shared_snapshot_id=env.store.current_snapshot_id(),
        register=register,
        joint_confirm=lambda labels: JointConfirm(passed=False, detail="planted interaction"),
    )

    assert result.promoted is False
    assert result.promoted_labels == ()
    assert result.active_insight_ids == ()
    statuses = [
        r["status"] for r in env.store.conn.execute("SELECT status FROM insights").fetchall()
    ]
    assert statuses and all(s == "quarantined" for s in statuses), "default deny"
    assert env.store.current_snapshot_id() == 0, "blocked union mints no promotion snapshot"

    # interaction-effect telemetry recorded
    assert joint_confirm_failure_rate(env.store) == pytest.approx(1.0)
    rec = env.store.conn.execute(
        "SELECT detail FROM batch_validations ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "blocked" in rec["detail"]
    env.store.close()


def test_per_batch_revert_removes_one_batch(tmp_path):
    """Reverting one merged batch removes exactly that batch's insights (R16)."""
    env = make_env(tmp_path, "revert")
    register = make_register(env, standard_embedder())
    batches = [
        EpisodeBatch(1, "reflect-ep1", (IDEA_A,), _pass_bench(), 20),
        EpisodeBatch(2, "reflect-ep2", (IDEA_A_NEAR, IDEA_B), _pass_bench(), 20),
    ]
    merge_batches(
        batches,
        store=env.store,
        shared_snapshot_id=env.store.current_snapshot_id(),
        register=register,
        joint_confirm=lambda labels: JointConfirm(passed=True),
    )
    from agent_families.reflector.validate import active_batch_insight_ids

    ep1_active = active_batch_insight_ids(env.store, "reflect-ep1")
    ep2_active = active_batch_insight_ids(env.store, "reflect-ep2")
    assert len(ep1_active) == 1 and len(ep2_active) == 1, "A and B active"

    reverted = revert_one_batch(env.store, "reflect-ep2")
    assert set(reverted) == set(ep2_active), "only ep2's insight reverted"
    assert active_batch_insight_ids(env.store, "reflect-ep2") == ()
    assert active_batch_insight_ids(env.store, "reflect-ep1") == ep1_active, (
        "the other batch is untouched"
    )
    env.store.close()


def test_independent_validation_drops_a_batch(tmp_path):
    """A batch that regresses its independent validation never reaches
    registration (R16)."""
    env = make_env(tmp_path, "drop")
    register = make_register(env, standard_embedder())
    batches = [
        EpisodeBatch(1, "reflect-ep1", (IDEA_A,), _pass_bench(), 20),
        EpisodeBatch(2, "reflect-ep2", (IDEA_B,), _fail_bench(), 20),  # regresses
    ]
    result = merge_batches(
        batches,
        store=env.store,
        shared_snapshot_id=env.store.current_snapshot_id(),
        register=register,
        joint_confirm=lambda labels: JointConfirm(passed=True),
    )

    assert result.dropped_labels == ("reflect-ep2",)
    assert result.validated_labels == ("reflect-ep1",)
    assert all(r.label == "reflect-ep1" for r in result.registrations)
    # only A was ever registered
    assert env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"] == 1
    env.store.close()


def test_canonical_order_determinism(tmp_path):
    """Verification: parallel and sequential runs that feed the same validated
    batches through the canonical order leave identical post-merge state, even
    when the batches arrive in opposite input orders (R16)."""
    batches_def = [
        EpisodeBatch(1, "reflect-ep1", (IDEA_A,), _pass_bench(), 20),
        EpisodeBatch(2, "reflect-ep2", (IDEA_A_NEAR, IDEA_B), _pass_bench(), 20),
    ]

    # Parallel flavour: episodes run concurrently via the scheduler and merely
    # RETURN their batch (library read-only); merge in completion order.
    env_p = make_env(tmp_path, "det-parallel")
    store_only = Store(tmp_path / "det-sched.db")
    store_only.migrate()
    specs = [EpisodeSpec(b.episode_id, b.label, payload=b) for b in batches_def]
    sched = EpisodeScheduler(SchedulerParams(max_concurrent=2))
    sched_result = sched.run(store_only, specs, lambda spec: spec.payload)
    parallel_batches = [r.outcome for r in sched_result.runs]
    merge_batches(
        parallel_batches,
        store=env_p.store,
        shared_snapshot_id=env_p.store.current_snapshot_id(),
        register=make_register(env_p, standard_embedder()),
        joint_confirm=lambda labels: JointConfirm(passed=True),
    )
    store_only.close()

    # Sequential flavour: feed the batches in the OPPOSITE input order.
    env_s = make_env(tmp_path, "det-sequential")
    merge_batches(
        list(reversed(batches_def)),
        store=env_s.store,
        shared_snapshot_id=env_s.store.current_snapshot_id(),
        register=make_register(env_s, standard_embedder()),
        joint_confirm=lambda labels: JointConfirm(passed=True),
    )

    assert merge_state_digest(env_p.store) == merge_state_digest(env_s.store)
    env_p.store.close()
    env_s.store.close()
