"""Partition-move engine (plan-009 U7, R15).

R3 reform (plan-009) **guts this module to the partition-move engine** (~120 of the
original 683 lines survive). Deleted here (R15): silhouette/k-means imports + all
threshold constants, ``check_base_prompt_residue``, ``evaluate_split_candidacy``,
``replay_agreement``/``routing_replay_agreement``, ``perform_agent_split``/
``run_agent_split``/``plan_agent_split``, the explorer-family seed evidence, and the
``AgentSplitParams`` dataclass. Group structure is now *derived* (``derive.py``), not
produced by clustering-and-routing-volume gating.

What survives:
- :class:`AgentSplitError` — the error type (callers catch it)
- :class:`SplitResult` — the handle returned by :func:`execute_split`
- :func:`execute_split` — distributes a parent module's members into two child modules,
  minting exactly one snapshot via ``queue_operation`` (not a plain transaction).
- :func:`revert_split` — inverse: restores all members to the parent and deletes the
  children; mints a forward snapshot.
- :func:`finalize_split` — stamps the parent a frozen retired-by-split anchor; blocks
  further reverts.

The three primitives operate on **module membership** (skill_members) and go through
``store.queue_operation`` (snapshot-minting), not the old plain-transaction path. The
derive pass assembles them to apply a whole partition atomically or revert it.

Offline by construction — no LLM calls, no clustering, no routing. Zero quota, no
``claude`` on PATH.

## Conformance (plan-009 U7 required acceptance tests -> ``tests/test_demotions.py``)

- ``test_agent_split_exposes_only_partition_engine`` — exports exactly
  ``execute_split`` / ``revert_split`` / ``finalize_split``; the R2 machinery raises
  ``AttributeError``. [enforced by this module's reduced public surface]
- ``test_execute_split_repoints_membership_and_mints_one_snapshot`` — one
  queue_operation row = one minted snapshot. [enforced by :func:`execute_split`]
- ``test_revert_split_restores_parent_membership`` — forward snapshot; children gone.
  [enforced by :func:`revert_split`]
- ``test_finalize_split_freezes_parent_and_blocks_revert`` — parent anchor stamped;
  subsequent revert raises. [enforced by :func:`finalize_split`]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Sequence

from agent_families.store import Store


class AgentSplitError(Exception):
    """Agent-split misuse or invariant breach with an actionable message."""


# ---- result handle -----------------------------------------------------------------


@dataclass
class SplitResult:
    """The handle returned by :func:`execute_split`.

    ``child_skill_ids`` — the two new child modules.
    ``snapshot_id`` — the snapshot minted for the split.
    ``finalized`` — True after :func:`finalize_split`; blocks :func:`revert_split`.
    """

    parent_skill_id: int
    child_skill_ids: tuple[int, int]
    snapshot_id: int
    finalized: bool = field(default=False)


# ---- the three primitives ----------------------------------------------------------


def execute_split(
    store: Store,
    parent_skill_id: int,
    clusters: tuple[Sequence[int], Sequence[int]],
    *,
    detail: str = "",
) -> SplitResult:
    """Distribute the parent module's members into two child modules (one snapshot).

    ``clusters`` must be a strict partition of the parent's current member set —
    a complete cover with no overlap and no empty cluster — or :class:`AgentSplitError`
    is raised before any write.

    One ``queue_operation("agent_split", ...)`` call mints exactly one snapshot; the
    two children carry ``parent_skill_id`` + ``split_snapshot_id`` for lineage.
    The parent's ``skill_members`` rows are deleted (emptied, not the skill itself —
    the parent remains as a frozen anchor, never deleted).
    """
    cluster0 = list(clusters[0])
    cluster1 = list(clusters[1])

    if not cluster0 or not cluster1:
        raise AgentSplitError(
            f"both clusters must be non-empty; got sizes {len(cluster0)}, {len(cluster1)}"
        )

    parent_members = set(store.skill_members(parent_skill_id))
    proposed = set(cluster0) | set(cluster1)
    overlap = set(cluster0) & set(cluster1)
    if overlap:
        raise AgentSplitError(
            f"clusters overlap on insight ids {sorted(overlap)} — not a valid partition"
        )
    if proposed != parent_members:
        missing = parent_members - proposed
        extra = proposed - parent_members
        raise AgentSplitError(
            f"clusters do not partition the parent's members (missing={sorted(missing)},"
            f" extra={sorted(extra)})"
        )

    # Resolve the parent's agent_id so the children inherit the same owner.
    parent_row = store.conn.execute(
        "SELECT agent_id, name FROM skills WHERE id = ?", (parent_skill_id,)
    ).fetchone()
    if parent_row is None:
        raise AgentSplitError(f"skill {parent_skill_id} does not exist")
    agent_id = parent_row["agent_id"]
    parent_name = parent_row["name"]

    label = detail or f"split {parent_skill_id}"
    with store.queue_operation("agent_split", label) as snapshot_id:
        # Stamp the parent with the split snapshot (retired-anchor marker).
        store.conn.execute(
            "UPDATE skills SET split_snapshot_id = ? WHERE id = ?",
            (snapshot_id, parent_skill_id),
        )
        # Create the two child modules.
        child0_id = store.conn.execute(
            "INSERT INTO skills (agent_id, name, description, parent_skill_id,"
            " split_snapshot_id) VALUES (?, ?, '', ?, ?)",
            (agent_id, f"{parent_name}-0", parent_skill_id, snapshot_id),
        ).lastrowid
        child1_id = store.conn.execute(
            "INSERT INTO skills (agent_id, name, description, parent_skill_id,"
            " split_snapshot_id) VALUES (?, ?, '', ?, ?)",
            (agent_id, f"{parent_name}-1", parent_skill_id, snapshot_id),
        ).lastrowid
        # Re-point the parent's members to the children (delete from parent, insert
        # into children in id-ascending order for determinism).
        store.conn.execute(
            "DELETE FROM skill_members WHERE skill_id = ?", (parent_skill_id,)
        )
        for pos, iid in enumerate(sorted(cluster0), start=1):
            store.conn.execute(
                "INSERT INTO skill_members (skill_id, insight_id, position)"
                " VALUES (?, ?, ?)",
                (child0_id, iid, pos),
            )
        for pos, iid in enumerate(sorted(cluster1), start=1):
            store.conn.execute(
                "INSERT INTO skill_members (skill_id, insight_id, position)"
                " VALUES (?, ?, ?)",
                (child1_id, iid, pos),
            )

    return SplitResult(
        parent_skill_id=parent_skill_id,
        child_skill_ids=(child0_id, child1_id),
        snapshot_id=snapshot_id,
    )


def revert_split(store: Store, split: SplitResult) -> None:
    """Inverse of :func:`execute_split`: restore members to the parent, delete children.

    Mints a *forward* snapshot (not an undo — the snapshot chain is append-only).
    Raises :class:`AgentSplitError` if the split has already been finalized.
    """
    if split.finalized:
        raise AgentSplitError(
            "cannot revert a finalized split (the parent is a frozen retired-by-split"
            " anchor — finalization is permanent)"
        )
    child0, child1 = split.child_skill_ids
    label = f"revert split of {split.parent_skill_id}"
    with store.queue_operation("agent_split_revert", label):
        # Collect all member ids from the children (id-ascending for determinism).
        rows0 = store.conn.execute(
            "SELECT insight_id FROM skill_members WHERE skill_id = ?"
            " ORDER BY insight_id ASC",
            (child0,),
        ).fetchall()
        rows1 = store.conn.execute(
            "SELECT insight_id FROM skill_members WHERE skill_id = ?"
            " ORDER BY insight_id ASC",
            (child1,),
        ).fetchall()
        all_ids = sorted(
            [r["insight_id"] for r in rows0] + [r["insight_id"] for r in rows1]
        )
        # Remove the children.
        store.conn.execute(
            "DELETE FROM skill_members WHERE skill_id IN (?, ?)", (child0, child1)
        )
        store.conn.execute(
            "DELETE FROM skills WHERE id IN (?, ?)", (child0, child1)
        )
        # Restore the parent: clear its split_snapshot_id anchor marker and re-add members.
        store.conn.execute(
            "UPDATE skills SET split_snapshot_id = NULL WHERE id = ?",
            (split.parent_skill_id,),
        )
        for pos, iid in enumerate(all_ids, start=1):
            store.conn.execute(
                "INSERT INTO skill_members (skill_id, insight_id, position)"
                " VALUES (?, ?, ?)",
                (split.parent_skill_id, iid, pos),
            )


@dataclass
class FinalizedSplit:
    """Returned by :func:`finalize_split`: the split is permanently committed."""

    parent_skill_id: int
    child_skill_ids: tuple[int, int]
    snapshot_id: int
    finalized: bool = True


def finalize_split(store: Store, split: SplitResult) -> FinalizedSplit:
    """Stamp the parent a frozen retired-by-split anchor; block further reverts.

    The split's ``split_snapshot_id`` on the parent already records the lineage; this
    call marks ``split.finalized = True`` on the handle and returns a :class:`FinalizedSplit`
    to make the committed state explicit. No additional DB write is required (the anchor
    is already in place from :func:`execute_split`). Raises if already finalized.
    """
    if split.finalized:
        raise AgentSplitError(
            f"split of skill {split.parent_skill_id} is already finalized"
        )
    split.finalized = True
    return FinalizedSplit(
        parent_skill_id=split.parent_skill_id,
        child_skill_ids=split.child_skill_ids,
        snapshot_id=split.snapshot_id,
    )
