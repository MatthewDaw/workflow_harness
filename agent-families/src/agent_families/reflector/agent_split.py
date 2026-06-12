"""Partition-move engine: apply / revert / finalize a module split (plan-009 U7, R15).

R3 reform (plan-009) **demotes the R2 organization machinery**. This module used to
carry the plan-005 family-split engine — a decision-count gate, base-prompt residue
check, k-means/silhouette clustering over skill descriptions, a compressibility gate,
routing replay, and the full ``perform_agent_split`` transaction. All of that is
**deleted**: group structure is now *derived* (see :mod:`agent_families.reflector.derive`),
not authored by clustering an agent's skills, and routing has been replaced by
whole-store insight-level retrieval (:mod:`agent_families.library.retrieval`).

What survives is the **transactional partition-move engine** — ``execute_split`` /
``revert_split`` / ``finalize_split`` — re-pointed from agent ownership onto **module
(``skills``) membership**, and from a plain ``transaction`` onto a **snapshot-minting
``queue_operation``** (R15). A "module" is a ``skills`` row; its membership is
``skill_members``. A move re-points members between modules and mints a snapshot, so
the snapshot-keyed rendering cache invalidates correctly (the R8 / R17 invariant:
every membership rebalance mints a snapshot).

The engine is a revertible three-step transaction:

1. **execute** — create two child modules (``parent_skill_id`` set,
   ``split_snapshot_id`` = the minted snapshot), distribute the parent's members
   between them, and empty the parent (it stays as a lineage anchor — never deleted).
   Mints exactly one snapshot.
2. **revert** — the inverse: move every child's members back onto the parent and
   delete the child modules. Mints its own (forward) snapshot. Refused once the split
   is finalized.
3. **finalize** — seal the split: stamp the parent's ``split_snapshot_id`` (it becomes
   a frozen, retired-by-split anchor). After this the split is no longer revertible.

The derive pass (plan-009 U4) applies whole partitions through its own
``_apply_partition`` seam; this engine is the re-pointed per-move primitive R8/R15
specify. Offline by construction — pure DB writes through the store, no LLM seam, no
clustering. Zero quota, no ``claude`` on PATH.

## Conformance (plan-009 U7 R15 — tests in tests/test_demotions.py)

- the module exports ONLY the partition-move engine; the R2 machinery
  (``replay_agreement``, ``perform_agent_split``, ``evaluate_split_candidacy``,
  ``check_base_prompt_residue``) is gone (importing it raises ``AttributeError``):
  ``test_agent_split_exposes_only_partition_engine``
- ``execute_split`` re-points module membership and mints exactly one snapshot:
  ``test_execute_split_repoints_membership_and_mints_one_snapshot``
- ``revert_split`` restores the parent's membership and deletes the children:
  ``test_revert_split_restores_parent_membership``
- ``finalize_split`` freezes the parent and blocks revert:
  ``test_finalize_split_freezes_parent_and_blocks_revert``
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from agent_families.store import Store


class AgentSplitError(Exception):
    """A broken partition-move precondition with an actionable message."""


@dataclass(frozen=True)
class ModuleSplit:
    """One executed module split's provenance — revertible until finalized (R15).

    ``parent_skill_id`` is the emptied lineage anchor; ``child_skill_ids`` are the two
    new modules its members moved into; ``snapshot_id`` is the snapshot the execute
    minted. ``finalized`` flips once :func:`finalize_split` seals the move.
    """

    parent_skill_id: int
    agent_id: int
    child_skill_ids: tuple[int, int]
    child_member_ids: tuple[tuple[int, ...], tuple[int, ...]]
    snapshot_id: int
    finalized: bool = False


def _parent_row(store: Store, parent_skill_id: int):
    row = store.conn.execute(
        "SELECT id, agent_id, split_snapshot_id FROM skills WHERE id = ?",
        (parent_skill_id,),
    ).fetchone()
    if row is None:
        raise AgentSplitError(f"module (skill) {parent_skill_id} does not exist")
    return row


def _has_children(store: Store, parent_skill_id: int) -> bool:
    row = store.conn.execute(
        "SELECT 1 FROM skills WHERE parent_skill_id = ? LIMIT 1", (parent_skill_id,)
    ).fetchone()
    return row is not None


def execute_split(
    store: Store,
    parent_skill_id: int,
    member_clusters: tuple[Sequence[int], Sequence[int]],
    *,
    child_names: tuple[str | None, str | None] = (None, None),
    detail: str = "",
) -> ModuleSplit:
    """Split a module's membership into two child modules under one minted snapshot.

    ``member_clusters`` is the two-way partition of the parent's current members; the
    union must equal the parent's membership and the two clusters must be disjoint and
    non-empty (a faithful split, not a drop or a duplication). Each child is created
    with ``parent_skill_id`` = the parent and ``split_snapshot_id`` = the minted
    snapshot; the parent is emptied (a frozen lineage anchor, never deleted). Mints
    exactly one snapshot via :meth:`Store.queue_operation` so the snapshot-keyed
    rendering cache invalidates (R8 / R17). Revertible until :func:`finalize_split`.
    """
    parent = _parent_row(store, parent_skill_id)
    if parent["split_snapshot_id"] is not None or _has_children(store, parent_skill_id):
        raise AgentSplitError(
            f"module {parent_skill_id} is already split; cannot split it again"
        )
    cluster0 = list(member_clusters[0])
    cluster1 = list(member_clusters[1])
    if not cluster0 or not cluster1:
        raise AgentSplitError(
            "a module split needs two non-empty member clusters, got sizes"
            f" {len(cluster0)}/{len(cluster1)}"
        )
    s0, s1 = set(cluster0), set(cluster1)
    if s0 & s1:
        raise AgentSplitError(
            f"member clusters must be disjoint; overlap = {sorted(s0 & s1)}"
        )
    members = set(store.skill_members(parent_skill_id))
    if s0 | s1 != members:
        raise AgentSplitError(
            "member clusters must partition the parent's membership exactly"
            f" (parent has {sorted(members)}, clusters cover {sorted(s0 | s1)})"
        )
    agent_id = parent["agent_id"]
    (name0, name1) = child_names

    with store.queue_operation("module_split", detail) as snapshot_id:
        child0 = _create_child(store, agent_id, name0, parent_skill_id, snapshot_id)
        child1 = _create_child(store, agent_id, name1, parent_skill_id, snapshot_id)
        for iid in cluster0:
            store.append_member(child0, iid)
        for iid in cluster1:
            store.append_member(child1, iid)
        store.conn.execute(
            "DELETE FROM skill_members WHERE skill_id = ?", (parent_skill_id,)
        )

    return ModuleSplit(
        parent_skill_id=parent_skill_id,
        agent_id=agent_id,
        child_skill_ids=(child0, child1),
        child_member_ids=(tuple(cluster0), tuple(cluster1)),
        snapshot_id=snapshot_id,
    )


def _create_child(
    store: Store,
    agent_id: int,
    name: str | None,
    parent_skill_id: int,
    split_snapshot_id: int,
) -> int:
    cur = store.conn.execute(
        "INSERT INTO skills (agent_id, name, description, parent_skill_id,"
        " split_snapshot_id) VALUES (?, ?, '', ?, ?)",
        (agent_id, name, parent_skill_id, split_snapshot_id),
    )
    return cur.lastrowid


def revert_split(store: Store, split: ModuleSplit) -> None:
    """Undo a pending split: members back onto the parent, children deleted (R15).

    The inverse of :func:`execute_split` — members move back onto the parent module
    (child0's then child1's, in stored order) and the child modules are deleted. Mints
    its own forward snapshot (the snapshot chain is append-only; nothing is un-minted).
    Refused once the split is finalized — a finalized parent is a frozen anchor.
    """
    parent = _parent_row(store, split.parent_skill_id)
    if parent["split_snapshot_id"] is not None:
        raise AgentSplitError(
            f"module {split.parent_skill_id} is finalized (a frozen split anchor);"
            " a finalized split is not revertible (R15)"
        )
    with store.queue_operation("module_split_revert", f"revert split of {split.parent_skill_id}"):
        for child_id in split.child_skill_ids:
            for iid in store.skill_members(child_id):
                store.append_member(split.parent_skill_id, iid)
            store.conn.execute(
                "DELETE FROM skill_members WHERE skill_id = ?", (child_id,)
            )
            store.conn.execute("DELETE FROM skills WHERE id = ?", (child_id,))


def finalize_split(store: Store, split: ModuleSplit) -> ModuleSplit:
    """Seal a split: stamp the parent a frozen, retired-by-split anchor (R15).

    Mints a snapshot and sets the parent's ``split_snapshot_id`` — the marker that the
    module is a retired lineage anchor (it is already empty from
    :func:`execute_split`). After this the split is no longer revertible.
    """
    parent = _parent_row(store, split.parent_skill_id)
    if parent["split_snapshot_id"] is not None:
        raise AgentSplitError(
            f"module {split.parent_skill_id} is already finalized"
        )
    with store.queue_operation(
        "module_split_finalize", f"finalize split of {split.parent_skill_id}"
    ) as snapshot_id:
        store.conn.execute(
            "UPDATE skills SET split_snapshot_id = ? WHERE id = ?",
            (snapshot_id, split.parent_skill_id),
        )
    return ModuleSplit(
        parent_skill_id=split.parent_skill_id,
        agent_id=split.agent_id,
        child_skill_ids=split.child_skill_ids,
        child_member_ids=split.child_member_ids,
        snapshot_id=split.snapshot_id,
        finalized=True,
    )
