"""Agent-split tests (plan-005 U4, R13/R14).

Fully offline: skill-description embeddings come through a deterministic fake seam,
clustering/silhouette is pure arithmetic, the contrastive describer is a fake, and
the routing-decision log is seeded through the real router (single-candidate path,
no judge). Zero quota, no ``claude`` on PATH.

## Conformance

§6 / R13 test-scenario -> test:

- split candidacy refuses below the decision-count gate ->
  ``test_split_candidacy_gated_by_decision_count``
- a two-planted-cluster agent splits with >=90% replay agreement ->
  ``test_two_cluster_agent_splits_with_replay_agreement``
- replay below threshold rejects and preserves the parent ->
  ``test_low_replay_agreement_rejects_and_preserves_parent``
- a reverted split restores routing identically -> ``test_reverted_split_restores_routing``
- residue in a base prompt blocks the split with the conversion warning ->
  ``test_base_prompt_residue_blocks_split``
- children inherit correct skill partitions incl. borderline members ->
  ``test_children_inherit_skill_partitions``
- a fixture split survives the full transaction incl. a benchmark-pass gate ->
  ``test_two_cluster_agent_splits_with_replay_agreement`` /
  ``test_split_reverted_on_benchmark_fail``
- the <=25-word compressibility gate blocks a bloated description ->
  ``test_compressibility_gate_blocks_bloated_description``
- low silhouette / undersized clusters reject -> ``test_low_silhouette_rejects`` /
  ``test_undersized_cluster_rejects``
- R14 explorer-family decision is documented seed-or-sink ->
  ``test_explorer_family_decision_sink`` / ``test_explorer_family_decision_seeds``
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_families.library.router import ROUTING_LOG_TABLE, ensure_routing_log, route
from agent_families.reflector.agent_split import (
    AgentSplitError,
    AgentSplitParams,
    agent_split_candidates,
    execute_agent_split,
    explorer_family_decision,
    plan_agent_split,
    revert_agent_split,
    run_agent_split,
)
from agent_families.library.router import family_candidates
from agent_families.store import Store

NEAR_A = [1.0, 0.0, 0.0, 0.0]
NEAR_B = [0.0, 1.0, 0.0, 0.0]


def embed(text: str):
    """Deterministic fake: a skill description's cluster is encoded in its text."""
    t = text.lower()
    if "alpha" in t:
        return [1.0, 0.0, 0.0, 0.0]
    if "beta" in t:
        return [0.0, 1.0, 0.0, 0.0]
    if "border" in t:
        return [0.6, 0.6, 0.0, 0.0]
    return [0.0, 0.0, 1.0, 0.0]


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    family_id = store.create_family("worker")
    yield SimpleNamespace(store=store, family_id=family_id)
    store.close()


def _agent_with_skills(env, descriptions, *, base_prompt=""):
    agent = env.store.create_agent(
        env.family_id, "generalist", base_prompt_specialty=base_prompt
    )
    skill_ids = []
    for i, desc in enumerate(descriptions):
        skill_ids.append(env.store.create_skill(agent, f"skill-{i}", desc))
    return agent, skill_ids


def _seed_routing(env, agent, skill_ids, *, request_vector, n):
    skill_vectors = {
        sid: embed(
            env.store.conn.execute(
                "SELECT description FROM skills WHERE id = ?", (sid,)
            ).fetchone()["description"]
        )
        for sid in skill_ids
    }
    for _ in range(n):
        route(
            env.store, family_id=env.family_id, request="seeded request",
            request_vector=request_vector, skill_vectors=skill_vectors,
        )


# === candidacy gate (§6) =======================================================


def test_split_candidacy_gated_by_decision_count(env):
    agent, _ = _agent_with_skills(env, ["alpha a", "alpha b", "beta c", "beta d"])
    params = AgentSplitParams(min_routing_decisions=5, min_cluster_skills=2)

    # Below the decision-count gate -> not a candidate.
    env.store.conn.execute(
        "UPDATE agents SET routing_decisions = 2 WHERE id = ?", (agent,)
    )
    assert agent_split_candidates(env.store, env.family_id, params) == []

    # At/above the gate -> a candidate.
    env.store.conn.execute(
        "UPDATE agents SET routing_decisions = 5 WHERE id = ?", (agent,)
    )
    assert agent_split_candidates(env.store, env.family_id, params) == [agent]


# === the full transaction: split + replay + benchmark (§6) =====================


def test_two_cluster_agent_splits_with_replay_agreement(env):
    agent, skill_ids = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"]
    )
    _seed_routing(env, agent, skill_ids, request_vector=NEAR_A, n=3)
    _seed_routing(env, agent, skill_ids, request_vector=NEAR_B, n=3)

    params = AgentSplitParams(min_routing_decisions=4, min_cluster_skills=2)
    outcome = run_agent_split(
        env.store, agent, embed_fn=embed, benchmark_fn=lambda: True, params=params
    )

    assert outcome.executed is True
    assert outcome.confirmed is True
    assert outcome.reverted is False
    assert outcome.replay.agreement == pytest.approx(1.0)

    # The parent is a frozen lineage anchor (retired, never deleted).
    parent = env.store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (agent,)
    ).fetchone()
    assert parent["lineage_status"] == "retired_by_split"

    # Two live children carry parent_id; their skills partition the parent's.
    c0, c1 = outcome.result.child_agent_ids
    for cid in (c0, c1):
        row = env.store.conn.execute(
            "SELECT parent_id, lineage_status FROM agents WHERE id = ?", (cid,)
        ).fetchone()
        assert row["parent_id"] == agent
        assert row["lineage_status"] is None  # confirmed -> live

    members0 = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (c0,)
        ).fetchall()
    }
    members1 = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (c1,)
        ).fetchall()
    }
    assert members0.isdisjoint(members1)
    assert members0 | members1 == set(skill_ids)
    # Routing now goes to the children, not the retired parent.
    candidates = {c.agent_id for c in family_candidates(env.store, env.family_id)}
    assert candidates == {c0, c1}


def test_low_replay_agreement_rejects_and_preserves_parent(env):
    agent, skill_ids = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"]
    )
    beta_skill = skill_ids[2]  # a beta-cluster skill

    # Seed decisions whose request points at A but whose served skill is in B:
    # replay must DISAGREE on every one (0% agreement).
    ensure_routing_log(env.store)
    for _ in range(6):
        env.store.conn.execute(
            f"INSERT INTO {ROUTING_LOG_TABLE} (family_id, request_hash, request,"
            " candidates_json, chosen_agent_id, confidence, request_vector_json,"
            " top_skill_id, snapshot_id, created_at)"
            " VALUES (?, 'h', 'req', ?, ?, 1.0, ?, ?, 0, datetime('now'))",
            (env.family_id, json.dumps([agent]), agent,
             json.dumps(NEAR_A), beta_skill),
        )
    env.store.conn.execute(
        "UPDATE agents SET routing_decisions = 6 WHERE id = ?", (agent,)
    )

    params = AgentSplitParams(min_routing_decisions=4, min_cluster_skills=2)
    outcome = run_agent_split(
        env.store, agent, embed_fn=embed, benchmark_fn=lambda: True, params=params
    )

    assert outcome.executed is True
    assert outcome.reverted is True
    assert outcome.confirmed is False
    assert outcome.replay.agreement < params.replay_agreement_threshold
    # Parent preserved: skills back on it, lineage cleared, no children remain.
    parent = env.store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (agent,)
    ).fetchone()
    assert parent["lineage_status"] is None
    parent_skills = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (agent,)
        ).fetchall()
    }
    assert parent_skills == set(skill_ids)
    children = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM agents WHERE parent_id = ?", (agent,)
    ).fetchone()["n"]
    assert children == 0


def test_reverted_split_restores_routing(env):
    agent, skill_ids = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"]
    )
    before = [c.agent_id for c in family_candidates(env.store, env.family_id)]

    plan = plan_agent_split(env.store, agent, embed_fn=embed)
    assert plan.accepted is True
    result = execute_agent_split(env.store, plan)
    # Mid-split, routing has moved to the children (parent retired).
    assert agent not in [
        c.agent_id for c in family_candidates(env.store, env.family_id)
    ]

    revert_agent_split(env.store, result)
    after = [c.agent_id for c in family_candidates(env.store, env.family_id)]
    assert after == before  # routing restored identically
    parent_skills = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (agent,)
        ).fetchall()
    }
    assert parent_skills == set(skill_ids)


def test_split_reverted_on_benchmark_fail(env):
    agent, skill_ids = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"]
    )
    _seed_routing(env, agent, skill_ids, request_vector=NEAR_A, n=3)
    _seed_routing(env, agent, skill_ids, request_vector=NEAR_B, n=3)

    params = AgentSplitParams(min_routing_decisions=4, min_cluster_skills=2)
    outcome = run_agent_split(
        env.store, agent, embed_fn=embed, benchmark_fn=lambda: False, params=params
    )
    # Replay passed but the benchmark gate failed -> reverted, parent preserved.
    assert outcome.replay.agreement == pytest.approx(1.0)
    assert outcome.reverted is True
    assert outcome.confirmed is False
    parent = env.store.conn.execute(
        "SELECT lineage_status FROM agents WHERE id = ?", (agent,)
    ).fetchone()
    assert parent["lineage_status"] is None


# === the description gates (§6) ================================================


def test_base_prompt_residue_blocks_split(env):
    agent, _ = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"],
        base_prompt="GENERIC-PARENT-PROMPT",
    )

    def residue_describer(parent, names0, names1):
        # child 0 carries the parent's base prompt verbatim — residue.
        return (
            ("c0", "spec a", "GENERIC-PARENT-PROMPT"),
            ("c1", "spec b", "You specialize in b."),
        )

    plan = plan_agent_split(
        env.store, agent, embed_fn=embed, describer_fn=residue_describer
    )
    assert plan.accepted is False
    assert "residue" in plan.reason
    assert "conversion warning" in plan.reason


def test_compressibility_gate_blocks_bloated_description(env):
    agent, _ = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four"]
    )

    def bloated_describer(parent, names0, names1):
        long_desc = " ".join(f"word{i}" for i in range(30))  # > 25 words
        return (
            ("c0", long_desc, "You specialize in a."),
            ("c1", "spec b", "You specialize in b."),
        )

    plan = plan_agent_split(
        env.store, agent, embed_fn=embed, describer_fn=bloated_describer,
        params=AgentSplitParams(max_description_words=25),
    )
    assert plan.accepted is False
    assert "compressibility" in plan.reason


def test_low_silhouette_rejects(env):
    # Every skill embeds to the same blob -> no separable structure.
    agent, _ = _agent_with_skills(
        env, ["blob one", "blob two", "blob three", "blob four"]
    )
    plan = plan_agent_split(env.store, agent, embed_fn=embed)
    assert plan.accepted is False
    assert "silhouette" in plan.reason


def test_undersized_cluster_rejects(env):
    # Three A and one B: the B child would be a singleton (< min_cluster_skills).
    agent, _ = _agent_with_skills(
        env, ["alpha one", "alpha two", "alpha three", "beta four"]
    )
    plan = plan_agent_split(
        env.store, agent, embed_fn=embed,
        params=AgentSplitParams(min_cluster_skills=2),
    )
    assert plan.accepted is False
    assert "undersized" in plan.reason


def test_children_inherit_skill_partitions(env):
    # Two A, two B, and one borderline skill: the partition is exhaustive and
    # disjoint over ALL skills, the borderline one following its nearest centroid.
    agent, skill_ids = _agent_with_skills(
        env, ["alpha one", "alpha two", "beta three", "beta four", "border five"]
    )
    params = AgentSplitParams(silhouette_threshold=0.0, min_cluster_skills=2)
    plan = plan_agent_split(env.store, agent, embed_fn=embed, params=params)
    assert plan.accepted is True
    result = execute_agent_split(env.store, plan, params=params)

    c0, c1 = result.child_agent_ids
    members0 = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (c0,)
        ).fetchall()
    }
    members1 = {
        r["id"] for r in env.store.conn.execute(
            "SELECT id FROM skills WHERE agent_id = ?", (c1,)
        ).fetchall()
    }
    assert members0.isdisjoint(members1)
    assert members0 | members1 == set(skill_ids)  # exhaustive
    border = skill_ids[4]
    assert (border in members0) ^ (border in members1)  # in exactly one child


# === guards + R14 explorer-family decision =====================================


def test_execute_rejects_unaccepted_plan(env):
    agent, _ = _agent_with_skills(env, ["blob one", "blob two", "blob three"])
    plan = plan_agent_split(env.store, agent, embed_fn=embed)
    assert plan.accepted is False
    with pytest.raises(AgentSplitError, match="cannot execute an unaccepted plan"):
        execute_agent_split(env.store, plan)


def test_agent_split_params_validate():
    with pytest.raises(AgentSplitError, match="silhouette_threshold"):
        AgentSplitParams(silhouette_threshold=2.0)
    with pytest.raises(AgentSplitError, match="replay_agreement_threshold"):
        AgentSplitParams(replay_agreement_threshold=1.5)
    with pytest.raises(AgentSplitError, match="min_cluster_skills"):
        AgentSplitParams(min_cluster_skills=0)


def test_explorer_family_decision_sink():
    # No idea-shaped volume -> the sink remains (the Phase 3a reality).
    decision = explorer_family_decision(2, threshold=5)
    assert decision.seed is False
    assert "sink remains" in decision.reason


def test_explorer_family_decision_seeds():
    # Recurring explorer-shaped lessons at/above the threshold -> seed.
    decision = explorer_family_decision(7, threshold=5)
    assert decision.seed is True
    assert "seed explorer family" in decision.reason
