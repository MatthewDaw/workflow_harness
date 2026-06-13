"""config.py — U9: Unified configuration for the Verified Learning loop (MAT-147).

All thresholds and mode flags live here.  Every submodule that needs a
threshold imports from this module — no scattered magic numbers.

Design
------
``LearningConfig`` is a frozen dataclass.  It can be constructed from:

  1. Defaults (all sane defaults from the plan).
  2. A ``dict`` of overrides via ``LearningConfig.from_dict()``.
  3. A ``.ini``-style config file section (optional; for production use).

The three shadow|enforce mode flags control which subsystems write to the
store versus only logging their decisions:

  ``verified_learning_mode``
      Shadow → observe distill/anchor/NLI decisions without writing.
      Enforce → write processed-PR cursors and idea records.

  ``supersede_mode``
      Shadow → classify locality collisions and log supersede verdicts without
               executing them.
      Enforce → execute authority-weighted supersession (stamp invalidAt +
               supersededBy).

  ``unfold_mode``
      Shadow → log un-fold decisions without writing new skill revisions.
      Enforce → author a new skill revision dropping the superseded lesson,
               repoint TRUE.  ONLY allowed when the supersede FP-calibration
               gate passes (supersede_fp_ceiling over min_samples spot-checks).

Rollout sequence (plan §Rollout):
  Step 1: verified_learning_mode = shadow   (observe distill/anchor/NLI)
  Step 2: verified_learning_mode = enforce  (write cursors + idea records)
  Step 3: supersede_mode = shadow           (observe supersession decisions)
  Step 4: supersede_mode = enforce          (execute supersession)
  Step 5: unfold_mode = shadow             (observe un-fold decisions)
  Step 6: unfold_mode = enforce            (execute un-fold — ONLY after FP gate)

Public API
----------
``LearningConfig``       — frozen dataclass with all thresholds + flags.
``ModeFlag``             — enum for shadow | enforce.
``DEFAULT_CONFIG``       — the canonical defaults (plan §Config & thresholds).
``config_from_env()``    — build from environment variables (production use).
``validate_config()``    — check invariants; raises ValueError on violations.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode flag enum
# ---------------------------------------------------------------------------


class ModeFlag(str, Enum):
    """shadow | enforce for each rollout gate."""

    SHADOW = "shadow"
    ENFORCE = "enforce"

    @classmethod
    def parse(cls, value: str) -> "ModeFlag":
        """Parse a string into a ModeFlag; raises ValueError on unknown values."""
        v = value.strip().lower()
        if v == "shadow":
            return cls.SHADOW
        if v == "enforce":
            return cls.ENFORCE
        raise ValueError(
            f"Unknown ModeFlag value: {value!r}. Expected 'shadow' or 'enforce'."
        )


# ---------------------------------------------------------------------------
# Config dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LearningConfig:
    """All thresholds + mode flags for the Verified Learning loop.

    Fields
    ------
    NLI classifier
    ~~~~~~~~~~~~~~
    candidate_floor : float
        Cosine similarity floor below which a candidate pair is not classified
        (treated as neutral without calling the NLI model).  Default 0.80.
    confidence_threshold : float
        NLI confidence below which the verdict is downgraded to ``refine``
        rather than hard-blocking as supersede/corroborate.  Default 0.65.

    Corroboration / fold
    ~~~~~~~~~~~~~~~~~~~~
    verified_K : float
        Accumulated ``corroborationWeight`` (Σ rung × author-credibility +
        recurrence bonus) required to fold an inferred idea.  Default 2.0.
        Authored ideas (user_directive / authored_import) skip this threshold.
    rung_weights : dict[str, float]
        Weights for each verification rung: test (1.0), normal (0.6), bare (0.4).
        A merged PR is **never** 0.
    recurrence_bonus : float
        Extra weight per repeat hit on the same idea (same prRef.number seen
        again, e.g. in a replay overlap or a PR that touches multiple anchors).
        Default 0.2.

    Author credibility
    ~~~~~~~~~~~~~~~~~~
    author_credibility : dict[str, float]
        User-curated per-coder weight overrides.  Empty dict by default
        (all coders use the defaults below).  Settable during or after ingest;
        boosts may be applied retroactively.
    default_author_credibility : float
        Fallback credibility for unknown / single-author coders.  Default 0.5.
    reviewer_credibility : float
        Fallback for coders whose PRs had a distinct non-author reviewer.
        Default 0.7 (slightly higher than the single-author default).

    Supersession
    ~~~~~~~~~~~~
    supersede_thrash_window : int
        Sliding-window length in seconds.  Supersede/revive flips within this
        window are counted; once the count exceeds the max_flips ceiling the
        operation is blocked.  Default 7 × 24 × 3600 (7 days).
    supersede_thrash_max_flips : int
        Maximum allowed flips per idea within the thrash window.  Default 3.
    supersede_fp_ceiling : float
        The calibration gate: before ``unfold_mode = enforce``, the supersede
        false-positive rate must be below this ceiling over at least
        ``supersede_fp_min_samples`` human-spot-checked verdicts.  Default 0.10.
    supersede_fp_min_samples : int
        Minimum spot-checks before the FP gate can open.  Default 30.

    Necessity gate
    ~~~~~~~~~~~~~~
    necessity_sample_rate : float
        Fraction of folded ideas assessed per scheduled scan (0.0..1.0).
        Default 1.0 (all).  Reduce for cost control on large corpora.
    necessity_min_firings : int
        Minimum golden-case firings required before assessing an idea.
        Default 0 (assess all folded ideas regardless of firing history).
    necessity_scan_schedule : str
        EventBridge schedule expression for the daily scan.
        Default ``"rate(1 day)"``.

    Mode flags
    ~~~~~~~~~~
    verified_learning_mode : ModeFlag
        Shadow (observe) or enforce (write) for PR ingestion + corroboration.
    supersede_mode : ModeFlag
        Shadow (observe) or enforce (execute) for supersession.
    unfold_mode : ModeFlag
        Shadow (observe) or enforce (execute un-fold revisions).
        Un-fold enforce is ONLY allowed after the FP calibration gate passes.
    """

    # --- NLI classifier ---
    candidate_floor: float = 0.80
    confidence_threshold: float = 0.65

    # --- Corroboration / fold ---
    verified_K: float = 2.0
    rung_weights: dict = field(default_factory=lambda: {
        "test": 1.0,
        "normal": 0.6,
        "bare": 0.4,
    })
    recurrence_bonus: float = 0.2

    # --- Author credibility ---
    author_credibility: dict = field(default_factory=dict)
    default_author_credibility: float = 0.5
    reviewer_credibility: float = 0.7

    # --- Supersession ---
    supersede_thrash_window: int = 7 * 24 * 3600  # 7 days in seconds
    supersede_thrash_max_flips: int = 3
    supersede_fp_ceiling: float = 0.10
    supersede_fp_min_samples: int = 30

    # --- Necessity gate ---
    necessity_sample_rate: float = 1.0
    necessity_min_firings: int = 0
    necessity_scan_schedule: str = "rate(1 day)"

    # --- Mode flags ---
    verified_learning_mode: ModeFlag = ModeFlag.SHADOW
    supersede_mode: ModeFlag = ModeFlag.SHADOW
    unfold_mode: ModeFlag = ModeFlag.SHADOW

    # ---------------------------------------------------------------------------
    # Derived properties
    # ---------------------------------------------------------------------------

    def is_ingest_enforce(self) -> bool:
        """True when PR ingestion + corroboration are in enforce mode."""
        return self.verified_learning_mode == ModeFlag.ENFORCE

    def is_supersede_enforce(self) -> bool:
        """True when supersession verdicts are executed (not just logged)."""
        return self.supersede_mode == ModeFlag.ENFORCE

    def is_unfold_enforce(self) -> bool:
        """True when un-fold skill revisions are authored."""
        return self.unfold_mode == ModeFlag.ENFORCE

    def get_rung_weight(self, rung: str) -> float:
        """Return the weight for a rung; falls back to bare (0.4) for unknown rungs."""
        w = self.rung_weights.get(rung)
        if w is None:
            logger.warning("unknown rung %r — defaulting to bare (0.4)", rung)
            return 0.4
        return w

    def get_author_credibility(self, author_id: str, *, has_reviewer: bool = False) -> float:
        """Return the credibility weight for an author.

        Lookup order:
        1. Per-coder override in ``author_credibility``.
        2. ``reviewer_credibility`` if ``has_reviewer`` is True.
        3. ``default_author_credibility``.
        """
        if author_id in self.author_credibility:
            return self.author_credibility[author_id]
        return self.reviewer_credibility if has_reviewer else self.default_author_credibility

    # ---------------------------------------------------------------------------
    # Factory methods
    # ---------------------------------------------------------------------------

    @classmethod
    def from_dict(cls, overrides: dict[str, Any]) -> "LearningConfig":
        """Build a LearningConfig from a dict of key → value overrides.

        Keys match the field names exactly.  Unknown keys are ignored with a
        warning (forward-compat: a new config key does not break old code).

        ``rung_weights`` is expected to be a dict[str, float].
        ``author_credibility`` is expected to be a dict[str, float].
        Mode flags accept ``"shadow"`` or ``"enforce"`` strings.
        """
        known_fields = {
            "candidate_floor", "confidence_threshold",
            "verified_K", "rung_weights", "recurrence_bonus",
            "author_credibility", "default_author_credibility", "reviewer_credibility",
            "supersede_thrash_window", "supersede_thrash_max_flips",
            "supersede_fp_ceiling", "supersede_fp_min_samples",
            "necessity_sample_rate", "necessity_min_firings", "necessity_scan_schedule",
            "verified_learning_mode", "supersede_mode", "unfold_mode",
        }

        # Start from defaults.
        defaults = cls()
        kwargs: dict[str, Any] = {}

        for key, value in overrides.items():
            if key not in known_fields:
                logger.warning("LearningConfig.from_dict: unknown key %r — ignored", key)
                continue

            # Parse mode flags from strings.
            if key in ("verified_learning_mode", "supersede_mode", "unfold_mode"):
                if isinstance(value, str):
                    value = ModeFlag.parse(value)
                elif not isinstance(value, ModeFlag):
                    raise TypeError(
                        f"Config key {key!r} must be a ModeFlag or 'shadow'/'enforce' string; "
                        f"got {type(value).__name__!r}"
                    )

            kwargs[key] = value

        # Merge with defaults: build a new instance using keyword arguments.
        merged = {
            "candidate_floor": defaults.candidate_floor,
            "confidence_threshold": defaults.confidence_threshold,
            "verified_K": defaults.verified_K,
            "rung_weights": dict(defaults.rung_weights),
            "recurrence_bonus": defaults.recurrence_bonus,
            "author_credibility": dict(defaults.author_credibility),
            "default_author_credibility": defaults.default_author_credibility,
            "reviewer_credibility": defaults.reviewer_credibility,
            "supersede_thrash_window": defaults.supersede_thrash_window,
            "supersede_thrash_max_flips": defaults.supersede_thrash_max_flips,
            "supersede_fp_ceiling": defaults.supersede_fp_ceiling,
            "supersede_fp_min_samples": defaults.supersede_fp_min_samples,
            "necessity_sample_rate": defaults.necessity_sample_rate,
            "necessity_min_firings": defaults.necessity_min_firings,
            "necessity_scan_schedule": defaults.necessity_scan_schedule,
            "verified_learning_mode": defaults.verified_learning_mode,
            "supersede_mode": defaults.supersede_mode,
            "unfold_mode": defaults.unfold_mode,
        }
        merged.update(kwargs)
        return cls(**merged)

    @classmethod
    def from_env(cls) -> "LearningConfig":
        """Build a LearningConfig from environment variables.

        Each config key maps to a prefixed env var:
          VL_CANDIDATE_FLOOR, VL_CONFIDENCE_THRESHOLD, VL_VERIFIED_K, ...
          VL_VERIFIED_LEARNING_MODE, VL_SUPERSEDE_MODE, VL_UNFOLD_MODE.

        Unset vars use the field defaults.
        """
        overrides: dict[str, Any] = {}

        def _float(name: str, key: str) -> None:
            v = os.environ.get(name)
            if v is not None:
                overrides[key] = float(v)

        def _int(name: str, key: str) -> None:
            v = os.environ.get(name)
            if v is not None:
                overrides[key] = int(v)

        def _str(name: str, key: str) -> None:
            v = os.environ.get(name)
            if v is not None:
                overrides[key] = v

        _float("VL_CANDIDATE_FLOOR", "candidate_floor")
        _float("VL_CONFIDENCE_THRESHOLD", "confidence_threshold")
        _float("VL_VERIFIED_K", "verified_K")
        _float("VL_RECURRENCE_BONUS", "recurrence_bonus")
        _float("VL_DEFAULT_AUTHOR_CREDIBILITY", "default_author_credibility")
        _float("VL_REVIEWER_CREDIBILITY", "reviewer_credibility")
        _int("VL_SUPERSEDE_THRASH_WINDOW", "supersede_thrash_window")
        _int("VL_SUPERSEDE_THRASH_MAX_FLIPS", "supersede_thrash_max_flips")
        _float("VL_SUPERSEDE_FP_CEILING", "supersede_fp_ceiling")
        _int("VL_SUPERSEDE_FP_MIN_SAMPLES", "supersede_fp_min_samples")
        _float("VL_NECESSITY_SAMPLE_RATE", "necessity_sample_rate")
        _int("VL_NECESSITY_MIN_FIRINGS", "necessity_min_firings")
        _str("VL_NECESSITY_SCAN_SCHEDULE", "necessity_scan_schedule")
        _str("VL_VERIFIED_LEARNING_MODE", "verified_learning_mode")
        _str("VL_SUPERSEDE_MODE", "supersede_mode")
        _str("VL_UNFOLD_MODE", "unfold_mode")

        return cls.from_dict(overrides)


# ---------------------------------------------------------------------------
# Canonical defaults (one place, referenced in tests and docs)
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = LearningConfig()
"""The canonical default LearningConfig (all shadow modes, plan §Config defaults)."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_config(cfg: LearningConfig) -> None:
    """Raise ValueError if any config invariant is violated.

    Invariants enforced:
    - All weights and thresholds are in their legal ranges.
    - unfold_mode = enforce requires supersede_mode = enforce.
    - rung_weights values are all positive (a merged PR is never 0).
    - verified_K > 0.
    - candidate_floor in (0, 1].
    - confidence_threshold in (0, 1].
    - necessity_sample_rate in (0, 1].
    - supersede_fp_ceiling in (0, 1).
    """
    errors: list[str] = []

    if cfg.verified_K <= 0:
        errors.append(f"verified_K must be > 0; got {cfg.verified_K!r}")

    if not (0.0 < cfg.candidate_floor <= 1.0):
        errors.append(
            f"candidate_floor must be in (0, 1]; got {cfg.candidate_floor!r}"
        )

    if not (0.0 < cfg.confidence_threshold <= 1.0):
        errors.append(
            f"confidence_threshold must be in (0, 1]; got {cfg.confidence_threshold!r}"
        )

    if not (0.0 < cfg.necessity_sample_rate <= 1.0):
        errors.append(
            f"necessity_sample_rate must be in (0, 1]; got {cfg.necessity_sample_rate!r}"
        )

    if not (0.0 < cfg.supersede_fp_ceiling < 1.0):
        errors.append(
            f"supersede_fp_ceiling must be in (0, 1); got {cfg.supersede_fp_ceiling!r}"
        )

    if cfg.recurrence_bonus < 0.0:
        errors.append(
            f"recurrence_bonus must be >= 0; got {cfg.recurrence_bonus!r}"
        )

    for rung, w in cfg.rung_weights.items():
        if w <= 0.0:
            errors.append(
                f"rung_weight for {rung!r} must be > 0 (a merged PR is never 0); "
                f"got {w!r}"
            )

    if not (0.0 < cfg.default_author_credibility <= 2.0):
        errors.append(
            f"default_author_credibility must be in (0, 2]; got {cfg.default_author_credibility!r}"
        )

    # un-fold enforce requires supersede enforce.
    if (
        cfg.unfold_mode == ModeFlag.ENFORCE
        and cfg.supersede_mode != ModeFlag.ENFORCE
    ):
        errors.append(
            "unfold_mode = 'enforce' requires supersede_mode = 'enforce'; "
            "un-fold is contingent on supersession being in enforce mode."
        )

    if cfg.supersede_thrash_window <= 0:
        errors.append(
            f"supersede_thrash_window must be > 0 seconds; got {cfg.supersede_thrash_window!r}"
        )

    if cfg.supersede_fp_min_samples < 0:
        errors.append(
            f"supersede_fp_min_samples must be >= 0; got {cfg.supersede_fp_min_samples!r}"
        )

    if errors:
        raise ValueError(
            "LearningConfig validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )
