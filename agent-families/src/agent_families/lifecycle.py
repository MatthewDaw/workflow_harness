"""Lifecycle operations: batch promote/revert and insight/skill retire/revive (R12).

Every operation here is an active-set mutation, so it flows through the store's
single-writer promotion queue (R4): one :meth:`Store.queue_operation` block per
call, which serializes against concurrent writers and mints exactly one
snapshot; every status flip inside writes a status-transition row keyed by that
snapshot (R3). Operations that find nothing to do raise :class:`LifecycleError`
BEFORE entering the queue, so failed and no-op calls mint no snapshot and the
snapshot parent chain stays linear and gapless.

Nothing in this module deletes or rewrites vec rows (R13): status visibility is
a query-time join owned by the readers (add-idea dedup sees all statuses;
render/export see active, plus quarantined behind a flag). The only deletion
anywhere is the revert-orphaned skill row plus its membership rows — insight
rows and their vectors are never dissolved.

R3 deferred-supersede + consolidation (008 U7, R16–R18):

- ``promote_batch`` activates the R3 **deferred-supersede** at
  :func:`_supersede_resolution_seam`: a contradiction detected at ingest is held
  as a ``contradicts`` edge (U6) and the incumbent is invalidated only here,
  under the promotion snapshot, *after* the batch passes validation — so an
  unvalidated/hallucinated idea can never silently kill a live rule. The legacy
  ``supersedes`` column / ``contradictions`` table stay the demoted interim
  flags (R6); they are NOT the resolution trigger.
- ``promote``/``revive`` admission is unconditional — the fixed-cap tournament is
  removed (DESIGN §6a governs survival in the slow derive pass, plan 009, not at
  promotion). :func:`_cap_tournament_seam` is a documented no-op (D-2).
- ``demote_to_dormant`` (R18) is the consolidation lifecycle op: children flip to
  ``dormant`` (preserved as evidence, never ``retired``) and gain
  ``generalizes_from`` edges to their parent; ``revive_insight`` accepts a
  ``dormant`` source. The *trigger* for consolidation is the plan-009 derive
  pass; this module only builds the op + transition.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_families import nli
from agent_families.store import INSIGHT_PROVENANCES, VALIDATION_CLASSES, Store

# Statuses a live insight may hold. `dormant` is produced by
# :func:`demote_to_dormant` (R18 consolidation) and is revivable like `retired`.
LIVE_STATUSES = ("quarantined", "active", "dormant")

# Source-authority rank for the deferred-supersede winner rule (R16): when a
# validated challenger contradicts a live incumbent, the winner is decided by
# authority > evidence-count > recency, in that exact order. Authority derives
# from provenance (DESIGN §5 "source authority"): deliberately curated human
# seeds and hand entry outrank machine-mined reflector lessons; `consolidated`
# derive-pass parents (plan 009) sit above reflector but below direct human /
# researched provenance. Higher wins. KTD-pinned here; config wiring is U9.
PROVENANCE_AUTHORITY = {
    "seeded": 4,
    "manual": 3,
    "researched": 2,
    "consolidated": 1,
    "reflector": 0,
}

# The NLI confidence floor for the promotion-time contradiction re-check (R16).
# Matches the ingest gate's default (pipeline NLI_CONFIDENCE_THRESHOLD_DEFAULT);
# U9 wires it from [nli].confidence_threshold. A re-check below the floor is
# treated as "no longer a confident contradiction" — the conservative choice
# leaves the live incumbent standing rather than retiring it on weak signal.
SUPERSEDE_RECHECK_CONFIDENCE_DEFAULT = 0.65


class LifecycleError(Exception):
    """Raised when a lifecycle operation cannot apply; nothing was written."""


@dataclass(frozen=True)
class LifecycleResult:
    """One applied lifecycle operation: its snapshot and everything it touched."""

    operation: str
    snapshot_id: int
    insight_ids: tuple[int, ...]
    removed_skill_ids: tuple[int, ...] = ()
    closed_contradiction_ids: tuple[int, ...] = ()
    # Incumbents retired by the promotion-time deferred-supersede (R16): a
    # validated challenger that beats a contradicted incumbent stamps the
    # incumbent's `invalid_at` and retires it under this op's snapshot.
    retired_incumbent_ids: tuple[int, ...] = ()


# --- promotion seams (R3) --------------------------------------------------------


def _cap_tournament_seam(store: Store, insight_ids: tuple[int, ...]) -> None:
    """DOCUMENTED NO-OP — the fixed-cap tournament is removed (008 D-2, R17).

    Promotion just *promotes* (the per-batch trust gate). Survival pressure —
    which insights stay in the active set under ``cost(G)`` — is governed by the
    DESIGN §6a objective in the slow derive pass (plan 009), which defines no
    per-promotion move. So there is nothing for this hook to do at promotion; it
    is kept (not deleted) only so the call sites read intentionally and a future
    objective pass has a named seam. It mints no snapshot and mutates nothing.
    """


def _insight_nli_text(row) -> str:
    """The atom text fed to the NLI re-check (premise/hypothesis).

    Mirrors the ingest gate's whole-atom orientation so a contradiction confirmed
    at ingest re-checks against the same surface form.
    """
    parts = [
        (row["precondition"] or "").strip(),
        (row["action"] or "").strip(),
        (row["expected_outcome"] or "").strip(),
    ]
    return " ".join(p for p in parts if p)


def _open_contradicts_edges(
    store: Store, challenger_ids: tuple[int, ...]
) -> list[tuple[int, int, int]]:
    """Open ``contradicts`` edges authored by the promoted challengers (R16).

    Returns ``(edge_id, challenger_id, incumbent_id)`` for every R3-proper
    ``contradicts`` edge (U6 writes ``src=challenger``, ``dst=incumbent``) whose
    incumbent is still live (not retired, ``invalid_at`` NULL) — i.e. unresolved.
    The demoted ``contradictions`` table is NOT read here: U6 records
    contradictions as edges, and resolving the legacy table would retire
    incumbents behind flags it was never meant to trigger.
    """
    if not challenger_ids:
        return []
    placeholders = ", ".join("?" for _ in challenger_ids)
    rows = store.conn.execute(
        "SELECT e.id AS edge_id, e.src AS challenger_id, e.dst AS incumbent_id"
        " FROM insight_edges e JOIN insights inc ON inc.id = e.dst"
        f" WHERE e.kind = 'contradicts' AND e.src IN ({placeholders})"
        " AND inc.status != 'retired' AND inc.invalid_at IS NULL"
        " ORDER BY e.id ASC",
        challenger_ids,
    ).fetchall()
    return [
        (r["edge_id"], r["challenger_id"], r["incumbent_id"]) for r in rows
    ]


def _corroborate_count(store: Store, insight_id: int) -> int:
    """Evidence count = append-only ``corroborate`` votes on the insight (R16).

    Counted across every mode (the §11 cross-target-recurrence signal is not
    channel-scoped), straight from the fitness-event log.
    """
    return store.conn.execute(
        "SELECT COUNT(*) AS n FROM fitness_events"
        " WHERE insight_id = ? AND kind = 'corroborate'",
        (insight_id,),
    ).fetchone()["n"]


def _challenger_beats_incumbent(
    store: Store, challenger_id: int, incumbent_id: int
) -> bool:
    """Apply authority > evidence-count > recency, in that exact order (R16).

    Returns True iff the validated challenger should retire the incumbent. A draw
    all the way down (same authority, evidence, recency) defaults to the
    incumbent — a live rule is never retired on a tie.
    """
    challenger = store.get_insight(challenger_id)
    incumbent = store.get_insight(incumbent_id)

    c_auth = PROVENANCE_AUTHORITY.get(challenger["provenance"], 0)
    i_auth = PROVENANCE_AUTHORITY.get(incumbent["provenance"], 0)
    if c_auth != i_auth:
        return c_auth > i_auth

    c_evid = _corroborate_count(store, challenger_id)
    i_evid = _corroborate_count(store, incumbent_id)
    if c_evid != i_evid:
        return c_evid > i_evid

    # Recency: newer wins. created_at is an ISO-8601 string (lexicographically
    # ordered); the row id is the deterministic tie-breaker within a timestamp.
    c_key = (challenger["created_at"], challenger_id)
    i_key = (incumbent["created_at"], incumbent_id)
    return c_key > i_key


def _supersede_resolution_seam(
    store: Store,
    challenger_ids: tuple[int, ...],
    snapshot_id: int,
    *,
    nli_model: str | None = None,
    nli_mode: str | None = None,
    nli_fixtures_dir: object | None = None,
    nli_confidence_threshold: float = SUPERSEDE_RECHECK_CONFIDENCE_DEFAULT,
    _nli_encoder=None,
) -> tuple[int, ...]:
    """Resolve deferred-supersedes for a just-promoted batch (R16).

    Runs *after* the batch's quarantined members are flipped to ``active`` (the
    admission/objective handoff), so only the surviving active set resolves: for
    each open ``contradicts`` edge a promoted challenger authored, re-check the
    contradiction with a cheap deterministic NLI confirm (NOT a quota judge call
    inside the write txn). If the contradiction no longer holds with confidence,
    the incumbent is left standing. If it holds, decide the winner by
    authority > evidence-count > recency; when the challenger wins, stamp the
    incumbent's ``invalid_at`` and ``set_status(incumbent, "retired",
    snapshot_id)`` under the minted snapshot, closing the edge. When the
    incumbent wins, nothing is retired — the challenger stays as promoted.

    Returns the retired incumbent ids. Must run inside the promotion queue block.
    """
    model = nli_model if nli_model is not None else nli.DEFAULT_MODEL
    retired: list[int] = []
    seen: set[int] = set()
    for _edge_id, challenger_id, incumbent_id in _open_contradicts_edges(
        store, challenger_ids
    ):
        if incumbent_id in seen:
            continue  # already retired by an earlier challenger this batch
        incumbent = store.get_insight(incumbent_id)
        challenger = store.get_insight(challenger_id)
        verdict = nli.classify(
            _insight_nli_text(incumbent),
            _insight_nli_text(challenger),
            model=model,
            mode=nli_mode,
            fixtures_dir=nli_fixtures_dir,
            _encoder=_nli_encoder,
        )
        if not (
            verdict.label == "contradiction"
            and verdict.confidence >= nli_confidence_threshold
        ):
            continue  # stale/weak contradiction — leave the live incumbent
        if not _challenger_beats_incumbent(store, challenger_id, incumbent_id):
            continue  # incumbent wins — challenger stays promoted, no retirement
        store.set_invalid_at(incumbent_id, _invalid_at_stamp(snapshot_id), snapshot_id)
        store.set_status(incumbent_id, "retired", snapshot_id)
        seen.add(incumbent_id)
        retired.append(incumbent_id)
    return tuple(retired)


def _invalid_at_stamp(snapshot_id: int) -> str:
    """The ``invalid_at`` value: the snapshot the supersede rode (R16/§5).

    ``valid_at``/``invalid_at`` are world/version-validity stamps; pinning to the
    promotion snapshot keeps the temporal stamp reconstructible against the same
    snapshot chain that retired the incumbent.
    """
    return f"snapshot:{int(snapshot_id)}"


# --- batch operations ---------------------------------------------------------------


def _batch_id(store: Store, batch_label: str) -> int:
    row = store.conn.execute(
        "SELECT id FROM batches WHERE label = ?", (batch_label,)
    ).fetchone()
    if row is None:
        raise LifecycleError(f"unknown batch '{batch_label}'")
    return row["id"]


def _batch_insights(
    store: Store, batch_id: int, statuses: tuple[str, ...]
) -> tuple[int, ...]:
    placeholders = ", ".join("?" for _ in statuses)
    rows = store.conn.execute(
        f"SELECT id FROM insights WHERE batch_id = ? AND status IN ({placeholders})"
        " ORDER BY id ASC",
        (batch_id, *statuses),
    ).fetchall()
    return tuple(r["id"] for r in rows)


def promote_batch(
    store: Store,
    batch_label: str,
    *,
    nli_model: str | None = None,
    nli_mode: str | None = None,
    nli_fixtures_dir: object | None = None,
    nli_confidence_threshold: float = SUPERSEDE_RECHECK_CONFIDENCE_DEFAULT,
    _nli_encoder=None,
) -> LifecycleResult:
    """Promote a batch: every quarantined member flips to active (R12).

    After admission, the R3 deferred-supersede resolves: any promoted challenger
    that contradicts a live incumbent (a ``contradicts`` edge from U6) retires it
    here — under this op's snapshot, never at ingest (R16). The NLI re-check runs
    only when such an edge exists, so a contradiction-free promote never touches
    the model and stays fully offline.
    """
    batch_id = _batch_id(store, batch_label)
    targets = _batch_insights(store, batch_id, ("quarantined",))
    if not targets:
        raise LifecycleError(
            f"batch '{batch_label}' has no quarantined insights to promote"
        )
    with store.queue_operation("promote_batch", batch_label) as snapshot_id:
        for insight_id in targets:
            store.set_status(insight_id, "active", snapshot_id)
        _cap_tournament_seam(store, targets)
        retired = _supersede_resolution_seam(
            store,
            targets,
            snapshot_id,
            nli_model=nli_model,
            nli_mode=nli_mode,
            nli_fixtures_dir=nli_fixtures_dir,
            nli_confidence_threshold=nli_confidence_threshold,
            _nli_encoder=_nli_encoder,
        )
    return LifecycleResult(
        "promote_batch", snapshot_id, targets, retired_incumbent_ids=retired
    )


def revert_batch(store: Store, batch_label: str) -> LifecycleResult:
    """Revert a batch: active/quarantined members flip to retired (R12).

    Open contradiction flags raised by reverted challengers close under the
    minted snapshot (R9), and batch-created skills are removed only when no
    non-reverted members remain — a member appended by any other batch keeps
    the skill alive (it survives flagged, see
    :func:`skills_created_by_reverted_batches`).
    """
    batch_id = _batch_id(store, batch_label)
    targets = _batch_insights(store, batch_id, ("active", "quarantined"))
    if not targets:
        raise LifecycleError(
            f"batch '{batch_label}' has no active or quarantined insights to revert"
        )
    placeholders = ", ".join("?" for _ in targets)
    with store.queue_operation("revert_batch", batch_label) as snapshot_id:
        for insight_id in targets:
            store.set_status(insight_id, "retired", snapshot_id)
        flag_rows = store.conn.execute(
            "SELECT id FROM contradictions WHERE status = 'open'"
            f" AND challenger_id IN ({placeholders}) ORDER BY id ASC",
            targets,
        ).fetchall()
        closed = tuple(r["id"] for r in flag_rows)
        if closed:
            flag_placeholders = ", ".join("?" for _ in closed)
            store.conn.execute(
                "UPDATE contradictions SET status = 'closed', closed_snapshot_id = ?"
                f" WHERE id IN ({flag_placeholders})",
                (snapshot_id, *closed),
            )
        removed = _remove_orphaned_batch_skills(store, batch_id)
    return LifecycleResult(
        "revert_batch",
        snapshot_id,
        targets,
        removed_skill_ids=removed,
        closed_contradiction_ids=closed,
    )


def _remove_orphaned_batch_skills(store: Store, batch_id: int) -> tuple[int, ...]:
    """Remove the reverted batch's created skills with no non-reverted members (R12).

    "Non-reverted member" = a member insight belonging to any other batch (or
    none). Membership rows of a removed skill go with it so no membership row
    dangles; the member insights and their vec rows are never deleted (R13).
    """
    rows = store.conn.execute(
        "SELECT s.id FROM skills s"
        " WHERE s.created_batch_id = ?"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM skill_members m JOIN insights i ON i.id = m.insight_id"
        "   WHERE m.skill_id = s.id AND i.batch_id IS NOT ?"
        " ) ORDER BY s.id ASC",
        (batch_id, batch_id),
    ).fetchall()
    removed = tuple(r["id"] for r in rows)
    if removed:
        placeholders = ", ".join("?" for _ in removed)
        store.conn.execute(
            f"DELETE FROM skill_members WHERE skill_id IN ({placeholders})", removed
        )
        store.conn.execute(f"DELETE FROM skills WHERE id IN ({placeholders})", removed)
    return removed


# --- insight retire/revive --------------------------------------------------------------


def _insight_status(store: Store, insight_id: int) -> str:
    row = store.get_insight(insight_id)
    if row is None:
        raise LifecycleError(f"insight {insight_id} does not exist")
    return row["status"]


def retire_insight(store: Store, insight_id: int) -> LifecycleResult:
    """Retire one insight: live status flips to retired; vec row untouched (R13)."""
    status = _insight_status(store, insight_id)
    if status == "retired":
        raise LifecycleError(f"insight {insight_id} is already retired")
    with store.queue_operation("retire_insight", str(insight_id)) as snapshot_id:
        store.set_status(insight_id, "retired", snapshot_id)
    return LifecycleResult("retire_insight", snapshot_id, (insight_id,))


def revive_insight(store: Store, insight_id: int) -> LifecycleResult:
    """Revive one retired-or-dormant insight back to active (R12/R14, R18).

    Both ``retired`` (manual/auto-revert) and ``dormant`` (R18 consolidation
    children, preserved as evidence) are revivable sources.
    """
    status = _insight_status(store, insight_id)
    if status not in ("retired", "dormant"):
        raise LifecycleError(
            f"insight {insight_id} has status '{status}' — revive operates only on"
            " retired or dormant insights"
        )
    with store.queue_operation("revive_insight", str(insight_id)) as snapshot_id:
        store.set_status(insight_id, "active", snapshot_id)
        _cap_tournament_seam(store, (insight_id,))
    return LifecycleResult("revive_insight", snapshot_id, (insight_id,))


def demote_to_dormant(
    store: Store, child_ids: tuple[int, ...], parent_insight_id: int
) -> LifecycleResult:
    """Demote consolidation children to ``dormant`` under one snapshot (R18).

    The consolidation lifecycle op (DESIGN §5 Operation 3): a synthesized parent
    insight subsumes its specifics, which are moved OUT of the active index but
    *preserved as evidence* — flipped to ``dormant``, never ``retired`` —, and a
    ``generalizes_from`` edge is materialized from the parent to each child. The
    *trigger* (when to consolidate) is the plan-009 derive pass; this builds only
    the op + the dormant transition. Raises before minting a snapshot if the
    parent or any child is unknown, or a child is already retired.
    """
    if store.get_insight(parent_insight_id) is None:
        raise LifecycleError(f"parent insight {parent_insight_id} does not exist")
    if not child_ids:
        raise LifecycleError("demote_to_dormant requires at least one child")
    targets = tuple(dict.fromkeys(child_ids))  # de-dup, preserve order
    for child_id in targets:
        if child_id == parent_insight_id:
            raise LifecycleError(
                f"insight {parent_insight_id} cannot be its own consolidation child"
            )
        status = _insight_status(store, child_id)  # raises if unknown
        if status == "retired":
            raise LifecycleError(
                f"insight {child_id} is retired — consolidation preserves children"
                " as dormant evidence, it never demotes a retired row"
            )
    with store.queue_operation(
        "consolidate", str(parent_insight_id)
    ) as snapshot_id:
        for child_id in targets:
            store.set_status(child_id, "dormant", snapshot_id)
            store.add_insight_edge(parent_insight_id, child_id, "generalizes_from")
    return LifecycleResult("demote_to_dormant", snapshot_id, targets)


# --- skill retire/revive ----------------------------------------------------------------


def _skill_members_with_status(
    store: Store, skill_id: int, statuses: tuple[str, ...]
) -> tuple[int, ...]:
    if store.conn.execute(
        "SELECT 1 FROM skills WHERE id = ?", (skill_id,)
    ).fetchone() is None:
        raise LifecycleError(f"skill {skill_id} does not exist")
    placeholders = ", ".join("?" for _ in statuses)
    rows = store.conn.execute(
        "SELECT m.insight_id FROM skill_members m JOIN insights i ON i.id = m.insight_id"
        f" WHERE m.skill_id = ? AND i.status IN ({placeholders})"
        " ORDER BY m.position ASC",
        (skill_id, *statuses),
    ).fetchall()
    return tuple(r["insight_id"] for r in rows)


def retire_skill(store: Store, skill_id: int) -> LifecycleResult:
    """Retire every live member of a skill in one queue operation (R12).

    The skill row always survives — an emptied skill is surfaced by ``status``
    via :func:`empty_skills`, never deleted.
    """
    targets = _skill_members_with_status(store, skill_id, LIVE_STATUSES)
    if not targets:
        raise LifecycleError(f"skill {skill_id} has no live members to retire")
    with store.queue_operation("retire_skill", str(skill_id)) as snapshot_id:
        for insight_id in targets:
            store.set_status(insight_id, "retired", snapshot_id)
    return LifecycleResult("retire_skill", snapshot_id, targets)


def revive_skill(store: Store, skill_id: int) -> LifecycleResult:
    """Revive every retired member of a skill back to active in one operation."""
    targets = _skill_members_with_status(store, skill_id, ("retired",))
    if not targets:
        raise LifecycleError(f"skill {skill_id} has no retired members to revive")
    with store.queue_operation("revive_skill", str(skill_id)) as snapshot_id:
        for insight_id in targets:
            store.set_status(insight_id, "active", snapshot_id)
        _cap_tournament_seam(store, targets)
    return LifecycleResult("revive_skill", snapshot_id, targets)


# --- status flags (consumed by `af status`, R21) -------------------------------------------


def empty_skills(store: Store) -> tuple[int, ...]:
    """Skills with no live (non-retired) members — flagged, never auto-deleted (R12)."""
    rows = store.conn.execute(
        "SELECT s.id FROM skills s WHERE NOT EXISTS ("
        "  SELECT 1 FROM skill_members m JOIN insights i ON i.id = m.insight_id"
        "  WHERE m.skill_id = s.id AND i.status != 'retired'"
        ") ORDER BY s.id ASC"
    ).fetchall()
    return tuple(r["id"] for r in rows)


def skills_created_by_reverted_batches(store: Store) -> tuple[int, ...]:
    """Surviving skills whose creating batch was later reverted (R12 flag).

    Derived from the promotion queue's audit trail: ``revert_batch`` rows carry
    the batch label as their detail, so no extra schema is needed.
    """
    rows = store.conn.execute(
        "SELECT DISTINCT s.id FROM skills s"
        " JOIN batches b ON b.id = s.created_batch_id"
        " JOIN promotion_queue q ON q.operation = 'revert_batch' AND q.detail = b.label"
        " ORDER BY s.id ASC"
    ).fetchall()
    return tuple(r["id"] for r in rows)


# --- Plan 007 U10: provenance & validation-class stamps (KTD5) ------------------
#
# ``insights.provenance`` and ``batches.validation_class`` (007 U1) are *metadata*
# columns, NOT active-set state: stamping one moves no insight between
# active/quarantined/dormant, so these writes do NOT flow through the promotion
# queue and mint NO snapshot (unlike every operation above). They live here
# because batch/insight stamping is the lifecycle concern that owns it — the
# reflector's label-derived auto-stamp at registration, and the human's
# ``af add-idea --provenance``. The connection is autocommit (isolation_level=None),
# so each single-statement UPDATE commits on its own, matching ``ensure_batch``.

# Reflector batches are labelled ``reflect-ep%`` (the stage_b.py convention); this
# is the exact predicate U1's migration backfills provenance on.
REFLECTOR_BATCH_LABEL_PREFIX = "reflect-ep"


def provenance_for_batch_label(batch_label: str) -> str:
    """The provenance a batch's insights carry by its label convention (007 KTD5).

    ``reflect-ep%`` → ``reflector`` (the stage_b convention U1 backfills on);
    everything else → ``manual``. ``researched``/``seeded`` are set explicitly by
    their loaders (``af induct`` / the seed batch), never inferred from a label.
    """
    return (
        "reflector"
        if batch_label.startswith(REFLECTOR_BATCH_LABEL_PREFIX)
        else "manual"
    )


def _check_provenance(provenance: str) -> None:
    if provenance not in INSIGHT_PROVENANCES:
        raise LifecycleError(
            f"unknown provenance '{provenance}'"
            f" (expected one of {INSIGHT_PROVENANCES})"
        )


def stamp_insight_provenance(
    store: Store, insight_id: int, provenance: str
) -> None:
    """Stamp one insight's provenance (007 KTD5) — ``af add-idea --provenance``.

    A metadata write; mints no snapshot. Raises if the insight is unknown so a bad
    ref fails loudly rather than silently no-op'ing.
    """
    _check_provenance(provenance)
    cur = store.conn.execute(
        "UPDATE insights SET provenance = ? WHERE id = ?", (provenance, insight_id)
    )
    if cur.rowcount == 0:
        raise LifecycleError(f"insight {insight_id} does not exist")


def auto_stamp_batch_provenance(
    store: Store, batch_label: str, provenance: str | None = None
) -> tuple[int, ...]:
    """Stamp every insight in a batch with ``provenance`` (007 KTD5).

    With ``provenance=None`` the value is derived from the batch label — the
    reflector auto-stamp (``reflect-ep%`` → ``reflector``). Returns the stamped
    insight ids (empty if the batch has none yet). A metadata write; no snapshot.
    """
    if provenance is None:
        provenance = provenance_for_batch_label(batch_label)
    _check_provenance(provenance)
    batch_id = _batch_id(store, batch_label)
    rows = store.conn.execute(
        "SELECT id FROM insights WHERE batch_id = ? ORDER BY id ASC", (batch_id,)
    ).fetchall()
    ids = tuple(r["id"] for r in rows)
    if ids:
        store.conn.execute(
            "UPDATE insights SET provenance = ? WHERE batch_id = ?",
            (provenance, batch_id),
        )
    return ids


def set_batch_validation_class(
    store: Store, batch_label: str, validation_class: str
) -> None:
    """Set a batch's validation-class routing tag at registration (007 KTD5).

    The tag ``validate.py``'s substrate routing reads (``code``/``elicitation``/
    ``general``). A metadata write; mints no snapshot.
    """
    if validation_class not in VALIDATION_CLASSES:
        raise LifecycleError(
            f"unknown validation_class '{validation_class}'"
            f" (expected one of {VALIDATION_CLASSES})"
        )
    batch_id = _batch_id(store, batch_label)
    store.conn.execute(
        "UPDATE batches SET validation_class = ? WHERE id = ?",
        (validation_class, batch_id),
    )
