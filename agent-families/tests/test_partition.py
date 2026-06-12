"""Partition proposers (plan-009 U3): Leiden + Infomap candidate generators.

Fully offline, zero quota, deterministic — no model, no `claude`, no native
wheel required. The two proposers each carry a deterministic pure-Python
fallback (greedy modularity for Leiden; greedy §6a-codelength minimization for
Infomap) that stands in when the native `graspologic_native` / `infomap` wheels
are absent (they are, in this wave — see `NEED_DEP.txt`). Because the §6a
objective selects among *whatever* candidates are proposed, the proposers only
have to be well-formed and byte-stable; this suite proves exactly that.

## Conformance (U3 required acceptance tests → invariant)

| Invariant (plan-009 U3, R6/R7) | Test |
|---|---|
| both proposers byte-stable under a fixed seed (canonical membership identical across runs) | `test_proposers_byte_stable_under_fixed_seed` |
| the seeded 2-cluster graph yields exactly two communities partitioning all ids (no unassigned, no overlap) | `test_two_cluster_graph_yields_two_communities` |
| graspologic-unavailable → Infomap-only candidates via an explicit typed branch, never a silent empty set | `test_infomap_only_fallback_path` |

Supporting (guard the API surface, not required):

- the Leiden sweep returns one candidate per resolution: `test_leiden_sweep_one_candidate_per_resolution`
- the orchestrator runs Leiden + Infomap when graspologic is available: `test_orchestrator_runs_both_when_graspologic_available`
- canonical relabelling makes membership label-order-independent: `test_membership_is_canonicalized`
"""

from __future__ import annotations

from agent_families.reflector import partition as part
from agent_families.reflector.partition import (
    propose_infomap,
    propose_leiden,
    propose_partitions,
)

# --- the seeded 2-cluster flow graph (same fixture shape as test_objective) --------
#
# Two dense 4-cliques A={0,1,2,3}, B={4,5,6,7}, joined by a single sparse bridge
# (3,4). Unit edge weights. The unique correct partition is {A, B}.
_INTRA_A = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
_INTRA_B = [(4, 5), (4, 6), (4, 7), (5, 6), (5, 7), (6, 7)]
_BRIDGE = [(3, 4)]
FLOW_GRAPH = [(u, v, 1.0) for (u, v) in _INTRA_A + _INTRA_B + _BRIDGE]

_CLUSTER_A = frozenset({0, 1, 2, 3})
_CLUSTER_B = frozenset({4, 5, 6, 7})


def _communities(membership):
    """The set of member-id frozensets, ignoring arbitrary community labels."""
    groups: dict[int, set[int]] = {}
    for node, community in membership.items():
        groups.setdefault(community, set()).add(node)
    return {frozenset(members) for members in groups.values()}


# --- R7 / determinism: byte-stable under a fixed seed -------------------------------


def test_proposers_byte_stable_under_fixed_seed():
    """Both proposers return byte-identical canonical membership across two
    consecutive in-process runs under the same seed. The canonical form (sorted
    (node, community) tuples) means set/dict iteration order cannot mask drift."""
    leiden_1 = propose_leiden(FLOW_GRAPH, resolutions=(1.0,), seed=7)
    leiden_2 = propose_leiden(FLOW_GRAPH, resolutions=(1.0,), seed=7)
    assert [c.canonical() for c in leiden_1] == [c.canonical() for c in leiden_2]

    info_1 = propose_infomap(FLOW_GRAPH, seed=7)
    info_2 = propose_infomap(FLOW_GRAPH, seed=7)
    assert info_1.canonical() == info_2.canonical()


# --- R6: the two-cluster graph yields two communities ------------------------------


def test_two_cluster_graph_yields_two_communities():
    """On the seeded 2-cluster graph BOTH proposers emit exactly two top-level
    communities partitioning all eight active ids — no insight unassigned, no
    overlap. (The §6a-optimal partition is exactly {A, B}.)"""
    leiden = propose_leiden(FLOW_GRAPH, resolutions=(1.0,), seed=7)[0]
    infomap = propose_infomap(FLOW_GRAPH, seed=7)

    for cand in (leiden, infomap):
        assert cand.num_communities == 2
        # every active id assigned exactly once (a partition, no overlap/gaps)
        assert set(cand.membership) == set(range(8))
        assert _communities(cand.membership) == {_CLUSTER_A, _CLUSTER_B}


# --- R7: graspologic unavailable → Infomap-only, explicit typed branch -------------


def test_infomap_only_fallback_path(monkeypatch):
    """With the graspologic seam mocked unavailable (no 3.12 wheel), the
    partitioner still produces candidates via Infomap ALONE, and the fallback is
    an explicit typed branch (`fallback='infomap-only'`), never a silent empty
    candidate set."""
    monkeypatch.setattr(part, "graspologic_available", lambda: False)

    result = propose_partitions(FLOW_GRAPH, seed=7)

    assert result.leiden_ran is False
    assert result.fallback == "infomap-only"
    # non-empty, and every candidate is from Infomap (no Leiden candidate)
    assert result.candidates  # NOT a silent empty set
    assert all(c.proposer == "infomap" for c in result.candidates)
    # the Infomap candidate is still a well-formed two-community partition
    only = result.candidates[0]
    assert only.num_communities == 2
    assert _communities(only.membership) == {_CLUSTER_A, _CLUSTER_B}


# --- supporting API-surface guards -------------------------------------------------


def test_leiden_sweep_one_candidate_per_resolution():
    """The Leiden proposer returns one candidate per resolution in the sweep,
    each tagged with its resolution."""
    resolutions = (0.5, 1.0, 2.0)
    cands = propose_leiden(FLOW_GRAPH, resolutions=resolutions, seed=7)
    assert len(cands) == len(resolutions)
    assert [c.resolution for c in cands] == list(resolutions)
    assert all(c.proposer == "leiden" for c in cands)


def test_orchestrator_runs_both_when_graspologic_available(monkeypatch):
    """When graspologic is available the orchestrator runs the Leiden sweep AND
    Infomap (no fallback flag); both proposers appear in the candidate set."""
    monkeypatch.setattr(part, "graspologic_available", lambda: True)

    result = propose_partitions(FLOW_GRAPH, resolutions=(1.0,), seed=7)

    assert result.leiden_ran is True
    assert result.fallback is None
    proposers = {c.proposer for c in result.candidates}
    assert proposers == {"leiden", "infomap"}


def test_membership_is_canonicalized():
    """Community ids are relabelled by ascending min member id, so the community
    containing node 0 is always id 0 — membership is label-order-independent."""
    cand = propose_infomap(FLOW_GRAPH, seed=7)
    # node 0 sits in the lowest-min-member community → canonical id 0
    assert cand.membership[0] == 0
    # community ids are a dense 0..k-1 range
    assert set(cand.membership.values()) == set(range(cand.num_communities))


def test_empty_graph_is_handled():
    """An empty edge list with explicit isolated nodes yields singleton
    communities (no crash, deterministic)."""
    cand = propose_infomap([], seed=7, nodes=[5, 9, 2])
    assert set(cand.membership) == {2, 5, 9}
    assert cand.num_communities == 3  # all isolated → singletons
