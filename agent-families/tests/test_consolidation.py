"""Consolidation + usage-conditioned retirement — the two per-move deltas (plan-009 U5).

Fully offline, zero quota, deterministic: a real ``Store`` + ``VecIndex`` over
hand-placed angle vectors, no embedding model, no ``claude``, no native partitioner.
Both moves are scored as an isolated ``cost(G)`` delta over the v1 similarity flow
graph and adopted only when they strictly lower cost; consolidation demotes its
children to ``dormant`` (never retired) via plan-008's ``lifecycle.demote_to_dormant``
and links a ``generalizes_from`` edge from the synthesized ``provenance=consolidated``
parent to every child; retirement candidacy is a usage hazard, not the old
net-fitness counter.

## Conformance (U5 required acceptance tests → invariant)

| Invariant (plan-009 U5, R11/R12) | Test |
|---|---|
| after consolidation every child is `dormant` (via `lifecycle.demote_to_dormant`), zero `retired`, each row preserved + revivable | `test_consolidation_children_dormant_never_retired` |
| the parent has `provenance=consolidated` and a `generalizes_from` edge to EVERY child (edge count == child count) | `test_parent_carries_generalizes_from_to_all_children` |
| a consolidation with `cost_delta >= 0` is NOT applied: no parent minted, no child demoted, active set unchanged | `test_consolidation_rejected_when_cost_rises` |
| retirement candidacy is the usage hazard (NOT net-fitness): a usage-dead insight is demoted while a low-fitness-but-recently-used one survives; `settle_fitness` retained + called | `test_retirement_is_usage_conditioned_survival` |

Supporting (guard the API surface, not required):

- a coherent consolidation lowers cost and is adopted, minting exactly one snapshot:
  `test_consolidation_adopted_when_cost_lowers`
"""

from __future__ import annotations

import math

import pytest

from agent_families import lifecycle
from agent_families.reflector import consolidation as cons
from agent_families.reflector import maintenance as m
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
PARAMS = cons.ConsolidationParams(knn_k=3)


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    with store.transaction():
        family_id = store.create_family("library")
        agent_id = store.create_agent(family_id, "librarian")
    yield store, vec, agent_id
    store.close()


def _unit(deg: float) -> list[float]:
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0]


def _insert(store, vec, hint, angle, *, status="active"):
    v = _unit(angle)
    with store.transaction():
        iid = store.insert_insight(
            precondition=f"precondition {hint}",
            action=f"action {hint}",
            expected_outcome=f"outcome {hint}",
            content_hash=f"hash-{hint}",
            status=status,
        )
        vec.insert(iid, v, v)
    return iid


def _status(store, insight_id) -> str:
    return store.get_insight(insight_id)["status"]


def _snapshot_count(store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]


def _generalizes_from_edges(store, parent_id) -> list[int]:
    rows = store.conn.execute(
        "SELECT dst FROM insight_edges WHERE src = ? AND kind = 'generalizes_from'"
        " ORDER BY dst ASC",
        (parent_id,),
    ).fetchall()
    return [r["dst"] for r in rows]


# --- R11 adopt: a coherent consolidation lowers cost (supporting) ------------------


def test_consolidation_adopted_when_cost_lowers(env):
    """A tight, isolated community of specifics consolidates into one parent: the
    move strictly lowers cost(G) and is adopted, minting exactly one snapshot."""
    store, vec, _ = env
    # A tight cluster of 4 children near 0°, plus a far-away cluster — the children
    # form their own connected component, so contracting them to one parent shrinks
    # the store (fewer nodes / lower locate bits).
    children = [_insert(store, vec, f"c{i}", a) for i, a in enumerate((0, 3, 6, 9))]
    [_insert(store, vec, f"o{i}", a) for i, a in enumerate((180, 183, 186, 189))]

    before = _snapshot_count(store)
    result = cons.consolidate(store, vec, child_insight_ids=children, params=PARAMS)

    assert result.adopted is True
    assert result.cost_delta < 0
    assert result.parent_insight_id is not None
    assert _snapshot_count(store) - before == 1  # exactly one snapshot for the move


# --- R11: children go dormant, never retired, and stay revivable -------------------


def test_consolidation_children_dormant_never_retired(env):
    """Every child is demoted to `dormant` (via lifecycle.demote_to_dormant), none
    is `retired`, each child row is preserved and revivable."""
    store, vec, _ = env
    children = [_insert(store, vec, f"c{i}", a) for i, a in enumerate((0, 3, 6, 9))]
    [_insert(store, vec, f"o{i}", a) for i, a in enumerate((180, 183, 186, 189))]

    result = cons.consolidate(store, vec, child_insight_ids=children, params=PARAMS)
    assert result.adopted is True

    for child in children:
        assert _status(store, child) == "dormant"   # demoted, preserved
        assert _status(store, child) != "retired"   # never retired
        assert store.get_insight(child) is not None  # row still exists
    # dormant children are revivable (plan-008 R18) — a revive flips one back active.
    revived = lifecycle.revive_insight(store, children[0])
    assert revived.insight_ids == (children[0],)
    assert _status(store, children[0]) == "active"


# --- R11: the parent carries provenance + generalizes_from to ALL children ---------


def test_parent_carries_generalizes_from_to_all_children(env):
    """The synthesized parent has provenance=consolidated and one generalizes_from
    edge to EVERY demoted child (edge count == child count); no child is unlinked."""
    store, vec, _ = env
    children = [_insert(store, vec, f"c{i}", a) for i, a in enumerate((0, 3, 6, 9))]
    [_insert(store, vec, f"o{i}", a) for i, a in enumerate((180, 183, 186, 189))]

    result = cons.consolidate(store, vec, child_insight_ids=children, params=PARAMS)
    assert result.adopted is True

    parent = store.get_insight(result.parent_insight_id)
    assert parent["provenance"] == "consolidated"

    linked = _generalizes_from_edges(store, result.parent_insight_id)
    assert linked == sorted(children)              # one edge to every child
    assert len(linked) == len(children)            # count of edges == count of children
    assert result.generalizes_from_edges == len(children)


# --- R11 reject: a bridging consolidation raises cost → no write -------------------


def test_consolidation_rejected_when_cost_rises(env):
    """Consolidating specifics that belong to DISTINCT communities makes the parent
    bridge two modules into one larger module — cost(G) rises, so the move is
    rejected: no parent minted, no child demoted, the active set is unchanged."""
    store, vec, _ = env
    # Two separate dense clusters X (near 0°) and Y (near 120°). The two children are
    # one member of each cluster, so contracting them bridges X and Y → cost rises.
    x = [_insert(store, vec, f"x{i}", a) for i, a in enumerate((0, 3, 6, 9))]
    y = [_insert(store, vec, f"y{i}", a) for i, a in enumerate((120, 123, 126, 129))]
    children = [x[3], y[3]]  # one from each distinct community

    snaps_before = _snapshot_count(store)
    statuses_before = {iid: _status(store, iid) for iid in x + y}
    consolidated_before = store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights WHERE provenance = 'consolidated'"
    ).fetchone()["n"]

    result = cons.consolidate(store, vec, child_insight_ids=children, params=PARAMS)

    assert result.adopted is False
    assert result.cost_delta >= 0                  # the gate saw a non-improving move
    assert result.parent_insight_id is None
    assert result.snapshot_id is None
    # Nothing was written: no snapshot, no parent insight, no status change.
    assert _snapshot_count(store) == snaps_before
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights WHERE provenance = 'consolidated'"
    ).fetchone()["n"] == consolidated_before
    assert {iid: _status(store, iid) for iid in x + y} == statuses_before
    # No generalizes_from edge was minted either.
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM insight_edges WHERE kind = 'generalizes_from'"
    ).fetchone()["n"] == 0


# --- R12: retirement is usage-conditioned survival, not the net-fitness counter ----


def _make_episode(store, *, snapshot_id):
    return store.create_episode("linkding", "digest", snapshot_id, mode="training")


def _make_ticket(store, ticket_id, status):
    store.conn.execute(
        "INSERT INTO trace_tkt (id, status) VALUES (?, ?)", (ticket_id, status)
    )


def test_retirement_is_usage_conditioned_survival(env):
    """Candidacy is the usage hazard, NOT the old net-fitness counter: a usage-dead
    insight is demoted while a low-fitness-but-recently-used insight survives. The
    `settle_fitness` trace writer is retained and is what writes the usage events.

    Setup: snapshots advance to a recent S=8. `dead` earned its retrievals long ago
    (last use at snapshot 1) → usage-stale → candidate; `live` is loss-heavy (negative
    net fitness) but was retrieved at the current snapshot 8 → not stale → survives.
    """
    store, vec, _ = env
    params = m.SurvivalParams(min_retrievals=5, staleness_window=3, knn_k=3)

    # `dead`: an isolated, usage-dead insight (peripheral → its removal lowers cost).
    dead = _insert(store, vec, "dead", 300)
    # `live`: sits in a dense cluster, loss-heavy but recently used.
    live = _insert(store, vec, "live", 0)
    [_insert(store, vec, f"clu{i}", a) for i, a in enumerate((3, 6, 9))]

    # Old usage for `dead`: 5 retrievals all stamped at snapshot 1 (the trace writer).
    ep_old = _make_episode(store, snapshot_id=1)
    for _ in range(5):
        m.settle_fitness(
            store, episode_id=ep_old,
            renders=[m.SessionRender(None, (dead,))], snapshot_id=1,
        )
    # Recent usage for `live` at snapshot 8, PLUS heavy causal-blame losses (negative
    # net fitness) — the old counter would retire it; the usage hazard must not.
    ep_now = _make_episode(store, snapshot_id=8)
    _make_ticket(store, "TKT-fail", "escalated")
    for _ in range(5):
        m.settle_fitness(
            store, episode_id=ep_now,
            renders=[m.SessionRender("TKT-fail", (live,))],
            implicated_ticket_ids={"TKT-fail"},
            causal_blame_insight_ids={live},
            snapshot_id=8,
        )

    # `live` is loss-heavy (the old net-fitness counter WOULD flag it)...
    assert m.fitness_score(store, live, snapshot_id=8) < 0
    assert live in m.retirement_candidates(
        store, 8, m.MaintenanceParams(retirement_min_retrievals=5)
    )
    # ...but the usage hazard spares it (recent retrieval) and flags the usage-dead one.
    candidates = m.survival_retirement_candidates(store, 8, params)
    assert dead in candidates
    assert live not in candidates

    # The cost(G)-gated pass demotes the usage-dead insight and leaves `live` active.
    result = m.survival_retirement(store, vec, params=params, snapshot_id=8)
    assert result.minted_snapshot is True
    assert result.demoted_insight_ids == (dead,)
    assert _status(store, dead) == "dormant"        # demoted, preserved (revivable)
    assert _status(store, live) == "active"         # low fitness, recently used → survives


def test_survival_retirement_noop_mints_no_snapshot(env):
    """No usage-stale candidate → the pass mints zero snapshots (gapless chain)."""
    store, vec, _ = env
    params = m.SurvivalParams(min_retrievals=5, staleness_window=3, knn_k=3)
    fresh = _insert(store, vec, "fresh", 0)
    ep = _make_episode(store, snapshot_id=8)
    for _ in range(5):
        m.settle_fitness(
            store, episode_id=ep,
            renders=[m.SessionRender(None, (fresh,))], snapshot_id=8,
        )
    before = _snapshot_count(store)
    result = m.survival_retirement(store, vec, params=params, snapshot_id=8)
    assert result.minted_snapshot is False
    assert result.demoted_insight_ids == ()
    assert _snapshot_count(store) == before
