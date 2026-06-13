"""Vecindex tests (008 U3): multi-column vec0 storage, column-selectable KNN,
the flattener fix, and deletion of the `_knn_dedup_view` workaround.

Fully offline: vectors are hand-written, no embedding model / judge / subprocess
runs. The vec0 table now carries three columns per insight (key_embedding,
full_embedding, and the legacy/retrieval `embedding`), all committed in one
transaction with the insight row (R5/R7).

## Conformance

| Invariant (008 R5/R8) | Test |
|---|---|
| insert writes both columns atomically; an injected mid-write failure leaves ZERO rows | `test_insert_is_atomic_partial_rejected` |
| `knn(on="key")` and `knn(on="full")` search DIFFERENT columns; `on=` is a validated allow-list, never interpolated | `test_knn_on_selects_the_named_column` |
| the knn query uses `LIMIT` in the subquery and runs without the double-ORDER-BY flattener error | `test_limit_subquery_avoids_flattener_rejection` |
| `_knn_dedup_view` is deleted and has zero callers | `test_knn_dedup_view_deleted_no_caller` |
| insert must run inside a Store.transaction (R7) | `test_insert_outside_transaction_raises` |
| legacy single-vector insert still fans out (back-compat) | `test_legacy_single_vector_fans_out` |
| status-visibility join filters KNN by status (R13) | `test_status_filter_applies` |
| vec rowcount is immutable under a status flip (R13) | `test_count_immutable_under_status_flip` |
"""

from __future__ import annotations

import pathlib

import pytest

import agent_families.vecindex as vecindex_mod
from agent_families.store import Store
from agent_families.vecindex import VEC_TABLE, VecIndex, VecIndexError

DIM = 4


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    yield store, vec
    store.close()


def _insert(store, vec, insight_id_hint, *, key, full=None, retrieval=None, status="active"):
    """Insert a real insight row + its vec row in one transaction; return id."""
    with store.transaction():
        iid = store.insert_insight(
            precondition=f"precondition {insight_id_hint}",
            action=f"action {insight_id_hint}",
            expected_outcome=f"outcome {insight_id_hint}",
            content_hash=f"hash-{insight_id_hint}",
            status=status,
        )
        vec.insert(iid, key, full, retrieval)
    return iid


# --- atomicity (R7) --------------------------------------------------------------


def test_insert_is_atomic_partial_rejected(env, monkeypatch):
    """insert(id, key, full) writes BOTH columns in one tx; an injected failure
    between the two vectors leaves ZERO rows for that insight_id (R7)."""
    store, vec = env

    # Success path: both columns written, one row.
    iid = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0], full=[0.0, 1.0, 0.0, 0.0])
    assert vec.count() == 1
    # Each column is independently searchable (proves both were written).
    assert vec.knn([1.0, 0.0, 0.0, 0.0], 5, on="key")[0].insight_id == iid
    assert vec.knn([0.0, 1.0, 0.0, 0.0], 5, on="full")[0].insight_id == iid

    # Injected failure BETWEEN the two vectors: make serialize_float32 raise on
    # its second invocation inside insert(). The single INSERT never executes, so
    # the surrounding transaction rolls back to ZERO rows — no half-written vec.
    import sqlite_vec

    calls = {"n": 0}
    orig = sqlite_vec.serialize_float32

    def flaky(values):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected mid-insert failure")
        return orig(values)

    monkeypatch.setattr(sqlite_vec, "serialize_float32", flaky)
    before = vec.count()
    with pytest.raises(RuntimeError, match="injected mid-insert"):
        with store.transaction():
            store.insert_insight(
                precondition="p2", action="a2", expected_outcome="o2",
                content_hash="hash-2", status="active",
            )
            vec.insert(99, [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0])
    assert vec.count() == before  # rollback: zero new rows
    rows = store.conn.execute(
        f"SELECT COUNT(*) AS n FROM {VEC_TABLE} WHERE insight_id = 99"
    ).fetchone()["n"]
    assert rows == 0

    # A mismatched-dim second vector is rejected before any write, too.
    with pytest.raises(VecIndexError):
        with store.transaction():
            vec.insert(123, [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0])  # bad full dim
    assert store.conn.execute(
        f"SELECT COUNT(*) AS n FROM {VEC_TABLE} WHERE insight_id = 123"
    ).fetchone()["n"] == 0


def test_insert_outside_transaction_raises(env):
    """insert refuses to run outside Store.transaction() (R7 atomicity guard)."""
    store, vec = env
    with pytest.raises(VecIndexError, match="must run inside"):
        vec.insert(1, [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0])


def test_legacy_single_vector_fans_out(env):
    """The pre-R3 single-vector form fans the one vector into all three columns,
    so on='key'/'full'/'retrieval' all resolve it (back-compat for deferred
    consumers until the pipeline embeds key+full)."""
    store, vec = env
    iid = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0])  # full=None → legacy
    q = [1.0, 0.0, 0.0, 0.0]
    for view in ("key", "full", "retrieval"):
        hit = vec.knn(q, 5, on=view)
        assert hit[0].insight_id == iid
        assert hit[0].distance == pytest.approx(0.0, abs=1e-6)


# --- column selection (R8) -------------------------------------------------------


def test_knn_on_selects_the_named_column(env):
    """knn(on='key') and knn(on='full') over a corpus where the two vectors
    DISAGREE return different rankings; on= is a validated allow-list and an
    out-of-set value raises (never interpolated into SQL)."""
    store, vec = env
    # A: key aligns with q, full points away. B: the reverse.
    a = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0], full=[0.0, 0.0, 0.0, 1.0])
    b = _insert(store, vec, 2, key=[0.0, 0.0, 0.0, 1.0], full=[1.0, 0.0, 0.0, 0.0])
    q = [1.0, 0.0, 0.0, 0.0]

    by_key = [n.insight_id for n in vec.knn(q, 5, on="key")]
    by_full = [n.insight_id for n in vec.knn(q, 5, on="full")]
    assert by_key == [a, b]
    assert by_full == [b, a]
    assert by_key != by_full  # the column genuinely changes the ranking

    # The allow-list is fixed; an unknown view raises, not an injection.
    with pytest.raises(VecIndexError, match="unknown knn view"):
        vec.knn(q, 5, on="key_embedding; DROP TABLE insights")
    with pytest.raises(VecIndexError, match="unknown knn view"):
        vec.knn(q, 5, on="bogus")


def test_status_filter_applies(env):
    """The status-visibility join filters KNN results (R13)."""
    store, vec = env
    active = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0],
                     full=[1.0, 0.0, 0.0, 0.0], status="active")
    quar = _insert(store, vec, 2, key=[0.9, 0.1, 0.0, 0.0],
                   full=[0.9, 0.1, 0.0, 0.0], status="quarantined")
    q = [1.0, 0.0, 0.0, 0.0]
    # Unfiltered dedup view sees both.
    assert {n.insight_id for n in vec.knn(q, 5, statuses=None, on="key")} == {active, quar}
    # Active-only view hides the quarantined neighbor.
    active_only = [n.insight_id for n in vec.knn(q, 5, statuses=("active",), on="key")]
    assert active_only == [active]


def test_count_immutable_under_status_flip(env):
    """Vec rows are written once at registration and never touched by a status
    change — count() is invariant under lifecycle status flips (R13)."""
    store, vec = env
    iid = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0], full=[0.0, 1.0, 0.0, 0.0])
    before = vec.count()
    with store.transaction():
        store.conn.execute("UPDATE insights SET status='retired' WHERE id=?", (iid,))
    assert vec.count() == before


# --- the flattener fix (R8) ------------------------------------------------------


def test_limit_subquery_avoids_flattener_rejection(env):
    """knn() uses LIMIT inside the subquery (not `k = ?`) and runs without the
    'Only a single ORDER BY distance' vec0 flattener error that the deleted
    `_knn_dedup_view` worked around."""
    store, vec = env
    a = _insert(store, vec, 1, key=[1.0, 0.0, 0.0, 0.0], full=[1.0, 0.0, 0.0, 0.0],
                status="active")
    # b is quarantined so the outer status filter excludes it — exercising the
    # join + outer ORDER BY that triggered the flattener rejection.
    _insert(store, vec, 2, key=[0.0, 1.0, 0.0, 0.0], full=[0.0, 1.0, 0.0, 0.0],
            status="quarantined")

    captured: list[str] = []
    store.conn.set_trace_callback(captured.append)
    try:
        # An outer ORDER BY + join over the KNN subquery: the exact shape that
        # tripped vec0's single-ORDER-BY rule under the `k = ?` form.
        result = vec.knn([1.0, 0.0, 0.0, 0.0], 5, statuses=("active",), on="key")
    finally:
        store.conn.set_trace_callback(None)

    assert [n.insight_id for n in result] == [a]  # ran, ordered, filtered
    knn_sql = [s for s in captured if "MATCH" in s]
    assert knn_sql, "expected a vec0 MATCH query"
    # The trace callback reports SQL with bound params inlined (LIMIT 5, not ?).
    sql = knn_sql[-1]
    assert "ORDER BY distance LIMIT" in sql  # the flattener-blocking form
    assert "k =" not in sql  # never the `AND k = ?` form that trips the flattener


def test_knn_dedup_view_deleted_no_caller():
    """`_knn_dedup_view` is gone from pipeline/__init__.py with zero callers."""
    pipeline_src = (
        pathlib.Path(vecindex_mod.__file__).parent / "pipeline" / "__init__.py"
    ).read_text(encoding="utf-8")
    assert "_knn_dedup_view" not in pipeline_src

    from agent_families import pipeline as pipeline_pkg

    assert not hasattr(pipeline_pkg, "_knn_dedup_view")
