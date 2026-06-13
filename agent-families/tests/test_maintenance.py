"""plan-004 U8 (as demoted by plan-009 U7/R16): settlement fitness + retirement.

The R3 reform (plan-009 U7) removed the cap-tournament, the k-means/silhouette skill
split, and the agent-split stub from ``maintenance``. What this module still covers is
the surviving, load-bearing behavior: the **settlement fitness writes** (R19) and the
retained **net-fitness retirement baseline** (the documented contrast the R3 usage
hazard is compared against — that hazard itself is exercised in test_consolidation.py).

Fully offline (the default suite): fitness events are written and reconstructed
through the store's append-only log. Zero quota, no ``claude`` on PATH.

## Conformance

Required acceptance tests (plan-004 U8) -> the invariant each enforces:

- ``test_loss_requires_causal_blame`` — an insight rendered into a FAILED run's
  context but absent from ``implicated_existing_insights`` receives ZERO loss (no
  bystander punishment).
- ``test_win_requires_done_and_unimplicated`` — a win is recorded only when the
  session's ticket reaches ``done`` AND is unimplicated in any failed SCEN chain.
- ``test_trial_benchmark_fitness_excluded`` — fitness events from ``trial`` /
  ``benchmark`` modes never feed the ratchet (separate channel).

Test-scenario / invariant -> test:

- rendered-but-failed insight gets no loss without causal blame:
  ``test_loss_requires_causal_blame``
- win requires done + unimplicated: ``test_win_requires_done_and_unimplicated``
- retirement respects the event-log reconstruction:
  ``test_retirement_respects_event_log_reconstruction``
- benchmark/trial fitness channels never feed the ratchet:
  ``test_trial_benchmark_fitness_excluded``

(The cap-tournament / skill-split / agent-split scenarios moved out with the machinery
they tested — see plan-009 U7 and tests/test_demotions.py.)
"""

from __future__ import annotations

import pytest

from agent_families.reflector import maintenance as m
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


def test_settle_fitness_unknown_episode_raises(store):
    with pytest.raises(m.MaintenanceError):
        m.settle_fitness(store, episode_id=999, renders=[])


# === retirement: the net-fitness contrast baseline (R20 -> demoted by R12) ======


def test_retirement_respects_event_log_reconstruction(store):
    """The retained net-fitness baseline demotes only exposed, net-non-positive
    insights — read purely from the reconstructed fitness log (R20)."""
    params = m.MaintenanceParams(retirement_min_retrievals=5,
                                 retirement_max_net_fitness=0)

    loser = _insert_insight(store)        # exposed, net negative -> candidate
    _set_fitness(store, loser, retrievals=6, wins=1, losses=3)

    winner = _insert_insight(store)       # exposed, net positive -> keep
    _set_fitness(store, winner, retrievals=6, wins=5, losses=1)

    untested = _insert_insight(store)     # net negative but under-exposed -> keep
    _set_fitness(store, untested, retrievals=2, wins=0, losses=2)

    cands = m.retirement_candidates(store, 0, params)
    assert loser in cands
    assert winner not in cands
    assert untested not in cands


def test_retirement_reconstruction_is_snapshot_scoped(store):
    """fitness_score reads the event log at a snapshot — later events do not rewrite
    the earlier reconstruction (append-only)."""
    iid = _insert_insight(store)
    _set_fitness(store, iid, wins=2, snapshot_id=1)
    _set_fitness(store, iid, losses=5, snapshot_id=3)
    assert m.fitness_score(store, iid, snapshot_id=1) == 2     # only the early wins
    assert m.fitness_score(store, iid, snapshot_id=3) == -3    # wins - all losses
    assert m.fitness_score(store, iid) == -3                   # all events


# === params validation =========================================================


def test_params_range_checks():
    """The trimmed retirement params still validate their one tunable (R16)."""
    with pytest.raises(m.MaintenanceError):
        m.MaintenanceParams(retirement_min_retrievals=-1)
    # The cap/split tunables are gone — they are no longer accepted kwargs.
    with pytest.raises(TypeError):
        m.MaintenanceParams(active_cap=2)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        m.MaintenanceParams(split_min_observations=10)  # type: ignore[call-arg]
