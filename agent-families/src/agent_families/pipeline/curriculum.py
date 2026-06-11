"""Epochs, target rotation, and the training-pool curriculum (plan-005 U1, R2).

Plan 4 closed the learning loop on one training target (linkding) with one
held-out micro-benchmark (Kanboard). Plan 5 turns that into a *curriculum*: the
training pool rotates per the DESIGN §11 schedule, an ``epoch`` is one rotation
through the pool, and a fixed held-out suite (``grading/suite.py``) measures
generalization across epochs. This module owns the bookkeeping that the suite
runner and episode scheduler read:

- **The held-out constraint (R2).** Three sets partition every target name:
  the **training pool** (trained on — linkding plus §11 rotation-pool candidates
  that pass the docker-boot qualification), the **held-out micro-benchmark**
  (Kanboard — Plan 4's instrument), and the **held-out generalization suite**
  (different instances of the training archetypes — never trained on). A held-out
  name is admitted to the training pool **only** through a documented migration
  (:func:`migrate_held_out_to_training`) — never a config flip
  (:func:`build_training_pool` refuses it outright). This is the structural guard
  that keeps the generalization measurement honest: a target the library was
  shaped on cannot also score it.

- **Docker-boot qualification (DESIGN §11).** A rotation-pool candidate joins the
  training pool only if it boots via docker-compose with seed data in under
  ~2 minutes (*re*-boot time), lands a 15–60-entry feature registry, and its
  license permits local use. The rule is a pure check (:func:`qualifies`) over a
  per-candidate :class:`QualificationRecord`; the live measurement is the
  docker-required half (pending-docker), the math is offline.

- **Deterministic rotation (R2).** ``rotation_order`` is a hash-keyed permutation
  of the pool per ``(epoch, seed)`` — reproducible across processes and platforms
  (no PRNG state, the KTD byte-stability discipline) and rotating with the epoch
  so successive epochs revisit targets in a different sequence. Determinism is
  what lets a parallel campaign replay identically (the §15 invariant).

Epoch state lives in the store's ``meta`` table (the established no-new-DDL
precedent — the ``epoch`` *column* on episodes is written by the scheduler when
it mints an episode; this counter is the campaign-level cursor). Thresholds
(``every_n`` suite cadence, the qualification bounds) are caller-supplied — the
``thresholds.toml`` routing is run-assembly's job; nothing here hardcodes a
tunable beyond the DESIGN §11 documented defaults, which carry provenance.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass

from agent_families.store import Store


class CurriculumError(Exception):
    """A curriculum invariant was violated (held-out leak, bad rotation)."""


# --- the target partition (DESIGN §11 opening sequence + held-out set) --------

# linkding is the machinery-shakedown training target (DESIGN §11 #1); the pool
# grows as rotation candidates pass the docker-boot check.
SEED_TRAINING_POOL: tuple[str, ...] = ("linkding",)

# The held-out generalization suite (grading/suite.py): one instance per training
# archetype, a DIFFERENT instance than anything trained on — so a score here is
# generalization, not novelty shock (DESIGN §11). NEVER trained on.
SUITE_TARGET_NAMES: tuple[str, ...] = ("shaarli", "privatebin", "dokuwiki")

# Plan 4's held-out micro-benchmark. Stays held-out (R2); may promote to the
# training pool ONLY via the documented migration below.
HELD_OUT_MICRO_BENCHMARK = "kanboard"

# The full held-out set: nothing in here may enter the training pool by config.
HELD_OUT: frozenset[str] = frozenset(SUITE_TARGET_NAMES) | {HELD_OUT_MICRO_BENCHMARK}

# §11 rotation-pool candidates admitted to TRAINING after the docker-boot check
# (distinct instances from the held-out suite, so admitting them never collapses
# the held-out boundary). The list is the candidate menu, not the active pool —
# admission is per-candidate via the qualification rule.
POOL_CANDIDATE_NAMES: tuple[str, ...] = (
    "mealie",
    "tandoor",
    "monica",
    "invoiceshelf",
)


# --- docker-boot qualification (DESIGN §11) -----------------------------------


@dataclass(frozen=True)
class QualificationRule:
    """The DESIGN §11 docker-boot qualification rule. Defaults carry the §11
    provenance; a caller may tighten them from ``thresholds.toml``."""

    # PROVENANCE: DESIGN §11 — "boots via docker-compose with seed data in under
    # ~2 minutes (*re*-boot time; first-boot setup amortizes into the cache)".
    max_reboot_seconds: float = 120.0
    # PROVENANCE: DESIGN §11 — "feature registry lands at 15-60 entries".
    min_registry: int = 15
    max_registry: int = 60

    def __post_init__(self) -> None:
        if self.max_reboot_seconds <= 0:
            raise CurriculumError(
                f"max_reboot_seconds must be positive, got {self.max_reboot_seconds}"
            )
        if not 1 <= self.min_registry <= self.max_registry:
            raise CurriculumError(
                "registry bounds must satisfy 1 <= min <= max, got"
                f" [{self.min_registry}, {self.max_registry}]"
            )


@dataclass(frozen=True)
class QualificationRecord:
    """One candidate's measured docker-boot qualification evidence. The reboot
    time and registry size are the docker-required measurements (pending-docker);
    license review is a human attestation."""

    target: str
    reboot_seconds: float
    registry_size: int
    license_permits_local_use: bool


def qualification_reasons(
    record: QualificationRecord, rule: QualificationRule = QualificationRule()
) -> list[str]:
    """Every reason the candidate fails the rule (empty == qualifies)."""
    reasons: list[str] = []
    if record.reboot_seconds > rule.max_reboot_seconds:
        reasons.append(
            f"reboot {record.reboot_seconds:.1f}s exceeds"
            f" {rule.max_reboot_seconds:.1f}s"
        )
    if not rule.min_registry <= record.registry_size <= rule.max_registry:
        reasons.append(
            f"registry size {record.registry_size} outside"
            f" [{rule.min_registry}, {rule.max_registry}]"
        )
    if not record.license_permits_local_use:
        reasons.append("license does not permit local use")
    return reasons


def qualifies(
    record: QualificationRecord, rule: QualificationRule = QualificationRule()
) -> bool:
    """True iff the candidate passes the §11 docker-boot qualification rule."""
    return not qualification_reasons(record, rule)


# --- training-pool construction (R2 held-out constraint) ----------------------


def build_training_pool(qualified: Sequence[str]) -> tuple[str, ...]:
    """The active training pool: the seed pool plus qualified candidates (R2).

    Refuses to admit any held-out name (the suite targets or Kanboard) — that is
    a documented apparatus migration, never a config flip
    (:func:`migrate_held_out_to_training`). Order is seed-first then candidate
    order, deduplicated; rotation (:func:`rotation_order`) reorders per epoch.
    """
    pool = list(SEED_TRAINING_POOL)
    for name in qualified:
        if name in HELD_OUT:
            raise CurriculumError(
                f"refusing to admit held-out target {name!r} to the training"
                " pool by config (R2): a held-out instance may enter training"
                " ONLY through a documented migration that retires its frozen"
                " slice and SPC history — never a config flip."
            )
        if name not in pool:
            pool.append(name)
    return tuple(pool)


def migrate_held_out_to_training(
    pool: Sequence[str],
    name: str,
    *,
    replacement_onboarded: bool,
    slice_retired: bool,
    spc_history_retired: bool,
) -> tuple[str, ...]:
    """Promote a held-out target into the training pool — the R2 migration.

    Kanboard (and any held-out instance) may promote **only after** a replacement
    held-out target is fully onboarded AND its frozen slice + accumulated SPC
    history are formally retired. All three must be attested; otherwise this
    refuses — the documented-migration guard, not a config flip.
    """
    if name not in HELD_OUT:
        raise CurriculumError(
            f"{name!r} is not a held-out target; use build_training_pool"
        )
    missing = [
        label
        for label, ok in (
            ("a replacement held-out target onboarded", replacement_onboarded),
            ("the frozen slice retired", slice_retired),
            ("the accumulated SPC history retired", spc_history_retired),
        )
        if not ok
    ]
    if missing:
        raise CurriculumError(
            f"refusing to migrate held-out {name!r} into the training pool (R2):"
            f" preconditions not met — needs {', '.join(missing)}."
        )
    result = list(pool)
    if name not in result:
        result.append(name)
    return tuple(result)


# --- deterministic rotation (R2) ----------------------------------------------


def rotation_order(
    pool: Sequence[str], epoch: int, seed: int
) -> tuple[str, ...]:
    """A deterministic per-``(epoch, seed)`` permutation of the pool (R2).

    Hash-keyed (sha256 over ``"{seed}:{epoch}:{target}"``) so it is reproducible
    across processes and platforms with no PRNG state (the KTD byte-stability
    rule), and rotates with the epoch — successive epochs revisit the pool in a
    different sequence. Always a permutation of the input (same multiset).
    """
    if len(set(pool)) != len(pool):
        raise CurriculumError(f"training pool has duplicate targets: {list(pool)}")
    return tuple(
        sorted(
            pool,
            key=lambda t: hashlib.sha256(
                f"{int(seed)}:{int(epoch)}:{t}".encode("utf-8")
            ).hexdigest(),
        )
    )


# --- epoch bookkeeping (store meta cursor) ------------------------------------

EPOCH_KEY = "curriculum:epoch"


def current_epoch(store: Store) -> int:
    """The campaign's current epoch cursor (0 before the first rotation)."""
    raw = store.get_meta(EPOCH_KEY)
    return int(raw) if raw is not None else 0


def advance_epoch(store: Store) -> int:
    """Advance to the next epoch; returns the new epoch number."""
    nxt = current_epoch(store) + 1
    store.set_meta(EPOCH_KEY, str(nxt))
    return nxt


def suite_due(episodes_completed: int, every_n: int) -> bool:
    """Whether a full held-out suite run is due (R1: "every N episodes").

    True at each Nth completed episode (and never at zero). ``every_n`` is a
    caller-supplied cadence tunable.
    """
    if every_n < 1:
        raise CurriculumError(f"suite cadence every_n must be >= 1, got {every_n}")
    return episodes_completed > 0 and episodes_completed % every_n == 0
