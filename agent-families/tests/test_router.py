"""Family router + routing-decision log tests (plan-005 U4, R12).

Fully offline: the judge is a scripted fake (no subprocess / quota), vectors are
hand-written, and the routing-decision log is the module's self-owned table.

## Conformance

R12 test-scenario -> test:

- routing decisions logged with full candidates ->
  ``test_routing_decision_logged_with_candidates``
- the chosen agent's routing_decisions counter increments ->
  ``test_route_increments_chosen_agent_counter``
- the routing-replay substrate (request vector + top skill) is recorded ->
  ``test_route_records_replay_substrate``
- near-tied confidence flags an ambiguous routing decision ->
  ``test_ambiguous_routing_flagged`` (the R14c boundary signal)
- a single generic agent still logs its (trivial) decision ->
  ``test_single_agent_route_still_logged``
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_families.library.router import (
    ROUTING_LOG_TABLE,
    RouterError,
    RouterParams,
    is_boundary_ticket,
    logged_decisions_for_agent,
    route,
    routing_decision_count,
)
from agent_families.store import Store


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    family_id = store.create_family("worker")
    agent_a = store.create_agent(family_id, "alpha-spec", description="handles A")
    agent_b = store.create_agent(family_id, "beta-spec", description="handles B")
    yield SimpleNamespace(
        store=store, family_id=family_id, agent_a=agent_a, agent_b=agent_b
    )
    store.close()


def fake_judge(chosen_name, confidence=0.9, runner_up=0.1):
    """A scripted routing judge returning a fixed choice + confidences."""

    def _judge(prompt, schema, model, **kwargs):
        # The schema's enum must contain the choice (the closed-schema contract).
        assert chosen_name in schema["properties"]["chosen_agent"]["enum"]
        return SimpleNamespace(
            output={
                "chosen_agent": chosen_name,
                "confidence": confidence,
                "runner_up_confidence": runner_up,
            }
        )

    return _judge


# --- R12: every routing decision is logged with full candidates ---------------


def test_routing_decision_logged_with_candidates(env):
    decision = route(
        env.store, family_id=env.family_id, request="build the A thing",
        judge_fn=fake_judge("beta-spec", confidence=0.8, runner_up=0.2),
    )
    assert decision.chosen_agent_id == env.agent_b
    assert decision.candidate_agent_ids == (env.agent_a, env.agent_b)

    row = env.store.conn.execute(
        f"SELECT * FROM {ROUTING_LOG_TABLE} WHERE id = ?", (decision.decision_id,)
    ).fetchone()
    assert json.loads(row["candidates_json"]) == [env.agent_a, env.agent_b]
    assert row["chosen_agent_id"] == env.agent_b
    assert row["confidence"] == pytest.approx(0.8)
    assert row["request"] == "build the A thing"


def test_route_increments_chosen_agent_counter(env):
    route(env.store, family_id=env.family_id, request="r1",
          judge_fn=fake_judge("alpha-spec"))
    route(env.store, family_id=env.family_id, request="r2",
          judge_fn=fake_judge("alpha-spec"))
    route(env.store, family_id=env.family_id, request="r3",
          judge_fn=fake_judge("beta-spec"))

    a_count = env.store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (env.agent_a,)
    ).fetchone()["routing_decisions"]
    b_count = env.store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (env.agent_b,)
    ).fetchone()["routing_decisions"]
    assert a_count == 2
    assert b_count == 1
    assert routing_decision_count(env.store, env.agent_a) == 2
    assert routing_decision_count(env.store, env.agent_b) == 1


def test_route_records_replay_substrate(env):
    # Skill-affinity substrate: skill 100 near the request, 200 far. The router
    # records the request embedding + the top-affinity skill for split replay.
    skill_vectors = {100: [1.0, 0.0, 0.0, 0.0], 200: [0.0, 1.0, 0.0, 0.0]}
    decision = route(
        env.store, family_id=env.family_id, request="A-shaped request",
        judge_fn=fake_judge("alpha-spec"),
        request_vector=[1.0, 0.0, 0.0, 0.0], skill_vectors=skill_vectors,
    )
    assert decision.top_skill_id == 100
    row = env.store.conn.execute(
        f"SELECT request_vector_json, top_skill_id FROM {ROUTING_LOG_TABLE}"
        " WHERE id = ?", (decision.decision_id,)
    ).fetchone()
    assert json.loads(row["request_vector_json"]) == [1.0, 0.0, 0.0, 0.0]
    assert row["top_skill_id"] == 100


# --- the ambiguity signal (R14c boundary trigger) -----------------------------


def test_ambiguous_routing_flagged(env):
    # Near-tied top-two confidence -> ambiguous (the boundary signal).
    ambiguous = route(
        env.store, family_id=env.family_id, request="cross-cutting",
        judge_fn=fake_judge("alpha-spec", confidence=0.55, runner_up=0.50),
    )
    assert ambiguous.ambiguous is True
    # A clear winner -> not ambiguous.
    clear = route(
        env.store, family_id=env.family_id, request="clearly A",
        judge_fn=fake_judge("alpha-spec", confidence=0.95, runner_up=0.10),
    )
    assert clear.ambiguous is False


def test_single_agent_route_still_logged(tmp_path):
    # The Phase 3a reality: one generic agent. Routing is trivial but still logged
    # — that is how the log accumulates toward the split gate.
    store = Store(tmp_path / "lib.db")
    store.migrate()
    fam = store.create_family("worker")
    agent = store.create_agent(fam, "generalist")
    decision = route(store, family_id=fam, request="anything")
    assert decision.chosen_agent_id == agent
    assert decision.confidence == 1.0
    assert decision.ambiguous is False
    assert len(logged_decisions_for_agent(store, agent)) == 1
    store.close()


# --- boundary detection (R14c) + param validation -----------------------------


def test_is_boundary_ticket_trigger():
    # Spanning >=2 clusters OR ambiguous routing -> boundary.
    assert is_boundary_ticket(agent_clusters={1, 2}, routing_ambiguous=False) is True
    assert is_boundary_ticket(agent_clusters={1}, routing_ambiguous=True) is True
    # A single cluster + confident routing -> NOT a boundary (rare by construction).
    assert is_boundary_ticket(agent_clusters={1}, routing_ambiguous=False) is False
    assert is_boundary_ticket(agent_clusters=set(), routing_ambiguous=False) is False


def test_router_params_validate():
    with pytest.raises(RouterError, match="ambiguity_margin"):
        RouterParams(ambiguity_margin=1.5)
    with pytest.raises(RouterError, match="boundary_min_clusters"):
        RouterParams(boundary_min_clusters=1)
    with pytest.raises(RouterError, match="max_cross_passes"):
        RouterParams(max_cross_passes=0)


def test_route_empty_family_raises(tmp_path):
    store = Store(tmp_path / "lib.db")
    store.migrate()
    fam = store.create_family("empty")
    with pytest.raises(RouterError, match="no routable agents"):
        route(store, family_id=fam, request="x")
    store.close()
