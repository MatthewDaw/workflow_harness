"""Similarity flow-graph build (009 U2, R4).

The derive pass (U4) partitions an insight graph and the §6a objective (U1)
scores it on a *flow* graph. This module builds that graph. At cold start —
before co-retrieval traces in ``fitness_events`` are dense — the flow is proxied
by vector *similarity*: a mutual-kNN (SNN) graph at k≈15 over the **full
clustering vectors**, with surviving edges re-weighted by the Tanimoto
coefficient. Blending in co-retrieval frequency once traces accumulate is the
documented R2 seam, not the v1 default.

**No materialized edge table (R4).** At <50k nodes the graph is recomputed from
sqlite-vec on every derive pass, so ``insight_edges`` rows of ``kind='similarity'``
are *never* written — the ``similarity`` kind exists in the enum for
reversibility but stays empty at v1. ``build_similarity_graph`` returns an
in-memory ``list[(src, dst, weight)]`` and touches no table.

Pure Python, deterministic for a fixed library snapshot: nodes are visited in
ascending id, undirected edges are emitted once in ``src < dst`` canonical form,
and Tanimoto is a closed form over the recovered vectors — two builds over the
same snapshot produce byte-identical edge lists.
"""

from __future__ import annotations

from agent_families.vecindex import VecIndex


def tanimoto(a: list[float], b: list[float]) -> float:
    """Tanimoto (extended Jaccard) coefficient ``T(a,b)=a·b/(‖a‖²+‖b‖²−a·b)``.

    Unlike cosine it is not scale-invariant — it folds in vector magnitude — and
    unlike the raw dot product it is normalized into ``[0, 1]`` for non-negative
    inputs. The all-zero case (degenerate denominator) returns ``0.0`` rather
    than dividing by zero.
    """
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a)
    nb = sum(y * y for y in b)
    denom = na + nb - dot
    if denom == 0.0:
        return 0.0
    return dot / denom


def build_similarity_graph(
    vec: VecIndex,
    *,
    k: int = 15,
    on: str = "full",
    statuses: tuple[str, ...] | None = ("active",),
) -> list[tuple[int, int, float]]:
    """Build the mutual-kNN + Tanimoto similarity graph (R4).

    Reciprocity (mutual-kNN / SNN): an undirected edge ``{a, b}`` survives only
    if ``a`` is among ``b``'s k nearest AND ``b`` is among ``a``'s k nearest. A
    one-directional kNN edge (``b`` near ``a`` but ``a`` not near ``b``) is
    dropped. Surviving edges are re-weighted by :func:`tanimoto` over the
    recovered full vectors. Self-edges are impossible (dropped in
    :meth:`VecIndex.all_neighbors` and again by the ``src < dst`` canonical form).

    Returns one ``(src, dst, weight)`` per surviving undirected edge with
    ``src < dst``, in ascending ``(src, dst)`` order. Writes nothing to sqlite.
    """
    neighbors = vec.all_neighbors(k, on=on, statuses=statuses)
    nbr_ids: dict[int, set[int]] = {
        src: {n.insight_id for n in lst} for src, lst in neighbors.items()
    }
    vectors: dict[int, list[float]] = {
        iid: vec.get_vector(iid, on=on) for iid in nbr_ids
    }

    edges: list[tuple[int, int, float]] = []
    for src in sorted(nbr_ids):
        for dst in sorted(nbr_ids[src]):
            if dst <= src:  # self-edge or the already-emitted mirror direction
                continue
            if src in nbr_ids.get(dst, ()):  # mutual-kNN reciprocity
                weight = tanimoto(vectors[src], vectors[dst])
                edges.append((src, dst, weight))
    return edges
