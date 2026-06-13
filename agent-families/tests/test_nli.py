"""NLI seam tests — fully offline, zero quota, zero model download (R9).

The local cross-encoder is never loaded here: replay-mode tests hit committed JSON
fixtures, and the record/passthrough paths drive an injected fake encoder. The real
``CrossEncoder`` load is asserted NEVER called in replay.

Manual record-mode smoke (documented per U4 verification, NOT in CI): with the
``cross-encoder/nli-deberta-v3-base`` model cached locally (``af init`` pre-fetches
it), from agent-families/ run

    $env:AF_NLI_MODE = "record"
    $env:AF_NLI_FIXTURES = "tests/fixtures/nli"
    uv run python -c "from agent_families.nli import classify; \
        print(classify('The deploy script aborts when the lockfile is stale.', \
                       'The deploy script proceeds when the lockfile is stale.'))"

then inspect the refreshed fixture under tests/fixtures/nli/ and commit it
deliberately.

## Conformance (U4 required acceptance tests → invariants)

| Invariant (plan 008 U4) | Test |
|---|---|
| replay returns the fixtured verdict byte-identically with zero model load | `test_replay_byte_identical_zero_model_load` |
| hard-coded {contradiction:0,entailment:1,neutral:2} map asserted at LOAD; wrong order / wrong arity fails before any classify | `test_label_map_asserted_at_load` |
| request_hash == sha256(sorted-json(model, premise, hypothesis)); each field changes the hash | `test_request_hash_covers_model_premise_hypothesis` |
| replay against a missing fixture raises an error naming the exact request_hash | `test_missing_fixture_fails_naming_hash` |
| recorded fixtures carry real signal: a contradiction pair and a paraphrase pair classify distinctly | `test_contradiction_and_paraphrase_classify_distinctly` |
"""

from __future__ import annotations

import hashlib
import json
import math
import types
from pathlib import Path

import pytest

from agent_families import nli

GOOD_MAP = {0: "contradiction", 1: "entailment", 2: "neutral"}

COMMITTED_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "nli"

# The exact pairs recorded under tests/fixtures/nli/ (committed deliberately).
CONTRADICTION_PAIR = (
    "The deploy script aborts when the lockfile is stale.",
    "The deploy script proceeds when the lockfile is stale.",
)
PARAPHRASE_PAIR = (
    "A developer is pinning subprocess encoding to utf-8 on Windows.",
    "An engineer sets the subprocess encoding to utf-8 on Windows.",
)


class FakeEncoder:
    """A stand-in cross-encoder: a label head plus scripted per-pair logits.

    Records every ``predict`` call so a test can prove the model was never asked
    for a verdict when the label-map assertion should have failed at load.
    """

    def __init__(self, id2label, *, default_logits=None, logits_by_pair=None):
        self.config = types.SimpleNamespace(id2label=dict(id2label))
        self.default_logits = default_logits
        self.logits_by_pair = logits_by_pair or {}
        self.predict_calls: list = []

    def predict(self, pairs):
        self.predict_calls.append(list(pairs))
        out = []
        for pair in pairs:
            logits = self.logits_by_pair.get(tuple(pair), self.default_logits)
            out.append(list(logits))
        return out


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Clear the encoder cache and NLI env vars around every test."""
    nli.reset_encoder_cache()
    monkeypatch.delenv(nli.MODE_ENV, raising=False)
    monkeypatch.delenv(nli.FIXTURES_ENV, raising=False)
    yield
    nli.reset_encoder_cache()


# --- request hashing ----------------------------------------------------------


def test_request_hash_covers_model_premise_hypothesis():
    model, premise, hypothesis = "some-model", "a premise", "a hypothesis"
    expected = hashlib.sha256(
        json.dumps(
            {"hypothesis": hypothesis, "model": model, "premise": premise},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    base = nli.request_hash(model, premise, hypothesis)
    assert base == expected
    # Every component is part of the key — change any one and the fixture misses.
    assert nli.request_hash("other-model", premise, hypothesis) != base
    assert nli.request_hash(model, "other premise", hypothesis) != base
    assert nli.request_hash(model, premise, "other hypothesis") != base


def test_model_change_misses_fixture(tmp_path):
    env = {"label": "neutral", "confidence": 0.5, "logits": [0.0, 0.0, 1.0]}
    nli.write_fixture(tmp_path, "model-a", "p", "h", env)
    assert nli.classify("p", "h", model="model-a", fixtures_dir=tmp_path).label == "neutral"
    # A different model id is a different head → different hash → no fixture.
    with pytest.raises(nli.NliFixtureMissing):
        nli.classify("p", "h", model="model-b", fixtures_dir=tmp_path)


# --- replay mode --------------------------------------------------------------


def test_replay_byte_identical_zero_model_load(tmp_path, monkeypatch):
    # If the model-load seam is touched in replay, fail loudly.
    loaded = []
    monkeypatch.setattr(nli, "_load_encoder", lambda *a, **k: loaded.append(1))

    label, confidence = "contradiction", 0.9906115858614851
    nli.write_fixture(
        tmp_path,
        nli.DEFAULT_MODEL,
        "premise",
        "hypothesis",
        {"label": label, "confidence": confidence, "logits": [4.1, -1.8, -0.9]},
    )
    h = nli.request_hash(nli.DEFAULT_MODEL, "premise", "hypothesis")

    result = nli.classify("premise", "hypothesis", fixtures_dir=tmp_path)

    assert result == nli.NliResult(label=label, confidence=confidence, request_hash=h)
    # Byte-identical against what is on disk — verbatim, not recomputed.
    on_disk = json.loads((tmp_path / f"{h}.json").read_text(encoding="utf-8"))
    assert result.label == on_disk["envelope"]["label"]
    assert result.confidence == on_disk["envelope"]["confidence"]
    assert result.request_hash == on_disk["request_hash"]
    assert loaded == []  # the CrossEncoder load seam was never called


def test_replay_is_the_default_mode(tmp_path):
    # No mode arg, no env var: a missing fixture proves we replayed (never loaded).
    with pytest.raises(nli.NliFixtureMissing):
        nli.classify("p", "h", fixtures_dir=tmp_path)


def test_missing_fixture_fails_naming_hash(tmp_path):
    h = nli.request_hash(nli.DEFAULT_MODEL, "unrecorded", "pair")
    with pytest.raises(nli.NliFixtureMissing, match=h):
        nli.classify("unrecorded", "pair", fixtures_dir=tmp_path)


def test_invalid_mode_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv(nli.MODE_ENV, "nonsense")
    with pytest.raises(nli.NliError, match=nli.MODE_ENV):
        nli.classify("p", "h", fixtures_dir=tmp_path)


def test_mode_and_fixtures_resolve_from_env(tmp_path, monkeypatch):
    nli.write_fixture(
        tmp_path,
        nli.DEFAULT_MODEL,
        "ep",
        "eh",
        {"label": "neutral", "confidence": 0.6, "logits": [0.0, 0.0, 1.0]},
    )
    monkeypatch.setenv(nli.FIXTURES_ENV, str(tmp_path))
    result = nli.classify("ep", "eh")  # mode defaults to replay; dir from env
    assert result.label == "neutral"
    assert result.confidence == 0.6


def test_contradiction_and_paraphrase_classify_distinctly():
    contradiction = nli.classify(*CONTRADICTION_PAIR, fixtures_dir=COMMITTED_FIXTURES_DIR)
    paraphrase = nli.classify(*PARAPHRASE_PAIR, fixtures_dir=COMMITTED_FIXTURES_DIR)
    assert contradiction.label == "contradiction"
    assert paraphrase.label in ("entailment", "neutral")
    assert contradiction.label != paraphrase.label  # real signal, not a constant
    assert contradiction.confidence > 0.5
    assert paraphrase.confidence > 0.5


# --- label-map assertion at load ----------------------------------------------


def test_label_map_asserted_at_load():
    # A head with the SAME three labels but a reordered class index would silently
    # flip every verdict — fatal at load, before any predict.
    reordered = FakeEncoder(
        {0: "entailment", 1: "contradiction", 2: "neutral"},
        default_logits=[1.0, 2.0, 3.0],
    )
    with pytest.raises(nli.NliUnavailable, match="label head"):
        nli.preflight(nli.DEFAULT_MODEL, _encoder=reordered)
    assert reordered.predict_calls == []  # failed at LOAD, no verdict attempted

    # classify's record/passthrough path enforces the same load-time gate.
    with pytest.raises(nli.NliUnavailable):
        nli.classify("p", "h", mode="passthrough", _encoder=reordered)
    assert reordered.predict_calls == []

    # A 2-class head ("not exactly 3 logits") also fails at load.
    two_class = FakeEncoder(
        {0: "contradiction", 1: "entailment"}, default_logits=[1.0, 2.0]
    )
    with pytest.raises(nli.NliUnavailable):
        nli.preflight(nli.DEFAULT_MODEL, _encoder=two_class)
    assert two_class.predict_calls == []

    # The correct map passes load and reaches a verdict.
    good = FakeEncoder(GOOD_MAP, default_logits=[5.0, 0.0, 0.0])
    nli.preflight(nli.DEFAULT_MODEL, _encoder=good)  # no raise
    assert nli.classify("p", "h", mode="passthrough", _encoder=good).label == "contradiction"


def test_unverifiable_head_is_refused():
    # An encoder exposing no id2label cannot be verified — refuse, never guess.
    headless = types.SimpleNamespace(predict=lambda pairs: [[1.0, 2.0, 3.0]])
    with pytest.raises(nli.NliUnavailable, match="id2label"):
        nli.preflight(nli.DEFAULT_MODEL, _encoder=headless)


# --- passthrough / fake-encoder verdicts --------------------------------------


def test_injected_fake_encoder_drives_label_and_confidence():
    fake = FakeEncoder(GOOD_MAP, default_logits=[0.2, 5.0, 0.1])
    result = nli.classify("p", "h", mode="passthrough", _encoder=fake)
    assert result.label == "entailment"
    probs = nli._softmax([0.2, 5.0, 0.1])
    assert result.confidence == max(probs)  # softmax-max is the confidence
    assert math.isclose(sum(probs), 1.0)
    assert fake.predict_calls == [[("p", "h")]]
    assert result.request_hash == nli.request_hash(nli.DEFAULT_MODEL, "p", "h")


def test_wrong_logit_count_at_predict_is_unavailable():
    # 3-class head passes load, but a predict that returns 2 logits is a swap.
    fake = FakeEncoder(GOOD_MAP, default_logits=[1.0, 2.0])
    with pytest.raises(nli.NliUnavailable, match="expected exactly 3"):
        nli.classify("p", "h", mode="passthrough", _encoder=fake)


# --- record mode --------------------------------------------------------------


def test_record_mode_writes_fixture_then_replays(tmp_path):
    fake = FakeEncoder(GOOD_MAP, default_logits=[4.0, -1.0, -1.0])
    recorded = nli.classify(
        "rp", "rh", mode="record", fixtures_dir=tmp_path, _encoder=fake
    )
    h = nli.request_hash(nli.DEFAULT_MODEL, "rp", "rh")
    fixture_path = tmp_path / f"{h}.json"
    assert fixture_path.exists()
    assert b"\r\n" not in fixture_path.read_bytes()  # newline discipline: LF-only

    replayed = nli.classify("rp", "rh", mode="replay", fixtures_dir=tmp_path)
    assert replayed == recorded
    assert len(fake.predict_calls) == 1  # replay added no model call
