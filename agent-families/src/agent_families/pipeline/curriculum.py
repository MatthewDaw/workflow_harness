"""Epoch bookkeeping and the training-pool curriculum (plan-005 U1, R2).

An **epoch** is one rotation through the target curriculum (DESIGN §11). This
module owns the two scheduling decisions that make epochs real:

- **The training pool and its rotation.** The pool is linkding (the
  machinery-shakedown target, always admitted) plus the §11 rotation-pool
  candidates that pass the **docker-boot qualification check** (boots via
  docker-compose with seed data in under ~2 minutes, registry lands at 15–60
  entries, license permits local use). The pool **rotates deterministically per
  epoch** (a stable permutation keyed by ``(seed, epoch)``) so a campaign is
  reproducible and resumable. The qualification check is injectable — the live
  check boots the real stack (pending-docker, like every target's live path);
  offline fixtures script it.

- **The held-out exclusion.** Suite targets (``grading.suite``) are **never**
  in the training pool — a target trained on measures memorization, not
  generalization (R1/R2). :func:`training_pool` enforces the disjointness as a
  hard error, not a convention.

- **Suite cadence.** The full held-out suite runs every N episodes
  (:func:`suite_due`); ``N`` is caller-supplied with PROVENANCE (the
  ValidateParams precedent — no hidden config reads).

The control-chart recompute, scoring, and curve queries live in ``grading.suite``;
this module only decides *what runs when*. Pure functions throughout (the one
store touch, :func:`current_epoch`, is a read) so the curriculum is trivially
testable offline.
"""

from __future__ import annotations

import hashlib
import logging
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from agent_families.grading.suite import HELD_OUT_SUITE, SuiteTarget, held_out_names
from agent_families.store import Store

logger = logging.getLogger(__name__)


class CurriculumError(Exception):
    """A broken curriculum invariant with an actionable message (R2)."""


# --- target specs -------------------------------------------------------------


@dataclass(frozen=True)
class TargetSpec:
    """One training-curriculum target: its name and its archetype."""

    name: str
    archetype: str


# linkding is the machinery-shakedown target (DESIGN §11 opening sequence #1):
# already qualified, always in the pool, never subject to the boot check.
LINKDING = TargetSpec("linkding", "bookmarks")

# §11 rotation-pool candidates — admitted only AFTER the docker-boot check. These
# are the names the design lists; their archetypes follow §11's descriptions.
POOL_CANDIDATES: tuple[TargetSpec, ...] = (
    TargetSpec("privatebin", "pastebin"),
    TargetSpec("shaarli", "bookmarks"),
    TargetSpec("dokuwiki", "wiki"),
    TargetSpec("mealie", "recipes"),
    TargetSpec("tandoor", "recipes"),
    TargetSpec("monica", "crm"),
    TargetSpec("invoiceshelf", "invoicing"),
)


# The qualification check (DESIGN §11): True iff the candidate boots+seeds under
# the time budget with a registry in range and a permissive license. The live
# implementation boots the real stack (pending-docker); offline fixtures script
# it. A candidate that fails the check is simply not admitted this epoch.
BootCheckFn = Callable[[TargetSpec], bool]


# --- tunables (caller-supplied; PROVENANCE per DESIGN §17) ---------------------

# PROVENANCE: DESIGN §11 — "full suite every N episodes". TUNING METRIC: chart
# latency (epochs to detect a regression) vs suite-run quota burn.
DEFAULT_SUITE_CADENCE = 5


@dataclass(frozen=True)
class CurriculumParams:
    """Curriculum tunables, caller-supplied (no hidden config reads)."""

    suite_cadence: int = DEFAULT_SUITE_CADENCE

    def __post_init__(self) -> None:
        if self.suite_cadence < 1:
            raise CurriculumError(
                f"suite_cadence must be >= 1, got {self.suite_cadence}"
            )


# --- pool construction and the held-out exclusion (R2) ------------------------


def assert_disjoint_from_holdout(
    pool: Sequence[TargetSpec],
    *,
    holdout: Sequence[SuiteTarget] = HELD_OUT_SUITE,
) -> None:
    """Refuse any training-pool target that is also a held-out suite target (R2).

    The held-out constraint is a hard error: a benchmark trained on measures
    memorization, not generalization (DESIGN §11)."""
    excluded = held_out_names(holdout)
    overlap = sorted(t.name for t in pool if t.name in excluded)
    if overlap:
        raise CurriculumError(
            f"training-pool targets {overlap} are in the held-out suite — a"
            " benchmark trained on measures memorization, not generalization"
            " (R1/R2 held-out constraint)"
        )


def training_pool(
    boot_check: BootCheckFn,
    *,
    base: Sequence[TargetSpec] = (LINKDING,),
    candidates: Sequence[TargetSpec] = POOL_CANDIDATES,
    holdout: Sequence[SuiteTarget] = HELD_OUT_SUITE,
) -> tuple[TargetSpec, ...]:
    """Build the epoch's training pool: ``base`` + candidates passing the
    docker-boot check, with the held-out set excluded as a hard error (R2).

    ``base`` (linkding) is admitted unconditionally — it is the already-qualified
    shakedown target. Candidates are admitted iff ``boot_check`` passes. Duplicate
    names across base+candidates are refused (the pool is a set of distinct
    targets). The result preserves base-then-admitted order before any rotation.
    """
    pool: list[TargetSpec] = list(base)
    seen = {t.name for t in pool}
    for candidate in candidates:
        if candidate.name in seen:
            raise CurriculumError(
                f"duplicate training target name {candidate.name!r}"
            )
        if boot_check(candidate):
            pool.append(candidate)
            seen.add(candidate.name)
        else:
            logger.info(
                "curriculum: candidate %s failed the docker-boot check; not"
                " admitted this epoch",
                candidate.name,
            )
    assert_disjoint_from_holdout(pool, holdout=holdout)
    return tuple(pool)


# --- deterministic rotation (R2) ----------------------------------------------


def _derive_seed(seed: int, epoch: int) -> int:
    """A platform-stable 64-bit PRNG seed from ``(seed, epoch)``.

    Uses a SHA256 of the pair rather than ``hash()`` so the rotation is identical
    across machines and Python runs (``hash()`` of strings is salted; KTD)."""
    digest = hashlib.sha256(f"{int(seed)}:{int(epoch)}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def rotation_order(
    pool: Sequence[TargetSpec], epoch: int, seed: int
) -> tuple[TargetSpec, ...]:
    """The deterministic per-epoch rotation of the training pool (R2).

    A stable permutation keyed by ``(seed, epoch)``: same ``(seed, epoch)`` → same
    order on any machine; a different epoch generally reorders. The base order is
    canonicalized by name first so the permutation is independent of how the pool
    was assembled."""
    items = sorted(pool, key=lambda t: t.name)
    rng = random.Random(_derive_seed(seed, epoch))
    idx = list(range(len(items)))
    rng.shuffle(idx)
    return tuple(items[i] for i in idx)


# --- suite cadence (R1) -------------------------------------------------------


def suite_due(episode_index: int, params: CurriculumParams | None = None) -> bool:
    """True when the full held-out suite is due (every ``suite_cadence`` episodes).

    ``episode_index`` is 1-based within a campaign; the suite runs on episode 1
    and every ``suite_cadence`` thereafter (R1)."""
    params = params or CurriculumParams()
    if episode_index < 1:
        raise CurriculumError(
            f"episode_index is 1-based, got {episode_index}"
        )
    return (episode_index - 1) % params.suite_cadence == 0


# --- epoch bookkeeping (R2) ---------------------------------------------------


def current_epoch(store: Store) -> int:
    """The highest epoch any episode has been stamped with (0 if none yet).

    Episodes carry a nullable ``epoch`` column (Plan 4 U1 seam); this is the
    read that tells the scheduler which rotation is in force."""
    row = store.conn.execute(
        "SELECT COALESCE(MAX(epoch), 0) AS v FROM episodes"
    ).fetchone()
    return int(row["v"])
