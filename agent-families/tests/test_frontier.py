"""plan-003 U3: the frontier ledger (R10) — slice selection, investigation
tracking, the mention-coverage audit.

Fully offline: rows are minted through the registry's mint path with synthetic
evidence; no docker, no quota.

## Conformance

Unit test scenario (plan-003 U3) -> tests:

- frontier ordering deterministic -> ``test_frontier_ordering_deterministic``
  (exact expected order; same state built in a different insertion order in a
  second store yields the identical slice; repeated calls identical)

R10 mechanisms -> tests:

- least-investigated first, slice size from config (caller-routed)
  -> ``test_frontier_ordering_deterministic``,
  ``test_select_slice_size_validated_and_honored``
- mention-coverage guarantee: never-mentioned FEATs force-scheduled ahead of
  the normal ordering, selectable even when 'explored', audit idempotent
  -> ``test_mention_coverage_audit_force_schedules_unmentioned``,
  ``test_force_scheduled_explored_feat_is_still_selectable``
- exploration status tracking -> ``test_record_investigation_bumps_and_flips``
- frontier drops deprecated features / never resurrects them
  -> ``test_deprecated_feats_never_selectable_or_reseeded``
- episode terminal: exhaustion exactly when no slice remains
  -> ``test_frontier_exhausted_mirrors_select_slice``
- seeding idempotent -> ``test_seed_frontier_idempotent``
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_families.grading.frontier import (
    FrontierError,
    frontier_exhausted,
    frontier_row,
    mention_coverage_audit,
    record_investigation,
    seed_frontier,
    select_slice,
)
from agent_families.grading.registry import FeatureCandidate, deprecate_feat, mint_feat
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "ab" * 32


def make_store(tmp_path: Path, name: str = "library.db") -> Store:
    store = Store(tmp_path / name)
    store.migrate()
    return store


def cand(key: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="bookmarks/urls.py",
        confirm_steps=({"action": "goto", "selector": "", "args": {}},),
        scenario_steps=(f"Exercise {key}",),
        expected_outcome="visible",
        tier="must",
    )


def mint(store: Store, key: str, **kw) -> str:
    return mint_feat(
        store, cand(key), f"ev/{key}.json", target=TARGET, digest=DIGEST, **kw
    )


def add_mention(store: Store, fid: str, msg_id: str = "MSG-1") -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO trace_msg (id, content) VALUES (?, '')", (msg_id,)
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)",
        (msg_id, fid),
    )


# --- scenario: frontier ordering deterministic -----------------------------------


def test_frontier_ordering_deterministic(tmp_path):
    def build(store: Store, keys: list[str]) -> None:
        for key in keys:
            mint(store, key)
        # investigation history: search-basic twice, bookmark-edit once
        record_investigation(store, ["FEAT-search-basic"], status="partially-explored")
        record_investigation(store, ["FEAT-search-basic"], status="partially-explored")
        record_investigation(store, ["FEAT-bookmark-edit"], status="partially-explored")
        # tag-filter force-scheduled by the coverage mechanism
        store.conn.execute(
            "UPDATE frontier SET force_scheduled = 1 WHERE feat_id = 'FEAT-tag-filter'"
        )

    keys = ["bookmark-create", "bookmark-edit", "search-basic", "tag-filter"]
    store_a = make_store(tmp_path, "a.db")
    build(store_a, keys)
    expected = [
        "FEAT-tag-filter",       # force-scheduled jumps the ordering
        "FEAT-bookmark-create",  # 0 investigations, id tiebreak
        "FEAT-bookmark-edit",    # 1 investigation
        "FEAT-search-basic",     # 2 investigations
    ]
    assert select_slice(store_a, TARGET, 10) == expected
    assert select_slice(store_a, TARGET, 10) == expected, "repeat-call stable"

    # the same state built in a different insertion order selects identically
    store_b = make_store(tmp_path, "b.db")
    build(store_b, list(reversed(keys)))
    assert select_slice(store_b, TARGET, 10) == expected


def test_select_slice_size_validated_and_honored(tmp_path):
    store = make_store(tmp_path)
    for key in ("bookmark-create", "bookmark-edit", "tag-filter"):
        mint(store, key)
    assert select_slice(store, TARGET, 2) == [
        "FEAT-bookmark-create", "FEAT-bookmark-edit",
    ]
    # slice size is config-routed by the orchestrator — never defaulted here
    for bad in (0, -3, True, "5", None):
        with pytest.raises(FrontierError, match="positive integer"):
            select_slice(store, TARGET, bad)


# --- investigation tracking --------------------------------------------------------


def test_record_investigation_bumps_and_flips(tmp_path):
    store = make_store(tmp_path)
    mint(store, "tag-filter")
    store.conn.execute(
        "UPDATE frontier SET force_scheduled = 1 WHERE feat_id = 'FEAT-tag-filter'"
    )

    record_investigation(store, ["FEAT-tag-filter"], status="partially-explored")
    row = frontier_row(store, "FEAT-tag-filter")
    assert row["investigation_count"] == 1
    assert row["status"] == "partially-explored"
    assert row["force_scheduled"] == 0, "a scheduled slot consumes the flag"

    record_investigation(store, ["FEAT-tag-filter"], status="explored")
    row = frontier_row(store, "FEAT-tag-filter")
    assert row["investigation_count"] == 2
    assert row["status"] == "explored"

    with pytest.raises(FrontierError, match="investigation status"):
        record_investigation(store, ["FEAT-tag-filter"], status="unexplored")
    with pytest.raises(FrontierError, match="no frontier row"):
        record_investigation(store, ["FEAT-never-was"], status="explored")
    # the failed batch rolled back atomically
    assert frontier_row(store, "FEAT-tag-filter")["investigation_count"] == 2


def test_newly_discovered_rows_enter_the_ordering(tmp_path):
    store = make_store(tmp_path)
    mint(store, "bookmark-create")
    record_investigation(store, ["FEAT-bookmark-create"], status="explored")
    mint(store, "bulk-edit", frontier_status="newly-discovered")
    assert select_slice(store, TARGET, 10) == ["FEAT-bulk-edit"]


# --- mention-coverage audit (R10's guarantee) ---------------------------------------


def test_mention_coverage_audit_force_schedules_unmentioned(tmp_path):
    store = make_store(tmp_path)
    for key in ("bookmark-create", "bookmark-edit", "tag-filter"):
        mint(store, key)
    add_mention(store, "FEAT-bookmark-create")

    flagged = mention_coverage_audit(store, TARGET)
    assert flagged == ["FEAT-bookmark-edit", "FEAT-tag-filter"]
    assert frontier_row(store, "FEAT-bookmark-create")["force_scheduled"] == 0
    # the flagged rows jump ahead of the normal ordering
    assert select_slice(store, TARGET, 10)[:2] == flagged
    # idempotent: pure recompute from mention state
    assert mention_coverage_audit(store, TARGET) == flagged

    # once mentioned, the next audit clears the flag mechanically
    add_mention(store, "FEAT-tag-filter", msg_id="MSG-2")
    assert mention_coverage_audit(store, TARGET) == ["FEAT-bookmark-edit"]
    assert frontier_row(store, "FEAT-tag-filter")["force_scheduled"] == 0


def test_force_scheduled_explored_feat_is_still_selectable(tmp_path):
    """Coverage converges by mechanism: explored-but-never-mentioned FEATs
    still get their forced slot in the next opening slice."""
    store = make_store(tmp_path)
    mint(store, "bookmark-create")
    mint(store, "tag-filter")
    record_investigation(store, ["FEAT-tag-filter"], status="explored")
    add_mention(store, "FEAT-bookmark-create")

    assert mention_coverage_audit(store, TARGET) == ["FEAT-tag-filter"]
    assert select_slice(store, TARGET, 10)[0] == "FEAT-tag-filter"
    assert not frontier_exhausted(store, TARGET)


# --- seeding, deprecation, exhaustion -------------------------------------------------


def test_seed_frontier_idempotent(tmp_path):
    store = make_store(tmp_path)
    mint(store, "bookmark-create")
    # a row minted elsewhere then dropped (e.g. restored database) re-seeds
    store.conn.execute("DELETE FROM frontier WHERE feat_id = 'FEAT-bookmark-create'")
    assert seed_frontier(store, TARGET) == ["FEAT-bookmark-create"]
    assert seed_frontier(store, TARGET) == [], "already-seeded rows untouched"
    assert frontier_row(store, "FEAT-bookmark-create")["status"] == "unexplored"


def test_deprecated_feats_never_selectable_or_reseeded(tmp_path):
    store = make_store(tmp_path)
    mint(store, "bookmark-create")
    mint(store, "tag-filter")
    deprecate_feat(store, "FEAT-tag-filter")

    assert select_slice(store, TARGET, 10) == ["FEAT-bookmark-create"]
    assert seed_frontier(store, TARGET) == [], (
        "seeding never resurrects deprecated features"
    )


def test_frontier_exhausted_mirrors_select_slice(tmp_path):
    store = make_store(tmp_path)
    assert frontier_exhausted(store, TARGET), "an empty frontier is exhausted"
    mint(store, "bookmark-create")
    mint(store, "tag-filter")
    assert not frontier_exhausted(store, TARGET)

    record_investigation(
        store, ["FEAT-bookmark-create", "FEAT-tag-filter"], status="explored"
    )
    assert frontier_exhausted(store, TARGET)
    assert select_slice(store, TARGET, 10) == []

    # a force-scheduled row reopens the frontier (and the slice), by sharing
    # one selectability predicate
    store.conn.execute(
        "UPDATE frontier SET force_scheduled = 1 WHERE feat_id = 'FEAT-tag-filter'"
    )
    assert not frontier_exhausted(store, TARGET)
    assert select_slice(store, TARGET, 10) == ["FEAT-tag-filter"]
