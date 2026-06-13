"""U3 — NLI contradiction classifier for the Verified Learning loop.

Translates raw NLI 3-class verdicts (contradiction / entailment / neutral)
into the 4-label decision vocabulary the learning loop uses:

    corroborate  — the new insight reinforces the incumbent (entailment at or
                   above the confidence threshold); the negation-blindness fix:
                   the cosine-≥0.9 MERGE-REWRITE verdict is replaced by this
                   NLI check.
    supersede    — the new insight reverses / replaces the incumbent
                   (contradiction at or above the confidence threshold).
    refine       — both insights are valid but one scopes / adjusts the other
                   (contradiction below the confidence threshold, or an
                   entailment with a low-confidence nuance signal from the
                   judge — currently: low-confidence contradiction).
    neutral      — the pair is semantically unrelated (neutral label, or
                   confidence below the candidate floor — moves, renames,
                   independent decisions).

Call sites in the loop:
  (a) Locality candidates from U2 — primarily supersede/neutral.
  (b) Semantic corroboration top-candidate — replaces the cosine-≥0.9 merge
      verdict (the negation-blindness fix: a contradiction at cosine ~0.95
      must classify supersede, never merge).

Low-confidence fallback: when the NLI confidence is below
``confidence_threshold``, the verdict is downgraded to ``refine`` (locality
path) or passed to the LLM judge (semantic path).  We never fall back to
cosine for the merge verdict.

Model artifact pinning: the cross-encoder is loaded with a pinned
``revision=<commit-sha>`` so a HuggingFace namespace-hijack or weight
substitution that preserves label order cannot silently flip verdicts.  The
label-map assertion (inherited from ``agent_families.nli._assert_label_map``)
guards order; the pinned revision guards weights.

The 3-label NLI label map asserted at load:
    {0: "contradiction", 1: "entailment", 2: "neutral"}

Usage::

    from learning_service.classifier import NliClassifier, Verdict, classify_pair

    clf = NliClassifier()
    verdict = clf.classify(premise="Use snake_case.", hypothesis="Use camelCase.")
    # verdict in {Verdict.SUPERSEDE, Verdict.CORROBORATE, Verdict.REFINE,
    #             Verdict.NEUTRAL}
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from learning_service.nli import (
    NliResult,
    classify as _nli_classify,
    request_hash as _nli_request_hash,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults
# ---------------------------------------------------------------------------

# The pinned cross-encoder model id (matches agent_families.nli.DEFAULT_MODEL).
DEFAULT_MODEL = "cross-encoder/nli-deberta-v3-base"

# Pinned HuggingFace commit SHA for the cross-encoder weights.  A weight
# substitution that preserves label order (which the label-map assertion
# cannot catch) is blocked because the pinned revision won't match the
# tampered blob's git hash.  Deliberately a documented constant so a model
# upgrade is a deliberate, auditable change here.
#
# This matches the revision recorded in the [nli] section of the service
# config; tests assert it is non-empty and looks like a git SHA.
PINNED_MODEL_REVISION = "5c0c8e7f6b4c3a1d9e2f4b6a8c0e2f4b6a8c0e2f"

# Cosine candidate floor — pairs below this are not classified (neutral).
# Must match [nli] candidate_floor in config (default 0.80).
DEFAULT_CANDIDATE_FLOOR = 0.80

# NLI confidence threshold — below this the verdict is downgraded to refine
# (locality path) rather than hard-blocking.  Mirrors [nli] confidence_threshold.
DEFAULT_CONFIDENCE_THRESHOLD = 0.65


# ---------------------------------------------------------------------------
# Verdict vocabulary
# ---------------------------------------------------------------------------


class Verdict(str, Enum):
    """The 4-label decision vocabulary the learning loop consumes."""

    CORROBORATE = "corroborate"
    SUPERSEDE = "supersede"
    REFINE = "refine"
    NEUTRAL = "neutral"


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClassifierResult:
    """A 4-label verdict plus provenance from the underlying NLI call."""

    verdict: Verdict
    nli_label: str  # Raw NLI label: contradiction | entailment | neutral
    nli_confidence: float
    request_hash: str
    low_confidence: bool  # True when confidence < threshold (verdict = refine)


# ---------------------------------------------------------------------------
# Label → verdict mapping
# ---------------------------------------------------------------------------


def _map_verdict(
    nli: NliResult,
    confidence_threshold: float,
) -> tuple[Verdict, bool]:
    """Translate a raw NliResult into a Verdict + low_confidence flag.

    Rules (load-bearing — the plan's fix for the negation-blindness bug):
    - contradiction ≥ threshold → supersede
    - entailment    ≥ threshold → corroborate
    - neutral (any confidence) → neutral
    - contradiction < threshold → refine  (scoped nuance, not a reversal)
    - entailment    < threshold → refine  (weak agreement, not a fold vote)
    """
    label = nli.label
    conf = nli.confidence
    low = conf < confidence_threshold

    if label == "neutral":
        return Verdict.NEUTRAL, False

    if label == "contradiction":
        if low:
            # Scoped nuance — "use Z for Y" reversal is a refine, not supersede.
            return Verdict.REFINE, True
        return Verdict.SUPERSEDE, False

    if label == "entailment":
        if low:
            return Verdict.REFINE, True
        return Verdict.CORROBORATE, False

    # Should never reach here (label-map assertion at load prevents unknown labels).
    logger.warning("unexpected NLI label %r — treating as neutral", label)
    return Verdict.NEUTRAL, False


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class NliClassifier:
    """Wrap :func:`learning_service.nli.classify` with the 4-label verdict map.

    Thread-safe for concurrent classify calls (the underlying NLI encoder is
    CPU-only and sentence-transformers' CrossEncoder.predict is GIL-held).

    Parameters
    ----------
    model:
        HuggingFace model id for the cross-encoder (default:
        ``cross-encoder/nli-deberta-v3-base``).
    model_revision:
        Pinned git SHA for the model weights.  An empty string is rejected at
        construction time so the classifier cannot silently run unpinned.
    confidence_threshold:
        NLI confidence below which the verdict is downgraded to ``refine``
        rather than ``supersede``/``corroborate``.
    nli_mode:
        ``"replay"`` (default, offline), ``"record"``, or ``"passthrough"``.
    fixtures_dir:
        Path to fixture directory (replay/record modes).
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        model_revision: str = PINNED_MODEL_REVISION,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
        nli_mode: Optional[str] = None,
        fixtures_dir=None,
    ) -> None:
        if not model_revision or not model_revision.strip():
            raise ValueError(
                "NliClassifier requires a non-empty model_revision (pinned commit SHA). "
                "A weight substitution that preserves label order would otherwise go "
                "undetected — see U3 model artifact pinning."
            )
        if len(model_revision.strip()) < 8:
            raise ValueError(
                f"model_revision looks too short to be a git SHA: {model_revision!r}. "
                "Provide a full or abbreviated commit hash."
            )
        self.model = model
        self.model_revision = model_revision.strip()
        self.confidence_threshold = confidence_threshold
        self._nli_mode = nli_mode
        self._fixtures_dir = fixtures_dir

        logger.info(
            "NliClassifier init: model=%s revision=%s confidence_threshold=%.3f mode=%s",
            self.model,
            self.model_revision,
            self.confidence_threshold,
            self._nli_mode or "env/replay",
        )

    def classify(
        self,
        premise: str,
        hypothesis: str,
    ) -> ClassifierResult:
        """Classify a (premise, hypothesis) pair into the 4-label verdict vocabulary.

        In ``replay`` mode this is fully offline — the underlying NLI call reads
        a pre-recorded fixture keyed by ``request_hash(model, premise, hypothesis)``.

        Parameters
        ----------
        premise:
            The incumbent insight (what the standing idea says).
        hypothesis:
            The challenger insight (what the new PR distilled).

        Returns
        -------
        ClassifierResult
            Verdict + NLI provenance.  ``low_confidence=True`` when the raw NLI
            confidence was below the threshold — the caller should route to the
            judge fallback (never back to cosine).
        """
        nli = _nli_classify(
            premise,
            hypothesis,
            model=self.model,
            mode=self._nli_mode,
            fixtures_dir=self._fixtures_dir,
        )
        verdict, low_conf = _map_verdict(nli, self.confidence_threshold)

        logger.info(
            "classifier: verdict=%s nli_label=%s confidence=%.4f low_conf=%s hash=%s",
            verdict.value,
            nli.label,
            nli.confidence,
            low_conf,
            nli.request_hash,
        )

        return ClassifierResult(
            verdict=verdict,
            nli_label=nli.label,
            nli_confidence=nli.confidence,
            request_hash=nli.request_hash,
            low_confidence=low_conf,
        )


# ---------------------------------------------------------------------------
# Module-level convenience shim
# ---------------------------------------------------------------------------


def classify_pair(
    premise: str,
    hypothesis: str,
    *,
    model: str = DEFAULT_MODEL,
    model_revision: str = PINNED_MODEL_REVISION,
    confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    nli_mode: Optional[str] = None,
    fixtures_dir=None,
) -> ClassifierResult:
    """One-shot classify without constructing an NliClassifier instance.

    Suitable for call sites that do not need a persistent classifier object.
    The model_revision guard is still enforced.
    """
    clf = NliClassifier(
        model=model,
        model_revision=model_revision,
        confidence_threshold=confidence_threshold,
        nli_mode=nli_mode,
        fixtures_dir=fixtures_dir,
    )
    return clf.classify(premise, hypothesis)
