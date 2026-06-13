"""MAT-139 (U3) — NLI contradiction classifier (reuse nli.py, in-loop, pinned).

Acceptance checklist (all 7 items from Linear MAT-139):
  [x] test_contradiction_at_high_cosine_is_supersede_not_merge
  [x] test_nli_replay_byte_identical_offline
  [x] test_label_map_swap_fails_at_load
  [x] test_refine_not_supersede_for_scoped_nuance
  [x] test_neutral_for_pure_rename
  [x] test_low_confidence_falls_to_judge_not_cosine
  [x] test_model_revision_pinned

All tests run OFFLINE (no model load, no network, no quota).  NLI verdicts
come from pre-seeded replay fixtures written inline via
``agent_families.nli.write_fixture``.

Design:
- ``NliClassifier`` wraps ``learning_service.nli.classify`` (which re-exports
  ``agent_families.nli.classify``) and translates the 3-label NLI verdict
  (contradiction / entailment / neutral) into the 4-label decision vocabulary
  (corroborate / supersede / refine / neutral).
- The model_revision guard (non-empty pinned SHA) is asserted at construction.
- The label-map assertion lives in ``agent_families.nli._assert_label_map``
  (executed at model load in passthrough/record mode); we test the guard with
  a fake encoder that has the wrong id2label.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------


def _seed_fixture(
    tmp_path: Path,
    *,
    model: str,
    premise: str,
    hypothesis: str,
    label: str,
    confidence: float,
    logits: list[float] | None = None,
) -> str:
    """Write a replay fixture and return the request_hash."""
    from learning_service.nli import request_hash, write_fixture

    h = request_hash(model, premise, hypothesis)
    write_fixture(
        tmp_path,
        model,
        premise,
        hypothesis,
        {
            "label": label,
            "confidence": confidence,
            "logits": logits or [0.0, 0.0, 0.0],
        },
    )
    return h


# The pinned model id used in every call below.
_MODEL = "cross-encoder/nli-deberta-v3-base"


# ---------------------------------------------------------------------------
# Test 1 — test_contradiction_at_high_cosine_is_supersede_not_merge
# ---------------------------------------------------------------------------


def test_contradiction_at_high_cosine_is_supersede_not_merge(tmp_path):
    """The negation-blindness fix: a contradiction at cosine ~0.95 must classify
    supersede, NOT merge/corroborate.

    Prior behaviour (cosine-≥0.9 MERGE-REWRITE verdict) blended a contradiction
    into the incumbent instead of retiring it.  NLI replaces that verdict for
    semantic candidates.

    Here we simulate the scenario: a pair that would score cosine ~0.95
    (very similar surface form) but NLI correctly detects contradiction.
    """
    from learning_service.classifier import NliClassifier, Verdict

    premise = "Always use snake_case for Python function names."
    hypothesis = "Python function names must use camelCase, never snake_case."

    # Seed a fixture with a high-confidence contradiction — mimicking the
    # situation where cosine ~0.95 but the semantics are opposite.
    _seed_fixture(
        tmp_path,
        model=_MODEL,
        premise=premise,
        hypothesis=hypothesis,
        label="contradiction",
        confidence=0.95,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",  # valid non-empty SHA
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )
    result = clf.classify(premise, hypothesis)

    # The critical assertion: contradiction at high confidence → supersede,
    # never corroborate (the old cosine merge path).
    assert result.verdict == Verdict.SUPERSEDE, (
        f"Expected SUPERSEDE for a high-confidence contradiction, got {result.verdict}. "
        "This is the negation-blindness regression — a contradiction at cosine ~0.95 "
        "must retire the incumbent, not merge it."
    )
    assert result.nli_label == "contradiction"
    assert result.nli_confidence == pytest.approx(0.95)
    assert not result.low_confidence


# ---------------------------------------------------------------------------
# Test 2 — test_nli_replay_byte_identical_offline
# ---------------------------------------------------------------------------


def test_nli_replay_byte_identical_offline(tmp_path):
    """Two replay calls for the same (premise, hypothesis) return byte-identical results.

    Determinism is load-bearing: the replay fixture is the source of truth in
    offline mode; any non-determinism would make tests flaky and undermine the
    "offline CI" guarantee.
    """
    from learning_service.classifier import NliClassifier, Verdict

    premise = "Prefer composition over inheritance."
    hypothesis = "Inheritance should always be favoured over composition."

    _seed_fixture(
        tmp_path,
        model=_MODEL,
        premise=premise,
        hypothesis=hypothesis,
        label="contradiction",
        confidence=0.88,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )

    r1 = clf.classify(premise, hypothesis)
    r2 = clf.classify(premise, hypothesis)

    assert r1.verdict == r2.verdict
    assert r1.nli_label == r2.nli_label
    assert r1.nli_confidence == r2.nli_confidence
    assert r1.request_hash == r2.request_hash
    assert r1.low_confidence == r2.low_confidence

    # Both should be SUPERSEDE (contradiction ≥ 0.65 threshold).
    assert r1.verdict == Verdict.SUPERSEDE


# ---------------------------------------------------------------------------
# Test 3 — test_label_map_swap_fails_at_load
# ---------------------------------------------------------------------------


def test_label_map_swap_fails_at_load():
    """A model whose id2label does not match the hard-coded 3-class map raises
    NliUnavailable at load (before any verdict is produced).

    This guards against a HuggingFace namespace-hijack where a replacement model
    with a different label order is substituted.  The label-map assertion in
    ``agent_families.nli._assert_label_map`` must fail loudly, not silently
    flip every verdict.
    """
    from learning_service.nli import NliUnavailable, preflight, reset_encoder_cache

    reset_encoder_cache()

    # Fake encoder: wrong id2label (entailment/contradiction swapped → would
    # flip every verdict if we let it through).
    bad_config = SimpleNamespace(
        id2label={0: "entailment", 1: "contradiction", 2: "neutral"}
    )
    bad_encoder = SimpleNamespace(config=bad_config)

    with pytest.raises(NliUnavailable, match="label head does not match"):
        preflight(_MODEL, _encoder=bad_encoder)


def test_label_map_wrong_class_count_fails_at_load():
    """A 2-class model (binary NLI) fails the 3-class assertion."""
    from learning_service.nli import NliUnavailable, preflight, reset_encoder_cache

    reset_encoder_cache()

    # 2-class model — would not be a standard 3-label NLI head.
    bad_config = SimpleNamespace(id2label={0: "contradiction", 1: "entailment"})
    bad_encoder = SimpleNamespace(config=bad_config)

    with pytest.raises(NliUnavailable, match="label head does not match"):
        preflight(_MODEL, _encoder=bad_encoder)


# ---------------------------------------------------------------------------
# Test 4 — test_refine_not_supersede_for_scoped_nuance
# ---------------------------------------------------------------------------


def test_refine_not_supersede_for_scoped_nuance(tmp_path):
    """A contradiction below the confidence threshold is ``refine``, not ``supersede``.

    Example: "use async/await for all I/O" vs. "use async/await for I/O in
    serverless — sync is fine for scripts" — the latter scopes the former,
    it does not reverse it.  NLI may see a weak contradiction; the load-bearing
    boundary is that a low-confidence contradiction does NOT trigger a supersede
    and hence does NOT un-fold the incumbent skill revision.
    """
    from learning_service.classifier import NliClassifier, Verdict

    premise = "Use async/await for all I/O operations."
    hypothesis = "Use async/await for I/O in serverless; sync is fine for CLI scripts."

    # Low-confidence contradiction — the refine boundary.
    _seed_fixture(
        tmp_path,
        model=_MODEL,
        premise=premise,
        hypothesis=hypothesis,
        label="contradiction",
        confidence=0.55,  # below DEFAULT_CONFIDENCE_THRESHOLD of 0.65
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )
    result = clf.classify(premise, hypothesis)

    assert result.verdict == Verdict.REFINE, (
        f"Expected REFINE for a low-confidence contradiction (scoped nuance), "
        f"got {result.verdict}. A refine must NOT trigger un-fold."
    )
    assert result.nli_label == "contradiction"
    assert result.low_confidence is True


# ---------------------------------------------------------------------------
# Test 5 — test_neutral_for_pure_rename
# ---------------------------------------------------------------------------


def test_neutral_for_pure_rename(tmp_path):
    """A pair that is semantically unrelated (pure rename / move) classifies neutral.

    A PR that renames a file or moves a function with no semantic change should
    not trigger a supersede or corroborate — those anchors just went stale,
    not contradicted.
    """
    from learning_service.classifier import NliClassifier, Verdict

    premise = "The `UserService` module handles authentication."
    hypothesis = "The `AuthService` module handles authentication."  # renamed

    _seed_fixture(
        tmp_path,
        model=_MODEL,
        premise=premise,
        hypothesis=hypothesis,
        label="neutral",
        confidence=0.91,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )
    result = clf.classify(premise, hypothesis)

    assert result.verdict == Verdict.NEUTRAL, (
        f"Expected NEUTRAL for a pure rename (no semantic change), got {result.verdict}."
    )
    assert result.nli_label == "neutral"
    # neutral is always non-low-confidence regardless of the raw score.
    assert not result.low_confidence


# ---------------------------------------------------------------------------
# Test 6 — test_low_confidence_falls_to_judge_not_cosine
# ---------------------------------------------------------------------------


def test_low_confidence_falls_to_judge_not_cosine(tmp_path):
    """When NLI confidence is below the threshold, the classifier sets
    ``low_confidence=True`` so the caller routes to the judge fallback.

    The critical contract: we NEVER fall back to cosine for the merge verdict.
    The ``low_confidence`` flag is the signal; the *caller* (the learning loop)
    is responsible for routing to the judge.  This test asserts the flag is set
    correctly for both contradiction and entailment at low confidence.
    """
    from learning_service.classifier import NliClassifier, Verdict

    premise_con = "Always validate inputs at the API boundary."
    hypothesis_con = "Skip validation for internal service calls."

    premise_ent = "Use environment variables for secrets."
    hypothesis_ent = "Store secrets in env vars."

    _seed_fixture(
        tmp_path, model=_MODEL, premise=premise_con, hypothesis=hypothesis_con,
        label="contradiction", confidence=0.50,
    )
    _seed_fixture(
        tmp_path, model=_MODEL, premise=premise_ent, hypothesis=hypothesis_ent,
        label="entailment", confidence=0.55,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )

    # Low-confidence contradiction → refine, low_confidence=True
    r_con = clf.classify(premise_con, hypothesis_con)
    assert r_con.low_confidence is True, (
        "low_confidence must be True when NLI confidence < threshold"
    )
    assert r_con.verdict == Verdict.REFINE, (
        "Low-confidence contradiction must be REFINE (not supersede, not cosine fallback)"
    )

    # Low-confidence entailment → refine, low_confidence=True
    r_ent = clf.classify(premise_ent, hypothesis_ent)
    assert r_ent.low_confidence is True, (
        "low_confidence must be True for low-confidence entailment too"
    )
    assert r_ent.verdict == Verdict.REFINE


def test_high_confidence_contradiction_does_not_set_low_confidence(tmp_path):
    """Sanity check: a high-confidence contradiction has low_confidence=False."""
    from learning_service.classifier import NliClassifier, Verdict

    premise = "Use GET for idempotent read endpoints."
    hypothesis = "POST must be used for all read endpoints."

    _seed_fixture(
        tmp_path, model=_MODEL, premise=premise, hypothesis=hypothesis,
        label="contradiction", confidence=0.92,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )
    result = clf.classify(premise, hypothesis)

    assert result.verdict == Verdict.SUPERSEDE
    assert not result.low_confidence


# ---------------------------------------------------------------------------
# Test 7 — test_model_revision_pinned
# ---------------------------------------------------------------------------


def test_model_revision_pinned():
    """NliClassifier requires a non-empty model_revision (pinned commit SHA).

    An empty or missing revision is rejected at construction time.  This
    ensures the classifier cannot run without the weight-pinning guard in place.
    """
    from learning_service.classifier import NliClassifier

    # Blank revision → ValueError at construction.
    with pytest.raises(ValueError, match="non-empty model_revision"):
        NliClassifier(model_revision="")

    # Whitespace-only revision → same rejection.
    with pytest.raises(ValueError, match="non-empty model_revision"):
        NliClassifier(model_revision="   ")

    # Very short string (< 8 chars) → rejected as unlikely to be a real SHA.
    with pytest.raises(ValueError, match="too short"):
        NliClassifier(model_revision="abc")


def test_model_revision_stored_on_classifier():
    """The pinned revision is stored and readable from the classifier instance."""
    from learning_service.classifier import NliClassifier, PINNED_MODEL_REVISION

    clf = NliClassifier(model_revision="deadbeef12345678")
    assert clf.model_revision == "deadbeef12345678"


def test_default_pinned_revision_is_sha_like():
    """The module-level PINNED_MODEL_REVISION constant is non-empty and SHA-like.

    This verifies the plan's requirement: the shipped default must be a real
    pinned revision, not an empty placeholder.
    """
    from learning_service.classifier import PINNED_MODEL_REVISION

    assert PINNED_MODEL_REVISION, "PINNED_MODEL_REVISION must not be empty"
    assert len(PINNED_MODEL_REVISION) >= 8, (
        f"PINNED_MODEL_REVISION looks too short to be a git SHA: {PINNED_MODEL_REVISION!r}"
    )
    # Must look like a hex string (git SHAs are hex).
    hex_chars = set("0123456789abcdefABCDEF")
    assert all(c in hex_chars for c in PINNED_MODEL_REVISION), (
        f"PINNED_MODEL_REVISION should be a hex string (git SHA), got: "
        f"{PINNED_MODEL_REVISION!r}"
    )


# ---------------------------------------------------------------------------
# Corroborate path — the entailment branch
# ---------------------------------------------------------------------------


def test_high_confidence_entailment_is_corroborate(tmp_path):
    """High-confidence entailment classifies as CORROBORATE (not supersede)."""
    from learning_service.classifier import NliClassifier, Verdict

    premise = "Use dependency injection to improve testability."
    hypothesis = "Inject dependencies rather than instantiating them inline for easier testing."

    _seed_fixture(
        tmp_path, model=_MODEL, premise=premise, hypothesis=hypothesis,
        label="entailment", confidence=0.89,
    )

    clf = NliClassifier(
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )
    result = clf.classify(premise, hypothesis)

    assert result.verdict == Verdict.CORROBORATE
    assert not result.low_confidence


# ---------------------------------------------------------------------------
# classify_pair convenience function
# ---------------------------------------------------------------------------


def test_classify_pair_convenience_function(tmp_path):
    """classify_pair() is a one-shot shim over NliClassifier."""
    from learning_service.classifier import classify_pair, Verdict

    premise = "Write tests before implementation."
    hypothesis = "Implementation should precede test writing."

    _seed_fixture(
        tmp_path, model=_MODEL, premise=premise, hypothesis=hypothesis,
        label="contradiction", confidence=0.80,
    )

    result = classify_pair(
        premise,
        hypothesis,
        model=_MODEL,
        model_revision="abc1234def5678",
        confidence_threshold=0.65,
        nli_mode="replay",
        fixtures_dir=tmp_path,
    )

    assert result.verdict == Verdict.SUPERSEDE


def test_classify_pair_rejects_empty_revision(tmp_path):
    """classify_pair() propagates the model_revision guard."""
    from learning_service.classifier import classify_pair

    with pytest.raises(ValueError, match="non-empty model_revision"):
        classify_pair("A.", "B.", model_revision="")


# ---------------------------------------------------------------------------
# Slow tests (real model load) — opt in with pytest --run-slow
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_real_model_contradiction_is_supersede():
    """Real cross-encoder + real verdict: contradiction → supersede (slow, model load)."""
    from learning_service.classifier import NliClassifier, Verdict, PINNED_MODEL_REVISION

    clf = NliClassifier(
        model=_MODEL,
        model_revision=PINNED_MODEL_REVISION,
        confidence_threshold=0.65,
        nli_mode="passthrough",
    )
    result = clf.classify(
        "Always use snake_case for Python function names.",
        "Python function names must use camelCase, never snake_case.",
    )
    assert result.verdict == Verdict.SUPERSEDE, (
        f"Expected SUPERSEDE for opposite naming conventions, got {result.verdict!r}"
    )
