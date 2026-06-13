"""The organization objective — `cost(G) = L(G) + L(traces | G)` (plan-009 U1, R1-R3).

This is the single authority of the R3 slow loop (DESIGN §6a): everything in §4-§6
defers to it. Partitioners (Leiden, Infomap — plan-009 U3) are only *proposal
generators*; this module *selects* whole partitions and *scores* the per-move
consolidate/retire deltas. Their internal objectives (modularity, the map equation)
never decide — `cost(G)` does. Build this first; pin its one underspecified number
(the per-module codebook overhead) before any partitioner is wired.

## The v1 operational definition (pinned here)

``cost(partition, flow_graph) = L(traces | partition) + L(partition)``

**`L(traces | partition)` — the map-equation codelength** of a random walker on the
flow graph (nodes = insights, edge weight = flow). A two-level codebook: the walker's
trajectory is described by *between-module routing bits* (which module — the index
codebook) plus *within-module locate bits* (which insight inside the module). Per the
§6a closed form, an access to an insight in module ``m`` costs

    locate  = log2(|m|)                  # paid every access (∝ visit frequency)
    routing = log2(#modules)             # paid only when the walk crosses a boundary

A well-matched partition keeps the walker inside dense communities, so crossings (and
thus routing bits) are rare while modules stay small (low locate bits). Merging
everything drives locate up (log2(N) per access); shattering into singletons drives
locate to 0 but pays routing on *every* step — and, decisively, pays the storage
overhead below for N codebooks.

Over the **flow graph** (which carries no explicit trajectory) the expected
codelength is computed analytically from the natural random walk: each undirected
edge ``(u, v, w)`` contributes ``w`` units of visit flow to both endpoints (locate)
and, when ``u`` and ``v`` sit in different modules, ``w`` units of crossing flow
(routing). This equals the expected per-walk codelength and needs no RNG — it is
deterministic. :func:`map_equation_codelength` is the same quantity for an *explicit*
hand-walked trajectory (used to verify the closed form).

**`L(partition)` — storage bits** = the active-insight count (one bit per active
insight; constant across partitions of the same node set, carried for honesty) **plus
a per-module codebook/description overhead** — the load-bearing Minimum-Description-
Length term. Without it, over-splitting is free (singletons drive locate to 0 and win);
with it, declaring N module codebooks is expensive and the balanced partition wins.

## The flow-graph seam (R2)

At v1 the flow graph is the **similarity graph** (mutual-kNN + Tanimoto, plan-009 U2)
used as a *proxy* for usage flow during cold start — before co-retrieval traces are
dense. This slightly undercuts "measured on real usage traces": once ``fitness_events``
accumulate, co-retrieval frequency blends into the edge weights (the seam, not the v1
default). The objective code is indifferent to which produced the weights — it scores
whatever flow graph it is handed.

## The pinned constant (KTD — the single underspecified number)

:data:`DEFAULT_MODULE_OVERHEAD_BITS` is the per-module codebook overhead. It is the
one tunable here; carried as a documented default (the `maintenance.py` Params
precedent — run-assembly will route the live value from ``thresholds.toml`` in
plan-009 U9; nothing here reads config). See its docstring for provenance and tuning
metric.

Pure Python (numpy optional, unused), deterministic: every sum iterates in sorted id
order so two calls on identical inputs return byte-identical floats.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "DEFAULT_MODULE_OVERHEAD_BITS",
    "Partition",
    "FlowEdge",
    "GraphState",
    "CostBreakdown",
    "partition_from_sets",
    "module_sizes",
    "num_modules",
    "map_equation_codelength",
    "cost_breakdown",
    "cost",
    "beats_incumbent",
    "cost_delta",
]

# --- the pinned per-module codebook overhead (KTD) ---------------------------------
#
# PROVENANCE: §6a v1 provisional. No canonical paper pins this number; it is the MDL
# description cost of declaring one module's codebook (its boundary + index entry).
# 4.0 bits ≈ naming a module among ~16 candidates — small enough not to over-merge a
# genuinely 2-community graph, large enough that the N-singleton partition's N
# codebooks dominate. On the calibration fixtures (tests/test_objective.py) it makes
# the correct partition the unique argmin and is *load-bearing*: zeroing it lets the
# all-singletons extreme win.
# TUNING METRIC: derive-pass partition stability vs. held-out silhouette across
# libraries; re-pin when co-retrieval flow (R2 seam) replaces the similarity proxy.
DEFAULT_MODULE_OVERHEAD_BITS = 4.0

# A partition maps each active insight id -> its module id. Module ids are arbitrary
# labels (identity tracking across passes is plan-009 U4's job, not the objective's).
Partition = Mapping[int, int]

# An undirected flow edge: (src, dst, weight). Weight = flow (similarity at v1).
FlowEdge = tuple[int, int, float]


@dataclass(frozen=True)
class GraphState:
    """A (partition, flow_graph) pair — the unit a per-move delta is scored over.

    A consolidate/retire *move* changes both the active node set and the membership,
    so :func:`cost_delta` compares two whole states rather than editing one in place.
    """

    partition: Partition
    flow_graph: Sequence[FlowEdge]


@dataclass(frozen=True)
class CostBreakdown:
    """The decomposition of ``cost(G)`` into its named §6a terms (bits)."""

    routing_bits: float  # L(traces|G): between-module routing
    locate_bits: float  # L(traces|G): within-module locate
    insight_count_bits: float  # L(G): active-insight count
    module_overhead_bits: float  # L(G): per-module codebook overhead
    total: float

    @property
    def traces_bits(self) -> float:
        """``L(traces | G)`` = routing + locate."""
        return self.routing_bits + self.locate_bits

    @property
    def storage_bits(self) -> float:
        """``L(G)`` = insight count + per-module overhead."""
        return self.insight_count_bits + self.module_overhead_bits


# --- partition helpers -------------------------------------------------------------


def partition_from_sets(modules: Iterable[Iterable[int]]) -> dict[int, int]:
    """Build a `node -> module_id` partition from an iterable of member-id groups.

    Module ids are the (0-based) index of each group, in the order supplied.
    """
    out: dict[int, int] = {}
    for module_id, members in enumerate(modules):
        for node in members:
            out[node] = module_id
    return out


def module_sizes(partition: Partition) -> dict[int, int]:
    """`module_id -> number of insights assigned to it`."""
    sizes: dict[int, int] = {}
    for module_id in partition.values():
        sizes[module_id] = sizes.get(module_id, 0) + 1
    return sizes


def num_modules(partition: Partition) -> int:
    """Distinct module count in the partition (the routing-codebook size)."""
    return len(set(partition.values()))


# --- the map-equation codelength ---------------------------------------------------


def map_equation_codelength(
    trajectory: Sequence[int],
    partition: Partition,
) -> float:
    """Map-equation codelength of an explicit hand-walked trajectory (R1 closed form).

    ``= Σ_access log2(|module(access)|)   (locate, every access)``
    ``+ (#boundary crossings) · log2(#modules entered)   (routing)``

    ``#modules entered`` is the count of *distinct* modules the trajectory visits.
    Deterministic; pure arithmetic.
    """
    if not trajectory:
        return 0.0
    sizes = module_sizes(partition)
    entered = {partition[node] for node in trajectory}
    routing_unit = math.log2(len(entered)) if entered else 0.0

    locate = 0.0
    routing = 0.0
    prev_module: int | None = None
    for node in trajectory:
        module_id = partition[node]
        locate += math.log2(sizes[module_id])
        if prev_module is not None and module_id != prev_module:
            routing += routing_unit
        prev_module = module_id
    return locate + routing


def _canonical_edges(
    flow_graph: Sequence[FlowEdge],
    partition: Partition,
) -> list[tuple[int, int, float]]:
    """Undirected, deduped, weight-summed edge list over partition nodes, sorted.

    Edges with an endpoint outside the active partition are dropped (defensive: the
    flow graph may be built over a superset). Iteration order is fully sorted so
    every downstream sum is byte-stable.
    """
    merged: dict[tuple[int, int], float] = {}
    for src, dst, weight in flow_graph:
        if src not in partition or dst not in partition:
            continue
        if src == dst:  # self-edges carry no routing/locate signal
            continue
        key = (src, dst) if src <= dst else (dst, src)
        merged[key] = merged.get(key, 0.0) + float(weight)
    return [(u, v, merged[(u, v)]) for (u, v) in sorted(merged)]


def cost_breakdown(
    partition: Partition,
    flow_graph: Sequence[FlowEdge],
    *,
    module_overhead_bits: float = DEFAULT_MODULE_OVERHEAD_BITS,
) -> CostBreakdown:
    """Decompose ``cost(partition, flow_graph)`` into its four named §6a terms.

    The expected map-equation codelength of the natural random walk on ``flow_graph``:
    each undirected edge ``(u, v, w)`` contributes ``w`` visit-flow to *both* endpoints
    (locate, ∝ visit frequency = node strength) and, when ``u``/``v`` are in different
    modules, ``w`` crossing-flow (routing). Storage = active-insight count + a
    per-module codebook overhead.
    """
    sizes = module_sizes(partition)
    m = len(sizes)
    routing_unit = math.log2(m) if m > 0 else 0.0

    edges = _canonical_edges(flow_graph, partition)

    # locate: Σ_v strength(v) · log2(|module(v)|). Accumulate node strength first so
    # the per-node sum is deterministic (sorted node order).
    strength: dict[int, float] = {}
    routing_bits = 0.0
    for src, dst, weight in edges:
        strength[src] = strength.get(src, 0.0) + weight
        strength[dst] = strength.get(dst, 0.0) + weight
        if partition[src] != partition[dst]:
            routing_bits += weight * routing_unit

    locate_bits = 0.0
    for node in sorted(strength):
        locate_bits += strength[node] * math.log2(sizes[partition[node]])

    insight_count_bits = float(len(partition))
    module_overhead_total = m * module_overhead_bits
    total = locate_bits + routing_bits + insight_count_bits + module_overhead_total
    return CostBreakdown(
        routing_bits=routing_bits,
        locate_bits=locate_bits,
        insight_count_bits=insight_count_bits,
        module_overhead_bits=module_overhead_total,
        total=total,
    )


def cost(
    partition: Partition,
    flow_graph: Sequence[FlowEdge],
    *,
    module_overhead_bits: float = DEFAULT_MODULE_OVERHEAD_BITS,
) -> float:
    """``cost(G) = L(G) + L(traces | G)`` in bits. Lower is better. Deterministic."""
    return cost_breakdown(
        partition, flow_graph, module_overhead_bits=module_overhead_bits
    ).total


# --- selection API (R3) ------------------------------------------------------------


def beats_incumbent(
    candidate_partition: Partition,
    incumbent_partition: Partition,
    flow_graph: Sequence[FlowEdge],
    *,
    module_overhead_bits: float = DEFAULT_MODULE_OVERHEAD_BITS,
) -> bool:
    """Whole-partition selection: True iff ``candidate`` *strictly* lowers ``cost(G)``.

    Strict ``<``: a tie is NOT adopted (the incumbent holds). Both partitions are
    scored on the same ``flow_graph``. Deterministic — identical inputs give an
    identical decision.
    """
    candidate_cost = cost(
        candidate_partition, flow_graph, module_overhead_bits=module_overhead_bits
    )
    incumbent_cost = cost(
        incumbent_partition, flow_graph, module_overhead_bits=module_overhead_bits
    )
    return candidate_cost < incumbent_cost


def cost_delta(
    before: GraphState,
    after: GraphState,
    *,
    module_overhead_bits: float = DEFAULT_MODULE_OVERHEAD_BITS,
) -> float:
    """``cost(after) - cost(before)`` for an isolated per-move op (consolidate/retire).

    Negative ⇒ the move lowers the objective and is adopted (strictly: ``< 0``). A
    move changes both the node set and the membership, so each :class:`GraphState`
    carries its own flow graph (the objective never edits a partition move-by-move).
    """
    return cost(
        after.partition, after.flow_graph, module_overhead_bits=module_overhead_bits
    ) - cost(
        before.partition, before.flow_graph, module_overhead_bits=module_overhead_bits
    )
