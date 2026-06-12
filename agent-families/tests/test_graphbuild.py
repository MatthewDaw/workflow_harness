"""Graph build + vecindex neighbors (009 U2): the similarity flow graph.

Fully offline: vectors are hand-written, no embedding model / judge / subprocess
runs. The graph is the v1 cold-start *flow* graph for the derive pass — a
mutual-kNN (SNN) graph over the full clustering vectors, edges re-weighted by
Tanimoto, recomputed from sqlite-vec each pass (never materialized).

## Conformance

| Invariant (009 R4/R5) | Test |
|---|---|
| mutual-kNN keeps only reciprocal pairs; a one-directional edge is dropped | `test_mutual_knn_drops_one_directional` |
| edge weight is Tanimoto `a·b/(‖a‖²+‖b‖²−a·b)`, not cosine, not dot | `test_tanimoto_matches_hand_computed` |
| a graph build writes ZERO `insight_edges` rows of `kind='similarity'` | `test_no_similarity_edge_rows_written` |
| no `(x,x)` self-edge; two builds over a fixed snapshot are byte-identical | `test_self_edges_dropped_and_deterministic` |
| `get_vector` recovers the stored vector from the named column (R5) | `test_get_vector_recovers_stored_vector` |
| `all_neighbors` is a per-node kNN loop with the self-edge dropped (R5) | `test_all_neighbors_drops_self_and_is_status_filtered` |
"""

from __future__ import annotations

import math

import pytest

from agent_families.reflector.graphbuild import build_similarity_graph, tanimoto
from agent_families.store import Store
from agent_families.vecindex import VecIndex, VecIndexError

DIM = 4


@pytest.fixture
def env(tmp_path):
    store = Store(tmp_path / "library.db")
    store.migrate()
    vec = VecIndex(store, DIM)
    vec.migrate()
    yield store, vec
    store.close()


def _insert(store, vec, hint, *, key, full=None, retrieval=None, status="active"):
    """Insert a real insight row + its vec row in one transaction; return id."""
    with store.transaction():
        iid = store.insert_insight(
            precondition=f"precondition {hint}",
            action=f"action {hint}",
            expected_outcome=f"outcome {hint}",
            content_hash=f"hash-{hint}",
            status=status,
        )
        vec.insert(iid, key, full, retrieval)
    return iid


def _unit2d(deg: float) -> list[float]:
    """A unit vector at angle ``deg`` in the first two dims (pad to DIM)."""
    r = math.radians(deg)
    return [math.cos(r), math.sin(r), 0.0, 0.0]


# --- mutual-kNN reciprocity (R4) -------------------------------------------------


def test_mutual_knn_drops_one_directional(env):
    """An edge present in one node's kNN but NOT reciprocated is dropped; only
    mutually-reciprocal pairs survive. A plain (non-mutual) kNN graph fails this.

    Layout (k=1, angles 0°/40°/70°): A's nearest is B, but B's nearest is C
    (not A), and C's nearest is B. So B–C is mutual and survives; A–B is
    one-directional and is dropped."""
    store, vec = env
    a = _insert(store, vec, "A", key=_unit2d(0), full=_unit2d(0))
    b = _insert(store, vec, "B", key=_unit2d(40), full=_unit2d(40))
    c = _insert(store, vec, "C", key=_unit2d(70), full=_unit2d(70))

    # Sanity: the per-node kNN really is asymmetric.
    nbrs = vec.all_neighbors(1, on="full")
    assert [n.insight_id for n in nbrs[a]] == [b]   # A → B
    assert [n.insight_id for n in nbrs[b]] == [c]   # B → C (not A!)
    assert [n.insight_id for n in nbrs[c]] == [b]   # C → B

    edges = build_similarity_graph(vec, k=1, on="full")
    edge_ids = {(s, d) for (s, d, _w) in edges}
    assert edge_ids == {(b, c)}        # only the reciprocal pair survives
    assert (min(a, b), max(a, b)) not in edge_ids  # one-directional A–B dropped


# --- Tanimoto weighting (R4) -----------------------------------------------------


def test_tanimoto_matches_hand_computed(env):
    """The edge weight equals Tanimoto to float tolerance, and is provably
    neither plain cosine nor raw dot product on the same pair."""
    a_vec = [1.0, 0.0, 0.0, 0.0]
    b_vec = [1.0, 1.0, 0.0, 0.0]
    # dot = 1, ‖a‖²=1, ‖b‖²=2  →  T = 1/(1+2-1) = 0.5
    dot = 1.0
    cosine = dot / (math.sqrt(1.0) * math.sqrt(2.0))  # 0.7071…
    assert tanimoto(a_vec, b_vec) == pytest.approx(0.5)
    assert tanimoto(a_vec, b_vec) != pytest.approx(cosine)  # not cosine
    assert tanimoto(a_vec, b_vec) != pytest.approx(dot)     # not the dot product

    # And the built graph uses exactly that weight: two nodes are each other's
    # only neighbor (k=1) → one mutual edge carrying the Tanimoto weight.
    store, vec = env
    a = _insert(store, vec, "A", key=a_vec, full=a_vec)
    b = _insert(store, vec, "B", key=b_vec, full=b_vec)
    edges = build_similarity_graph(vec, k=1, on="full")
    assert len(edges) == 1
    src, dst, weight = edges[0]
    assert (src, dst) == (min(a, b), max(a, b))
    assert weight == pytest.approx(0.5)


# --- no materialized edge table (R4) ---------------------------------------------


def test_no_similarity_edge_rows_written(env):
    """The graph is recomputed in-memory each pass; `insight_edges` has ZERO
    rows of kind='similarity' after a build (no materialized edge table)."""
    store, vec = env
    _insert(store, vec, "A", key=_unit2d(0), full=_unit2d(0))
    _insert(store, vec, "B", key=_unit2d(5), full=_unit2d(5))
    _insert(store, vec, "C", key=_unit2d(10), full=_unit2d(10))

    edges = build_similarity_graph(vec, k=2, on="full")
    assert edges  # the build produced edges in memory…

    n = store.conn.execute(
        "SELECT COUNT(*) AS n FROM insight_edges WHERE kind = 'similarity'"
    ).fetchone()["n"]
    assert n == 0  # …but wrote none to the table


# --- self-edges + determinism (R4) -----------------------------------------------


def test_self_edges_dropped_and_deterministic(env):
    """No (x,x) self-edge appears; two builds over the same fixed snapshot
    produce byte-identical edge lists (same order, same weights)."""
    store, vec = env
    # A tight cluster + a far outlier, so several mutual edges form.
    for i, deg in enumerate((0, 8, 16, 24, 175)):
        _insert(store, vec, f"n{i}", key=_unit2d(deg), full=_unit2d(deg))

    first = build_similarity_graph(vec, k=3, on="full")
    second = build_similarity_graph(vec, k=3, on="full")

    assert all(s != d for (s, d, _w) in first)  # no self-edges
    assert first == second  # byte-identical: same order, same float weights
    # canonical undirected form: every edge has src < dst, list is sorted
    assert all(s < d for (s, d, _w) in first)
    assert first == sorted(first)


# --- vecindex R5 additions -------------------------------------------------------


def test_get_vector_recovers_stored_vector(env):
    """get_vector returns the exact stored vector from the named column, and the
    column allow-list is validated (an out-of-set view raises, never SQL)."""
    store, vec = env
    key_v = [0.25, 0.5, 0.75, 1.0]
    full_v = [1.0, 0.0, 0.0, 0.5]
    iid = _insert(store, vec, "A", key=key_v, full=full_v)

    assert vec.get_vector(iid, on="full") == pytest.approx(full_v)
    assert vec.get_vector(iid, on="key") == pytest.approx(key_v)

    with pytest.raises(VecIndexError, match="unknown vector view"):
        vec.get_vector(iid, on="full_embedding; DROP TABLE insights")
    with pytest.raises(VecIndexError, match="no vec row"):
        vec.get_vector(999_999, on="full")


def test_all_neighbors_drops_self_and_is_status_filtered(env):
    """all_neighbors is a per-node kNN loop: every node maps to its nearest
    neighbors with itself dropped, and the status filter scopes both the node
    set and the neighbor lists (R5/R13)."""
    store, vec = env
    a = _insert(store, vec, "A", key=_unit2d(0), full=_unit2d(0), status="active")
    b = _insert(store, vec, "B", key=_unit2d(5), full=_unit2d(5), status="active")
    quar = _insert(store, vec, "Q", key=_unit2d(10), full=_unit2d(10),
                   status="quarantined")

    active = vec.all_neighbors(5, on="full", statuses=("active",))
    # Only active insights are nodes; the quarantined one is absent as a key…
    assert set(active) == {a, b}
    # …and never appears as a neighbor of an active node.
    for node_id, nbrs in active.items():
        ids = [n.insight_id for n in nbrs]
        assert node_id not in ids          # self dropped
        assert quar not in ids             # quarantined hidden by the status join

    # statuses=None is the all-statuses view: the quarantined node joins in.
    everything = vec.all_neighbors(5, on="full", statuses=None)
    assert set(everything) == {a, b, quar}
