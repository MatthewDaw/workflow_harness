"""Lifecycle operation tests (U6): promote/revert, retire/revive, snapshots, R13.

Fully offline: no judge, no embedding model — vectors are hand-written and the
store is exercised directly, the same write shape the U5 registration
transaction produces.
"""

from __future__ import annotations

import random
from types import SimpleNamespace

import pytest
import sqlite_vec

from agent_families.lifecycle import (
    LifecycleError,
    empty_skills,
    promote_batch,
    retire_insight,
    retire_skill,
    revert_batch,
    revive_insight,
    revive_skill,
    skills_created_by_reverted_batches,
)
from agent_families.store import Store, STATUSES
from agent_families.vecindex import VEC_TABLE, VecIndex

DIM = 4
V1 = [1.0, 0.0, 0.0, 0.0]
V2 = [0.0, 1.0, 0.0, 0.0]

# Visibility matrix views (plan-001 "Operation × status visibility").
DEDUP_VIEW = STATUSES  # add-idea sees everything, retired included (R14)
RENDER_VIEW = ("active",)
RENDER_VIEW_WITH_QUARANTINED = ("active", "quarantined")


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist")
    skill_id = store.create_skill(agent_id, "elicitation")
    e = SimpleNamespace(
        store=store,
        vec=vec,
        agent_id=agent_id,
        skill_id=skill_id,
        counter=0,
    )
    yield e
    store.close()


def seed(
    env,
    *,
    batch: str | None = None,
    status: str = "quarantined",
    skill_id: int | None = ...,
    vector=None,
    supersedes: int | None = None,
) -> int:
    """Insert insight + vec row + membership — the U5 registration write shape."""
    env.counter += 1
    n = env.counter
    with env.store.transaction():
        batch_id = env.store.ensure_batch(batch) if batch is not None else None
        insight_id = env.store.insert_insight(
            precondition=f"precondition {n}",
            action=f"action {n}",
            expected_outcome=f"outcome {n}",
            content_hash=f"hash-{n}",
            status=status,
            batch_id=batch_id,
            supersedes=supersedes,
        )
        env.vec.insert(insight_id, vector if vector is not None else [float(n), 1.0, 0.0, 0.0])
        if skill_id is ...:
            skill_id = env.skill_id
        if skill_id is not None:
            env.store.append_member(skill_id, insight_id)
    return insight_id


def open_contradiction(env, challenger_id: int, incumbent_id: int) -> int:
    cur = env.store.conn.execute(
        "INSERT INTO contradictions (challenger_id, incumbent_id, status, opened_at)"
        " VALUES (?, ?, 'open', '2026-06-10T00:00:00+00:00')",
        (challenger_id, incumbent_id),
    )
    return cur.lastrowid


def status_of(env, insight_id: int) -> str:
    return env.store.get_insight(insight_id)["status"]


def transitions_for(env, insight_id: int) -> list[tuple[str, str, int]]:
    rows = env.store.conn.execute(
        "SELECT from_status, to_status, snapshot_id FROM status_transitions"
        " WHERE insight_id = ? ORDER BY id ASC",
        (insight_id,),
    ).fetchall()
    return [(r["from_status"], r["to_status"], r["snapshot_id"]) for r in rows]


def vec_dump(env) -> dict[int, bytes]:
    """Every vec row's raw bytes — lifecycle ops must never change this (R13)."""
    rows = env.store.conn.execute(
        f"SELECT insight_id, embedding FROM {VEC_TABLE} ORDER BY insight_id"
    ).fetchall()
    return {r["insight_id"]: bytes(r["embedding"]) for r in rows}


def knn_visible(env, vector, k: int, statuses: tuple[str, ...]) -> list[int]:
    """Filtered KNN per the visibility matrix (R13).

    Uses the LIMIT-form vec0 subquery (the `_knn_dedup_view` workaround from
    U5) because `VecIndex.knn`'s `k = ?` form trips the SQLite query-flattener
    incompatibility — that fix belongs to vecindex.py's owning unit (U3).
    """
    placeholders = ", ".join("?" for _ in statuses)
    rows = env.store.conn.execute(
        "SELECT v.insight_id AS insight_id"
        f" FROM (SELECT insight_id, distance FROM {VEC_TABLE}"
        "        WHERE embedding MATCH ? ORDER BY distance LIMIT ?) v"
        " JOIN insights i ON i.id = v.insight_id"
        f" WHERE i.status IN ({placeholders})"
        " ORDER BY v.distance ASC, v.insight_id ASC",
        (sqlite_vec.serialize_float32(list(vector)), int(k), *statuses),
    ).fetchall()
    return [r["insight_id"] for r in rows]


def assert_snapshot_chain(env) -> None:
    """The snapshot chain is linear and gapless: ids 1..N, parent = previous."""
    rows = env.store.conn.execute(
        "SELECT id, parent_id FROM snapshots ORDER BY id ASC"
    ).fetchall()
    expected_parent = None
    for expected_id, row in enumerate(rows, start=1):
        assert row["id"] == expected_id
        assert row["parent_id"] == expected_parent
        expected_parent = row["id"]


def assert_no_orphan_membership(env) -> None:
    n = env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM skill_members m"
        " LEFT JOIN skills s ON s.id = m.skill_id"
        " LEFT JOIN insights i ON i.id = m.insight_id"
        " WHERE s.id IS NULL OR i.id IS NULL"
    ).fetchone()["n"]
    assert n == 0


def snapshot_count(env) -> int:
    return env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM snapshots"
    ).fetchone()["n"]


# --- promote (R3, R12) ----------------------------------------------------------


def test_promote_flips_whole_batch_and_mints_exactly_one_snapshot(env):
    ids = [seed(env, batch="b1") for _ in range(3)]
    bystander = seed(env, batch="other")

    result = promote_batch(env.store, "b1")

    assert result.insight_ids == tuple(ids)
    for insight_id in ids:
        assert status_of(env, insight_id) == "active"
        assert transitions_for(env, insight_id) == [
            ("quarantined", "active", result.snapshot_id)
        ]
    assert status_of(env, bystander) == "quarantined"
    assert snapshot_count(env) == 1
    assert env.store.current_snapshot_id() == result.snapshot_id
    queue = env.store.conn.execute("SELECT * FROM promotion_queue").fetchall()
    assert len(queue) == 1
    assert queue[0]["operation"] == "promote_batch"
    assert queue[0]["snapshot_id"] == result.snapshot_id


def test_promote_leaves_superseded_insight_untouched(env):
    # Phase 3 seam probe (R9): supersedes links are NOT auto-resolved on
    # promote — the incumbent keeps its status and its open flag.
    incumbent = seed(env, status="active")
    challenger = seed(env, batch="b1", supersedes=incumbent)
    flag_id = open_contradiction(env, challenger, incumbent)

    promote_batch(env.store, "b1")

    assert status_of(env, challenger) == "active"
    assert status_of(env, incumbent) == "active"
    assert transitions_for(env, incumbent) == []
    flag = env.store.conn.execute(
        "SELECT status FROM contradictions WHERE id = ?", (flag_id,)
    ).fetchone()
    assert flag["status"] == "open"


# --- revert (R9, R12) -------------------------------------------------------------


def test_revert_closes_reverted_challengers_contradiction_flag(env):
    incumbent = seed(env, status="active")
    challenger = seed(env, batch="b1")
    flag_id = open_contradiction(env, challenger, incumbent)
    promote_batch(env.store, "b1")

    result = revert_batch(env.store, "b1")

    assert status_of(env, challenger) == "retired"
    assert result.closed_contradiction_ids == (flag_id,)
    flag = env.store.conn.execute(
        "SELECT * FROM contradictions WHERE id = ?", (flag_id,)
    ).fetchone()
    assert flag["status"] == "closed"
    assert flag["closed_snapshot_id"] == result.snapshot_id
    assert status_of(env, incumbent) == "active"  # incumbent untouched


def test_revert_removes_batch_created_empty_skill(env):
    with env.store.transaction():
        batch_id = env.store.ensure_batch("b1")
        new_skill = env.store.create_skill(
            env.agent_id, "admin-probing", created_batch_id=batch_id
        )
    member = seed(env, batch="b1", skill_id=new_skill)
    vec_before = vec_dump(env)

    result = revert_batch(env.store, "b1")

    assert result.removed_skill_ids == (new_skill,)
    assert env.store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (new_skill,)
    ).fetchone() is None
    assert env.store.skill_members(new_skill) == []
    # the insight is never dissolved: row retired, vec row byte-identical (R13)
    assert status_of(env, member) == "retired"
    assert vec_dump(env) == vec_before
    assert_no_orphan_membership(env)


def test_revert_keeps_skill_appended_to_by_another_batch(env):
    # Batch A creates a skill; batch B appends to it. Reverting A must leave
    # the skill standing with B's member intact (no orphan) — flagged as
    # "created by reverted batch" for `status` (R12).
    with env.store.transaction():
        batch_a = env.store.ensure_batch("batch-a")
        shared_skill = env.store.create_skill(
            env.agent_id, "shared", created_batch_id=batch_a
        )
    a_member = seed(env, batch="batch-a", skill_id=shared_skill)
    b_member = seed(env, batch="batch-b", skill_id=shared_skill)
    promote_batch(env.store, "batch-a")
    promote_batch(env.store, "batch-b")

    result = revert_batch(env.store, "batch-a")

    assert result.removed_skill_ids == ()
    assert env.store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (shared_skill,)
    ).fetchone() is not None
    assert env.store.skill_members(shared_skill) == [a_member, b_member]
    assert status_of(env, a_member) == "retired"
    assert status_of(env, b_member) == "active"
    assert skills_created_by_reverted_batches(env.store) == (shared_skill,)
    assert_no_orphan_membership(env)


# --- retire / revive (R12, R13, R14) ------------------------------------------------


def test_retire_last_member_leaves_skill_row_present_and_flagged_empty(env):
    only_member = seed(env, status="active")
    assert empty_skills(env.store) == ()

    retire_insight(env.store, only_member)

    assert env.store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (env.skill_id,)
    ).fetchone() is not None
    assert env.store.skill_members(env.skill_id) == [only_member]
    assert empty_skills(env.store) == (env.skill_id,)


def test_revive_restores_searchability_under_visibility_matrix(env):
    target = seed(env, status="active", vector=V1)
    other = seed(env, status="active", vector=V2)

    assert knn_visible(env, V1, 10, RENDER_VIEW) == [target, other]

    retire_insight(env.store, target)
    # render/export view loses it; the add-idea dedup view never does (R13/R14)
    assert knn_visible(env, V1, 10, RENDER_VIEW) == [other]
    assert knn_visible(env, V1, 10, RENDER_VIEW_WITH_QUARANTINED) == [other]
    assert knn_visible(env, V1, 10, DEDUP_VIEW) == [target, other]

    revive_insight(env.store, target)
    assert status_of(env, target) == "active"
    assert knn_visible(env, V1, 10, RENDER_VIEW) == [target, other]


def test_retire_skill_flips_all_live_members_under_one_snapshot(env):
    members = [seed(env, status="active") for _ in range(2)]
    already_retired = seed(env, status="retired")

    result = retire_skill(env.store, env.skill_id)

    assert result.insight_ids == tuple(members)
    assert all(status_of(env, m) == "retired" for m in members)
    assert snapshot_count(env) == 1  # one snapshot for the whole skill
    assert transitions_for(env, already_retired) == []


def test_revive_skill_restores_all_retired_members(env):
    members = [seed(env, status="retired") for _ in range(2)]
    live = seed(env, status="active")

    result = revive_skill(env.store, env.skill_id)

    assert result.insight_ids == tuple(members)
    assert all(status_of(env, m) == "active" for m in members)
    assert transitions_for(env, live) == []
    assert snapshot_count(env) == 1


# --- error paths: no snapshot is ever minted by a failed operation -------------------


def test_failed_operations_mint_no_snapshot(env):
    live = seed(env, status="active", batch="promoted")
    retired = seed(env, status="retired")
    with env.store.transaction():
        empty_skill = env.store.create_skill(env.agent_id, "empty")

    with pytest.raises(LifecycleError, match="unknown batch"):
        promote_batch(env.store, "no-such-batch")
    with pytest.raises(LifecycleError, match="no quarantined insights"):
        promote_batch(env.store, "promoted")  # nothing left to promote
    with pytest.raises(LifecycleError, match="already retired"):
        retire_insight(env.store, retired)
    with pytest.raises(LifecycleError, match="revive operates only on retired"):
        revive_insight(env.store, live)
    with pytest.raises(LifecycleError, match="does not exist"):
        retire_insight(env.store, 9999)
    with pytest.raises(LifecycleError, match="no live members"):
        retire_skill(env.store, empty_skill)
    with pytest.raises(LifecycleError, match="does not exist"):
        revive_skill(env.store, 9999)

    assert snapshot_count(env) == 0
    assert env.store.conn.execute(
        "SELECT COUNT(*) AS n FROM promotion_queue"
    ).fetchone()["n"] == 0


# --- cross-op invariants (R3, R13) -----------------------------------------------------


def test_vec_rows_identical_before_and_after_every_op(env):
    ids = [seed(env, batch="b1") for _ in range(2)]
    baseline = vec_dump(env)
    assert len(baseline) == 2

    for op in (
        lambda: promote_batch(env.store, "b1"),
        lambda: retire_insight(env.store, ids[0]),
        lambda: revive_insight(env.store, ids[0]),
        lambda: retire_skill(env.store, env.skill_id),
        lambda: revive_skill(env.store, env.skill_id),
        lambda: revert_batch(env.store, "b1"),
    ):
        op()
        assert vec_dump(env) == baseline


def test_snapshot_parent_chain_is_linear_and_gapless(env):
    ids = [seed(env, batch="b1") for _ in range(2)]
    promote_batch(env.store, "b1")
    retire_insight(env.store, ids[0])
    revive_insight(env.store, ids[0])
    with pytest.raises(LifecycleError):
        revive_insight(env.store, ids[0])  # failed op must not leave a gap
    revert_batch(env.store, "b1")

    assert snapshot_count(env) == 4
    assert_snapshot_chain(env)
    # state-at-snapshot reconstruction across the sequence (R3)
    s_promote, s_retire, s_revive, s_revert = range(1, 5)
    assert env.store.status_at(ids[0], s_promote) == "active"
    assert env.store.status_at(ids[0], s_retire) == "retired"
    assert env.store.status_at(ids[0], s_revive) == "active"
    assert env.store.status_at(ids[0], s_revert) == "retired"


# --- property-style verification: random op sequences preserve invariants -------------


@pytest.mark.parametrize("seed_value", [0, 1, 2])
def test_random_lifecycle_sequences_preserve_invariants(env, seed_value):
    """Plan U6 Verification: any sequence of lifecycle ops preserves vec-row
    immutability, snapshot-chain integrity, and orphan-free membership."""
    rng = random.Random(seed_value)
    expected_vecs = vec_dump(env)
    batches: list[str] = []

    def register_batch() -> None:
        # Registration-shaped write: new batch, 1-3 quarantined insights, each
        # appended to an existing skill or a new batch-created skill. Adds vec
        # rows (tracked) and must mint no snapshot.
        label = f"batch-{len(batches)}"
        batches.append(label)
        before = snapshot_count(env)
        for _ in range(rng.randint(1, 3)):
            skills = [
                r["id"]
                for r in env.store.conn.execute("SELECT id FROM skills").fetchall()
            ]
            if rng.random() < 0.4:
                with env.store.transaction():
                    batch_id = env.store.ensure_batch(label)
                    target_skill = env.store.create_skill(
                        env.agent_id,
                        f"skill-{env.counter}",
                        created_batch_id=batch_id,
                    )
            else:
                target_skill = rng.choice(skills)
            insight_id = seed(env, batch=label, skill_id=target_skill)
            expected_vecs[insight_id] = vec_dump(env)[insight_id]
        assert snapshot_count(env) == before  # registration never mints (R3)

    def random_insight() -> int:
        rows = env.store.conn.execute("SELECT id FROM insights").fetchall()
        return rng.choice([r["id"] for r in rows]) if rows else 9999

    def random_skill() -> int:
        rows = env.store.conn.execute("SELECT id FROM skills").fetchall()
        return rng.choice([r["id"] for r in rows])

    ops = [
        register_batch,
        lambda: promote_batch(env.store, rng.choice(batches)),
        lambda: revert_batch(env.store, rng.choice(batches)),
        lambda: retire_insight(env.store, random_insight()),
        lambda: revive_insight(env.store, random_insight()),
        lambda: retire_skill(env.store, random_skill()),
        lambda: revive_skill(env.store, random_skill()),
    ]

    register_batch()  # something to operate on
    applied = 0
    for _ in range(40):
        op = rng.choice(ops)
        snapshots_before = snapshot_count(env)
        try:
            op()
            if op is not register_batch:
                applied += 1
                assert snapshot_count(env) == snapshots_before + 1
        except LifecycleError:
            assert snapshot_count(env) == snapshots_before  # failed op: no mint
        assert vec_dump(env) == expected_vecs  # vec rows immutable (R13)
        assert_snapshot_chain(env)
        assert_no_orphan_membership(env)
    assert applied > 0  # the sequence actually exercised lifecycle ops
