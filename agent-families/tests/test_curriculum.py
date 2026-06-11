"""plan-005 U1: epochs, rotation, and the training-pool curriculum (R2).

Offline only (zero quota): the held-out constraint, the docker-boot qualification
math, deterministic rotation, and the store-backed epoch cursor. The live
docker-boot *measurement* that feeds a QualificationRecord is pending-docker.

## Conformance

- rotation order deterministic per epoch seed:
  ``test_rotation_order_is_deterministic_per_epoch_seed``,
  ``test_rotation_order_rotates_across_epochs``
- held-out constraint (a held-out name never enters the training pool by config):
  ``test_build_training_pool_refuses_held_out``,
  ``test_migrate_held_out_requires_all_preconditions``
- docker-boot qualification rule (DESIGN §11): ``test_qualifies``,
  ``test_qualification_reasons_enumerates_failures``
- epoch bookkeeping + suite cadence: ``test_epoch_cursor``, ``test_suite_due``
- the target partition is disjoint: ``test_held_out_partition_is_disjoint``
"""

from __future__ import annotations

import pytest

from agent_families.pipeline import curriculum as cur
from agent_families.pipeline.curriculum import (
    CurriculumError,
    QualificationRecord,
    QualificationRule,
    advance_epoch,
    build_training_pool,
    current_epoch,
    migrate_held_out_to_training,
    qualification_reasons,
    qualifies,
    rotation_order,
    suite_due,
)
from agent_families.store import Store


# --- the target partition (R2) ------------------------------------------------


def test_held_out_partition_is_disjoint():
    seed = set(cur.SEED_TRAINING_POOL)
    suite = set(cur.SUITE_TARGET_NAMES)
    micro = {cur.HELD_OUT_MICRO_BENCHMARK}
    candidates = set(cur.POOL_CANDIDATE_NAMES)
    # training (seed + candidates) and held-out (suite + micro) never overlap
    assert seed.isdisjoint(suite)
    assert seed.isdisjoint(micro)
    assert candidates.isdisjoint(cur.HELD_OUT)
    assert suite.isdisjoint(micro)
    assert cur.HELD_OUT == suite | micro


# --- docker-boot qualification (DESIGN §11) -----------------------------------


def _record(**kw) -> QualificationRecord:
    defaults = dict(
        target="mealie",
        reboot_seconds=45.0,
        registry_size=30,
        license_permits_local_use=True,
    )
    return QualificationRecord(**{**defaults, **kw})


def test_qualifies():
    assert qualifies(_record())
    # the §11 boundary values qualify
    assert qualifies(_record(reboot_seconds=120.0, registry_size=15))
    assert qualifies(_record(registry_size=60))


def test_qualification_reasons_enumerates_failures():
    slow = _record(reboot_seconds=200.0)
    assert not qualifies(slow)
    assert any("reboot" in r for r in qualification_reasons(slow))

    small = _record(registry_size=5)
    assert not qualifies(small)
    assert any("registry" in r for r in qualification_reasons(small))

    big = _record(registry_size=120)
    assert not qualifies(big)

    unlicensed = _record(license_permits_local_use=False)
    assert not qualifies(unlicensed)
    assert any("license" in r for r in qualification_reasons(unlicensed))


def test_qualification_rule_validates():
    with pytest.raises(CurriculumError):
        QualificationRule(max_reboot_seconds=0)
    with pytest.raises(CurriculumError):
        QualificationRule(min_registry=60, max_registry=15)


# --- training-pool construction + held-out constraint (R2) --------------------


def test_build_training_pool_admits_qualified_candidates():
    pool = build_training_pool(["mealie", "tandoor"])
    assert pool[0] == "linkding"  # seed first
    assert set(pool) == {"linkding", "mealie", "tandoor"}
    # idempotent dedup
    assert build_training_pool(["linkding", "mealie", "mealie"]) == (
        "linkding",
        "mealie",
    )


def test_build_training_pool_refuses_held_out():
    # Kanboard (the held-out micro-benchmark) cannot enter training by config (R2)
    with pytest.raises(CurriculumError, match="documented migration"):
        build_training_pool(["kanboard"])
    # nor a held-out suite instance
    with pytest.raises(CurriculumError, match="held-out"):
        build_training_pool(["shaarli"])


def test_migrate_held_out_requires_all_preconditions():
    base = ("linkding",)
    # any missing precondition refuses
    for kw in (
        dict(replacement_onboarded=False, slice_retired=True, spc_history_retired=True),
        dict(replacement_onboarded=True, slice_retired=False, spc_history_retired=True),
        dict(replacement_onboarded=True, slice_retired=True, spc_history_retired=False),
    ):
        with pytest.raises(CurriculumError, match="preconditions not met"):
            migrate_held_out_to_training(base, "kanboard", **kw)
    # all three attested -> promotes
    promoted = migrate_held_out_to_training(
        base,
        "kanboard",
        replacement_onboarded=True,
        slice_retired=True,
        spc_history_retired=True,
    )
    assert "kanboard" in promoted and "linkding" in promoted


def test_migrate_rejects_non_held_out():
    with pytest.raises(CurriculumError, match="not a held-out"):
        migrate_held_out_to_training(
            ("linkding",),
            "mealie",
            replacement_onboarded=True,
            slice_retired=True,
            spc_history_retired=True,
        )


# --- deterministic rotation (R2) ----------------------------------------------

_POOL = ("linkding", "mealie", "tandoor", "monica", "invoiceshelf")


def test_rotation_order_is_deterministic_per_epoch_seed():
    a = rotation_order(_POOL, epoch=2, seed=7)
    b = rotation_order(_POOL, epoch=2, seed=7)
    assert a == b, "same (epoch, seed) is reproducible"
    # it is a permutation of the pool (same multiset, no drops/dups)
    assert sorted(a) == sorted(_POOL)
    assert len(set(a)) == len(_POOL)


def test_rotation_order_rotates_across_epochs():
    seed = 7
    orders = {rotation_order(_POOL, epoch=e, seed=seed) for e in range(8)}
    # the schedule actually rotates: not every epoch yields the same sequence
    assert len(orders) > 1
    # a different seed gives a different schedule family
    assert rotation_order(_POOL, epoch=0, seed=7) != rotation_order(
        _POOL, epoch=0, seed=99
    )


def test_rotation_order_rejects_duplicate_pool():
    with pytest.raises(CurriculumError, match="duplicate"):
        rotation_order(("linkding", "linkding"), epoch=0, seed=0)


# --- epoch bookkeeping --------------------------------------------------------


def test_epoch_cursor(tmp_path):
    store = Store(tmp_path / "lib.db")
    store.migrate()
    assert current_epoch(store) == 0  # before the first rotation
    assert advance_epoch(store) == 1
    assert advance_epoch(store) == 2
    assert current_epoch(store) == 2
    store.close()


def test_suite_due():
    # R1: full suite "every N episodes" — at each Nth, never at zero
    assert not suite_due(0, 5)
    assert not suite_due(3, 5)
    assert suite_due(5, 5)
    assert suite_due(10, 5)
    assert not suite_due(11, 5)
    with pytest.raises(CurriculumError):
        suite_due(5, 0)
