"""Frontier ledger: per-FEAT exploration status driving episodes (plan-003 U3, R10).

The frontier is seeded from the registry (every confirmed FEAT gets a row;
:func:`agent_families.grading.registry.mint_feat` inserts one at mint) and
drives increment requests: **least-investigated first**, slice size from
config. Slice size is a behavior tunable routed from ``thresholds.toml`` by
the episode orchestrator (003 U6) — caller-supplied here per the established
U2 precedent; nothing in this module hardcodes it.

Mention-coverage guarantee (R10): at every settlement a deterministic audit
joins registry FEATs against ALL MSG ``mentions`` for the target; FEATs never
mentioned in any prompt/Q&A are **force-scheduled** — they jump ahead of the
normal ordering in the next episode's opening slice, and a force-scheduled
row is selectable even if its exploration status says ``explored`` (coverage
converges by mechanism, not by trusting explorer behavior). The flag clears
when the FEAT is investigated, and the audit recomputes it from mention state
alone, so re-running the audit is idempotent.

Deprecated FEATs never appear here: deprecation drops the frontier row
(:func:`~agent_families.grading.registry.deprecate_feat`), seeding skips
non-confirmed rows, and slice selection joins on ``status = 'confirmed'``
belt-and-braces.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from typing import TYPE_CHECKING

from agent_families.store import FRONTIER_STATUSES

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# Statuses an investigation may flip a frontier row to. `unexplored` is birth
# state only; `newly-discovered` is set at mid-episode mint (registry).
INVESTIGATED_STATUSES = ("partially-explored", "explored")

# The selectable-rows predicate, shared by slice selection and exhaustion so
# the two can never disagree: not-yet-explored rows, plus force-scheduled rows
# regardless of exploration status (the R10 coverage mechanism).
_SELECTABLE_SQL = (
    "FROM frontier f JOIN trace_feat t ON t.id = f.feat_id"
    " WHERE t.target = ? AND t.status = 'confirmed'"
    " AND (f.status != 'explored' OR f.force_scheduled = 1)"
)


class FrontierError(Exception):
    """Frontier misuse or a broken invariant, with an actionable message."""


def seed_frontier(store: Store, target: str) -> list[str]:
    """Insert ``unexplored`` rows for confirmed FEATs that lack one.

    Idempotent: FEATs already on the frontier are untouched, deprecated FEATs
    are never resurrected. Returns the newly-seeded FEAT ids, id-ordered.
    """
    with store.transaction():
        rows = store.conn.execute(
            "SELECT t.id FROM trace_feat t"
            " LEFT JOIN frontier f ON f.feat_id = t.id"
            " WHERE t.target = ? AND t.status = 'confirmed'"
            " AND f.feat_id IS NULL ORDER BY t.id",
            (target,),
        ).fetchall()
        for row in rows:
            store.conn.execute(
                "INSERT INTO frontier (feat_id) VALUES (?)", (row["id"],)
            )
    return [row["id"] for row in rows]


def select_slice(store: Store, target: str, slice_size: int) -> list[str]:
    """The next increment's frontier slice (R10): force-scheduled rows first,
    then least-investigated, FEAT id as the deterministic tiebreak.

    ``slice_size`` comes from the thresholds config via the episode
    orchestrator (U6) — validated here, never defaulted.
    """
    if (
        not isinstance(slice_size, int)
        or isinstance(slice_size, bool)
        or slice_size <= 0
    ):
        raise FrontierError(
            f"slice_size must be a positive integer (routed from thresholds"
            f" config by the orchestrator), got {slice_size!r}"
        )
    rows = store.conn.execute(
        f"SELECT f.feat_id {_SELECTABLE_SQL}"
        " ORDER BY f.force_scheduled DESC, f.investigation_count ASC,"
        " f.feat_id ASC LIMIT ?",
        (target, slice_size),
    ).fetchall()
    return [row["feat_id"] for row in rows]


def record_investigation(
    store: Store, feat_ids: Sequence[str], *, status: str
) -> None:
    """Record one investigation pass over a slice: bump each row's
    ``investigation_count``, flip its status, and clear ``force_scheduled``
    (the row got its scheduled slot; the next audit re-flags it if it is
    still unmentioned)."""
    if status not in INVESTIGATED_STATUSES:
        raise FrontierError(
            f"investigation status must be one of {INVESTIGATED_STATUSES}"
            f" (full ledger vocabulary: {FRONTIER_STATUSES}), got {status!r}"
        )
    with store.transaction():
        for fid in feat_ids:
            cur = store.conn.execute(
                "UPDATE frontier SET"
                " investigation_count = investigation_count + 1,"
                " status = ?, force_scheduled = 0 WHERE feat_id = ?",
                (status, fid),
            )
            if cur.rowcount == 0:
                raise FrontierError(
                    f"{fid} has no frontier row (deprecated, or never"
                    " minted/seeded) — investigations record against the"
                    " ledger only"
                )


def mention_coverage_audit(store: Store, target: str) -> list[str]:
    """R10's deterministic settlement audit: FEATs never mentioned in ANY MSG
    across episodes are force-scheduled ahead of the normal ordering; FEATs
    that have been mentioned get the flag cleared. Pure recompute from
    mention state — idempotent and order-independent. Returns the
    force-scheduled FEAT ids, id-ordered."""
    with store.transaction():
        store.conn.execute(
            "UPDATE frontier SET force_scheduled = CASE WHEN feat_id IN"
            " (SELECT DISTINCT feat_id FROM trace_msg_mentions)"
            " THEN 0 ELSE 1 END"
            " WHERE feat_id IN (SELECT id FROM trace_feat"
            "  WHERE target = ? AND status = 'confirmed')",
            (target,),
        )
        rows = store.conn.execute(
            "SELECT f.feat_id FROM frontier f"
            " JOIN trace_feat t ON t.id = f.feat_id"
            " WHERE t.target = ? AND f.force_scheduled = 1 ORDER BY f.feat_id",
            (target,),
        ).fetchall()
    flagged = [row["feat_id"] for row in rows]
    logger.info(
        "mention-coverage audit (%s): %d FEAT(s) force-scheduled", target,
        len(flagged),
    )
    return flagged


def frontier_exhausted(store: Store, target: str) -> bool:
    """True when no selectable rows remain — by construction exactly when
    :func:`select_slice` would return nothing (the episode terminal
    ``frontier_exhausted``, R3)."""
    row = store.conn.execute(
        f"SELECT 1 {_SELECTABLE_SQL} LIMIT 1", (target,)
    ).fetchone()
    return row is None


def frontier_row(store: Store, fid: str) -> sqlite3.Row | None:
    """One ledger row (status, investigation_count, force_scheduled)."""
    return store.conn.execute(
        "SELECT * FROM frontier WHERE feat_id = ?", (fid,)
    ).fetchone()
