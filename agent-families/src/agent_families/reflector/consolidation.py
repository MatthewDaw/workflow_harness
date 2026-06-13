"""Consolidation — the §5 Operation-3 per-move delta (plan-009 U5, R11).

The derive pass (U4) adopts a *whole* partition; this module and the retirement
re-spec in ``maintenance.py`` (R12) are the two **per-move** deltas the §6a
objective scores in isolation (DESIGN §5/§6). Consolidation is the move that
*compresses* a community of corroborating/refining specifics into one general
parent:

1. A community of active insights (the children — many specifics that say the
   same general thing) is identified by the caller (the derive pass at v1).
2. A general **parent** insight is synthesized at the right *altitude* via the
   admission gate's generalization (the seam :data:`GeneralizerFn`; a
   deterministic fake offline, ``run_admission_gate`` live), stamped
   ``provenance=consolidated``.
3. The move is **scored as an isolated ``cost(G)`` delta** (U1): consolidation is
   modeled as a graph **contraction** of the children into the parent over the
   v1 similarity flow graph (U2) — the parent inherits the union of the
   children's edges; child↔child edges become a dropped self-loop. The partition
   the delta is scored against is the flow graph's **connected components** (the
   natural v1 community structure before derive has authored modules; when derive
   has, those modules are the partition — a documented seam).
4. **Adopt only if it strictly lowers ``cost(G)``** (``cost_delta < 0``). On
   adoption the parent is inserted (active) with a centroid vector, and the
   children are demoted to ``dormant`` via plan-008's
   :func:`lifecycle.demote_to_dormant` (preserved as evidence, **never**
   retired), which materializes a ``generalizes_from`` edge from the parent to
   **every** child and mints exactly one snapshot. On rejection **nothing is
   written** — no parent, no demotion, the active set is untouched.

## Why contraction + connected-components scores the move honestly

A *coherent* consolidation collapses a tight, internally-connected community into
one node: the children's edges were internal, so the contracted parent is nearly
isolated, the module shrinks, and ``cost(G)`` falls (fewer nodes, lower locate
bits, fewer module codebooks). A *bad* consolidation — fusing specifics that
belong to **distinct** communities — makes the contracted parent inherit edges
into both, **bridging** two modules into one larger module; the locate term (∝
``log2(|module|)`` paid by every member) rises faster than the node-count saving,
so ``cost_delta >= 0`` and the move is rejected. This is exactly the "over-general
insight that blurs two skills" failure the altitude audit guards against, made
quantitative.

Pure-Python and deterministic: the graph is byte-stable for a fixed library
snapshot, union-find labels each component by its minimum member id, the
contraction merges parallel edges in sorted order, and the generalizer is an
injected seam (no quota, no ``claude`` on PATH offline).

## Conformance

See ``tests/test_consolidation.py`` — the U5 required acceptance tests
(``test_consolidation_children_dormant_never_retired``,
``test_parent_carries_generalizes_from_to_all_children``,
``test_consolidation_rejected_when_cost_rises``) and the retirement test
(``test_retirement_is_usage_conditioned_survival``) map each R11/R12 invariant to
a behavioral test.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable

from agent_families import lifecycle
from agent_families.reflector import graphbuild
from agent_families.reflector import objective as obj
from agent_families.store import Store
from agent_families.vecindex import VecIndex

__all__ = [
    "ParentAtom",
    "GeneralizerFn",
    "default_generalizer",
    "ConsolidationParams",
    "ConsolidationResult",
    "active_flow_graph",
    "connected_components",
    "centroid",
    "consolidation_cost_delta",
    "removal_cost_delta",
    "consolidate",
]


# --- the generalizer seam (R11) ----------------------------------------------------
#
# Synthesize the general parent atom at the right altitude. The live binding routes
# the admission gate's generalize/altitude-audit (``run_admission_gate``); the suite
# injects a deterministic fake. The gate owns generalization (DESIGN §13) — this is
# the same seam shape the reflector and ingest path already lean on.


@dataclass(frozen=True)
class ParentAtom:
    """The synthesized general parent's schema'd atom (Operation-1 shape).

    ``negative_scope`` ("when NOT to apply") is mandatory — the altitude audit's
    output; ``rationale`` ("because Z") is optional.
    """

    precondition: str
    action: str
    expected_outcome: str
    negative_scope: str
    rationale: str | None = None


GeneralizerFn = Callable[[Store, Sequence[int]], ParentAtom]


def default_generalizer(store: Store, child_ids: Sequence[int]) -> ParentAtom:
    """Deterministic offline general parent (the live judge/gate overrides).

    Synthesizes a stable, child-derived parent atom so a consolidation move without
    a live generalizer still produces a provenance-bearing, altitude-raised parent.
    The text is a pure function of the (sorted) child ids, so the move is byte-stable
    run-to-run.
    """
    ids = sorted(child_ids)
    return ParentAtom(
        precondition=(
            "the conditions shared across the corroborating specifics"
            f" {ids}"
        ),
        action="apply the single generalized rule those specifics share",
        expected_outcome="the shared expected outcome holds for the general case",
        negative_scope=(
            "when a specific's precondition diverges from the shared general"
            " condition (the specifics are not interchangeable)"
        ),
        rationale=f"consolidated parent generalizing insights {ids}",
    )


@dataclass(frozen=True)
class ConsolidationParams:
    """Caller-supplied consolidation tunables (routed from thresholds.toml, U9)."""

    knn_k: int = 15
    module_overhead_bits: float = obj.DEFAULT_MODULE_OVERHEAD_BITS

    def __post_init__(self) -> None:
        if self.knn_k < 1:
            raise ValueError(f"knn_k must be >= 1, got {self.knn_k}")
        if self.module_overhead_bits < 0:
            raise ValueError(
                f"module_overhead_bits must be >= 0, got {self.module_overhead_bits}"
            )


@dataclass(frozen=True)
class ConsolidationResult:
    """The outcome of one consolidation move — adopted (one snapshot) or rejected."""

    adopted: bool
    cost_delta: float
    child_insight_ids: tuple[int, ...]
    parent_insight_id: int | None = None
    snapshot_id: int | None = None
    generalizes_from_edges: int = 0
    reason: str = ""


class ConsolidationError(Exception):
    """A broken consolidation precondition; nothing was written."""


# --- the flow graph + the partition it is scored against ---------------------------


def active_flow_graph(
    vec: VecIndex, *, k: int
) -> tuple[list[int], list[tuple[int, int, float]]]:
    """The v1 similarity flow graph over the active insight set (U2/R4).

    Returns ``(node_ids, edges)`` where ``node_ids`` is every active insight with a
    vec row (ascending — including the isolated ones that carry no edge) and
    ``edges`` is the mutual-kNN + Tanimoto edge list. Deterministic for a fixed
    library snapshot.
    """
    neighbors = vec.all_neighbors(k, on="full", statuses=("active",))
    node_ids = sorted(neighbors)
    edges = graphbuild.build_similarity_graph(vec, k=k, on="full", statuses=("active",))
    return node_ids, edges


def connected_components(
    node_ids: Sequence[int], edges: Sequence[tuple[int, int, float]]
) -> dict[int, int]:
    """Partition the node set into the flow graph's connected components (R11).

    Each component is a module; isolated nodes are their own singleton module. The
    component label is its **minimum member id** (union-by-min), so the partition is
    byte-stable. This is the v1 incumbent grouping the per-move delta is scored
    against before derive has authored modules.
    """
    root: dict[int, int] = {n: n for n in node_ids}

    def find(x: int) -> int:
        while root[x] != x:
            root[x] = root[root[x]]
            x = root[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if ra < rb:  # union-by-min: the root is the lowest member id
            root[rb] = ra
        else:
            root[ra] = rb

    for src, dst, _weight in edges:
        if src in root and dst in root:
            union(src, dst)
    return {n: find(n) for n in node_ids}


def centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    """Component-wise mean of the vectors (the synthesized parent's vector)."""
    if not vectors:
        raise ConsolidationError("cannot take the centroid of zero vectors")
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[d] for v in vectors) / n for d in range(dim)]


def _contract_children(
    node_ids: Sequence[int],
    edges: Sequence[tuple[int, int, float]],
    child_ids: Sequence[int],
    parent_node: int,
) -> tuple[list[int], list[tuple[int, int, float]]]:
    """Contract the children into one ``parent_node`` (the consolidation after-graph).

    Every edge incident to a child is re-pointed to ``parent_node``; child↔child
    edges collapse to a dropped self-loop; parallel edges merge by summing weight.
    The parent inherits the *union* of the children's external connectivity — so a
    parent that would bridge two distinct communities shows up as one merged
    component, which is what makes a bad consolidation raise ``cost(G)``.
    """
    children = set(child_ids)
    after_nodes = [n for n in node_ids if n not in children]
    after_nodes.append(parent_node)
    merged: dict[tuple[int, int], float] = {}
    for src, dst, weight in edges:
        u = parent_node if src in children else src
        v = parent_node if dst in children else dst
        if u == v:  # internal child↔child edge → self-loop, carries no structure
            continue
        key = (u, v) if u <= v else (v, u)
        merged[key] = merged.get(key, 0.0) + float(weight)
    after_edges = [(u, v, merged[(u, v)]) for (u, v) in sorted(merged)]
    return after_nodes, after_edges


def _remove_node(
    node_ids: Sequence[int],
    edges: Sequence[tuple[int, int, float]],
    victim: int,
) -> tuple[list[int], list[tuple[int, int, float]]]:
    """Drop ``victim`` and its incident edges (the retirement after-graph, R12)."""
    after_nodes = [n for n in node_ids if n != victim]
    after_edges = [
        (src, dst, weight)
        for (src, dst, weight) in edges
        if src != victim and dst != victim
    ]
    return after_nodes, after_edges


def consolidation_cost_delta(
    node_ids: Sequence[int],
    edges: Sequence[tuple[int, int, float]],
    child_ids: Sequence[int],
    *,
    module_overhead_bits: float = obj.DEFAULT_MODULE_OVERHEAD_BITS,
) -> float:
    """``cost(after) - cost(before)`` for contracting ``child_ids`` into one parent.

    Negative ⇒ the consolidation compresses the store and is adopted (strict
    ``< 0``). Both states are scored on their own connected-components partition (the
    move changes the node set and the membership), exactly the isolated-per-move
    contract :func:`objective.cost_delta` was built for.
    """
    parent_node = (max(node_ids) + 1) if node_ids else 0
    before_partition = connected_components(node_ids, edges)
    after_nodes, after_edges = _contract_children(
        node_ids, edges, child_ids, parent_node
    )
    after_partition = connected_components(after_nodes, after_edges)
    return obj.cost_delta(
        obj.GraphState(before_partition, edges),
        obj.GraphState(after_partition, after_edges),
        module_overhead_bits=module_overhead_bits,
    )


def removal_cost_delta(
    node_ids: Sequence[int],
    edges: Sequence[tuple[int, int, float]],
    victim: int,
    *,
    module_overhead_bits: float = obj.DEFAULT_MODULE_OVERHEAD_BITS,
) -> float:
    """``cost(after) - cost(before)`` for removing ``victim`` from the active graph.

    The retirement per-move delta (R12): a usage-dead, peripheral insight lowers
    ``cost(G)`` on removal (fewer nodes, one fewer codebook); a load-bearing bridge
    node would raise it (its removal shatters a module). Negative ⇒ adopt.
    """
    before_partition = connected_components(node_ids, edges)
    after_nodes, after_edges = _remove_node(node_ids, edges, victim)
    after_partition = connected_components(after_nodes, after_edges)
    return obj.cost_delta(
        obj.GraphState(before_partition, edges),
        obj.GraphState(after_partition, after_edges),
        module_overhead_bits=module_overhead_bits,
    )


# --- parent synthesis + the move ---------------------------------------------------


def _parent_content_hash(atom: ParentAtom, child_ids: Sequence[int]) -> str:
    """A unique content hash for the synthesized parent (the dedup key, R5 shape)."""
    payload = json.dumps(
        {
            "precondition": atom.precondition,
            "action": atom.action,
            "expected_outcome": atom.expected_outcome,
            "children": sorted(child_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return "consolidated:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def consolidate(
    store: Store,
    vec: VecIndex,
    *,
    child_insight_ids: Sequence[int],
    params: ConsolidationParams = ConsolidationParams(),
    generalizer_fn: GeneralizerFn | None = None,
) -> ConsolidationResult:
    """Consolidate a community of specifics into one general parent (R11).

    Scores the move as an isolated ``cost(G)`` delta and **adopts it only if it
    strictly lowers cost**. On adoption: synthesize the parent
    (``provenance=consolidated``) at the gate's altitude, insert it active with a
    centroid vector, and demote the children to ``dormant`` via
    :func:`lifecycle.demote_to_dormant` — which links a ``generalizes_from`` edge
    from the parent to every child and mints exactly one snapshot. On rejection:
    write nothing (no parent minted, no child demoted, the active set unchanged).

    Raises :class:`ConsolidationError` BEFORE any write if a child is unknown,
    retired, lacks a vec row, or the child set is empty — so a failed move, like a
    rejected one, leaves the store untouched.
    """
    children = tuple(dict.fromkeys(child_insight_ids))  # de-dup, preserve order
    if not children:
        raise ConsolidationError("consolidate requires at least one child insight")

    # Validate up front — a child must be a real, active insight with a vec row.
    child_vectors: list[list[float]] = []
    for child_id in children:
        row = store.get_insight(child_id)
        if row is None:
            raise ConsolidationError(f"child insight {child_id} does not exist")
        if row["status"] != "active":
            raise ConsolidationError(
                f"child insight {child_id} is '{row['status']}', not active —"
                " consolidation operates over the active community"
            )
        child_vectors.append(vec.get_vector(child_id, on="full"))

    # Score the move as an isolated cost(G) delta over the v1 similarity flow graph.
    node_ids, edges = active_flow_graph(vec, k=params.knn_k)
    delta = consolidation_cost_delta(
        node_ids, edges, children, module_overhead_bits=params.module_overhead_bits
    )

    if delta >= 0:
        return ConsolidationResult(
            adopted=False,
            cost_delta=delta,
            child_insight_ids=children,
            reason=(
                f"cost_delta {delta:.4f} >= 0: consolidation does not compress the"
                " store (it would bridge distinct communities) — rejected, no write"
            ),
        )

    # Adopt: synthesize the general parent, insert it active, demote the children.
    generalizer = generalizer_fn if generalizer_fn is not None else default_generalizer
    atom = generalizer(store, children)
    parent_vector = centroid(child_vectors)
    with store.transaction():
        parent_id = store.insert_insight(
            precondition=atom.precondition,
            action=atom.action,
            expected_outcome=atom.expected_outcome,
            content_hash=_parent_content_hash(atom, children),
            status="active",
            provenance="consolidated",
            negative_scope=atom.negative_scope,
            rationale=atom.rationale,
        )
        vec.insert(parent_id, parent_vector, parent_vector)

    demote = lifecycle.demote_to_dormant(store, children, parent_id)

    return ConsolidationResult(
        adopted=True,
        cost_delta=delta,
        child_insight_ids=children,
        parent_insight_id=parent_id,
        snapshot_id=demote.snapshot_id,
        generalizes_from_edges=len(children),
        reason=(
            f"adopted: cost_delta {delta:.4f} < 0; parent {parent_id} generalizes"
            f" {len(children)} now-dormant children, snapshot {demote.snapshot_id}"
        ),
    )
