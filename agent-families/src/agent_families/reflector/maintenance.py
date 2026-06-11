"""Ratchet governance and the first self-reorganization (plan-004 U8, R19-R21).

Three mechanisms close the learning loop's governance side (DESIGN §4, §6):

1. **Settlement fitness writes** (R19) — the orchestrator's once-per-episode write
   of the append-only fitness log. The event semantics are exact and load-bearing:

   - *retrieval* = an insight **actually rendered** into a session prompt
     (post-budget — what survived the retrieval injection, not what was a
     candidate);
   - *win* = the session's ticket reaches ``done`` **AND** is unimplicated in any
     failed SCEN chain at settlement (co-occurrence is not enough);
   - *loss* = **causal blame only** — the final attribution's
     ``implicated_existing_insights``; an insight merely present in a failed run's
     context is **not** punished (no bystander punishment, DESIGN §4).

   ``trial`` / ``benchmark`` events land in their own channel (keyed by the
   episode's ``mode``); the ratchet reads the ``training`` channel only, so
   validation traffic never moves fitness (R19).

2. **The maintenance pass** (R20) — post-promotion, queue-serialized, **skipped
   mid-validation** for the split step (R21). It runs over the latest snapshot's
   reconstructed fitness:

   - **Outcome-driven retirement** — an active insight that has earned real
     exposure (``retirement_min_retrievals`` retrievals) yet a net fitness at or
     below ``retirement_max_net_fitness`` is demoted to ``dormant`` (preserved,
     revivable — never deleted; DESIGN §4 "dormant archive").
   - **Cap tournament** — admitting a promoted batch past an agent's
     ``active_cap`` displaces the **weakest incumbents by fitness**; ties favor
     incumbents (a new admission loses a tie); the displaced go ``dormant`` (R20).

3. **Skill split** (R21, DESIGN §6) — the first self-reorganization. A skill over
   ``split_insight_count`` members (or ``split_token_count`` tokens) that has
   accumulated ``split_min_observations`` members is split by **k-means (k=2) on
   its active members' embeddings**; the partition is accepted when its silhouette
   clears ``split_silhouette_threshold``, else an **LLM thematic split** (judge
   seam) decides. The two children get fresh **names/descriptions** (a judge seam)
   and carry ``parent_skill_id`` + ``split_snapshot_id`` for provenance.
   **Quarantined members follow the nearest child centroid** (they do not vote in
   the clustering). The parent is left a frozen, empty lineage anchor (never
   deleted). **Caches invalidate per Phase 0 semantics**: rendering and
   delta-compile caches are keyed ``(skill_id, snapshot_id)``, and the split mints
   a new snapshot under fresh child skill ids, so every post-split render is a
   cache miss — no explicit eviction is needed (and ``rendering.py`` is not
   touched).

**Agent split is a gated stub** (R21): it gates on ``min_routing_decisions``, a
volume only Plan 5's router produces; with no router writing ``routing_decisions``
it is always deferred. The seam exists; the machinery is Plan 5's.

Discipline carried from Phase 0: every active-set mutation flows through
:meth:`Store.queue_operation` (one minted snapshot per pass, R4); a no-op pass
mints nothing so the snapshot chain stays gapless; **insights are never deleted
and vec rows are never touched** (R13) — retirement/displacement are status flips,
a split only re-points membership. Tunables ride in :class:`MaintenanceParams`
(the U2/U5/U6/U7 precedent — carried defaults record PROVENANCE; run-assembly
routes the live values from ``thresholds.toml``; nothing here reads config).

Offline by construction: the split's clustering/silhouette is pure deterministic
arithmetic, and the LLM thematic-split / child-naming calls go through injected
seams (scripted fakes in the suite, ``run_judge`` live). Zero quota, no ``claude``
on PATH.

## Conformance

Required acceptance tests / invariants (plan-004 U8) -> test (in
``tests/test_maintenance.py``):

- ``test_loss_requires_causal_blame`` — an insight rendered into a FAILED run's
  context but absent from ``implicated_existing_insights`` receives zero loss (no
  bystander punishment). [enforced by :func:`settle_fitness`]
- ``test_win_requires_done_and_unimplicated`` — a win is recorded only when the
  session's ticket reaches ``done`` AND is unimplicated in any failed SCEN chain.
  [enforced by :func:`settle_fitness`]
- ``test_trial_benchmark_fitness_excluded`` — ``trial`` / ``benchmark`` fitness
  events never feed the ratchet (separate channel). [enforced by
  :func:`fitness_score` reading the ``training`` channel only]

Test-scenario / invariant (plan-004 U8) -> test:

- rendered-but-failed insight gets no loss without causal blame:
  ``test_loss_requires_causal_blame``
- win requires done + unimplicated: ``test_win_requires_done_and_unimplicated``
- tournament displaces weakest, ties favor incumbents, displaced go dormant:
  ``test_tournament_displaces_weakest_ties_favor_incumbents``
- retirement respects the event-log reconstruction:
  ``test_retirement_respects_event_log_reconstruction``
- split on a 30-insight fixture skill produces two children with
  provenance-correct membership incl. quarantined follow-the-centroid:
  ``test_split_produces_two_children_with_provenance``
- split deferred while a batch is mid-validation:
  ``test_split_deferred_while_batch_mid_validation``
- benchmark/trial fitness channels never feed the ratchet:
  ``test_trial_benchmark_fitness_excluded``
- property: any maintenance sequence preserves insights-never-deleted,
  vec-rows-untouched, membership-changes-snapshot-keyed:
  ``test_maintenance_preserves_invariants``
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass

from agent_families.store import Store
from agent_families.vecindex import VEC_TABLE

# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) -----
#
# Not read from config here — they ride in :class:`MaintenanceParams`, and the
# run-assembly wiring routes the live values from ``thresholds.toml`` (the same
# "carried now so the seam reads it" discipline as ``active_cap``).

# PROVENANCE: §17 "active cap ~50"; thresholds.toml [lifecycle] active_cap. TUNING
# METRIC: routing selection accuracy vs active library size (Skill Shadowing).
DEFAULT_ACTIVE_CAP = 50

# PROVENANCE: §4 outcome-driven retirement / §6 R2 "minimum observation count" —
# never retire an insight that has not earned real exposure (clustering/fitness on
# small N is noise). TUNING METRIC: false-retirement rate vs library churn.
DEFAULT_RETIREMENT_MIN_RETRIEVALS = 5

# PROVENANCE: §4 "insights that stop earning retrievals-with-wins are demoted" — a
# net fitness (wins - losses) at or below this floor, after real exposure, retires.
# TUNING METRIC: dormant-revival rate (over-eager pruning) vs library size.
DEFAULT_RETIREMENT_MAX_NET_FITNESS = 0

# PROVENANCE: §6 "Split when insight_count > ~25". TUNING METRIC: post-split
# routing accuracy vs skill size.
DEFAULT_SPLIT_INSIGHT_COUNT = 25

# PROVENANCE: §6 "Split when token_count > ~2,000". TUNING METRIC: prompt-cache
# stability vs skill size.
DEFAULT_SPLIT_TOKEN_COUNT = 2000

# PROVENANCE: §6 R2 "do not evaluate split candidacy until a minimum observation
# count" — clustering metrics on small N are noise. TUNING METRIC: split-accept
# silhouette stability vs member count.
DEFAULT_SPLIT_MIN_OBSERVATIONS = 10

# PROVENANCE: §6 "accept if silhouette > ~0.3, else LLM thematic split". TUNING
# METRIC: split-acceptance precision on hand-labeled clusterable skills.
DEFAULT_SPLIT_SILHOUETTE_THRESHOLD = 0.3

# PROVENANCE: §6 agent split "best silhouette > ~0.35". Carried for the stub only.
DEFAULT_AGENT_SPLIT_SILHOUETTE_THRESHOLD = 0.35

# PROVENANCE: R21 — agent split "gates on a min_routing_decisions threshold only
# Plan 5's volume can produce". Carried so the gated stub has a value to read.
DEFAULT_MIN_ROUTING_DECISIONS = 50


class MaintenanceError(Exception):
    """A broken maintenance precondition with an actionable message."""


@dataclass(frozen=True)
class MaintenanceParams:
    """Caller-supplied ratchet/split tunables (routed from thresholds.toml by the
    run-assembly wiring; never read from config here)."""

    active_cap: int = DEFAULT_ACTIVE_CAP
    retirement_min_retrievals: int = DEFAULT_RETIREMENT_MIN_RETRIEVALS
    retirement_max_net_fitness: int = DEFAULT_RETIREMENT_MAX_NET_FITNESS
    split_insight_count: int = DEFAULT_SPLIT_INSIGHT_COUNT
    split_token_count: int = DEFAULT_SPLIT_TOKEN_COUNT
    split_min_observations: int = DEFAULT_SPLIT_MIN_OBSERVATIONS
    split_silhouette_threshold: float = DEFAULT_SPLIT_SILHOUETTE_THRESHOLD
    agent_split_silhouette_threshold: float = DEFAULT_AGENT_SPLIT_SILHOUETTE_THRESHOLD
    min_routing_decisions: int = DEFAULT_MIN_ROUTING_DECISIONS

    def __post_init__(self) -> None:
        if self.active_cap < 1:
            raise MaintenanceError(f"active_cap must be >= 1, got {self.active_cap}")
        if self.retirement_min_retrievals < 0:
            raise MaintenanceError(
                "retirement_min_retrievals must be >= 0, got"
                f" {self.retirement_min_retrievals}"
            )
        if self.split_insight_count < 2:
            raise MaintenanceError(
                f"split_insight_count must be >= 2, got {self.split_insight_count}"
            )
        if self.split_token_count < 1:
            raise MaintenanceError(
                f"split_token_count must be >= 1, got {self.split_token_count}"
            )
        if self.split_min_observations < 2:
            raise MaintenanceError(
                "split_min_observations must be >= 2 (k-means k=2 needs two"
                f" points), got {self.split_min_observations}"
            )
        if not (-1.0 <= self.split_silhouette_threshold <= 1.0):
            raise MaintenanceError(
                "split_silhouette_threshold must be in [-1, 1], got"
                f" {self.split_silhouette_threshold}"
            )
        if self.min_routing_decisions < 0:
            raise MaintenanceError(
                f"min_routing_decisions must be >= 0, got {self.min_routing_decisions}"
            )


# --- settlement fitness writes (R19) --------------------------------------------


@dataclass(frozen=True)
class SessionRender:
    """The insights rendered (post-budget) into one session's prompt, tied to the
    session's ticket (``None`` for ticket-less sessions such as planning).

    The orchestrator builds one per session from the retrieval result's injected
    skills resolved to their visible member insight ids.
    """

    ticket_id: str | None
    insight_ids: tuple[int, ...]


@dataclass(frozen=True)
class FitnessSettlement:
    """The once-per-episode fitness write's audit trail (R19)."""

    episode_id: int
    mode: str
    snapshot_id: int
    retrieval_count: int
    win_count: int
    loss_count: int
    won_ticket_ids: tuple[str, ...]


def _ticket_status(store: Store, ticket_id: str) -> str | None:
    row = store.conn.execute(
        "SELECT status FROM trace_tkt WHERE id = ?", (ticket_id,)
    ).fetchone()
    return row["status"] if row is not None else None


def settle_fitness(
    store: Store,
    *,
    episode_id: int,
    renders: Sequence[SessionRender],
    implicated_ticket_ids: Collection[str] = (),
    causal_blame_insight_ids: Collection[int] = (),
    snapshot_id: int | None = None,
) -> FitnessSettlement:
    """Write an episode's fitness events once, at settlement (R19).

    - *retrieval*: one event per (rendered insight, session) — what the budget
      actually injected.
    - *win*: one event per (rendered insight, session) whose ticket reached
      ``done`` AND is **not** in ``implicated_ticket_ids`` (the failed-SCEN-chain
      set from Stage A). Ticket-less sessions never win.
    - *loss*: one event per insight in ``causal_blame_insight_ids`` (the final
      attribution's ``implicated_existing_insights``). Causal blame only — an
      insight merely rendered into a failed run is not punished.

    The events are stamped with the episode's library snapshot and the episode's
    ``mode`` (the channel). No snapshot is minted (R1).
    """
    episode = store.get_episode(episode_id)
    if episode is None:
        raise MaintenanceError(f"episode {episode_id} does not exist")
    mode = episode["mode"]
    snap = episode["snapshot_id"] if snapshot_id is None else snapshot_id
    implicated = set(implicated_ticket_ids)

    # A ticket wins iff the store says it is `done` AND it is unimplicated.
    candidate_tickets = {r.ticket_id for r in renders if r.ticket_id is not None}
    won_tickets = {
        tid
        for tid in candidate_tickets
        if tid not in implicated and _ticket_status(store, tid) == "done"
    }

    retrieval_count = 0
    win_count = 0
    loss_count = 0
    with store.transaction():
        for render in renders:
            won = render.ticket_id is not None and render.ticket_id in won_tickets
            for insight_id in render.insight_ids:
                store.record_fitness_event(
                    insight_id, "retrieval", mode, snap, episode_id=episode_id
                )
                retrieval_count += 1
                if won:
                    store.record_fitness_event(
                        insight_id, "win", mode, snap, episode_id=episode_id
                    )
                    win_count += 1
        for insight_id in causal_blame_insight_ids:
            store.record_fitness_event(
                insight_id, "loss", mode, snap, episode_id=episode_id
            )
            loss_count += 1

    return FitnessSettlement(
        episode_id=episode_id,
        mode=mode,
        snapshot_id=snap,
        retrieval_count=retrieval_count,
        win_count=win_count,
        loss_count=loss_count,
        won_ticket_ids=tuple(sorted(won_tickets)),
    )


def fitness_score(
    store: Store, insight_id: int, *, snapshot_id: int | None = None
) -> int:
    """The ratchet's fitness scalar for an insight: ``wins - losses`` over the
    **training** channel only (R19). Trial/benchmark events are excluded by the
    channel filter, so validation traffic never moves this number."""
    counts = store.fitness_counts(
        insight_id, snapshot_id=snapshot_id, mode="training"
    )
    return counts["win"] - counts["loss"]


def _training_retrievals(
    store: Store, insight_id: int, snapshot_id: int | None
) -> int:
    counts = store.fitness_counts(
        insight_id, snapshot_id=snapshot_id, mode="training"
    )
    return counts["retrieval"]


# --- ratchet: outcome-driven retirement (R20, DESIGN §4) ------------------------


def _active_insight_ids(store: Store, snapshot_id: int) -> list[int]:
    rows = store.conn.execute(
        "SELECT id FROM insights ORDER BY id ASC"
    ).fetchall()
    return [
        r["id"]
        for r in rows
        if store.status_at(r["id"], snapshot_id) == "active"
    ]


def retirement_candidates(
    store: Store, snapshot_id: int, params: MaintenanceParams
) -> list[int]:
    """Active insights that have earned real exposure yet a net-non-positive
    fitness — the persistent losers the maintenance pass demotes to dormant (R20).

    Both gates read the reconstructed event log (``fitness_counts``), so the
    decision is a pure function of the append-only fitness history at the snapshot.
    """
    losers: list[int] = []
    for insight_id in _active_insight_ids(store, snapshot_id):
        retrievals = _training_retrievals(store, insight_id, snapshot_id)
        if retrievals < params.retirement_min_retrievals:
            continue
        if fitness_score(store, insight_id, snapshot_id=snapshot_id) <= (
            params.retirement_max_net_fitness
        ):
            losers.append(insight_id)
    return losers


# --- ratchet: cap tournament (R20) ----------------------------------------------


def agent_active_insight_ids(
    store: Store, agent_id: int, snapshot_id: int
) -> list[int]:
    """Distinct active insights reachable through ``agent_id``'s skills, at a
    snapshot, id-ordered."""
    rows = store.conn.execute(
        "SELECT DISTINCT i.id AS id FROM insights i"
        " JOIN skill_members m ON m.insight_id = i.id"
        " JOIN skills s ON s.id = m.skill_id"
        " WHERE s.agent_id = ? ORDER BY i.id ASC",
        (agent_id,),
    ).fetchall()
    return [
        r["id"]
        for r in rows
        if store.status_at(r["id"], snapshot_id) == "active"
    ]


def _agents_with_skills(store: Store) -> list[int]:
    rows = store.conn.execute(
        "SELECT DISTINCT agent_id FROM skills ORDER BY agent_id ASC"
    ).fetchall()
    return [r["agent_id"] for r in rows]


def tournament_displacements(
    store: Store,
    snapshot_id: int,
    admitted_insight_ids: Collection[int],
    params: MaintenanceParams,
    *,
    exclude: Collection[int] = (),
) -> list[int]:
    """The insights an over-cap admission must displace, per agent (R20).

    For every agent whose active set exceeds ``active_cap``, keep the top
    ``active_cap`` by fitness; the rest are displaced. **Ties favor incumbents** —
    on equal fitness a just-admitted insight (in ``admitted_insight_ids``) sorts
    below an incumbent, so the admission is the one displaced. ``exclude`` drops
    insights already retired earlier in the same pass.
    """
    admitted = set(admitted_insight_ids)
    excluded = set(exclude)
    displaced: set[int] = set()
    for agent_id in _agents_with_skills(store):
        active = [
            iid
            for iid in agent_active_insight_ids(store, agent_id, snapshot_id)
            if iid not in excluded
        ]
        if len(active) <= params.active_cap:
            continue
        # Keep the strongest active_cap. Sort by (fitness desc, incumbent-first,
        # id asc) — incumbents outrank admissions on a fitness tie.
        ranked = sorted(
            active,
            key=lambda iid: (
                -fitness_score(store, iid, snapshot_id=snapshot_id),
                1 if iid in admitted else 0,  # incumbents (0) sort first
                iid,
            ),
        )
        for iid in ranked[params.active_cap :]:
            displaced.add(iid)
    return sorted(displaced)


# --- skill split: k-means (k=2) + silhouette (R21, DESIGN §6) --------------------


def _unpack_vector(blob) -> list[float]:
    raw = bytes(blob)
    return list(struct.unpack(f"<{len(raw) // 4}f", raw))


def _load_member_vectors(
    store: Store, insight_ids: Sequence[int]
) -> dict[int, list[float]]:
    if not insight_ids:
        return {}
    placeholders = ", ".join("?" for _ in insight_ids)
    rows = store.conn.execute(
        f"SELECT insight_id, embedding FROM {VEC_TABLE}"
        f" WHERE insight_id IN ({placeholders})",
        tuple(insight_ids),
    ).fetchall()
    return {r["insight_id"]: _unpack_vector(r["embedding"]) for r in rows}


def _euclid(a: Sequence[float], b: Sequence[float]) -> float:
    return math.sqrt(sum((x - y) * (x - y) for x, y in zip(a, b)))


def _centroid(vectors: Sequence[Sequence[float]]) -> list[float]:
    n = len(vectors)
    dim = len(vectors[0])
    return [sum(v[d] for v in vectors) / n for d in range(dim)]


def _kmeans2(
    ids: Sequence[int], vectors: dict[int, list[float]], *, max_iters: int = 50
) -> tuple[list[int], list[int]]:
    """Deterministic k-means (k=2) over ``ids``. Returns two id-lists (id-ordered).

    Init is deterministic: the two members farthest apart seed the clusters (no
    RNG — the offline suite needs byte-stable membership). Empty clusters are
    re-seeded with the point farthest from its current centroid.
    """
    pts = [(iid, vectors[iid]) for iid in ids]
    # Farthest-pair seeds (deterministic; ties break on id order).
    best = (-1.0, pts[0][0], pts[1][0])
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            d = _euclid(pts[i][1], pts[j][1])
            if d > best[0]:
                best = (d, pts[i][0], pts[j][0])
    c0 = list(vectors[best[1]])
    c1 = list(vectors[best[2]])

    assign: dict[int, int] = {}
    for _ in range(max_iters):
        new_assign: dict[int, int] = {}
        for iid, v in pts:
            new_assign[iid] = 0 if _euclid(v, c0) <= _euclid(v, c1) else 1
        if new_assign == assign:
            break
        assign = new_assign
        g0 = [v for iid, v in pts if assign[iid] == 0]
        g1 = [v for iid, v in pts if assign[iid] == 1]
        if not g0 or not g1:
            # Degenerate: re-seed the empty cluster with the farthest outlier.
            full = g0 or g1
            cen = _centroid(full)
            outlier = max(pts, key=lambda p: _euclid(p[1], cen))
            if not g0:
                c0 = list(outlier[1])
                c1 = _centroid([v for iid, v in pts if iid != outlier[0]])
            else:
                c1 = list(outlier[1])
                c0 = _centroid([v for iid, v in pts if iid != outlier[0]])
            continue
        c0, c1 = _centroid(g0), _centroid(g1)

    cluster0 = sorted(iid for iid in ids if assign.get(iid, 0) == 0)
    cluster1 = sorted(iid for iid in ids if assign.get(iid, 1) == 1)
    return cluster0, cluster1


def silhouette_two(
    cluster0: Sequence[int],
    cluster1: Sequence[int],
    vectors: dict[int, list[float]],
) -> float:
    """Mean silhouette of a 2-cluster partition (R21 accept test).

    For each point: ``a`` = mean intra-cluster distance, ``b`` = mean distance to
    the other cluster; silhouette = ``(b - a) / max(a, b)``. A singleton cluster
    contributes ``a = 0``. Empty clusters yield ``-1`` (an unacceptable split).
    """
    if not cluster0 or not cluster1:
        return -1.0
    members = {0: list(cluster0), 1: list(cluster1)}
    total = 0.0
    n = 0
    for label, own in members.items():
        other = members[1 - label]
        for iid in own:
            same = [j for j in own if j != iid]
            a = (
                sum(_euclid(vectors[iid], vectors[j]) for j in same) / len(same)
                if same
                else 0.0
            )
            b = sum(_euclid(vectors[iid], vectors[j]) for j in other) / len(other)
            denom = max(a, b)
            total += (b - a) / denom if denom > 0 else 0.0
            n += 1
    return total / n if n else -1.0


def _nearest_centroid(
    vector: Sequence[float],
    centroid0: Sequence[float],
    centroid1: Sequence[float],
) -> int:
    return 0 if _euclid(vector, centroid0) <= _euclid(vector, centroid1) else 1


# --- skill split: planning + the seams (R21) ------------------------------------

# A thematic-split seam: given the active member ids + their loaded vectors, return
# a 2-way partition ``(cluster0_ids, cluster1_ids)``. Used only when k-means'
# silhouette is below threshold (DESIGN §6 "else LLM thematic split"). The live
# binding routes ``run_judge``; the suite injects a deterministic fake.
ThematicSplitFn = Callable[[list[int], dict[int, list[float]]], tuple[list[int], list[int]]]

# A namer seam: given the parent skill row + the two member-id clusters, return
# ``((name0, desc0), (name1, desc1))`` regenerated bottom-up (DESIGN §6). The live
# binding routes ``run_judge``; the suite injects a deterministic fake.
NamerFn = Callable[..., tuple[tuple[str, str], tuple[str, str]]]


@dataclass(frozen=True)
class SkillSplit:
    """One executed skill split's provenance (R21)."""

    parent_skill_id: int
    child_skill_ids: tuple[int, int]
    child_member_ids: tuple[tuple[int, ...], tuple[int, ...]]
    silhouette: float
    accepted_by: str  # "kmeans" | "thematic"
    split_snapshot_id: int


def _skill_member_count(store: Store, skill_id: int) -> int:
    return len(store.skill_members(skill_id))


def split_candidates(
    store: Store, snapshot_id: int, params: MaintenanceParams
) -> list[int]:
    """Skills that trip the split trigger AND have enough members to cluster (R21).

    Trigger: ``token_count > split_token_count`` OR member_count >
    ``split_insight_count`` (DESIGN §6). Gate: member_count >=
    ``split_min_observations`` (the R2 "minimum observation count" — clustering on
    small N is noise). Member count is total membership (active + quarantined),
    matching the size DESIGN §6 reasons about.
    """
    rows = store.conn.execute(
        "SELECT id, token_count FROM skills ORDER BY id ASC"
    ).fetchall()
    out: list[int] = []
    for row in rows:
        n = _skill_member_count(store, row["id"])
        if n < params.split_min_observations:
            continue
        if n > params.split_insight_count or row["token_count"] > params.split_token_count:
            out.append(row["id"])
    return out


def _partition_skill(
    store: Store,
    skill_id: int,
    snapshot_id: int,
    params: MaintenanceParams,
    thematic_split_fn: ThematicSplitFn | None,
) -> tuple[list[int], list[int], float, str]:
    """Compute the 2-way member partition for a split (pure / seam-driven).

    Active members are clustered (the routing substrate); quarantined members
    *follow the nearest child centroid* (they do not vote). Returns
    ``(members0, members1, silhouette, accepted_by)`` with each member list
    id-ordered. Raises if the skill cannot be split (no separable active members).
    """
    member_ids = store.skill_members(skill_id)
    active = [
        iid for iid in member_ids if store.status_at(iid, snapshot_id) == "active"
    ]
    quarantined = [
        iid
        for iid in member_ids
        if store.status_at(iid, snapshot_id) == "quarantined"
    ]
    if len(active) < 2:
        raise MaintenanceError(
            f"skill {skill_id} has {len(active)} active member(s); k-means k=2"
            " needs at least two active members to split"
        )
    vectors = _load_member_vectors(store, member_ids)
    missing = [iid for iid in active if iid not in vectors]
    if missing:
        raise MaintenanceError(
            f"skill {skill_id} active members {missing} have no embedding row;"
            " cannot cluster (the vec row is written at registration)"
        )

    cluster0, cluster1 = _kmeans2(active, vectors)
    sil = silhouette_two(cluster0, cluster1, vectors)
    if sil >= params.split_silhouette_threshold:
        accepted_by = "kmeans"
    else:
        if thematic_split_fn is None:
            raise MaintenanceError(
                f"skill {skill_id} silhouette {sil:.3f} <"
                f" {params.split_silhouette_threshold}: an LLM thematic split is"
                " required but no thematic_split_fn seam was provided (R21)"
            )
        cluster0, cluster1 = thematic_split_fn(list(active), dict(vectors))
        cluster0 = sorted(cluster0)
        cluster1 = sorted(cluster1)
        if not cluster0 or not cluster1 or set(cluster0) & set(cluster1):
            raise MaintenanceError(
                f"thematic_split_fn returned an invalid partition for skill"
                f" {skill_id} (clusters must be non-empty and disjoint)"
            )
        accepted_by = "thematic"

    # Quarantined members follow the nearest child centroid (they do not vote).
    centroid0 = _centroid([vectors[iid] for iid in cluster0])
    centroid1 = _centroid([vectors[iid] for iid in cluster1])
    members0 = list(cluster0)
    members1 = list(cluster1)
    for iid in quarantined:
        if iid not in vectors:
            # No embedding to place it; keep it with the first child deterministically.
            members0.append(iid)
            continue
        if _nearest_centroid(vectors[iid], centroid0, centroid1) == 0:
            members0.append(iid)
        else:
            members1.append(iid)
    return sorted(members0), sorted(members1), sil, accepted_by


def _create_child_skill(
    store: Store,
    *,
    agent_id: int,
    name: str,
    description: str,
    parent_skill_id: int,
    split_snapshot_id: int,
    member_ids: Sequence[int],
) -> int:
    cur = store.conn.execute(
        "INSERT INTO skills (agent_id, name, description, parent_skill_id,"
        " split_snapshot_id) VALUES (?, ?, ?, ?, ?)",
        (agent_id, name, description, parent_skill_id, split_snapshot_id),
    )
    child_id = cur.lastrowid
    for iid in member_ids:
        store.append_member(child_id, iid)
    return child_id


def _default_namer(
    parent_row, cluster0: Sequence[int], cluster1: Sequence[int]
) -> tuple[tuple[str, str], tuple[str, str]]:
    """Deterministic offline child names — the live binding overrides with a judge
    seam (DESIGN §6 bottom-up regeneration). Kept offline so a split without a
    namer seam still produces stable, provenance-bearing children."""
    base = parent_row["name"]
    return (
        (f"{base}-a", f"{parent_row['description']} (split a)"),
        (f"{base}-b", f"{parent_row['description']} (split b)"),
    )


# --- agent split: gated stub (R21) ----------------------------------------------


@dataclass(frozen=True)
class AgentSplitDecision:
    """The agent-split gate's outcome — always deferred in Phase 3a (R21)."""

    agent_id: int
    triggered: bool
    routing_decisions: int
    reason: str


def maybe_agent_split(
    store: Store, agent_id: int, params: MaintenanceParams
) -> AgentSplitDecision:
    """Evaluate the agent-split gate (R21) — a stub that always defers.

    Agent split gates on ``min_routing_decisions`` (DESIGN §6 / R21): a router with
    historical routing decisions is Plan 5's deliverable. No Phase 3a writer
    increments ``agents.routing_decisions``, so the gate is never met and the split
    is deferred. The seam exists; the machinery is Plan 5's.
    """
    row = store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        raise MaintenanceError(f"agent {agent_id} does not exist")
    decisions = row["routing_decisions"]
    return AgentSplitDecision(
        agent_id=agent_id,
        triggered=False,
        routing_decisions=decisions,
        reason=(
            f"agent split deferred: routing_decisions={decisions} <"
            f" min_routing_decisions={params.min_routing_decisions} (Plan 5 gate;"
            " no router writes routing decisions in Phase 3a — R21)"
        ),
    )


# --- the maintenance pass (R20/R21) ---------------------------------------------


def is_mid_validation(store: Store) -> bool:
    """True while any batch validation is open (verdict not yet recorded).

    The skill-split trigger is skipped while a batch is mid-validation (R21) — a
    split that re-points membership mid-trial would invalidate the trial's
    retrieval pool. Detected from an open ``batch_validations`` row (verdict NULL).
    """
    row = store.conn.execute(
        "SELECT 1 FROM batch_validations WHERE verdict IS NULL LIMIT 1"
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class MaintenanceResult:
    """One maintenance pass's full effect (R20/R21)."""

    snapshot_id: int
    minted_snapshot: bool
    retired_insight_ids: tuple[int, ...]
    displaced_insight_ids: tuple[int, ...]
    splits: tuple[SkillSplit, ...]
    split_deferred: bool
    agent_split_decisions: tuple[AgentSplitDecision, ...]


def run_maintenance(
    store: Store,
    *,
    admitted_insight_ids: Collection[int] = (),
    params: MaintenanceParams = MaintenanceParams(),
    do_split: bool = True,
    thematic_split_fn: ThematicSplitFn | None = None,
    namer_fn: NamerFn | None = None,
) -> MaintenanceResult:
    """Run the post-promotion maintenance pass through the queue (R20/R21).

    Order: outcome-driven retirement, then cap-tournament displacement (excluding
    the just-retired), then — unless a batch is mid-validation — skill splits. The
    whole pass is one queue operation minting exactly one snapshot; a pass with
    nothing to do mints nothing (the snapshot chain stays gapless). Retirement and
    displacement are status flips to ``dormant``; a split only re-points
    membership and creates child skills (parent frozen, never deleted). Insights
    are never deleted and vec rows never touched (R13).
    """
    snap_before = store.current_snapshot_id()
    mid_validation = is_mid_validation(store)

    retire_ids = retirement_candidates(store, snap_before, params)
    displaced_ids = tournament_displacements(
        store, snap_before, admitted_insight_ids, params, exclude=retire_ids
    )

    # Plan the splits (pure / seam-driven) before opening the queue, so the queue
    # block is DB writes only (the lifecycle.py discipline).
    split_deferred = mid_validation or not do_split
    planned_splits: list[tuple[int, list[int], list[int], float, str]] = []
    if not split_deferred:
        for skill_id in split_candidates(store, snap_before, params):
            members0, members1, sil, accepted_by = _partition_skill(
                store, skill_id, snap_before, params, thematic_split_fn
            )
            planned_splits.append((skill_id, members0, members1, sil, accepted_by))

    if not retire_ids and not displaced_ids and not planned_splits:
        # No-op pass: mint nothing, keep the snapshot chain gapless.
        return MaintenanceResult(
            snapshot_id=snap_before,
            minted_snapshot=False,
            retired_insight_ids=(),
            displaced_insight_ids=(),
            splits=(),
            split_deferred=split_deferred,
            agent_split_decisions=_agent_split_stub(store, params),
        )

    splits: list[SkillSplit] = []
    with store.queue_operation("maintenance", "ratchet+split") as snapshot_id:
        for insight_id in retire_ids:
            store.set_status(insight_id, "dormant", snapshot_id)
        for insight_id in displaced_ids:
            store.set_status(insight_id, "dormant", snapshot_id)
        for skill_id, members0, members1, sil, accepted_by in planned_splits:
            parent = store.conn.execute(
                "SELECT id, agent_id, name, description FROM skills WHERE id = ?",
                (skill_id,),
            ).fetchone()
            namer = namer_fn if namer_fn is not None else _default_namer
            (name0, desc0), (name1, desc1) = namer(parent, members0, members1)
            child0 = _create_child_skill(
                store,
                agent_id=parent["agent_id"],
                name=name0,
                description=desc0,
                parent_skill_id=skill_id,
                split_snapshot_id=snapshot_id,
                member_ids=members0,
            )
            child1 = _create_child_skill(
                store,
                agent_id=parent["agent_id"],
                name=name1,
                description=desc1,
                parent_skill_id=skill_id,
                split_snapshot_id=snapshot_id,
                member_ids=members1,
            )
            # Freeze the parent: re-point its membership to the children. The skill
            # row stays as a lineage anchor (never deleted); the insights and their
            # vec rows are untouched — only the membership rows move.
            store.conn.execute(
                "DELETE FROM skill_members WHERE skill_id = ?", (skill_id,)
            )
            splits.append(
                SkillSplit(
                    parent_skill_id=skill_id,
                    child_skill_ids=(child0, child1),
                    child_member_ids=(tuple(members0), tuple(members1)),
                    silhouette=sil,
                    accepted_by=accepted_by,
                    split_snapshot_id=snapshot_id,
                )
            )

    return MaintenanceResult(
        snapshot_id=snapshot_id,
        minted_snapshot=True,
        retired_insight_ids=tuple(retire_ids),
        displaced_insight_ids=tuple(displaced_ids),
        splits=tuple(splits),
        split_deferred=split_deferred,
        agent_split_decisions=_agent_split_stub(store, params),
    )


def _agent_split_stub(
    store: Store, params: MaintenanceParams
) -> tuple[AgentSplitDecision, ...]:
    """Evaluate the (always-deferred) agent-split gate for every agent (R21)."""
    rows = store.conn.execute("SELECT id FROM agents ORDER BY id ASC").fetchall()
    return tuple(maybe_agent_split(store, r["id"], params) for r in rows)
