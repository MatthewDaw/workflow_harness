"""plan-005 U1: epoch bookkeeping and the training-pool curriculum (R2).

Pure-function, store-light, fully offline (no quota, no Docker). The docker-boot
qualification check is injected as a scripted predicate.

## Conformance

Test-scenario / invariant (plan-005 U1) -> tests:

- rotation order deterministic per epoch seed:
  ``test_rotation_order_deterministic_per_epoch_seed``,
  ``test_rotation_order_varies_by_epoch``, ``test_rotation_order_varies_by_seed``,
  ``test_rotation_is_a_permutation``
- training pool = linkding + candidates passing the docker-boot check (R2):
  ``test_training_pool_admits_passing_candidates``,
  ``test_training_pool_rejects_failing_candidates``,
  ``test_training_pool_rejects_duplicate_names``
- held-out targets are never in the training pool (R1/R2):
  ``test_training_pool_excludes_a_held_out_candidate``,
  ``test_assert_disjoint_from_holdout``,
  ``test_default_candidates_are_disjoint_from_holdout``
- suite cadence (every N episodes):
  ``test_suite_due_cadence``, ``test_suite_due_rejects_nonpositive_index``
- epoch bookkeeping: ``test_current_epoch_reads_max_episode_epoch``
- params validation: ``test_curriculum_params_validates``
"""

from __future__ import annotations

import pytest

from agent_families.pipeline.curriculum import (
    LINKDING,
    POOL_CANDIDATES,
    CurriculumError,
    CurriculumParams,
    TargetSpec,
    assert_disjoint_from_holdout,
    current_epoch,
    rotation_order,
    suite_due,
    training_pool,
)
from agent_families.store import Store

_POOL = (
    LINKDING,
    TargetSpec("dokuwiki", "wiki"),
    TargetSpec("mealie", "recipes"),
    TargetSpec("privatebin", "pastebin"),
    TargetSpec("monica", "crm"),
)


# --- deterministic rotation (R2) ---------------------------------------------


def test_rotation_order_deterministic_per_epoch_seed():
    a = rotation_order(_POOL, epoch=3, seed=42)
    b = rotation_order(_POOL, epoch=3, seed=42)
    assert a == b, "same (epoch, seed) must yield the same order on any run"
    # order is stable regardless of the input pool's ordering
    shuffled = (_POOL[2], _POOL[0], _POOL[4], _POOL[1], _POOL[3])
    assert rotation_order(shuffled, epoch=3, seed=42) == a


def test_rotation_is_a_permutation():
    order = rotation_order(_POOL, epoch=1, seed=7)
    assert {t.name for t in order} == {t.name for t in _POOL}
    assert len(order) == len(_POOL)


def test_rotation_order_varies_by_epoch():
    orders = {
        tuple(t.name for t in rotation_order(_POOL, epoch=e, seed=42))
        for e in range(6)
    }
    assert len(orders) > 1, "the rotation must actually rotate across epochs"


def test_rotation_order_varies_by_seed():
    orders = {
        tuple(t.name for t in rotation_order(_POOL, epoch=0, seed=s))
        for s in range(6)
    }
    assert len(orders) > 1, "different seeds must give different rotations"


# --- training pool + docker-boot qualification (R2) --------------------------


def test_training_pool_admits_passing_candidates():
    candidates = (TargetSpec("aaa", "x"), TargetSpec("bbb", "y"))
    pool = training_pool(
        lambda t: t.name == "aaa", candidates=candidates, holdout=()
    )
    assert [t.name for t in pool] == ["linkding", "aaa"], (
        "linkding is admitted unconditionally; candidates only on a boot pass"
    )


def test_training_pool_rejects_failing_candidates():
    candidates = (TargetSpec("aaa", "x"),)
    pool = training_pool(lambda t: False, candidates=candidates, holdout=())
    assert [t.name for t in pool] == ["linkding"]


def test_training_pool_rejects_duplicate_names():
    candidates = (TargetSpec("linkding", "bookmarks"),)
    with pytest.raises(CurriculumError, match="duplicate"):
        training_pool(lambda t: True, candidates=candidates, holdout=())


def test_training_pool_excludes_a_held_out_candidate():
    # a candidate that passes the boot check but is held-out is a hard error
    candidates = (TargetSpec("kanboard", "kanban"),)
    with pytest.raises(CurriculumError, match="held-out"):
        training_pool(lambda t: True, candidates=candidates)


def test_assert_disjoint_from_holdout():
    assert_disjoint_from_holdout([LINKDING])  # linkding is training, fine
    with pytest.raises(CurriculumError, match="held-out"):
        assert_disjoint_from_holdout([TargetSpec("linkace", "bookmarks")])


def test_default_candidates_are_disjoint_from_holdout():
    # the named §11 rotation candidates never collide with the held-out suite
    pool = training_pool(lambda t: True)
    names = {t.name for t in pool}
    assert {"kanboard", "linkace"}.isdisjoint(names)
    assert {t.name for t in POOL_CANDIDATES} <= names


# --- suite cadence (R1) ------------------------------------------------------


def test_suite_due_cadence():
    params = CurriculumParams(suite_cadence=5)
    due = [i for i in range(1, 12) if suite_due(i, params)]
    assert due == [1, 6, 11], "the full suite runs on episode 1 and every 5th"


def test_suite_due_rejects_nonpositive_index():
    with pytest.raises(CurriculumError, match="1-based"):
        suite_due(0)


# --- epoch bookkeeping (R2) --------------------------------------------------


def test_current_epoch_reads_max_episode_epoch(tmp_path):
    store = Store(tmp_path / "lib.db")
    store.migrate()
    assert current_epoch(store) == 0, "no episodes -> epoch 0"
    store.create_episode("linkding", "sha256:" + "0" * 64, 0, epoch=0)
    store.create_episode("dokuwiki", "sha256:" + "1" * 64, 0, epoch=2)
    assert current_epoch(store) == 2
    store.close()


# --- params validation -------------------------------------------------------


def test_curriculum_params_validates():
    with pytest.raises(CurriculumError, match="suite_cadence"):
        CurriculumParams(suite_cadence=0)
    assert CurriculumParams().suite_cadence >= 1
