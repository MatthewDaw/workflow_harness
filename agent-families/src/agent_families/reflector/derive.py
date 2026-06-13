"""The derive pass — group structure is *derived*, not authored (plan-009 U4, R8-R10).

This is the R3 **slow loop** (DESIGN §6): a batch pass that rebuilds the module
partition from the insight graph rather than authoring it incrementally. End to end:

1. **Build** the v1 cold-start *flow* graph — a mutual-kNN + Tanimoto similarity
   graph over the full clustering vectors (``graphbuild.build_similarity_graph``,
   U2/R4), recomputed from sqlite-vec, never materialized.
2. **Propose** candidate whole partitions — the Leiden sweep + Infomap
   (``partition.propose_partitions``, U3/R6); neither proposer's internal objective
   is authoritative.
3. **Select** the candidate that minimizes the §6a organization objective
   (``objective.cost``, U1/R1) and adopt it **only if it strictly beats the
   incumbent partition** (``objective.beats_incumbent``, R3). A pass whose best
   candidate does not beat the incumbent is a **no-op: it mints no snapshot**.
4. **Identity-track** — match each adopted community to a prior module by
   *majority-overlap of member insight ids* (R9), so a module's id, lazy name,
   fitness roll-ups, and ``SKILL.md`` export stay stable run-to-run. A community
   with no strict-majority prior is genuinely new and gets a fresh module.
5. **Lazily name** only the communities whose membership *changed* (R10) — not
   every pass, not every community — via the namer seam (live judge / scripted
   fake), then **apply the whole partition as one** ``store.queue_operation
   ("derive", ...)`` (R8), which mints exactly one snapshot for the entire rewrite.

## What a "module" is here

A module is a ``skills`` row; its membership is ``skill_members``. The R3 ingest
gauntlet (plan 008) authors *no* group — every active insight arrives ungrouped —
so at cold start the incumbent partition is all-singletons and the first non-trivial
derive pass adopts the first real grouping. Modules are owned by one library agent
(``agent_id``); identity tracking keeps each ``skills`` row pointed at the same
community across passes.

## The partition-move engine (R8 / R15 note)

R8 says "reuse the repurposed ``agent_split`` transaction engine, re-pointed onto
module membership and snapshot-minting." That repurposing is R15 — owned by U7,
which guts ``agent_split.py`` after this unit lands. To keep the offline suite green
while U7 is still pending (``agent_split`` still carries its plan-005 family-split
engine and its tests), the module-membership apply/revert here is implemented
**in this module** rather than by gutting ``agent_split`` early. The behavior is the
one R8 specifies — a whole partition applied atomically inside a single
snapshot-minting ``queue_operation``, or not applied at all (the no-op path needs no
revert because nothing was written). U7 re-points the surviving engine; this module's
``_apply_partition`` is the seam it lands on.

Offline, deterministic: the graph, the proposers, and the objective are all
byte-stable for a fixed library snapshot; communities are created in canonical
(ascending min-member) order so module ids are reproducible; the namer is an
injected seam (a deterministic fake offline, a live judge in run-assembly).
Zero quota, no ``claude`` on PATH.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Callable

from agent_families.reflector import graphbuild, objective as obj, partition as part
from agent_families.store import Store
from agent_families.vecindex import VecIndex

__all__ = [
    "DeriveParams",
    "DeriveResult",
    "NamerFn",
    "default_namer",
    "derive_skills",
]


# --- the namer seam (R10) ----------------------------------------------------------
#
# A brief LLM pass over a changed community's central insights fills the module's
# name/description. The live binding routes a judge; the suite injects a deterministic
# fake. Called ONLY for communities whose membership changed and that need a name.
NamerFn = Callable[[Store, int, Sequence[int]], tuple[str, str]]


def default_namer(
    store: Store, skill_id: int, member_insight_ids: Sequence[int]
) -> tuple[str, str]:
    """Deterministic offline module name/description (the live judge overrides).

    Names are unique per module id so the ``skills`` ``UNIQUE(agent_id, name)``
    constraint never trips, and stable run-to-run so identity-tracked modules keep
    a byte-identical name across passes.
    """
    return (
        f"module-{skill_id}",
        f"Derived module {skill_id} ({len(member_insight_ids)} insights)",
    )


@dataclass(frozen=True)
class DeriveParams:
    """Caller-supplied derive tunables (routed from thresholds.toml live, plan-009 U9)."""

    knn_k: int = 15
    resolutions: tuple[float, ...] = part.DEFAULT_RESOLUTIONS
    seed: int = part.DEFAULT_SEED
    module_overhead_bits: float = obj.DEFAULT_MODULE_OVERHEAD_BITS

    def __post_init__(self) -> None:
        if self.knn_k < 1:
            raise ValueError(f"knn_k must be >= 1, got {self.knn_k}")
        if not self.resolutions:
            raise ValueError("resolutions must be non-empty")
        if self.module_overhead_bits < 0:
            raise ValueError(
                f"module_overhead_bits must be >= 0, got {self.module_overhead_bits}"
            )


@dataclass(frozen=True)
class DeriveResult:
    """The outcome of one derive pass — adopted (one snapshot) or a no-op.

    ``community_module`` maps each adopted community's canonical label to the
    ``skills`` id it landed on (inherited or fresh); ``renamed_module_ids`` are the
    modules the namer touched this pass; ``namer_calls`` is the namer invocation
    count (the R10 "only changed communities" witness).
    """

    adopted: bool
    snapshot_id: int | None
    incumbent_cost: float
    selected_cost: float
    num_communities: int
    community_module: dict[int, int] = field(default_factory=dict)
    inherited_module_ids: tuple[int, ...] = ()
    new_module_ids: tuple[int, ...] = ()
    renamed_module_ids: tuple[int, ...] = ()
    namer_calls: int = 0
    reason: str = ""

    @property
    def module_ids(self) -> tuple[int, ...]:
        """Every module id the adopted partition resolved to, ascending."""
        return tuple(sorted(self.community_module.values()))


# --- incumbent + prior-module reads ------------------------------------------------


def _agent_max_skill_id(store: Store) -> int:
    row = store.conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM skills").fetchone()
    return row["m"]


def _prior_module_members(store: Store, agent_id: int) -> dict[int, set[int]]:
    """``skill_id -> {member insight ids}`` for the agent's current modules.

    The prior partition the new communities are identity-matched against (R9).
    """
    rows = store.conn.execute(
        "SELECT s.id AS skill_id, m.insight_id AS insight_id"
        " FROM skills s JOIN skill_members m ON m.skill_id = s.id"
        " WHERE s.agent_id = ?",
        (agent_id,),
    ).fetchall()
    out: dict[int, set[int]] = {}
    for row in rows:
        out.setdefault(row["skill_id"], set()).add(row["insight_id"])
    return out


def _incumbent_partition(
    node_ids: Sequence[int],
    prior_members: Mapping[int, set[int]],
) -> dict[int, int]:
    """The current module membership over ``node_ids`` (R3 incumbent for §6a).

    Each node maps to its owning module's ``skill_id``; an ungrouped node (no
    module — the R3 ingest default) is its own singleton, labelled with a synthetic
    id above every real ``skills`` id so it cannot collide with a module label. The
    objective only reads partition *structure* (sizes, crossings), so the synthetic
    labels' values are immaterial — only their distinctness is.
    """
    owner: dict[int, int] = {}
    for skill_id, members in prior_members.items():
        for insight_id in members:
            owner[insight_id] = skill_id
    next_singleton = max(prior_members, default=0) + 1
    membership: dict[int, int] = {}
    for insight_id in node_ids:
        if insight_id in owner:
            membership[insight_id] = owner[insight_id]
        else:
            membership[insight_id] = next_singleton
            next_singleton += 1
    return membership


# --- identity tracking: majority-overlap match (R9) --------------------------------


def _communities_of(membership: Mapping[int, int]) -> dict[int, list[int]]:
    """``community_label -> sorted member insight ids`` from a candidate membership."""
    groups: dict[int, list[int]] = {}
    for insight_id, label in membership.items():
        groups.setdefault(label, []).append(insight_id)
    return {label: sorted(members) for label, members in groups.items()}


def _match_to_prior_modules(
    communities: Mapping[int, Sequence[int]],
    prior_members: Mapping[int, set[int]],
) -> dict[int, int]:
    """Match each community to a prior module by *majority-overlap* of member ids (R9).

    A community inherits a prior module only when that module holds a **strict
    majority** of the community's members (``overlap * 2 > |community|``). Each prior
    module is claimed by at most one community — when several communities each draw a
    strict majority from the same module (a module that *split*), the community with
    the largest overlap wins (ties broken by the lower canonical community label), and
    the losers fall through to fresh modules. Genuinely new communities (no
    strict-majority prior) get no match. Fully deterministic — prior modules are
    scanned in ascending id, communities in ascending label.

    Returns ``{community_label: skill_id}`` for the *winning* matches only.
    """
    # 1. each community's preferred prior module: the max-overlap module, kept only
    #    if that overlap is a strict majority of the community.
    preference: dict[int, tuple[int, int]] = {}  # label -> (skill_id, overlap)
    for label in sorted(communities):
        members = set(communities[label])
        best_skill: int | None = None
        best_overlap = -1
        for skill_id in sorted(prior_members):
            overlap = len(members & prior_members[skill_id])
            if overlap > best_overlap:  # ascending scan + strict > => lowest-id tie win
                best_overlap = overlap
                best_skill = skill_id
        if best_skill is not None and best_overlap * 2 > len(members):
            preference[label] = (best_skill, best_overlap)

    # 2. resolve contested modules: highest overlap wins, tie -> lowest label.
    contenders: dict[int, list[tuple[int, int]]] = {}  # skill_id -> [(overlap, label)]
    for label, (skill_id, overlap) in preference.items():
        contenders.setdefault(skill_id, []).append((overlap, label))
    assigned: dict[int, int] = {}
    for skill_id, claims in contenders.items():
        winner_label = sorted(claims, key=lambda t: (-t[0], t[1]))[0][1]
        assigned[winner_label] = skill_id
    return assigned


# --- apply (R8): one whole partition, one snapshot ---------------------------------


def _apply_partition(
    store: Store,
    agent_id: int,
    communities: Mapping[int, Sequence[int]],
    matched: Mapping[int, int],
    prior_members: Mapping[int, set[int]],
    namer_fn: NamerFn,
    *,
    detail: str,
) -> tuple[int, dict[int, int], list[int], list[int], list[int], int]:
    """Rewrite module membership for the whole partition inside one queue op (R8).

    Inherited communities reuse their matched ``skills`` id; new communities mint a
    fresh ``skills`` row (NULL name until named). Membership for every one of the
    agent's modules is rewritten from scratch (delete-then-append in sorted
    insight-id order) so append-position carries no stale order. Only communities
    whose membership *changed* (or that still lack a name) are passed to the namer
    (R10). Mints exactly one snapshot for the entire rewrite.

    Returns ``(snapshot_id, community_module, inherited, new, renamed, namer_calls)``.
    """
    community_module: dict[int, int] = {}
    inherited: list[int] = []
    new: list[int] = []
    renamed: list[int] = []
    namer_calls = 0

    with store.queue_operation("derive", detail) as snapshot_id:
        # Clear every existing membership for this agent's modules; the partition is
        # rewritten wholesale (a module that loses all members survives as an empty
        # row — demote, never drop).
        store.conn.execute(
            "DELETE FROM skill_members WHERE skill_id IN"
            " (SELECT id FROM skills WHERE agent_id = ?)",
            (agent_id,),
        )

        # Process communities in canonical (ascending-label) order so new module ids
        # are minted reproducibly.
        for label in sorted(communities):
            members = list(communities[label])
            skill_id = matched.get(label)
            is_new = skill_id is None
            if is_new:
                skill_id = store.create_skill(agent_id, None, "")
                new.append(skill_id)
            else:
                inherited.append(skill_id)
            community_module[label] = skill_id

            for insight_id in members:
                store.append_member(skill_id, insight_id)

            prior = prior_members.get(skill_id, set()) if not is_new else set()
            changed = is_new or set(members) != prior
            name_row = store.conn.execute(
                "SELECT name FROM skills WHERE id = ?", (skill_id,)
            ).fetchone()
            needs_name = changed or name_row["name"] is None
            if needs_name:
                name, description = namer_fn(store, skill_id, members)
                namer_calls += 1
                store.conn.execute(
                    "UPDATE skills SET name = ?, description = ? WHERE id = ?",
                    (name, description, skill_id),
                )
                renamed.append(skill_id)

    return snapshot_id, community_module, inherited, new, renamed, namer_calls


# --- the derive pass orchestration (R8) --------------------------------------------


def derive_skills(
    store: Store,
    vec: VecIndex,
    *,
    agent_id: int,
    params: DeriveParams = DeriveParams(),
    namer_fn: NamerFn | None = None,
    snapshot_id: int | None = None,
) -> DeriveResult:
    """Run one derive pass: propose → select → identity-track → name → apply (R8-R10).

    Builds the v1 similarity flow graph over the active insights, proposes candidate
    whole partitions, selects the lowest-``cost(G)`` candidate, and adopts it **only
    if it strictly beats the incumbent** module partition. On adoption the whole
    partition is applied as one snapshot-minting ``queue_operation("derive")`` with
    majority-overlap identity tracking and lazy naming of changed communities only;
    on rejection the active set is untouched and **no snapshot is minted**.

    ``snapshot_id`` is the library snapshot the incumbent state is read against
    (defaults to the current snapshot); it is recorded in the operation detail. The
    modules are owned by ``agent_id``. ``namer_fn`` defaults to the deterministic
    offline :func:`default_namer`.
    """
    namer = namer_fn if namer_fn is not None else default_namer
    read_snapshot = snapshot_id if snapshot_id is not None else store.current_snapshot_id()

    # 1. Build the flow graph over the active insight node set.
    neighbors = vec.all_neighbors(params.knn_k, on="full", statuses=("active",))
    node_ids = sorted(neighbors)
    edges = graphbuild.build_similarity_graph(
        vec, k=params.knn_k, on="full", statuses=("active",)
    )

    # 2. Read the incumbent partition (current module membership over the node set).
    prior_members = _prior_module_members(store, agent_id)
    incumbent = _incumbent_partition(node_ids, prior_members)
    incumbent_cost = obj.cost(
        incumbent, edges, module_overhead_bits=params.module_overhead_bits
    )

    if not node_ids:  # nothing to derive over
        return DeriveResult(
            adopted=False,
            snapshot_id=None,
            incumbent_cost=incumbent_cost,
            selected_cost=incumbent_cost,
            num_communities=0,
            reason="no active insights with vectors; nothing to derive",
        )

    # 3. Propose candidate whole partitions and select the §6a-minimal one.
    proposal = part.propose_partitions(
        edges, resolutions=params.resolutions, seed=params.seed, nodes=node_ids
    )
    candidates = proposal.candidates
    selected = min(
        candidates,
        key=lambda c: (
            obj.cost(c.membership, edges, module_overhead_bits=params.module_overhead_bits),
            c.canonical(),
        ),
    )
    selected_cost = obj.cost(
        selected.membership, edges, module_overhead_bits=params.module_overhead_bits
    )

    # 4. Adopt only if the selection strictly beats the incumbent (R3).
    if not obj.beats_incumbent(
        selected.membership,
        incumbent,
        edges,
        module_overhead_bits=params.module_overhead_bits,
    ):
        return DeriveResult(
            adopted=False,
            snapshot_id=None,
            incumbent_cost=incumbent_cost,
            selected_cost=selected_cost,
            num_communities=selected.num_communities,
            reason=(
                f"selected cost {selected_cost:.4f} does not beat incumbent"
                f" {incumbent_cost:.4f}: no-op pass, no snapshot minted"
            ),
        )

    # 5. Identity-track + lazy-name + apply as one snapshot-minting op (R8-R10).
    communities = _communities_of(selected.membership)
    matched = _match_to_prior_modules(communities, prior_members)
    (
        snap,
        community_module,
        inherited,
        new,
        renamed,
        namer_calls,
    ) = _apply_partition(
        store,
        agent_id,
        communities,
        matched,
        prior_members,
        namer,
        detail=f"snapshot<={read_snapshot}; {len(communities)} communities",
    )
    return DeriveResult(
        adopted=True,
        snapshot_id=snap,
        incumbent_cost=incumbent_cost,
        selected_cost=selected_cost,
        num_communities=len(communities),
        community_module=community_module,
        inherited_module_ids=tuple(sorted(inherited)),
        new_module_ids=tuple(sorted(new)),
        renamed_module_ids=tuple(sorted(renamed)),
        namer_calls=namer_calls,
        reason=(
            f"adopted: selected cost {selected_cost:.4f} <"
            f" incumbent {incumbent_cost:.4f}; {len(communities)} communities,"
            f" {len(new)} new / {len(inherited)} inherited, snapshot {snap}"
        ),
    )
