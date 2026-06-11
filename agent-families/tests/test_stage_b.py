"""plan-004 U6: Reflector Stage B — stingy, validated, counterfactual reflection
(R10-R13).

Fully offline (the default suite): clustering is pure store lookups; the
per-cluster counterfactual reflection goes through the judge seam (a scripted fake
here, ``run_judge`` live); registration goes through an injected ``register_fn``
(a fake in the unit tests, the real ``add_idea`` + record/replay fixtures in the
two integration tests). Zero quota, no ``claude`` on PATH.

## Conformance

Test-scenario / invariant (plan-004 U6) -> test:

- clustering merges six same-cause SCEN failures into one cluster:
  ``test_clustering_merges_same_cause_failures``
- distinct causes split into distinct clusters:
  ``test_clustering_splits_distinct_causes``
- no-lesson output -> no insight + a telemetry row:
  ``test_no_lesson_produces_no_insight_and_a_telemetry_row``
- over-budget ranking drops the right clusters (must-tier > size > confidence):
  ``test_over_budget_ranking_drops_the_right_clusters`` (+ the direct ranking unit
  ``test_rank_and_select_orders_must_tier_then_size_then_confidence``)
- one idea per cluster enforced (verification):
  ``test_one_idea_per_cluster_enforced``
- validator rejects a non-active (same-batch quarantined) implicated ref in
  training mode: ``test_validator_rejects_non_active_implicated_ref_in_training``
- a quarantined batch-under-trial ref is legal in trial mode:
  ``test_validator_allows_batch_under_trial_ref_in_trial``
- validator rejects free-prose insights:
  ``test_validator_rejects_free_prose_insight``
- override below the confidence threshold recorded but not applied:
  ``test_override_below_threshold_recorded_not_applied``
- override at/above the threshold applied:
  ``test_override_at_threshold_is_applied``
- empty batch settles legally (no validation cycle, telemetry row):
  ``test_empty_batch_settles_legally``
- batch lands quarantined with full provenance:
  ``test_batch_lands_quarantined_with_full_provenance``
- a full fixture episode yields a batch whose every insight passes registration:
  ``test_full_episode_every_insight_passes_registration``

## Deviations

- The U6 Approach line mentions "run-memory nominations", but run memory (R5/R6)
  is unit U3's ``pipeline/runmemory.py`` (absent in this wave) and is not among
  U6's requirements (R10-R13). The success-channel nomination is left to U3, which
  submits through the same ``add_idea`` batch tag Stage B establishes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

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
from agent_families.judge import write_fixture
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    build_idea_text,
    build_taxonomy_prompt,
    content_hash,
)
from agent_families.reflector import stage_a as sa
from agent_families.reflector import stage_b as sb
from agent_families.store import Store
from agent_families.vecindex import VecIndex


# --- store + attribution builders ------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def _cause(role="worker", aspect="implementation", refs=()):
    return sa.Cause(role, aspect, tuple(refs))


def _case_file(scen_id, feat_id, tkt_ids=("TKT-1",)):
    return sa.CaseFile(
        scen_id=scen_id,
        feat_id=feat_id,
        msg_ids=(),
        req_ids=(),
        tkt_ids=tuple(tkt_ids),
        ac_ids=(),
        span_ids=(),
        chk_ids=(),
        qa_ids=(),
        evidence=(f"SCEN {scen_id}: failed",),
    )


def _attr(scen_id, feat_id="FEAT-1", *, role="worker", aspect="implementation",
          tkt_ids=("TKT-1",), contributing=()):
    return sa.Attribution(
        scen_id=scen_id,
        primary=_cause(role, aspect),
        contributing=tuple(contributing),
        case_file=_case_file(scen_id, feat_id, tkt_ids),
        sink=None,
    )


def _scen_row(store, scen_id, feat_id="FEAT-1", *, tier="should", episode_id,
              result="fail"):
    store.conn.execute(
        "INSERT OR IGNORE INTO trace_feat (id, evidence_ref) VALUES (?, 'e')",
        (feat_id,),
    )
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, result, tier, episode_id, snapshot_id)"
        " VALUES (?, ?, ?, ?, ?, 0)",
        (scen_id, feat_id, result, tier, episode_id),
    )


def _stage_a_result(episode_id, attributions):
    return sa.StageAResult(
        episode_id=episode_id,
        attributions=tuple(attributions),
        instrument_health_ids=(),
    )


# --- scripted reflection judge ---------------------------------------------------


def _reflection_output(*, has_lesson=True, insight=None,
                       implicated=(), scope_tag=None, override=None,
                       confidence=0.9):
    if insight is None and has_lesson:
        insight = {
            "precondition": "A web target is being implemented",
            "action": "Wire the create-form submit handler to the API before UI",
            "expected_outcome": "Creating an entity persists and reloads the list",
        }
    return {
        "has_lesson": has_lesson,
        "counterfactual_insight": insight,
        "implicated_existing_insights": list(implicated),
        "scope_tag_proposal": scope_tag,
        "attribution_override": override,
        "confidence": confidence,
    }


def _judge_returning(output_or_fn):
    """A fake judge_fn: a fixed output dict, or a callable(prompt)->output."""

    def fake(prompt, schema, model, **kwargs):
        out = output_or_fn(prompt) if callable(output_or_fn) else output_or_fn
        return SimpleNamespace(output=out)

    return fake


# --- clustering (R10) ------------------------------------------------------------


def test_clustering_merges_same_cause_failures(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    attrs = []
    for i in range(1, 7):
        sid = f"SCEN-{i}"
        _scen_row(store, sid, "FEAT-1", tier="should", episode_id=ep)
        attrs.append(_attr(sid, "FEAT-1", role="worker", aspect="implementation"))
    clusters = sb.cluster_failures(store, attrs)
    assert len(clusters) == 1
    assert clusters[0].size == 6
    assert clusters[0].scen_ids == tuple(f"SCEN-{i}" for i in range(1, 7))
    assert clusters[0].signature == ("worker", "implementation")


def test_clustering_splits_distinct_causes(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _scen_row(store, "SCEN-1", "FEAT-1", episode_id=ep)
    _scen_row(store, "SCEN-2", "FEAT-1", episode_id=ep)   # same feat/ticket...
    _scen_row(store, "SCEN-3", "FEAT-2", episode_id=ep)   # ...different feat
    attrs = [
        _attr("SCEN-1", "FEAT-1", role="worker", aspect="implementation"),
        _attr("SCEN-2", "FEAT-1", role="planner", aspect="coverage"),  # diff cause
        _attr("SCEN-3", "FEAT-2", role="worker", aspect="implementation"),
    ]
    clusters = sb.cluster_failures(store, attrs)
    assert len(clusters) == 3
    # Deterministic order by cluster_id.
    assert [c.cluster_id for c in clusters] == sorted(c.cluster_id for c in clusters)


def test_cluster_must_tier_is_any_member(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _scen_row(store, "SCEN-1", "FEAT-1", tier="should", episode_id=ep)
    _scen_row(store, "SCEN-2", "FEAT-1", tier="must", episode_id=ep)
    attrs = [_attr("SCEN-1", "FEAT-1"), _attr("SCEN-2", "FEAT-1")]
    clusters = sb.cluster_failures(store, attrs)
    assert len(clusters) == 1 and clusters[0].must_tier is True


# --- one reflection per cluster (R10) --------------------------------------------


def test_one_idea_per_cluster_enforced(store):
    """A reflection yields at most one insight — the schema carries a single
    counterfactual_insight, so a cluster can never emit two ideas."""
    ep = store.create_episode("linkding", "sha256:x", 0)
    for i in range(1, 7):
        _scen_row(store, f"SCEN-{i}", "FEAT-1", episode_id=ep)
    attrs = [_attr(f"SCEN-{i}", "FEAT-1") for i in range(1, 7)]
    sar = _stage_a_result(ep, attrs)
    registered = []

    def register_fn(idea):
        iid = store.insert_insight(
            precondition=idea.precondition, action=idea.action,
            expected_outcome=idea.expected_outcome,
            content_hash=content_hash(idea.precondition, idea.action,
                                      idea.expected_outcome),
            status="quarantined", batch_id=store.ensure_batch(idea.batch_label),
        )
        registered.append(idea)
        return iid

    result = sb.run_stage_b(
        store, sar, register_fn=register_fn, episode_id=ep,
        judge_fn=_judge_returning(_reflection_output()),
    )
    # Six failures, one cluster, exactly one insight registered.
    assert len(result.registered) == 1
    assert len(registered) == 1


# --- no-lesson + empty batch (R11) -----------------------------------------------


def test_no_lesson_produces_no_insight_and_a_telemetry_row(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _scen_row(store, "SCEN-1", "FEAT-1", episode_id=ep)
    sar = _stage_a_result(ep, [_attr("SCEN-1", "FEAT-1")])

    called = []
    result = sb.run_stage_b(
        store, sar,
        register_fn=lambda idea: called.append(idea),
        episode_id=ep,
        judge_fn=_judge_returning(_reflection_output(has_lesson=False)),
    )
    assert result.registered == ()
    assert called == []  # no registration attempted
    assert result.no_lesson_cluster_ids != ()
    rows = store.conn.execute(
        "SELECT kind FROM review_queue WHERE episode_id = ?", (ep,)
    ).fetchall()
    kinds = {r["kind"] for r in rows}
    assert sb.NO_LESSON_KIND in kinds


def test_empty_batch_settles_legally(store):
    """No failed scenarios -> no clusters -> legal empty batch: no insight, no
    batch label, one empty-batch telemetry row."""
    ep = store.create_episode("linkding", "sha256:x", 0)
    sar = _stage_a_result(ep, [])
    result = sb.run_stage_b(
        store, sar,
        register_fn=lambda idea: pytest.fail("must not register on empty batch"),
        episode_id=ep,
        judge_fn=_judge_returning(_reflection_output()),
    )
    assert result.is_empty
    assert result.batch_label is None
    rows = store.conn.execute(
        "SELECT kind FROM review_queue WHERE episode_id = ?", (ep,)
    ).fetchall()
    assert {r["kind"] for r in rows} == {sb.EMPTY_BATCH_KIND}


# --- budget + ranking (R11) ------------------------------------------------------


def _cluster(cluster_id, *, must_tier=False, size=1):
    attrs = tuple(_attr(f"{cluster_id}-SCEN-{i}") for i in range(size))
    return sb.Cluster(
        cluster_id=cluster_id, feat_id="FEAT-1", tkt_key="TKT-1",
        signature=("worker", "implementation"), must_tier=must_tier,
        attributions=attrs,
    )


def _lesson(cluster, confidence=0.5):
    refl = sb.Reflection(
        cluster=cluster, has_lesson=True,
        insight={"precondition": "p", "action": "a", "expected_outcome": "e"},
        implicated_existing_insights=(), scope_tag_proposal=None,
        attribution_override=None, confidence=confidence,
    )
    return sb.ValidatedLesson(
        reflection=refl, fields={"precondition": "p", "action": "a",
                                 "expected_outcome": "e"},
        scope_tag=None, override=None, override_applied=False,
        effective_role="worker",
    )


def test_rank_and_select_orders_must_tier_then_size_then_confidence():
    must = _lesson(_cluster("C-must", must_tier=True, size=1), confidence=0.1)
    big = _lesson(_cluster("C-big", must_tier=False, size=5), confidence=0.1)
    conf = _lesson(_cluster("C-conf", must_tier=False, size=1), confidence=0.99)
    small = _lesson(_cluster("C-small", must_tier=False, size=1), confidence=0.2)
    selected, dropped = sb.rank_and_select([small, conf, big, must], budget=2)
    # must-tier first, then the larger cluster — confidence only breaks size ties.
    assert [l.cluster.cluster_id for l in selected] == ["C-must", "C-big"]
    assert [l.cluster.cluster_id for l in dropped] == ["C-conf", "C-small"]


def test_over_budget_ranking_drops_the_right_clusters(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    # Three lesson-bearing clusters (distinct feats), budget = 1. One is must-tier.
    feats = ["FEAT-keep", "FEAT-drop-a", "FEAT-drop-b"]
    attrs = []
    for i, feat in enumerate(feats):
        sid = f"SCEN-{i}"
        tier = "must" if feat == "FEAT-keep" else "should"
        _scen_row(store, sid, feat, tier=tier, episode_id=ep)
        attrs.append(_attr(sid, feat))
    sar = _stage_a_result(ep, attrs)
    registered = []

    def register_fn(idea):
        registered.append(idea.feat_id)
        return store.insert_insight(
            precondition=idea.precondition, action=idea.action,
            expected_outcome=idea.expected_outcome,
            content_hash=content_hash(idea.precondition, idea.action,
                                      idea.expected_outcome) + idea.feat_id,
            status="quarantined", batch_id=store.ensure_batch(idea.batch_label),
        )

    result = sb.run_stage_b(
        store, sar, register_fn=register_fn, episode_id=ep, idea_budget=1,
        judge_fn=_judge_returning(_reflection_output()),
    )
    assert registered == ["FEAT-keep"]  # must-tier survives
    assert len(result.dropped_cluster_ids) == 2
    over = store.conn.execute(
        "SELECT COUNT(*) AS n FROM review_queue WHERE episode_id = ? AND kind = ?",
        (ep, sb.OVER_BUDGET_KIND),
    ).fetchone()["n"]
    assert over == 2


# --- typed output validation (R12) -----------------------------------------------


def _reflection(cluster, *, insight, implicated=(), override=None, confidence=0.9,
                scope_tag=None):
    return sb.Reflection(
        cluster=cluster, has_lesson=True, insight=insight,
        implicated_existing_insights=tuple(implicated), scope_tag_proposal=scope_tag,
        attribution_override=override, confidence=confidence,
    )


GOOD_INSIGHT = {"precondition": "p", "action": "a", "expected_outcome": "e"}


def test_validator_rejects_free_prose_insight(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    refl = _reflection(_cluster("C"), insight={"lesson": "just some prose"})
    with pytest.raises(sb.StageBValidationError):
        sb.validate_reflection(store, refl, episode_id=ep)
    # A blank structural field is also free-prose-equivalent and rejected.
    refl2 = _reflection(_cluster("C"), insight=dict(GOOD_INSIGHT, action="   "))
    with pytest.raises(sb.StageBValidationError):
        sb.validate_reflection(store, refl2, episode_id=ep)


def test_validator_rejects_non_active_implicated_ref_in_training(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    # A quarantined insight (e.g. same-batch) is NOT active at the episode snapshot.
    quarantined = store.insert_insight(
        precondition="q", action="q", expected_outcome="q",
        content_hash="hash-quar", status="quarantined",
    )
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT,
                       implicated=[quarantined])
    with pytest.raises(sb.StageBValidationError):
        sb.validate_reflection(store, refl, episode_id=ep, mode="training")


def test_validator_accepts_active_implicated_ref(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    active = store.insert_insight(
        precondition="a", action="a", expected_outcome="a",
        content_hash="hash-active", status="active",
    )
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT, implicated=[active])
    lesson = sb.validate_reflection(store, refl, episode_id=ep, mode="training")
    assert lesson.reflection.implicated_existing_insights == (active,)


def test_validator_rejects_nonexistent_implicated_ref(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT, implicated=[404])
    with pytest.raises(sb.StageBValidationError):
        sb.validate_reflection(store, refl, episode_id=ep)


def test_validator_allows_batch_under_trial_ref_in_trial(store):
    ep = store.create_episode("linkding", "sha256:x", 0, mode="trial")
    batch_id = store.ensure_batch("batch-under-trial")
    member = store.insert_insight(
        precondition="m", action="m", expected_outcome="m",
        content_hash="hash-member", status="quarantined", batch_id=batch_id,
    )
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT, implicated=[member])
    # In a training episode this quarantined ref would be rejected...
    with pytest.raises(sb.StageBValidationError):
        sb.validate_reflection(store, refl, episode_id=ep, mode="training")
    # ...but in a trial for that batch it is legal (feeds the batch verdict).
    lesson = sb.validate_reflection(
        store, refl, episode_id=ep, mode="trial", batch_under_trial_id=batch_id
    )
    assert lesson.reflection.implicated_existing_insights == (member,)


# --- attribution override (R13) --------------------------------------------------


def test_override_below_threshold_recorded_not_applied(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    override = {"role": "planner", "confidence": 0.5}
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT, override=override)
    lesson = sb.validate_reflection(
        store, refl, episode_id=ep, override_confidence_threshold=0.7
    )
    assert lesson.override == override          # recorded
    assert lesson.override_applied is False     # but not applied
    assert lesson.effective_role == "worker"    # Stage A's role stands


def test_override_at_threshold_is_applied(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    override = {"role": "planner", "confidence": 0.7}
    refl = _reflection(_cluster("C"), insight=GOOD_INSIGHT, override=override)
    lesson = sb.validate_reflection(
        store, refl, episode_id=ep, override_confidence_threshold=0.7
    )
    assert lesson.override_applied is True
    assert lesson.effective_role == "planner"   # override wins (R13)


# --- batch formation + provenance (R10/R13) --------------------------------------

DIM = 8
CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
)
V_IDEA = [1.0] + [0.0] * (DIM - 1)


class FakeEncoder:
    def __init__(self, vector):
        self.vector = list(vector)

    def encode(self, text):
        return list(self.vector)


def _add_idea_env(store):
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist", description="generic")
    embedder = EmbeddingService(CFG.embedding, encoder=FakeEncoder(V_IDEA))
    return SimpleNamespace(vec=vec, agent_id=agent_id, embedder=embedder)


def _record_new_skill_fixture(store, fixtures_dir, fields, agent_id):
    """Write the cold-start taxonomy fixture so add_idea registers a new skill."""
    prompt = build_taxonomy_prompt(store, build_idea_text(**fields), None)
    envelope = {
        "type": "result", "subtype": "success", "is_error": False,
        "duration_ms": 900, "num_turns": 1, "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": {
            "outcome": "new_skill",
            "new_skill": {"agent_id": agent_id, "name": "counterfactuals",
                          "description": "lessons from failed scenarios"},
            "scope_tag": {"value": "universal", "justification": "any web target"},
            "lint": {"verdict": "pass"},
            "confidence": 0.9,
        },
    }
    write_fixture(fixtures_dir, prompt, JUDGE_SCHEMA, "sonnet", envelope)


def test_batch_lands_quarantined_with_full_provenance(store, tmp_path):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _scen_row(store, "SCEN-1", "FEAT-1", tier="must", episode_id=ep)
    sar = _stage_a_result(ep, [_attr("SCEN-1", "FEAT-1")])
    env = _add_idea_env(store)
    fixtures = tmp_path / "fixtures"
    insight_fields = {
        "precondition": "A web target is being implemented",
        "action": "Wire the create-form submit handler to the API before UI",
        "expected_outcome": "Creating an entity persists and reloads the list",
    }
    _record_new_skill_fixture(store, fixtures, insight_fields, env.agent_id)

    register_fn = sb.make_add_idea_registrar(
        store, env.vec, env.embedder, CFG, judge_fixtures_dir=fixtures
    )
    result = sb.run_stage_b(
        store, sar, register_fn=register_fn, episode_id=ep,
        judge_fn=_judge_returning(_reflection_output(insight=insight_fields)),
    )
    assert len(result.registered) == 1
    prov = result.registered[0]
    # Provenance chain: episode -> cluster -> scenario -> insight.
    assert prov.scen_ids == ("SCEN-1",)
    assert prov.feat_id == "FEAT-1"
    assert result.batch_label == f"reflect-ep{ep}"
    # The insight landed quarantined and batch-tagged.
    row = store.conn.execute(
        "SELECT status, batch_id FROM insights WHERE id = ?", (prov.insight_id,)
    ).fetchone()
    assert row["status"] == "quarantined"
    batch = store.conn.execute(
        "SELECT label FROM batches WHERE id = ?", (row["batch_id"],)
    ).fetchone()
    assert batch["label"] == f"reflect-ep{ep}"


def test_full_episode_every_insight_passes_registration(store, tmp_path):
    """Verification: a full fixture episode yields a batch whose every insight
    passes registration (one cluster lessons, one declares no-lesson)."""
    ep = store.create_episode("linkding", "sha256:x", 0)
    _scen_row(store, "SCEN-1", "FEAT-1", tier="must", episode_id=ep)
    _scen_row(store, "SCEN-2", "FEAT-2", tier="should", episode_id=ep)
    attrs = [
        _attr("SCEN-1", "FEAT-1", role="worker", aspect="implementation"),
        _attr("SCEN-2", "FEAT-2", role="planner", aspect="coverage"),
    ]
    sar = _stage_a_result(ep, attrs)
    env = _add_idea_env(store)
    fixtures = tmp_path / "fixtures"
    insight_fields = {
        "precondition": "A web target is being implemented",
        "action": "Wire the create-form submit handler to the API before UI",
        "expected_outcome": "Creating an entity persists and reloads the list",
    }
    _record_new_skill_fixture(store, fixtures, insight_fields, env.agent_id)

    # FEAT-1's cluster lessons; FEAT-2's declares no lesson (by failure signature).
    def route(prompt):
        if "FEAT-1" in prompt:
            return _reflection_output(insight=insight_fields)
        return _reflection_output(has_lesson=False)

    register_fn = sb.make_add_idea_registrar(
        store, env.vec, env.embedder, CFG, judge_fixtures_dir=fixtures
    )
    result = sb.run_stage_b(
        store, sar, register_fn=register_fn, episode_id=ep,
        judge_fn=_judge_returning(route),
    )
    assert not result.is_empty
    assert len(result.registered) == 1
    assert len(result.no_lesson_cluster_ids) == 1
    # Every registered insight is a real, quarantined row.
    for prov in result.registered:
        row = store.conn.execute(
            "SELECT status FROM insights WHERE id = ?", (prov.insight_id,)
        ).fetchone()
        assert row is not None and row["status"] == "quarantined"


def test_reflector_exposes_stage_b():
    assert sb.run_stage_b is not None and sb.REFLECTION_SCHEMA["type"] == "object"
