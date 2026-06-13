"""plan-009 U7: demotions — router, agent_split, maintenance (R14, R15, R16).

The R3 reform removes the R2 organization machinery: per-family routing is replaced
by whole-store insight-level retrieval, agent-by-routing-volume splitting is replaced
by the *derived* partition, and the cap-tournament / k-means-silhouette skill split is
gone. This module proves the demolition is real and that the surviving partition-move
engine + settlement fitness keep working.

Fully offline: pure store/DB writes, no LLM seam, no clustering. Zero quota, no
``claude`` on PATH.

## Conformance (plan-009 U7 required acceptance tests -> the invariant each enforces)

- ``test_no_production_caller_of_router_route`` (R14) — no production source (outside
  tests and the kept-for-reversibility ``router.py``) references ``router.route``; the
  file and the ``routing_decisions`` table still exist (demoted, not deleted).
- ``test_no_kmeans_or_silhouette_in_src`` (R16) — the cap-tournament and
  k-means/silhouette split machinery are not importable from the reflector modules and
  no call site survives in ``src/`` (the ``active_cap`` schema column / config field
  are retained for reversibility and out of scope).
- ``test_agent_split_exposes_only_partition_engine`` (R15) — ``agent_split`` exports
  exactly ``execute_split`` / ``revert_split`` / ``finalize_split`` (re-pointed to
  module membership + snapshot-minting); the R2 machinery raises ``AttributeError``.
- ``test_maintenance_keeps_only_settle_fitness_and_telemetry`` (R16) —
  ``settle_fitness`` + telemetry + the usage-conditioned retirement survive; the
  cap-tournament / split / agent-split entry points are gone, and ``settle_fitness``
  runs without touching any removed path.

Partition-move engine behavior (R15) -> test:

- ``test_execute_split_repoints_membership_and_mints_one_snapshot``
- ``test_revert_split_restores_parent_membership``
- ``test_finalize_split_freezes_parent_and_blocks_revert``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_families.library import router
from agent_families.reflector import agent_split as a
from agent_families.reflector import maintenance as m
from agent_families.store import Store
from agent_families.vecindex import VecIndex

SRC = Path(__file__).resolve().parent.parent / "src"
DIM = 4


# --- fixtures + builders --------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    vec = VecIndex(s, DIM)
    vec.migrate()
    try:
        yield s
    finally:
        s.close()


def _insight(store: Store, n: int) -> int:
    return store.insert_insight(
        precondition=f"pre {n}",
        action=f"act {n}",
        expected_outcome=f"out {n}",
        content_hash=f"hash-{n}",
        status="active",
    )


def _module_with_members(store: Store, member_count: int) -> tuple[int, list[int]]:
    """A module (skill) owned by one agent, carrying ``member_count`` insights."""
    fam = store.create_family("worker")
    agent = store.create_agent(fam, "generalist")
    skill = store.create_skill(agent, "parent-module", "broad")
    ids = []
    with store.transaction():
        for i in range(member_count):
            iid = _insight(store, i)
            store.append_member(skill, iid)
            ids.append(iid)
    return skill, ids


def _snapshot_count(store: Store) -> int:
    return store.conn.execute("SELECT COUNT(*) AS n FROM snapshots").fetchone()["n"]


# === R14: router demoted to dead code ===========================================


def test_no_production_caller_of_router_route(store):
    """A source-tree scan (excluding tests and ``router.py`` itself) finds ZERO
    references to ``router.route``; the file and the ``routing_decisions`` table still
    exist for reversibility (R14)."""
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name == "router.py":
            continue  # the kept-for-reversibility definition site
        text = py.read_text(encoding="utf-8")
        if "router.route" in text:
            offenders.append(py.relative_to(SRC).as_posix())
    assert offenders == [], f"production code still calls router.route: {offenders}"

    # router.py is retained (demoted, not deleted) ...
    assert (SRC / "agent_families" / "library" / "router.py").exists()
    # ... and its routing_decisions table is still creatable (reversibility seam).
    router.ensure_routing_log(store)
    assert store.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='routing_decisions'"
    ).fetchone() is not None


# === R16: the cap-tournament / k-means-silhouette machinery is gone =============


def test_no_kmeans_or_silhouette_in_src():
    """The cap-tournament and k-means/silhouette split machinery are gone from the
    importable reflector modules, and no call site survives in ``src/`` (R16).

    ``hasattr`` is stronger than a text grep — it proves the symbol is not an
    importable name, which a docstring/comment mention cannot fake. The ``active_cap``
    *schema column* (store.py) and the ``LifecycleConfig`` field are deliberately
    retained for reversibility (plan-009 non-goal) and are out of scope here.
    """
    removed_from_maintenance = [
        "tournament_displacements",
        "agent_active_insight_ids",
        "_kmeans2",
        "silhouette_two",
        "split_candidates",
        "_partition_skill",
        "maybe_agent_split",
        "run_maintenance",
        "is_mid_validation",
        "MaintenanceResult",
        "SkillSplit",
        "AgentSplitDecision",
        "DEFAULT_ACTIVE_CAP",
        "DEFAULT_SPLIT_INSIGHT_COUNT",
        "DEFAULT_SPLIT_TOKEN_COUNT",
        "DEFAULT_SPLIT_SILHOUETTE_THRESHOLD",
        "DEFAULT_SPLIT_MIN_OBSERVATIONS",
    ]
    for name in removed_from_maintenance:
        assert not hasattr(m, name), f"maintenance still exposes removed symbol {name!r}"
    assert "active_cap" not in m.MaintenanceParams.__dataclass_fields__, (
        "active_cap is gone from MaintenanceParams (the cap tournament is removed)"
    )

    removed_from_agent_split = [
        "_kmeans2",
        "silhouette_two",
        "evaluate_split_candidacy",
        "perform_agent_split",
        "replay_agreement",
        "check_base_prompt_residue",
        "AgentSplitParams",
        "SplitCandidacy",
        "explorer_family_decision_evidence",
    ]
    for name in removed_from_agent_split:
        assert not hasattr(a, name), f"agent_split still exposes removed symbol {name!r}"

    # A source scan confirms no call/def sites survive anywhere in src (catches any
    # stray re-introduction). We match call/def TOKENS, not prose, so a docstring that
    # names the removed machinery as "deleted" does not trip this.
    forbidden_tokens = [
        "_kmeans2(",
        "silhouette_two(",
        "tournament_displacements(",
        "split_candidates(",
        "_partition_skill(",
        "maybe_agent_split(",
        "def run_maintenance",
        "DEFAULT_ACTIVE_CAP",
    ]
    offenders = []
    for py in SRC.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for tok in forbidden_tokens:
            if tok in text:
                offenders.append((py.relative_to(SRC).as_posix(), tok))
    assert offenders == [], f"removed machinery survives in src: {offenders}"


# === R15: agent_split is only the partition-move engine =========================


def test_agent_split_exposes_only_partition_engine(store):
    """``agent_split`` exports exactly ``execute_split`` / ``revert_split`` /
    ``finalize_split`` (re-pointed to module membership + snapshot-minting); the R2
    machinery is removed (importing it raises ``AttributeError``) (R15)."""
    # The three partition-move primitives survive ...
    for name in ("execute_split", "revert_split", "finalize_split"):
        assert callable(getattr(a, name))

    # ... and the R2 machinery is gone — attribute access raises AttributeError.
    for name in (
        "replay_agreement",
        "perform_agent_split",
        "evaluate_split_candidacy",
        "check_base_prompt_residue",
    ):
        with pytest.raises(AttributeError):
            getattr(a, name)

    # The engine genuinely operates on MODULE membership + mints a snapshot (not the
    # old agent-ownership + plain-transaction path): one execute re-points skill_members
    # and mints exactly one snapshot.
    skill, ids = _module_with_members(store, 4)
    before = _snapshot_count(store)
    split = a.execute_split(store, skill, (ids[:2], ids[2:]))
    assert _snapshot_count(store) == before + 1  # snapshot-minting, not a plain txn
    assert store.skill_members(skill) == []  # parent re-pointed (emptied)
    assert set(store.skill_members(split.child_skill_ids[0])) == set(ids[:2])
    assert set(store.skill_members(split.child_skill_ids[1])) == set(ids[2:])


def test_execute_split_repoints_membership_and_mints_one_snapshot(store):
    """execute_split distributes the parent's members into two child modules under one
    minted snapshot; children carry parent_skill_id + split_snapshot_id; the parent is
    emptied (a frozen anchor, never deleted) (R15)."""
    skill, ids = _module_with_members(store, 6)
    before = _snapshot_count(store)
    split = a.execute_split(store, skill, (ids[:3], ids[3:]), detail="t")

    assert _snapshot_count(store) == before + 1
    assert split.snapshot_id == store.current_snapshot_id()
    child0, child1 = split.child_skill_ids
    for cid, expected in ((child0, ids[:3]), (child1, ids[3:])):
        row = store.conn.execute(
            "SELECT parent_skill_id, split_snapshot_id, agent_id FROM skills WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row["parent_skill_id"] == skill
        assert row["split_snapshot_id"] == split.snapshot_id
        assert set(store.skill_members(cid)) == set(expected)
    # Parent emptied but not deleted.
    assert store.skill_members(skill) == []
    assert store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (skill,)
    ).fetchone() is not None


def test_execute_split_rejects_non_partition(store):
    """A cluster pair that does not partition the parent's membership is refused."""
    skill, ids = _module_with_members(store, 4)
    with pytest.raises(a.AgentSplitError):
        a.execute_split(store, skill, (ids[:2], ids[1:]))  # overlap
    with pytest.raises(a.AgentSplitError):
        a.execute_split(store, skill, (ids[:2], []))  # empty cluster


def test_revert_split_restores_parent_membership(store):
    """revert_split is the inverse: members back on the parent, children deleted; it
    mints its own forward snapshot (R15)."""
    skill, ids = _module_with_members(store, 4)
    split = a.execute_split(store, skill, (ids[:2], ids[2:]))
    after_execute = _snapshot_count(store)

    a.revert_split(store, split)

    assert _snapshot_count(store) == after_execute + 1  # forward snapshot, not un-minted
    assert set(store.skill_members(skill)) == set(ids)
    for cid in split.child_skill_ids:
        assert store.conn.execute(
            "SELECT 1 FROM skills WHERE id = ?", (cid,)
        ).fetchone() is None


def test_finalize_split_freezes_parent_and_blocks_revert(store):
    """finalize_split stamps the parent a frozen retired-by-split anchor; after it the
    split is no longer revertible (R15)."""
    skill, ids = _module_with_members(store, 4)
    split = a.execute_split(store, skill, (ids[:2], ids[2:]))

    finalized = a.finalize_split(store, split)
    assert finalized.finalized is True
    row = store.conn.execute(
        "SELECT split_snapshot_id FROM skills WHERE id = ?", (skill,)
    ).fetchone()
    assert row["split_snapshot_id"] is not None  # the retired-anchor marker

    with pytest.raises(a.AgentSplitError):
        a.revert_split(store, finalized)


# === R16: maintenance keeps only settlement fitness + telemetry + retirement ====


def test_maintenance_keeps_only_settle_fitness_and_telemetry(store):
    """``settle_fitness`` + telemetry + the usage-conditioned retirement survive; the
    cap-tournament / split / agent-split entry points are gone; ``settle_fitness`` runs
    without touching any removed path (R16)."""
    # The survivors are exposed and callable.
    for name in (
        "settle_fitness",
        "fitness_score",
        "provenance_world_fitness",
        "survival_retirement",
        "survival_retirement_candidates",
        "retirement_candidates",
    ):
        assert callable(getattr(m, name))

    # The removed orchestration / machinery is gone.
    for name in (
        "run_maintenance",
        "tournament_displacements",
        "split_candidates",
        "maybe_agent_split",
        "is_mid_validation",
    ):
        assert not hasattr(m, name)

    # settle_fitness runs the settlement path end-to-end (no removed path touched).
    ep = store.create_episode("linkding", "digest", 0, mode="training")
    store.conn.execute("INSERT INTO trace_tkt (id, status) VALUES ('TKT-1', 'done')")
    iid = _insight(store, 1)
    settlement = m.settle_fitness(
        store, episode_id=ep, renders=[m.SessionRender("TKT-1", (iid,))]
    )
    assert settlement.win_count == 1
    assert m.fitness_score(store, iid) == 1
    # Telemetry reads cleanly off the same log.
    buckets = m.provenance_world_fitness(store)
    assert any(b.wins == 1 for b in buckets)
