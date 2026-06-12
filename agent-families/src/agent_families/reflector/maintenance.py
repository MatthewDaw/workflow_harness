"""Settlement fitness, telemetry, and usage-conditioned retirement (plan-009 U7, R16).

R3 reform (plan-009) **demotes the R2 ratchet machinery this module used to carry**.
Deleted here (R16): the **cap tournament** (``tournament_displacements`` + the
``active_cap`` tunable), all the **skill-split machinery** (``split_candidates``,
``_partition_skill``, the k-means / silhouette arithmetic, the ``DEFAULT_SPLIT_*``
thresholds), the **agent-split stub** (``maybe_agent_split``), and the
``run_maintenance`` pass that orchestrated them. Group structure is now *derived*
(:mod:`agent_families.reflector.derive`), not authored by clustering-and-splitting,
and admission is unconditional (the fixed cap is gone — plan-008 D-2 /
:func:`agent_families.lifecycle._cap_tournament_seam`).

What survives, end to end:

1. **Settlement fitness writes** (R19, retained) — the orchestrator's once-per-episode
   write of the append-only fitness log. The event semantics are exact and
   load-bearing:

   - *retrieval* = an insight **actually rendered** into a session prompt (post-budget);
   - *win* = the session's ticket reaches ``done`` **AND** is unimplicated in any
     failed SCEN chain at settlement (co-occurrence is not enough);
   - *loss* = **causal blame only** — the final attribution's
     ``implicated_existing_insights``; a mere bystander is never punished.

   ``trial`` / ``benchmark`` events land in their own channel (keyed by the episode's
   ``mode``); the ratchet reads the ``training`` channel only, so validation traffic
   never moves fitness (R19). ``settle_fitness`` is the trace writer the R3
   usage-conditioned retirement (below) reads.

2. **Provenance × world telemetry** (007 U10, retained) — the fitness log grouped by
   (insight provenance, episode world), so "wins greenfield, loses brownfield" is a
   queryable fact.

3. **R3 usage-conditioned retirement** (plan-009 U5, R12) — candidacy is a
   censored-survival hazard on **usage** (a usage-stale insight is "presumed dead"),
   and each candidate's demotion is then scored as an isolated ``cost(G)`` delta
   (DESIGN §6a). The old **net-fitness** ``retirement_candidates`` counter is retained
   alongside it as the documented contrast baseline that the U5 acceptance test
   (`test_retirement_is_usage_conditioned_survival`) compares the hazard against — no
   R3 caller reads it; the survival hazard is the live mechanism.

Discipline carried from Phase 0: every active-set mutation flows through
:meth:`Store.queue_operation` (one minted snapshot per pass); a no-op pass mints
nothing so the snapshot chain stays gapless; **insights are never deleted and vec
rows are never touched** — retirement is a status flip to ``dormant`` (preserved,
revivable). Offline by construction: every LLM call this module would make is gone;
it reads and writes the store only. Zero quota, no ``claude`` on PATH.

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
  events never feed the ratchet (separate channel). [enforced by :func:`fitness_score`
  reading the ``training`` channel only]

Required acceptance test / invariant (plan-007 U10) -> test (in
``tests/test_provenance_telemetry.py``):

- ``test_provenance_world_fitness_grouping`` — the fitness table groups correctly by
  provenance × world. [enforced by :func:`provenance_world_fitness`]

Required acceptance test / invariant (plan-009 U5, R12) -> test (in
``tests/test_consolidation.py``):

- ``test_retirement_is_usage_conditioned_survival`` — candidacy is the usage hazard,
  not the net-fitness counter; ``settle_fitness`` is retained. [enforced by
  :func:`survival_retirement_candidates` / :func:`survival_retirement`]

Required acceptance test / invariant (plan-009 U7, R16) -> test (in
``tests/test_demotions.py``):

- ``test_maintenance_keeps_only_settle_fitness_and_telemetry`` — ``settle_fitness``,
  telemetry, and the usage-conditioned retirement survive; the cap-tournament / split
  / k-means / agent-split entry points are gone.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from agent_families.reflector import consolidation
from agent_families.reflector import objective as obj
from agent_families.store import RUN_MODES, Store
from agent_families.vecindex import VecIndex

# --- tunables (caller-supplied, carried defaults; PROVENANCE per DESIGN §17) -----
#
# Not read from config here — they ride in :class:`MaintenanceParams` /
# :class:`SurvivalParams`, and the run-assembly wiring routes the live values from
# ``thresholds.toml``.

# PROVENANCE: §4 outcome-driven retirement / §6 R2 "minimum observation count" — never
# retire an insight that has not earned real exposure (clustering/fitness on small N is
# noise). TUNING METRIC: false-retirement rate vs library churn.
DEFAULT_RETIREMENT_MIN_RETRIEVALS = 5

# PROVENANCE: §4 "insights that stop earning retrievals-with-wins are demoted" — a net
# fitness (wins - losses) at or below this floor, after real exposure, retires. TUNING
# METRIC: dormant-revival rate (over-eager pruning) vs library size.
#
# DEMOTED at plan-009 U5 (R12): the net-fitness counter is REPLACED by the
# usage-conditioned censored-survival hazard below (:func:`survival_retirement`). Kept
# (not deleted) only as the documented contrast baseline the U5 acceptance test
# compares the hazard against; no R3 caller reads it.
DEFAULT_RETIREMENT_MAX_NET_FITNESS = 0

# PROVENANCE: §4/§6 R3 "usage-conditioned survival" (plan-009 R12). An insight that has
# earned real exposure but whose USAGE has gone quiet — no retrieval within the last
# ``staleness_window`` snapshots — is censored-survival "presumed dead" and becomes a
# retirement candidate, REGARDLESS of its win/loss sign. A still-used insight (a recent
# retrieval) survives even with a negative net fitness, because usage — not the fitness
# sign — is the R3 survival signal. TUNING METRIC: dormant-revival rate vs library
# churn; re-pin once co-retrieval flow replaces the similarity proxy.
DEFAULT_RETIREMENT_STALENESS_WINDOW = 3


class MaintenanceError(Exception):
    """A broken maintenance precondition with an actionable message."""


@dataclass(frozen=True)
class MaintenanceParams:
    """Caller-supplied retirement tunables (routed from thresholds.toml by the
    run-assembly wiring; never read from config here).

    Trimmed at plan-009 U7 (R16): the cap-tournament (``active_cap``) and skill-split
    (``split_*`` / ``agent_split_*`` / ``min_routing_decisions``) tunables are gone
    with the machinery they fed. Only the net-fitness retirement gates remain (the
    contrast baseline — see the module docstring); the live R3 retirement uses
    :class:`SurvivalParams`.
    """

    retirement_min_retrievals: int = DEFAULT_RETIREMENT_MIN_RETRIEVALS
    retirement_max_net_fitness: int = DEFAULT_RETIREMENT_MAX_NET_FITNESS

    def __post_init__(self) -> None:
        if self.retirement_min_retrievals < 0:
            raise MaintenanceError(
                "retirement_min_retrievals must be >= 0, got"
                f" {self.retirement_min_retrievals}"
            )


# --- settlement fitness writes (R19) --------------------------------------------


@dataclass(frozen=True)
class SessionRender:
    """The insights rendered (post-budget) into one session's prompt, tied to the
    session's ticket (``None`` for ticket-less sessions such as planning).

    The orchestrator builds one per session from the retrieval result's injected
    insights.
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
    - *win*: one event per (rendered insight, session) whose ticket reached ``done``
      AND is **not** in ``implicated_ticket_ids`` (the failed-SCEN-chain set from
      Stage A). Ticket-less sessions never win.
    - *loss*: one event per insight in ``causal_blame_insight_ids`` (the final
      attribution's ``implicated_existing_insights``). Causal blame only — an insight
      merely rendered into a failed run is not punished.

    The events are stamped with the episode's library snapshot and the episode's
    ``mode`` (the channel). No snapshot is minted.
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
    **training** channel only (R19). Trial/benchmark events are excluded by the channel
    filter, so validation traffic never moves this number."""
    counts = store.fitness_counts(insight_id, snapshot_id=snapshot_id, mode="training")
    return counts["win"] - counts["loss"]


def _training_retrievals(store: Store, insight_id: int, snapshot_id: int | None) -> int:
    counts = store.fitness_counts(insight_id, snapshot_id=snapshot_id, mode="training")
    return counts["retrieval"]


# --- provenance × world fitness telemetry (007 U10, KTD5/KTD9) -------------------
#
# KTD9's named decision telemetry: group the fitness log by (insight provenance,
# episode world) so "wins greenfield, loses brownfield" is a queryable FACT. World is
# joined via ``fitness_events.episode_id -> episodes.world`` (no world column on
# fitness rows, 007 KTD6); events with no episode bucket under ``brownfield``.


@dataclass(frozen=True)
class ProvenanceWorldFitness:
    """One (provenance, world) fitness bucket over the event log (007 U10)."""

    provenance: str
    world: str
    retrievals: int
    wins: int
    losses: int

    @property
    def net(self) -> int:
        """The ratchet's fitness scalar for this bucket: ``wins - losses``."""
        return self.wins - self.losses


def provenance_world_fitness(
    store: Store,
    *,
    snapshot_id: int | None = None,
    mode: str = "training",
) -> list[ProvenanceWorldFitness]:
    """The fitness log grouped by insight provenance × episode world (007 U10).

    ``mode`` selects the channel (default the ratchet's ``training`` channel, so
    validation traffic stays out, R19); ``snapshot_id`` bounds events to
    ``snapshot_id <= S`` when given. World is joined through
    ``fitness_events.episode_id``; a NULL episode buckets under ``brownfield``. Rows
    are ordered by (provenance, world) for stable, eyeballable output.
    """
    if mode not in RUN_MODES:
        raise MaintenanceError(
            f"unknown run mode '{mode}' (expected one of {RUN_MODES})"
        )
    sql = (
        "SELECT i.provenance AS provenance,"
        " COALESCE(e.world, 'brownfield') AS world,"
        " f.kind AS kind, COUNT(*) AS n"
        " FROM fitness_events f"
        " JOIN insights i ON i.id = f.insight_id"
        " LEFT JOIN episodes e ON e.id = f.episode_id"
        " WHERE f.mode = ?"
    )
    params: list[object] = [mode]
    if snapshot_id is not None:
        sql += " AND f.snapshot_id <= ?"
        params.append(int(snapshot_id))
    # Group by the COALESCE expression, NOT the `world` alias: SQLite binds a bare
    # `world` in GROUP BY to the episodes.world column (NULL for a standalone run),
    # which would split NULL-episode events into their own group before the COALESCE
    # buckets them under 'brownfield'.
    sql += " GROUP BY i.provenance, COALESCE(e.world, 'brownfield'), f.kind"

    buckets: dict[tuple[str, str], dict[str, int]] = {}
    for row in store.conn.execute(sql, params).fetchall():
        key = (row["provenance"], row["world"])
        counts = buckets.setdefault(key, {"retrieval": 0, "win": 0, "loss": 0})
        counts[row["kind"]] = row["n"]
    return [
        ProvenanceWorldFitness(
            provenance=provenance,
            world=world,
            retrievals=counts["retrieval"],
            wins=counts["win"],
            losses=counts["loss"],
        )
        for (provenance, world), counts in sorted(buckets.items())
    ]


# --- net-fitness retirement: the demoted contrast baseline (R20 -> R12) ----------


def _active_insight_ids(store: Store, snapshot_id: int) -> list[int]:
    rows = store.conn.execute("SELECT id FROM insights ORDER BY id ASC").fetchall()
    return [
        r["id"] for r in rows if store.status_at(r["id"], snapshot_id) == "active"
    ]


def retirement_candidates(
    store: Store, snapshot_id: int, params: MaintenanceParams
) -> list[int]:
    """Active insights that earned real exposure yet a net-non-positive fitness.

    DEMOTED (R12): this net-fitness counter is no longer the live retirement mechanism
    — :func:`survival_retirement` (usage-conditioned) is. It is retained as the
    documented contrast baseline the U5 acceptance test compares the usage hazard
    against. Both gates read the reconstructed event log, so the decision is a pure
    function of the append-only fitness history at the snapshot.
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


# --- R3 retirement: usage-conditioned censored-survival (plan-009 U5, R12) -------
#
# The R3 survival model, both pure functions of the append-only fitness log + the v1
# similarity flow graph:
#
# 1. **Hazard candidacy** (:func:`survival_retirement_candidates`) — an active insight
#    that earned real exposure (``min_retrievals``) but whose last retrieval is more
#    than ``staleness_window`` snapshots old is "presumed dead" and becomes a
#    candidate. The win/loss SIGN is never read here.
# 2. **cost(G) gate** (:func:`survival_retirement`) — each candidate's demotion is
#    adopted only if removing it from the active graph strictly lowers ``cost(G)``; a
#    load-bearing bridge node survives. ``settle_fitness`` is retained (it feeds this).


@dataclass(frozen=True)
class SurvivalParams:
    """Tunables for the R3 usage-conditioned retirement (routed from config, U9)."""

    min_retrievals: int = DEFAULT_RETIREMENT_MIN_RETRIEVALS
    staleness_window: int = DEFAULT_RETIREMENT_STALENESS_WINDOW
    knn_k: int = 15
    module_overhead_bits: float = obj.DEFAULT_MODULE_OVERHEAD_BITS

    def __post_init__(self) -> None:
        if self.min_retrievals < 0:
            raise MaintenanceError(
                f"min_retrievals must be >= 0, got {self.min_retrievals}"
            )
        if self.staleness_window < 1:
            raise MaintenanceError(
                f"staleness_window must be >= 1, got {self.staleness_window}"
            )
        if self.knn_k < 1:
            raise MaintenanceError(f"knn_k must be >= 1, got {self.knn_k}")
        if self.module_overhead_bits < 0:
            raise MaintenanceError(
                f"module_overhead_bits must be >= 0, got {self.module_overhead_bits}"
            )


@dataclass(frozen=True)
class SurvivalRetirementResult:
    """One usage-conditioned retirement pass (R12) — what it demoted and spared."""

    snapshot_id: int | None
    minted_snapshot: bool
    demoted_insight_ids: tuple[int, ...]
    # Hazard candidates the cost(G) gate spared (removing them did not lower cost).
    spared_by_cost_gate: tuple[int, ...]


def _last_retrieval_snapshot(
    store: Store, insight_id: int, snapshot_id: int
) -> int | None:
    """The snapshot of an insight's most recent training retrieval (``<= snapshot_id``).

    ``None`` if the insight has never been retrieved in the training channel.
    """
    row = store.conn.execute(
        "SELECT MAX(snapshot_id) AS s FROM fitness_events"
        " WHERE insight_id = ? AND kind = 'retrieval' AND mode = 'training'"
        " AND snapshot_id <= ?",
        (insight_id, int(snapshot_id)),
    ).fetchone()
    return row["s"] if row is not None else None


def survival_retirement_candidates(
    store: Store, snapshot_id: int, params: SurvivalParams
) -> list[int]:
    """Active insights whose USAGE has gone stale — the censored-survival hazard (R12).

    A candidate has (a) earned real exposure — at least ``min_retrievals`` training
    retrievals at or before ``snapshot_id`` — AND (b) no retrieval within the last
    ``staleness_window`` snapshots (``snapshot_id - last_retrieval >= window``). The
    win/loss balance is **not** consulted, so a low-fitness-but-recently-used insight
    is never a candidate. Pure function of the append-only fitness log; id-ordered.
    """
    candidates: list[int] = []
    for insight_id in _active_insight_ids(store, snapshot_id):
        retrievals = _training_retrievals(store, insight_id, snapshot_id)
        if retrievals < params.min_retrievals:
            continue  # not enough exposure to judge — never retire the under-tested
        last = _last_retrieval_snapshot(store, insight_id, snapshot_id)
        if last is None:
            continue  # no retrieval at all (defensive; retrievals>=min implies one)
        if snapshot_id - last >= params.staleness_window:
            candidates.append(insight_id)
    return candidates


def survival_retirement(
    store: Store,
    vec: VecIndex,
    *,
    params: SurvivalParams = SurvivalParams(),
    snapshot_id: int | None = None,
) -> SurvivalRetirementResult:
    """Run the R3 usage-conditioned retirement: hazard candidacy → cost(G) gate (R12).

    Candidates come from :func:`survival_retirement_candidates` (the usage hazard);
    each is demoted to ``dormant`` only if removing it from the active similarity flow
    graph strictly lowers ``cost(G)`` (the isolated per-move delta, U1) — a bridge node
    the store still leans on is spared. Demotions go to ``dormant`` (preserved as
    evidence, revivable — never deleted) under one minted snapshot; a pass with nothing
    to demote mints nothing. The ``settle_fitness`` trace writer is untouched and
    remains the producer of the usage events read here.
    """
    snap = store.current_snapshot_id() if snapshot_id is None else snapshot_id
    candidates = survival_retirement_candidates(store, snap, params)
    if not candidates:
        return SurvivalRetirementResult(
            snapshot_id=snap,
            minted_snapshot=False,
            demoted_insight_ids=(),
            spared_by_cost_gate=(),
        )

    node_ids, edges = consolidation.active_flow_graph(vec, k=params.knn_k)
    demote: list[int] = []
    spared: list[int] = []
    for insight_id in candidates:
        delta = consolidation.removal_cost_delta(
            node_ids,
            edges,
            insight_id,
            module_overhead_bits=params.module_overhead_bits,
        )
        if delta < 0:
            demote.append(insight_id)
        else:
            spared.append(insight_id)

    if not demote:
        return SurvivalRetirementResult(
            snapshot_id=snap,
            minted_snapshot=False,
            demoted_insight_ids=(),
            spared_by_cost_gate=tuple(spared),
        )

    with store.queue_operation(
        "survival_retirement", f"{len(demote)} demoted"
    ) as new_snap:
        for insight_id in demote:
            store.set_status(insight_id, "dormant", new_snap)
    return SurvivalRetirementResult(
        snapshot_id=new_snap,
        minted_snapshot=True,
        demoted_insight_ids=tuple(demote),
        spared_by_cost_gate=tuple(spared),
    )
