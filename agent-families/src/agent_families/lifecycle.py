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

Phase 3 seams (built as seams, never the deferred machinery — plan-001 Scope
Boundaries):

- ``promote`` does NOT auto-resolve ``supersedes`` links — the superseded
  incumbent keeps its status; retiring it is a manual ``retire`` decision
  surfaced by ``status`` (R9). Ratchet governance lands at
  :func:`_supersede_resolution_seam`.
- ``promote``/``revive`` admission is unconditional — the active-cap tournament
  lands at :func:`_cap_tournament_seam`.
- ``retire``/``revive`` pivot on the ``retired`` status only; fitness-driven
  demotion to ``dormant`` is Phase 3 and no Phase 0 operation produces it.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_families.store import INSIGHT_PROVENANCES, VALIDATION_CLASSES, Store

# Statuses retire() may flip to retired. `dormant` is included for
# forward-compatibility although no Phase 0 operation produces it.
LIVE_STATUSES = ("quarantined", "active", "dormant")


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


# --- Phase 3 seams ---------------------------------------------------------------


def _cap_tournament_seam(store: Store, insight_ids: tuple[int, ...]) -> None:
    """PHASE 3 SEAM — active-cap tournament admission (DESIGN §17).

    Phase 0 promote/revive admission is unconditional by design (plan-001 Scope
    Boundaries). Phase 3 replaces this no-op with the tournament that decides
    which insights enter an agent's active set once it sits at ``active_cap``.
    """


def _supersede_resolution_seam(store: Store, insight_ids: tuple[int, ...]) -> None:
    """PHASE 3 SEAM — ratchet governance over ``supersedes`` links (R9).

    Promote leaves superseded incumbents untouched: links recorded at
    registration stay as flags, and retirement is a manual ``retire`` decision
    surfaced by ``status``. Phase 3's retire-on-promote / dissolve-on-revert
    lands here.
    """


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


def promote_batch(store: Store, batch_label: str) -> LifecycleResult:
    """Promote a batch: every quarantined member flips to active (R12)."""
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
        _supersede_resolution_seam(store, targets)
    return LifecycleResult("promote_batch", snapshot_id, targets)


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
    """Revive one retired insight back to active, unconditionally (R12/R14)."""
    status = _insight_status(store, insight_id)
    if status != "retired":
        raise LifecycleError(
            f"insight {insight_id} has status '{status}' — revive operates only on"
            " retired insights (dormant transitions are Phase 3)"
        )
    with store.queue_operation("revive_insight", str(insight_id)) as snapshot_id:
        store.set_status(insight_id, "active", snapshot_id)
        _cap_tournament_seam(store, (insight_id,))
    return LifecycleResult("revive_insight", snapshot_id, (insight_id,))


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
