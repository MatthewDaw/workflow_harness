"""plan-004 U8: Ratchet governance and the first self-reorganization (R19-R21).

Fully offline (the default suite): fitness events are written and reconstructed
through the store's append-only log, the split's clustering/silhouette is pure
deterministic arithmetic, and the thematic-split / child-naming calls are injected
seams. Zero quota, no ``claude`` on PATH. The ratchet/split mutations drive the
real store queue.

## Conformance

Required acceptance tests (plan-004 U8) -> the invariant each enforces:

- ``test_loss_requires_causal_blame`` — an insight rendered into a FAILED run's
  context but absent from ``implicated_existing_insights`` receives ZERO loss (no
  bystander punishment).
- ``test_win_requires_done_and_unimplicated`` — a win is recorded only when the
  session's ticket reaches ``done`` AND is unimplicated in any failed SCEN chain.
- ``test_trial_benchmark_fitness_excluded`` — fitness events from ``trial`` /
  ``benchmark`` modes never feed the ratchet (separate channel).

Test-scenario / invariant (plan-004 U8) -> test:

- rendered-but-failed insight gets no loss without causal blame:
  ``test_loss_requires_causal_blame``
- win requires done + unimplicated: ``test_win_requires_done_and_unimplicated``
- tournament displaces weakest, ties favor incumbents, displaced go dormant:
  ``test_tournament_displaces_weakest_ties_favor_incumbents``
- retirement respects the event-log reconstruction:
  ``test_retirement_respects_event_log_reconstruction``
- split on a 30-insight fixture skill produces two children with
  provenance-correct membership incl. quarantined follow-the-centroid:
  ``test_split_produces_two_children_with_provenance``
- split deferred while a batch is mid-validation:
  ``test_split_deferred_while_batch_mid_validation``
- benchmark/trial fitness channels never feed the ratchet:
  ``test_trial_benchmark_fitness_excluded``
- property: any maintenance sequence preserves insights-never-deleted,
  vec-rows-untouched, membership-changes-snapshot-keyed:
  ``test_maintenance_preserves_invariants``
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_families.reflector import maintenance as m
from agent_families.store import Store
from agent_families.vecindex import VEC_TABLE, VecIndex

DIM = 4


# --- fixtures + builders --------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def env(tmp_path):
    """A store + vec index + one family/agent — the split tests need embeddings."""
    s = Store(tmp_path / "library.db")
    s.migrate()
    vec = VecIndex(s, DIM)
    vec.migrate()
    family_id = s.create_family("worker")
    agent_id = s.create_agent(family_id, "generalist")
    ns = SimpleNamespace(store=s, vec=vec, family_id=family_id, agent_id=agent_id, n=0)
    try:
        yield ns
    finally:
        s.close()


def _insert_insight(store: Store, *, status: str = "active", batch_id=None) -> int:
    """A bare insight (no membership / vec). Birth status = ``status`` (no
    transition), so ``status_at`` returns it at any snapshot."""
    _insert_insight.counter += 1  # type: ignore[attr-defined]
    n = _insert_insight.counter  # type: ignore[attr-defined]
    return store.insert_insight(
        precondition=f"pre {n}",
        action=f"act {n}",
        expected_outcome=f"out {n}",
        content_hash=f"hash-{n}",
        status=status,
        batch_id=batch_id,
    )


_insert_insight.counter = 0  # type: ignore[attr-defined]


def _insert_member_vec(env, skill_id, *, vector, status="active") -> int:
    """Insight + vec row + membership; birth status = ``status``."""
    env.n += 1
    n = env.n
    with env.store.transaction():
        iid = env.store.insert_insight(
            precondition=f"pre {n}",
            action=f"act {n}",
            expected_outcome=f"out {n}",
            content_hash=f"vec-hash-{n}",
            status=status,
        )
        env.vec.insert(iid, vector)
        env.store.append_member(skill_id, iid)
    return iid


def _add_member(store: Store, skill_id: int, insight_id: int) -> None:
    with store.transaction():
        store.append_member(skill_id, insight_id)


def _set_fitness(store: Store, insight_id: int, *, wins=0, losses=0, retrievals=0,
                 mode="training", episode_id=None, snapshot_id=0) -> None:
    for _ in range(retrievals):
        store.record_fitness_event(insight_id, "retrieval", mode, snapshot_id,
                                   episode_id=episode_id)
    for _ in range(wins):
        store.record_fitness_event(insight_id, "win", mode, snapshot_id,
                                   episode_id=episode_id)
    for _ in range(losses):
        store.record_fitness_event(insight_id, "loss", mode, snapshot_id,
                                   episode_id=episode_id)


def _make_episode(store: Store, *, mode="training", snapshot_id=0) -> int:
    return store.create_episode("linkding", "digest", snapshot_id, mode=mode)


def _make_ticket(store: Store, ticket_id: str, status: str) -> None:
    store.conn.execute(
        "INSERT INTO trace_tkt (id, status) VALUES (?, ?)", (ticket_id, status)
    )


def _status(store: Store, insight_id: int) -> str:
    return store.get_insight(insight_id)["status"]


# === R19: settlement fitness writes ============================================


def test_loss_requires_causal_blame(store):
    """An insight rendered into a FAILED run's context but absent from causal blame
    gets ZERO loss; only the causally-blamed insight is charged (required)."""
    ep = _make_episode(store, mode="training")
    _make_ticket(store, "TKT-fail", "escalated")  # the run failed (not done)

    bystander = _insert_insight(store)
    blamed = _insert_insight(store)

    settlement = m.settle_fitness(
        store,
        episode_id=ep,
        renders=[m.SessionRender("TKT-fail", (bystander, blamed))],
        implicated_ticket_ids={"TKT-fail"},
        causal_blame_insight_ids={blamed},
    )

    # The bystander was rendered into the failing run but never causally blamed.
    assert m.fitness_score(store, bystander) == 0
    assert store.fitness_counts(bystander)["loss"] == 0
    assert store.fitness_counts(bystander)["retrieval"] == 1  # rendered, yes
    # Only the causally-blamed insight takes a loss.
    assert store.fitness_counts(blamed)["loss"] == 1
    assert m.fitness_score(store, blamed) == -1
    assert settlement.loss_count == 1


def test_win_requires_done_and_unimplicated(store):
    """A win is recorded only when the ticket is `done` AND unimplicated (required)."""
    ep = _make_episode(store, mode="training")
    _make_ticket(store, "TKT-done", "done")  # done + unimplicated -> win
    _make_ticket(store, "TKT-done-implicated", "done")  # done but implicated -> no win
    _make_ticket(store, "TKT-open", "in_progress")  # not done -> no win

    won = _insert_insight(store)
    done_but_implicated = _insert_insight(store)
    not_done = _insert_insight(store)

    m.settle_fitness(
        store,
        episode_id=ep,
        renders=[
            m.SessionRender("TKT-done", (won,)),
            m.SessionRender("TKT-done-implicated", (done_but_implicated,)),
            m.SessionRender("TKT-open", (not_done,)),
        ],
        implicated_ticket_ids={"TKT-done-implicated"},
    )

    assert store.fitness_counts(won)["win"] == 1
    assert m.fitness_score(store, won) == 1
    # Done but implicated in a failed SCEN chain -> no win.
    assert store.fitness_counts(done_but_implicated)["win"] == 0
    # Not done -> no win (every rendered insight still gets a retrieval event).
    assert store.fitness_counts(not_done)["win"] == 0
    assert store.fitness_counts(not_done)["retrieval"] == 1


def test_planner_session_without_ticket_gets_retrieval_no_win(store):
    """A ticket-less session (planning) earns retrieval but never a win (R19)."""
    ep = _make_episode(store, mode="training")
    planned = _insert_insight(store)
    m.settle_fitness(
        store, episode_id=ep,
        renders=[m.SessionRender(None, (planned,))],
    )
    assert store.fitness_counts(planned)["retrieval"] == 1
    assert store.fitness_counts(planned)["win"] == 0


def test_trial_benchmark_fitness_excluded(store):
    """trial/benchmark fitness events never feed the ratchet's training channel
    (required) — they land in their own mode-keyed channel."""
    for mode in ("trial", "benchmark"):
        ep = _make_episode(store, mode=mode)
        _make_ticket(store, f"TKT-{mode}", "done")
        iid = _insert_insight(store)
        m.settle_fitness(
            store, episode_id=ep,
            renders=[m.SessionRender(f"TKT-{mode}", (iid,))],
            causal_blame_insight_ids={iid},
        )
        # The events exist in the mode's own channel...
        assert store.fitness_counts(iid, mode=mode)["retrieval"] == 1
        assert store.fitness_counts(iid, mode=mode)["win"] == 1
        assert store.fitness_counts(iid, mode=mode)["loss"] == 1
        # ...but the ratchet (training channel) sees nothing.
        assert m.fitness_score(store, iid) == 0
        assert store.fitness_counts(iid, mode="training")["retrieval"] == 0
        # ...so it is never a retirement candidate from validation traffic.
        assert iid not in m.retirement_candidates(store, 0, m.MaintenanceParams())


# === R20: outcome-driven retirement ============================================


def test_retirement_respects_event_log_reconstruction(store):
    """Retirement demotes only exposed, net-non-positive insights — read purely
    from the reconstructed fitness log (R20, §4)."""
    params = m.MaintenanceParams(retirement_min_retrievals=5,
                                 retirement_max_net_fitness=0)

    loser = _insert_insight(store)        # exposed, net negative -> retire
    _set_fitness(store, loser, retrievals=6, wins=1, losses=3)

    winner = _insert_insight(store)       # exposed, net positive -> keep
    _set_fitness(store, winner, retrievals=6, wins=5, losses=1)

    untested = _insert_insight(store)     # net negative but under-exposed -> keep
    _set_fitness(store, untested, retrievals=2, wins=0, losses=2)

    cands = m.retirement_candidates(store, 0, params)
    assert loser in cands
    assert winner not in cands
    assert untested not in cands

    result = m.run_maintenance(store, params=params)
    assert result.retired_insight_ids == (loser,)
    assert _status(store, loser) == "dormant"   # preserved, not deleted
    assert _status(store, winner) == "active"
    assert _status(store, untested) == "active"
    # Retirement minted exactly one snapshot (the queue op).
    assert result.minted_snapshot is True


def test_retirement_reconstruction_is_snapshot_scoped(store):
    """fitness_score reads the event log at a snapshot — later events do not
    rewrite the earlier reconstruction (append-only)."""
    iid = _insert_insight(store)
    _set_fitness(store, iid, wins=2, snapshot_id=1)
    _set_fitness(store, iid, losses=5, snapshot_id=3)
    assert m.fitness_score(store, iid, snapshot_id=1) == 2     # only the early wins
    assert m.fitness_score(store, iid, snapshot_id=3) == -3    # wins - all losses
    assert m.fitness_score(store, iid) == -3                   # all events


# === R20: cap tournament =======================================================


def test_tournament_displaces_weakest_ties_favor_incumbents(store):
    """Over-cap admission displaces the weakest by fitness; a fitness tie displaces
    the new admission (incumbents win ties); the displaced go dormant (R20)."""
    params = m.MaintenanceParams(active_cap=2)
    family = store.create_family("fam")

    # Agent 1: weakest incumbent is displaced (admission is strong enough to stay).
    a1 = store.create_agent(family, "a1")
    s1 = store.create_skill(a1, "s1")
    inc_strong = _insert_insight(store); _set_fitness(store, inc_strong, wins=5)
    inc_weak = _insert_insight(store); _set_fitness(store, inc_weak, wins=1)
    adm1 = _insert_insight(store); _set_fitness(store, adm1, wins=5)
    for iid in (inc_strong, inc_weak, adm1):
        _add_member(store, s1, iid)

    # Agent 2: a three-way fitness tie -> the admission is the one displaced.
    a2 = store.create_agent(family, "a2")
    s2 = store.create_skill(a2, "s2")
    inc_a = _insert_insight(store); _set_fitness(store, inc_a, wins=5)
    inc_b = _insert_insight(store); _set_fitness(store, inc_b, wins=5)
    adm2 = _insert_insight(store); _set_fitness(store, adm2, wins=5)
    for iid in (inc_a, inc_b, adm2):
        _add_member(store, s2, iid)

    result = m.run_maintenance(
        store, admitted_insight_ids={adm1, adm2}, params=params
    )

    assert set(result.displaced_insight_ids) == {inc_weak, adm2}
    assert _status(store, inc_weak) == "dormant"   # weakest displaced
    assert _status(store, adm2) == "dormant"        # tie -> incumbents kept
    # Survivors stay active.
    for iid in (inc_strong, adm1, inc_a, inc_b):
        assert _status(store, iid) == "active"


def test_tournament_within_cap_is_a_noop(store):
    """An agent at or under the cap displaces nothing — no snapshot minted."""
    params = m.MaintenanceParams(active_cap=5)
    family = store.create_family("fam")
    agent = store.create_agent(family, "a")
    skill = store.create_skill(agent, "s")
    for _ in range(3):
        iid = _insert_insight(store); _add_member(store, skill, iid)
    result = m.run_maintenance(store, params=params)
    assert result.displaced_insight_ids == ()
    assert result.minted_snapshot is False


# === R21: skill split ==========================================================


def _two_cluster_skill(env, *, n_a=15, n_b=15):
    """A skill with ``n_a`` active members near [1,0,..] and ``n_b`` near [0,1,..],
    inserted A-first so the lower ids cluster around A. Returns (skill_id, a_ids,
    b_ids)."""
    skill = env.store.create_skill(env.agent_id, "big-skill", "a broad skill")
    a_ids, b_ids = [], []
    for i in range(n_a):
        a_ids.append(_insert_member_vec(env, skill, vector=[1.0, 0.0, i * 0.001, 0.0]))
    for i in range(n_b):
        b_ids.append(_insert_member_vec(env, skill, vector=[0.0, 1.0, 0.0, i * 0.001]))
    return skill, a_ids, b_ids


def test_split_produces_two_children_with_provenance(env):
    """A 30-member skill splits into two children with parent_skill_id +
    split_snapshot_id; quarantined members follow the nearest child centroid;
    the parent is emptied (frozen lineage anchor, never deleted) (R21)."""
    skill, a_ids, b_ids = _two_cluster_skill(env)
    # Two quarantined followers — one near each cluster — that do not vote.
    q_near_a = _insert_member_vec(env, skill, vector=[0.95, 0.05, 0.0, 0.0],
                                  status="quarantined")
    q_near_b = _insert_member_vec(env, skill, vector=[0.05, 0.95, 0.0, 0.0],
                                  status="quarantined")

    result = m.run_maintenance(env.store, do_split=True)

    assert len(result.splits) == 1
    split = result.splits[0]
    assert split.parent_skill_id == skill
    assert split.accepted_by == "kmeans"          # silhouette cleared the gate
    assert split.silhouette >= 0.3
    assert split.split_snapshot_id == result.snapshot_id

    child0, child1 = split.child_skill_ids
    rows = {
        cid: env.store.conn.execute(
            "SELECT parent_skill_id, split_snapshot_id, agent_id FROM skills"
            " WHERE id = ?", (cid,)
        ).fetchone()
        for cid in (child0, child1)
    }
    for cid, row in rows.items():
        assert row["parent_skill_id"] == skill          # lineage
        assert row["split_snapshot_id"] == result.snapshot_id
        assert row["agent_id"] == env.agent_id

    members0 = set(env.store.skill_members(child0))
    members1 = set(env.store.skill_members(child1))
    # Partition is exhaustive + disjoint over the active members.
    assert members0.isdisjoint(members1)
    assert (members0 | members1) == set(a_ids) | set(b_ids) | {q_near_a, q_near_b}

    # The A-cluster active insights landed together; q_near_a followed them.
    a_child = child0 if set(a_ids) <= members0 else child1
    b_child = child1 if a_child == child0 else child0
    assert set(a_ids) <= set(env.store.skill_members(a_child))
    assert set(b_ids) <= set(env.store.skill_members(b_child))
    assert q_near_a in set(env.store.skill_members(a_child))
    assert q_near_b in set(env.store.skill_members(b_child))

    # The parent is frozen empty (re-pointed), but never deleted.
    assert env.store.skill_members(skill) == []
    assert env.store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (skill,)
    ).fetchone() is not None


def test_split_below_observation_floor_does_not_split(env):
    """A skill under the minimum observation count is never split (R21 §6 R2)."""
    skill = env.store.create_skill(env.agent_id, "small", "few insights")
    for i in range(4):
        _insert_member_vec(env, skill, vector=[1.0, 0.0, i * 0.01, 0.0])
    params = m.MaintenanceParams(split_min_observations=10, split_insight_count=2)
    result = m.run_maintenance(env.store, params=params)
    assert result.splits == ()


def test_split_low_silhouette_falls_back_to_thematic(env):
    """When k-means silhouette is below threshold, the LLM thematic seam decides
    the partition (R21 §6)."""
    skill = env.store.create_skill(env.agent_id, "blob", "one blob")
    ids = [
        _insert_member_vec(env, skill, vector=[1.0, 0.0, i * 0.001, 0.0])
        for i in range(12)
    ]
    # A single tight blob has no natural 2-way structure -> low silhouette.
    captured = {}

    def thematic(active_ids, vectors):
        captured["called"] = True
        half = len(active_ids) // 2
        return active_ids[:half], active_ids[half:]

    params = m.MaintenanceParams(
        split_silhouette_threshold=0.99, split_insight_count=5
    )
    result = m.run_maintenance(
        env.store, params=params, thematic_split_fn=thematic
    )
    assert captured.get("called") is True
    assert len(result.splits) == 1
    assert result.splits[0].accepted_by == "thematic"
    members = (
        set(env.store.skill_members(result.splits[0].child_skill_ids[0]))
        | set(env.store.skill_members(result.splits[0].child_skill_ids[1]))
    )
    assert members == set(ids)


def test_split_deferred_while_batch_mid_validation(env):
    """The skill-split trigger is skipped while any batch is mid-validation (R21)."""
    skill, _, _ = _two_cluster_skill(env)
    # An open batch validation (verdict NULL) == mid-validation.
    batch_id = env.store.ensure_batch("reflect-ep1")
    env.store.insert_batch_validation(batch_id, snapshot_id=0)  # verdict NULL

    assert m.is_mid_validation(env.store) is True
    result = m.run_maintenance(env.store, do_split=True)
    assert result.split_deferred is True
    assert result.splits == ()
    # The skill is untouched — still one skill with all its members.
    assert len(env.store.skill_members(skill)) == 30
    assert result.minted_snapshot is False

    # Once the verdict is recorded, the split is no longer deferred.
    val_id = env.store.conn.execute(
        "SELECT id FROM batch_validations LIMIT 1"
    ).fetchone()["id"]
    env.store.set_batch_verdict(val_id, "promote")
    assert m.is_mid_validation(env.store) is False
    result2 = m.run_maintenance(env.store, do_split=True)
    assert result2.split_deferred is False
    assert len(result2.splits) == 1


# === R21: agent split is a gated stub ==========================================


def test_agent_split_is_deferred_stub(store):
    """Agent split always defers — it gates on min_routing_decisions, which only
    Plan 5's router can produce (R21)."""
    family = store.create_family("fam")
    agent = store.create_agent(family, "a")
    decision = m.maybe_agent_split(store, agent, m.MaintenanceParams())
    assert decision.triggered is False
    assert decision.routing_decisions == 0
    assert "min_routing_decisions" in decision.reason


# === verification: the property invariants =====================================


def test_maintenance_preserves_invariants(env):
    """Any maintenance sequence preserves: insights never deleted, vec rows
    untouched, every membership change snapshot-keyed (U8 verification)."""
    # A split-eligible two-cluster skill...
    skill, a_ids, b_ids = _two_cluster_skill(env)
    # ...plus a persistent loser and a cap-overflow pair under a second agent.
    family2 = env.store.create_family("fam2")
    agent2 = env.store.create_agent(family2, "a2")
    skill2 = env.store.create_skill(agent2, "s2")
    loser = _insert_member_vec(env, skill2, vector=[0.0, 0.0, 1.0, 0.0])
    _set_fitness(env.store, loser, retrievals=6, wins=0, losses=4)
    keep = _insert_member_vec(env, skill2, vector=[0.0, 0.0, 0.0, 1.0])
    _set_fitness(env.store, keep, retrievals=6, wins=6)

    insights_before = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    vec_before = env.vec.count()

    result = m.run_maintenance(env.store, do_split=True)

    # 1. Insights are never deleted.
    insights_after = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM insights"
    ).fetchone()["n"]
    assert insights_after == insights_before

    # 2. Vec rows are never touched.
    assert env.vec.count() == vec_before

    # 3. Every membership change is snapshot-keyed: the children born of the split
    #    carry the minted snapshot id.
    assert len(result.splits) == 1
    for cid in result.splits[0].child_skill_ids:
        row = env.store.conn.execute(
            "SELECT split_snapshot_id FROM skills WHERE id = ?", (cid,)
        ).fetchone()
        assert row["split_snapshot_id"] == result.snapshot_id

    # The loser retired to dormant (preserved); the split happened too.
    assert _status(env.store, loser) == "dormant"
    assert _status(env.store, keep) == "active"


def test_noop_maintenance_mints_no_snapshot(store):
    """A maintenance pass with nothing to do mints no snapshot (gapless chain)."""
    before = store.current_snapshot_id()
    result = m.run_maintenance(store)
    assert result.minted_snapshot is False
    assert result.snapshot_id == before
    assert store.current_snapshot_id() == before


# === pure helpers: k-means / silhouette / params ===============================


def test_kmeans2_separates_two_clusters():
    vectors = {
        1: [1.0, 0.0], 2: [1.1, 0.0], 3: [0.9, 0.0],
        4: [0.0, 1.0], 5: [0.0, 1.1], 6: [0.0, 0.9],
    }
    c0, c1 = m._kmeans2([1, 2, 3, 4, 5, 6], vectors)
    clusters = {frozenset(c0), frozenset(c1)}
    assert clusters == {frozenset({1, 2, 3}), frozenset({4, 5, 6})}
    assert m.silhouette_two(c0, c1, vectors) > 0.3


def test_silhouette_empty_cluster_is_minus_one():
    vectors = {1: [1.0, 0.0], 2: [1.1, 0.0]}
    assert m.silhouette_two([1, 2], [], vectors) == -1.0


def test_params_range_checks():
    with pytest.raises(m.MaintenanceError):
        m.MaintenanceParams(active_cap=0)
    with pytest.raises(m.MaintenanceError):
        m.MaintenanceParams(split_min_observations=1)
    with pytest.raises(m.MaintenanceError):
        m.MaintenanceParams(split_silhouette_threshold=2.0)


def test_settle_fitness_unknown_episode_raises(store):
    with pytest.raises(m.MaintenanceError):
        m.settle_fitness(store, episode_id=999, renders=[])
