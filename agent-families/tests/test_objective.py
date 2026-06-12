"""The organization objective — `cost(G) = L(G) + L(traces|G)` (plan-009 U1, R1-R3).

Fully offline, zero quota, pure deterministic arithmetic — no model, no `claude`, no
RNG. The fixtures are tiny seeded flow graphs with a *known* optimal partition; the
suite proves the objective selects it, that the per-module overhead term is
load-bearing (not decorative), that the cost decomposes into the named §6a terms, and
that selection is strict and byte-deterministic.

## Conformance (U1 required acceptance tests → invariant)

| Invariant (plan-009 U1) | Test |
|---|---|
| correct 2-module partition is the *unique* argmin; strictly beats merged AND shattered | `test_correct_partition_strictly_beats_merged_and_shattered` |
| per-module overhead penalizes both extremes; zeroing it lets an extreme win (term is load-bearing) | `test_overhead_penalizes_both_extremes` |
| cost = routing + locate (L(traces|G)) + count + overhead (L(G)); map-equation matches the closed form | `test_cost_is_routing_plus_locate_plus_storage` |
| `beats_incumbent` is strict (`<`, no ties adopted) and deterministic (byte-identical floats) | `test_beats_incumbent_is_strict_and_deterministic` |

Supporting (not required, but guard the API surface):

- `cost_delta` is `cost(after) - cost(before)` and signs correctly: `test_cost_delta_signs_moves`
- the pinned constant is documented (provenance present): `test_overhead_constant_is_documented`
"""

from __future__ import annotations

import math

from agent_families.reflector import objective as obj
from agent_families.reflector.objective import (
    DEFAULT_MODULE_OVERHEAD_BITS,
    GraphState,
    beats_incumbent,
    cost,
    cost_breakdown,
    cost_delta,
    map_equation_codelength,
    partition_from_sets,
)

# --- the seeded 2-cluster flow graph ----------------------------------------------
#
# Two dense 4-cliques A={0,1,2,3}, B={4,5,6,7}, joined by a single sparse bridge
# (3,4). Unit edge weights. This is the textbook "two communities, one bridge" graph
# whose unique correct partition is {A, B}.
_INTRA_A = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
_INTRA_B = [(4, 5), (4, 6), (4, 7), (5, 6), (5, 7), (6, 7)]
_BRIDGE = [(3, 4)]
FLOW_GRAPH = [(u, v, 1.0) for (u, v) in _INTRA_A + _INTRA_B + _BRIDGE]

CORRECT = partition_from_sets([[0, 1, 2, 3], [4, 5, 6, 7]])
MERGED = partition_from_sets([[0, 1, 2, 3, 4, 5, 6, 7]])
SHATTERED = partition_from_sets([[n] for n in range(8)])
# A balanced-but-wrong 2-split (mixes the clusters): same module sizes as CORRECT, so
# it can only lose on the *flow* (routing) term — proves selection is flow-aware, not
# size-counting.
MIS_SPLIT = partition_from_sets([[0, 1, 4, 5], [2, 3, 6, 7]])


def test_correct_partition_strictly_beats_merged_and_shattered():
    """On the seeded 2-cluster graph the correct partition is the unique argmin."""
    c_correct = cost(CORRECT, FLOW_GRAPH)
    c_merged = cost(MERGED, FLOW_GRAPH)
    c_shattered = cost(SHATTERED, FLOW_GRAPH)
    c_mis = cost(MIS_SPLIT, FLOW_GRAPH)

    # strictly beats both extremes (not merely <=)
    assert c_correct < c_merged
    assert c_correct < c_shattered

    # unique argmin over the whole candidate set, including a same-size wrong split
    candidates = {
        "correct": c_correct,
        "merged": c_merged,
        "shattered": c_shattered,
        "mis_split": c_mis,
    }
    best = min(candidates, key=candidates.__getitem__)
    assert best == "correct"
    # uniqueness: no other candidate ties the minimum
    others = [v for k, v in candidates.items() if k != "correct"]
    assert all(c_correct < v for v in others)


def test_overhead_penalizes_both_extremes():
    """The per-module overhead is load-bearing, not decorative.

    With the pinned overhead, BOTH the all-singletons and the one-module extreme
    score above the correct partition. Removing the overhead term (set to 0) lets one
    extreme win — proving the term is what tames over-splitting.
    """
    # pinned overhead: correct beats both extremes
    c_correct = cost(CORRECT, FLOW_GRAPH, module_overhead_bits=DEFAULT_MODULE_OVERHEAD_BITS)
    c_merged = cost(MERGED, FLOW_GRAPH, module_overhead_bits=DEFAULT_MODULE_OVERHEAD_BITS)
    c_shattered = cost(SHATTERED, FLOW_GRAPH, module_overhead_bits=DEFAULT_MODULE_OVERHEAD_BITS)
    assert c_correct < c_merged
    assert c_correct < c_shattered

    # zero the overhead term: an extreme now wins, correct is no longer the argmin
    z_correct = cost(CORRECT, FLOW_GRAPH, module_overhead_bits=0.0)
    z_merged = cost(MERGED, FLOW_GRAPH, module_overhead_bits=0.0)
    z_shattered = cost(SHATTERED, FLOW_GRAPH, module_overhead_bits=0.0)
    z = {"correct": z_correct, "merged": z_merged, "shattered": z_shattered}
    winner = min(z, key=z.__getitem__)
    assert winner in ("merged", "shattered")  # an EXTREME wins without the term
    assert winner != "correct"
    # concretely on this fixture the all-singletons extreme is freed (locate -> 0)
    assert z_shattered < z_correct


def test_cost_is_routing_plus_locate_plus_storage():
    """cost(G) decomposes as L(traces|G) (routing+locate) + L(G) (count+overhead)."""
    bd = cost_breakdown(CORRECT, FLOW_GRAPH)

    # the four named terms sum to the total
    assert math.isclose(
        bd.total,
        bd.routing_bits + bd.locate_bits + bd.insight_count_bits + bd.module_overhead_bits,
    )
    # the two §6a halves
    assert math.isclose(bd.traces_bits, bd.routing_bits + bd.locate_bits)
    assert math.isclose(bd.storage_bits, bd.insight_count_bits + bd.module_overhead_bits)
    assert math.isclose(bd.total, cost(CORRECT, FLOW_GRAPH))

    # concrete §6a values for the seeded graph (hand-computed):
    #   locate  = total_strength(26) * log2(|module|=4) = 52
    #   routing = 1 crossing edge (3,4) * log2(#modules=2) = 1
    #   count   = 8 active insights ; overhead = 2 modules * 4.0 = 8
    assert math.isclose(bd.locate_bits, 26.0 * math.log2(4))
    assert math.isclose(bd.routing_bits, 1.0 * math.log2(2))
    assert math.isclose(bd.insight_count_bits, 8.0)
    assert math.isclose(bd.module_overhead_bits, 2 * DEFAULT_MODULE_OVERHEAD_BITS)

    # map_equation_codelength on a hand-walked trajectory matches the closed form
    #   `log2(#modules entered) [on crossings] + log2(|module|) [per access]`.
    trajectory = [0, 1, 2, 3, 4, 5]  # one A->B crossing at 3->4
    sizes = obj.module_sizes(CORRECT)
    entered = {CORRECT[n] for n in trajectory}
    crossings = sum(
        1 for a, b in zip(trajectory, trajectory[1:]) if CORRECT[a] != CORRECT[b]
    )
    closed_form = sum(math.log2(sizes[CORRECT[n]]) for n in trajectory) + crossings * math.log2(
        len(entered)
    )
    assert math.isclose(map_equation_codelength(trajectory, CORRECT), closed_form)
    assert math.isclose(closed_form, 13.0)  # 6 accesses * log2(4)=12, +1 crossing bit


def test_beats_incumbent_is_strict_and_deterministic():
    """`beats_incumbent` is strict `<` (no ties adopted) and byte-deterministic."""
    # strictly-lower cost -> True
    assert beats_incumbent(CORRECT, MERGED, FLOW_GRAPH) is True
    # strictly-higher cost -> False
    assert beats_incumbent(MERGED, CORRECT, FLOW_GRAPH) is False
    # equal cost (same partition) -> False; a tie is NOT adopted
    assert beats_incumbent(CORRECT, CORRECT, FLOW_GRAPH) is False

    # determinism: identical inputs -> byte-identical floats across calls
    a = cost(CORRECT, FLOW_GRAPH)
    b = cost(CORRECT, FLOW_GRAPH)
    assert a == b  # exact equality, no float drift
    assert cost(SHATTERED, FLOW_GRAPH) == cost(SHATTERED, FLOW_GRAPH)


# --- supporting guards -------------------------------------------------------------


def test_cost_delta_signs_moves():
    """`cost_delta(before, after)` = cost(after) - cost(before); sign drives adoption."""
    improving = cost_delta(
        GraphState(MERGED, FLOW_GRAPH), GraphState(CORRECT, FLOW_GRAPH)
    )
    worsening = cost_delta(
        GraphState(CORRECT, FLOW_GRAPH), GraphState(MERGED, FLOW_GRAPH)
    )
    assert improving < 0  # MERGED -> CORRECT lowers cost: adopt
    assert worsening > 0  # CORRECT -> MERGED raises cost: reject
    assert math.isclose(improving, -worsening)
    # exact identity with the direct cost difference
    assert math.isclose(
        improving, cost(CORRECT, FLOW_GRAPH) - cost(MERGED, FLOW_GRAPH)
    )


def test_overhead_constant_is_documented():
    """The pinned per-module bit-cost carries provenance in its source comment."""
    assert DEFAULT_MODULE_OVERHEAD_BITS > 0
    # provenance lives in the module: the constant is named and explained, not a bare
    # magic number sprinkled through the code.
    src = (
        __import__("agent_families.reflector.objective", fromlist=["__file__"]).__file__
    )
    text = open(src, encoding="utf-8").read()
    assert "PROVENANCE:" in text
    assert "TUNING METRIC:" in text
    assert "DEFAULT_MODULE_OVERHEAD_BITS" in text
