"""Partition proposers (plan-009 U3, R6/R7).

Two candidate **whole-partition** proposers feeding the single §6a objective
(``objective.py``, U1): hierarchical **Leiden** (Constant Potts Model across a
small resolution sweep) and **Infomap** (the map-equation optimizer). Neither
proposer's internal objective is authoritative — they only *generate candidates*;
``objective.cost(G)`` *selects* among them (U4). Which proposer wins is telemetry,
not a hardcoded choice (DESIGN §6, KTD).

## The native backend seam, and the offline pure-Python fallback

The matched native optimizers are ``graspologic_native.hierarchical_leiden`` and
the ``infomap`` library (R7). Those wheels are **not installed in this wave**
(recorded in ``NEED_DEP.txt``; the frozen offline gate may not add deps), so each
proposer carries a **deterministic pure-Python fallback** that stands in when the
native wheel is absent. The fallback is greedy agglomerative community detection:

* Leiden fallback — greedy **modularity** maximization (resolution-parameterized
  CPM null model), the standard CNM agglomeration.
* Infomap fallback — greedy minimization of the **§6a map-equation codelength**
  itself (``objective.cost``), so the fallback is *literally* the matched optimizer
  the native Infomap approximates.

Because §6a selects among whatever candidates are proposed, swapping the backend
never changes correctness — only candidate quality. The native adapters below are
used the moment their wheel resolves; until then the pure-Python path is the one
the offline suite exercises and proves byte-stable.

**R7's Infomap-only fallback** (graspologic wheel missing) is an *explicit typed
branch* in :func:`propose_partitions`, never a silent empty candidate set: when
:func:`graspologic_available` is False the Leiden proposer is skipped and the
candidate set comes from Infomap alone, flagged ``fallback="infomap-only"``.

Pure-Python, deterministic for a fixed edge list. Community ids are canonicalized
(relabelled by ascending minimum member id) so set-iteration order can never leak
into the membership — two runs on identical inputs return byte-identical
memberships. The ``seed`` is accepted for native-backend parity and the
determinism contract; the pure-Python path is seed-independent (it is exact, not
sampled).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from agent_families.reflector import objective as obj

__all__ = [
    "DEFAULT_SEED",
    "DEFAULT_RESOLUTIONS",
    "CandidatePartition",
    "ProposalSet",
    "graspologic_available",
    "infomap_available",
    "propose_leiden",
    "propose_infomap",
    "propose_partitions",
]

# A fixed seed keeps the native backends (graspologic takes ``random_seed``;
# Infomap takes ``seed``) byte-stable for the offline membership-stability
# discipline. The pure-Python fallback is exact and ignores it.
DEFAULT_SEED = 1234

# CPM resolution sweep for the Leiden proposer (R6). Higher resolution → more,
# smaller communities; the sweep hands several granularities to the §6a scorer,
# which selects the whole partition that minimizes cost(G).
DEFAULT_RESOLUTIONS: tuple[float, ...] = (0.5, 1.0, 2.0)

# A flow/similarity edge: (src, dst, weight). Same shape as graphbuild output.
Edge = tuple[int, int, float]


@dataclass(frozen=True)
class CandidatePartition:
    """One proposed whole partition: ``insight_id -> community id``.

    Community ids are canonical (relabelled by ascending minimum member id), so a
    partition is identified by its membership alone — the labels carry no order
    dependence. ``proposer`` / ``backend`` are telemetry (which generator and which
    backend produced it); ``resolution`` is set for Leiden candidates only.
    """

    proposer: str  # "leiden" | "infomap"
    membership: Mapping[int, int]
    resolution: float | None = None
    backend: str = "python"  # "graspologic" | "infomap" | "python"

    @property
    def num_communities(self) -> int:
        return len({c for c in self.membership.values()})

    def canonical(self) -> tuple[tuple[int, int], ...]:
        """Sorted ``(node, community)`` tuples — a hashable byte-stable identity."""
        return tuple(sorted(self.membership.items()))


@dataclass(frozen=True)
class ProposalSet:
    """The candidate set handed to the §6a selector, plus backend telemetry.

    ``fallback`` is ``"infomap-only"`` when the graspologic Leiden wheel is absent
    (R7's explicit typed fallback branch), else ``None``.
    """

    candidates: tuple[CandidatePartition, ...]
    leiden_ran: bool
    fallback: str | None = None


# --- backend availability (the native seam) ----------------------------------------


def graspologic_available() -> bool:
    """True iff the ``graspologic_native`` wheel is importable (Leiden backend)."""
    return importlib.util.find_spec("graspologic_native") is not None


def infomap_available() -> bool:
    """True iff the ``infomap`` wheel is importable (Infomap backend)."""
    return importlib.util.find_spec("infomap") is not None


# --- shared pure-Python machinery --------------------------------------------------


def _nodes_of(
    edges: Sequence[Edge], nodes: Sequence[int] | None
) -> list[int]:
    """All node ids: edge endpoints plus any supplied isolated nodes, sorted."""
    seen: set[int] = set()
    for src, dst, _w in edges:
        seen.add(src)
        seen.add(dst)
    if nodes:
        seen.update(nodes)
    return sorted(seen)


def _canonicalize(membership: Mapping[int, int]) -> dict[int, int]:
    """Relabel communities 0,1,2,… by ascending minimum member id.

    Removes any dependence on the arbitrary community labels assigned during
    agglomeration, so identical structure → identical membership dict.
    """
    comm_min: dict[int, int] = {}
    for node, community in membership.items():
        if community not in comm_min or node < comm_min[community]:
            comm_min[community] = node
    order = sorted(comm_min, key=lambda c: comm_min[c])
    relabel = {community: new for new, community in enumerate(order)}
    return {node: relabel[membership[node]] for node in sorted(membership)}


def _greedy_merge(
    edges: Sequence[Edge],
    nodes: Sequence[int],
    score_fn,
) -> dict[int, int]:
    """Greedy agglomerative community detection maximizing ``score_fn(membership)``.

    Start every node in its own community; each round, over every pair of
    *adjacent* communities (sharing at least one edge) try the merge, keep the one
    with the greatest score gain, and stop when no merge improves the score. Ties
    are broken deterministically by the pair's ``(min-member, min-member)`` reps,
    so the result is independent of dict/set iteration order.
    """
    membership: dict[int, int] = {n: n for n in nodes}
    if not edges:
        return _canonicalize(membership)

    while True:
        # Adjacent community pairs (canonical c < d).
        pairs: set[tuple[int, int]] = set()
        for src, dst, _w in edges:
            cu, cv = membership[src], membership[dst]
            if cu != cv:
                pairs.add((cu, cv) if cu < cv else (cv, cu))
        if not pairs:
            break

        # Representative (min member id) per live community, for tie-breaking.
        rep: dict[int, int] = {}
        for node in nodes:
            community = membership[node]
            if community not in rep or node < rep[community]:
                rep[community] = node

        base = score_fn(membership)
        best: tuple[float, int, int, int, int] | None = None
        for c, d in sorted(pairs):
            trial = {
                n: (c if membership[n] == d else membership[n]) for n in nodes
            }
            gain = score_fn(trial) - base
            if gain <= 1e-12:
                continue
            lo, hi = sorted((rep[c], rep[d]))
            cand = (-gain, lo, hi, c, d)
            if best is None or cand < best:
                best = cand

        if best is None:
            break
        _, _, _, c, d = best
        membership = {
            n: (c if membership[n] == d else membership[n]) for n in nodes
        }

    membership = _refine(membership, edges, nodes, score_fn)
    return _canonicalize(membership)


def _refine(
    membership: Mapping[int, int],
    edges: Sequence[Edge],
    nodes: Sequence[int],
    score_fn,
) -> dict[int, int]:
    """Local node-move polish (Louvain/Leiden local-moving phase).

    Greedy agglomeration alone can stall in a local optimum (e.g. a bridge node
    fused to the wrong side). Each pass moves every node, in id order, to the
    adjacent community — or a fresh singleton — that most improves ``score_fn``,
    repeating until a full pass yields no strict improvement. Deterministic: nodes
    and candidate targets are visited in sorted order; only strict gains apply.
    """
    current: dict[int, int] = dict(membership)
    # adjacency: each node's incident neighbors (for the candidate target set)
    neighbors: dict[int, set[int]] = {n: set() for n in nodes}
    for src, dst, _w in edges:
        if src == dst:
            continue
        neighbors[src].add(dst)
        neighbors[dst].add(src)

    changed = True
    while changed:
        changed = False
        base = score_fn(current)
        fresh = max(current.values()) + 1  # an unused label to split a node off
        for node in nodes:
            cur = current[node]
            targets = {current[nbr] for nbr in neighbors[node]}
            targets.add(fresh)
            targets.discard(cur)
            best_target: int | None = None
            best_score = base
            for target in sorted(targets):
                trial = dict(current)
                trial[node] = target
                score = score_fn(trial)
                if score > best_score + 1e-12:
                    best_score = score
                    best_target = target
            if best_target is not None:
                current[node] = best_target
                base = best_score
                fresh = max(fresh, best_target + 1)
                changed = True
    return current


def _modularity_score(resolution: float, edges: Sequence[Edge]):
    """A resolution-parameterized modularity scorer over a fixed edge list.

    ``Q = Σ_c [ L_c/m − γ (K_c / 2m)^2 ]`` with ``L_c`` the within-community edge
    weight and ``K_c`` the community strength (CPM null model). Higher is better.
    """
    strength: dict[int, float] = {}
    m = 0.0
    for src, dst, weight in edges:
        if src == dst:
            continue
        strength[src] = strength.get(src, 0.0) + weight
        strength[dst] = strength.get(dst, 0.0) + weight
        m += weight

    def score(membership: Mapping[int, int]) -> float:
        if m == 0.0:
            return 0.0
        within: dict[int, float] = {}
        k_tot: dict[int, float] = {}
        for node in sorted(strength):
            k_tot[membership[node]] = (
                k_tot.get(membership[node], 0.0) + strength[node]
            )
        for src, dst, weight in edges:
            if src == dst:
                continue
            if membership[src] == membership[dst]:
                within[membership[src]] = (
                    within.get(membership[src], 0.0) + weight
                )
        q = 0.0
        for community in sorted(k_tot):
            l_c = within.get(community, 0.0)
            q += l_c / m - resolution * (k_tot[community] / (2.0 * m)) ** 2
        return q

    return score


def _coarsen_by_cost(
    membership: Mapping[int, int],
    edges: Sequence[Edge],
    nodes: Sequence[int],
) -> dict[int, int]:
    """§6a-guided coarsening of a community seed (the Infomap fallback's character).

    The v1 §6a codelength (``objective.cost``) is a *selector*, not a generator:
    its locate term dominates on small/sparse graphs, so greedily minimizing it
    from singletons over-shatters. Instead the Infomap fallback seeds from a
    modularity partition and merges adjacent communities **while it strictly lowers
    ``cost(G)``** — refining a sensible proposal *toward* the map-equation optimum
    (Infomap is the §6a-matched optimizer) without ever over-splitting. Native
    Infomap (when its wheel lands) minimizes the map equation directly.

    Deterministic: adjacent pairs and their reps are visited in sorted order; only
    strict cost reductions are taken.
    """
    current: dict[int, int] = dict(membership)
    while True:
        pairs: set[tuple[int, int]] = set()
        for src, dst, _w in edges:
            cu, cv = current[src], current[dst]
            if cu != cv:
                pairs.add((cu, cv) if cu < cv else (cv, cu))
        if not pairs:
            break

        rep: dict[int, int] = {}
        for node in nodes:
            community = current[node]
            if community not in rep or node < rep[community]:
                rep[community] = node

        base_cost = obj.cost(current, edges)
        best: tuple[float, int, int, int, int] | None = None
        for c, d in sorted(pairs):
            trial = {
                n: (c if current[n] == d else current[n]) for n in nodes
            }
            reduction = base_cost - obj.cost(trial, edges)
            if reduction <= 1e-12:
                continue
            lo, hi = sorted((rep[c], rep[d]))
            cand = (-reduction, lo, hi, c, d)
            if best is None or cand < best:
                best = cand

        if best is None:
            break
        _, _, _, c, d = best
        current = {
            n: (c if current[n] == d else current[n]) for n in nodes
        }
    return _canonicalize(current)


# --- Leiden proposer (R6) ----------------------------------------------------------


def _leiden_native(
    edges: Sequence[Edge], resolution: float, seed: int
) -> dict[int, int] | None:
    """graspologic_native hierarchical Leiden adapter (used when the wheel resolves).

    Returns the final-level flat membership, or ``None`` if the wheel is absent or
    the call fails — the caller then uses the pure-Python fallback. This adapter is
    exercised only once a 3.12 wheel lands (R7); until then it is inert.
    """
    try:  # lazy: importing at module scope would crash collection when absent
        import graspologic_native as gn  # type: ignore
    except Exception:
        return None
    try:
        native_edges = [(str(src), str(dst), float(w)) for src, dst, w in edges]
        result = gn.hierarchical_leiden(
            native_edges,
            resolution=resolution,
            randomness=0.001,
            use_modularity=True,
            random_seed=seed,
        )
    except Exception:
        return None
    membership: dict[int, int] = {}
    for row in result:
        if getattr(row, "is_final_cluster", True):
            membership[int(row.node)] = int(row.cluster)
    return membership or None


def propose_leiden(
    edges: Sequence[Edge],
    *,
    resolutions: Sequence[float] = DEFAULT_RESOLUTIONS,
    seed: int = DEFAULT_SEED,
    nodes: Sequence[int] | None = None,
) -> list[CandidatePartition]:
    """Leiden candidate partitions, one per resolution in the sweep (R6).

    Uses ``graspologic_native`` when its wheel is importable, else the deterministic
    pure-Python modularity-agglomeration fallback. Both produce a canonical flat
    membership; the sweep is the hierarchy of granularities the §6a scorer selects
    across.
    """
    node_list = _nodes_of(edges, nodes)
    out: list[CandidatePartition] = []
    use_native = graspologic_available()
    for resolution in resolutions:
        membership: dict[int, int] | None = None
        backend = "python"
        if use_native:
            membership = _leiden_native(edges, resolution, seed)
            if membership is not None:
                # native may omit isolated nodes; place each in its own community
                for node in node_list:
                    membership.setdefault(node, node)
                membership = _canonicalize(membership)
                backend = "graspologic"
        if membership is None:
            membership = _greedy_merge(
                edges, node_list, _modularity_score(resolution, edges)
            )
        out.append(
            CandidatePartition(
                proposer="leiden",
                membership=membership,
                resolution=resolution,
                backend=backend,
            )
        )
    return out


# --- Infomap proposer (R6) ---------------------------------------------------------


def _infomap_native(
    edges: Sequence[Edge], seed: int
) -> dict[int, int] | None:
    """``infomap`` library adapter (used when the wheel resolves), else ``None``."""
    try:
        from infomap import Infomap  # type: ignore
    except Exception:
        return None
    try:
        im = Infomap(silent=True, seed=seed, num_trials=10)
        for src, dst, weight in edges:
            im.add_link(int(src), int(dst), float(weight))
        im.run()
        membership = {int(node): int(module) for node, module in im.modules}
    except Exception:
        return None
    return membership or None


def propose_infomap(
    edges: Sequence[Edge],
    *,
    seed: int = DEFAULT_SEED,
    nodes: Sequence[int] | None = None,
) -> CandidatePartition:
    """The Infomap candidate partition (R6).

    Uses the ``infomap`` library when importable, else the deterministic
    pure-Python map-equation-minimizing fallback (which minimizes the very
    ``objective.cost`` the selector scores). Canonical flat membership.
    """
    node_list = _nodes_of(edges, nodes)
    membership: dict[int, int] | None = None
    backend = "python"
    if infomap_available():
        membership = _infomap_native(edges, seed)
        if membership is not None:
            for node in node_list:
                membership.setdefault(node, node)
            membership = _canonicalize(membership)
            backend = "infomap"
    if membership is None:
        seed_partition = _greedy_merge(
            edges, node_list, _modularity_score(1.0, edges)
        )
        membership = _coarsen_by_cost(seed_partition, edges, node_list)
    return CandidatePartition(
        proposer="infomap", membership=membership, backend=backend
    )


# --- orchestration (R7: explicit Infomap-only fallback) ----------------------------


def propose_partitions(
    edges: Sequence[Edge],
    *,
    resolutions: Sequence[float] = DEFAULT_RESOLUTIONS,
    seed: int = DEFAULT_SEED,
    nodes: Sequence[int] | None = None,
) -> ProposalSet:
    """All candidate partitions for one derive pass, plus backend telemetry.

    Runs the Leiden sweep **only if** the graspologic Leiden backend is available
    (:func:`graspologic_available`). When it is not, this is R7's explicit typed
    **Infomap-only fallback**: the candidate set comes from Infomap alone and
    ``fallback="infomap-only"`` is recorded — never a silent empty set. Infomap
    always proposes (its own native/pure-Python backend).
    """
    candidates: list[CandidatePartition] = []
    leiden_ran = graspologic_available()
    fallback: str | None = None
    if leiden_ran:
        candidates.extend(
            propose_leiden(
                edges, resolutions=resolutions, seed=seed, nodes=nodes
            )
        )
    else:
        fallback = "infomap-only"
    candidates.append(propose_infomap(edges, seed=seed, nodes=nodes))
    return ProposalSet(
        candidates=tuple(candidates), leiden_ran=leiden_ran, fallback=fallback
    )
