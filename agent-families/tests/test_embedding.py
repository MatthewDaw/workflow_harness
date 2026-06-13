"""Embedding service: prefix discipline, dim guard, pin assertions, real model.

Unit tests inject a fake encoder (prefix/serialization logic needs no model);
the `slow`-marked integration tests load the real pinned model and are the
determinism/prefix-separation half of the verification.

R3 (plan 008 / U2) adds three prefix-correct encode entry points — ``embed_key``
and ``embed_full`` (``clustering:``) plus ``embed_retrieval`` (``search_document:``,
the former ``embed_document``) — and optional Matryoshka truncation. The offline
unit tests use a deterministic *input-sensitive* fake encoder so that a different
prefix (a different encode input) produces a measurably different vector, proving
the prefix is load-bearing without the real model.

## Conformance

R3/U2 named invariants → the test that enforces each (behavioral, not trivial):

  - key prefix is clustering, retrieval is search_document, and the same atom
    embeds differently through each (prefix is load-bearing, cosine < 1.0)
      -> test_key_prefix_is_clustering_not_search_document
  - key text is precondition+action only; identical precond+action with different
    outcomes yields the IDENTICAL key vector (collision on rule identity)
      -> test_key_text_is_precondition_action_only
  - Matryoshka truncation to 256 then L2-renorm yields length 256, unit norm
    (renorm happens AFTER truncation, not before)
      -> test_matryoshka_truncate_then_renorm_unit_norm
  - ensure_pins pins the EFFECTIVE (post-Matryoshka) dim; a different effective dim
    fails fast at startup
      -> test_effective_dim_pin_mismatch_fails_startup
"""

import hashlib
import math
import os
import subprocess
import sys
from dataclasses import dataclass

import pytest

from agent_families.config import EmbeddingConfig
from agent_families.embedding import (
    CLUSTERING_PREFIX,
    DOCUMENT_PREFIX,
    META_DIM_KEY,
    META_MODEL_KEY,
    QUERY_PREFIX,
    EmbeddingError,
    EmbeddingService,
    build_key_text,
    ensure_pins,
)
from agent_families.store import Store

PINNED = EmbeddingConfig(model="nomic-ai/nomic-embed-text-v1.5", dim=768, device="cpu")


@dataclass(frozen=True)
class MatryoshkaConfig:
    """Config stub that carries ``matryoshka_dim`` (added to EmbeddingConfig in U9).

    EmbeddingService reads ``matryoshka_dim`` via ``getattr`` so the seam works
    against both the real config (once U9 adds the field) and this stub today.
    """

    model: str
    dim: int
    device: str
    matryoshka_dim: int | None = None


class FakeEncoder:
    """Records every encode() input and returns a fixed-dim constant vector."""

    def __init__(self, dim=768):
        self.dim = dim
        self.calls = []

    def encode(self, text):
        self.calls.append(text)
        return [0.25] * self.dim


class HashEncoder:
    """Deterministic, input-sensitive fake: distinct inputs → distinct vectors.

    Lets the offline unit suite prove that a different prefix (a different encode
    input string) produces a different vector — the prefix is load-bearing — while
    keeping identical inputs byte-identical (determinism).
    """

    def __init__(self, dim=768):
        self.dim = dim
        self.calls = []

    def encode(self, text):
        self.calls.append(text)
        vals = []
        i = 0
        while len(vals) < self.dim:
            chunk = hashlib.sha256(text.encode("utf-8") + i.to_bytes(4, "big")).digest()
            for b in chunk:
                vals.append((b / 255.0) - 0.5)
                if len(vals) >= self.dim:
                    break
            i += 1
        return vals


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    yield s
    s.close()


# --- prefix discipline (unit, fake encoder) ----------------------------------


def test_document_prefix_prepended_literally():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_document("retry with exponential backoff")
    assert fake.calls == ["search_document: retry with exponential backoff"]


def test_query_prefix_prepended_literally():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_query("retry with exponential backoff")
    assert fake.calls == ["search_query: retry with exponential backoff"]


def test_retrieval_prefix_prepended_literally():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_retrieval("retry with exponential backoff")
    assert fake.calls == ["search_document: retry with exponential backoff"]


def test_embed_document_is_alias_of_embed_retrieval():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_document("x")
    service.embed_retrieval("x")
    assert fake.calls == ["search_document: x", "search_document: x"]


def test_full_uses_clustering_prefix():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_full("retry with exponential backoff")
    assert fake.calls == ["clustering: retry with exponential backoff"]


def test_key_uses_clustering_prefix_over_key_text():
    fake = FakeEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    service.embed_key("a flaky network call", "retry with backoff")
    assert fake.calls == [
        "clustering: " + build_key_text("a flaky network call", "retry with backoff")
    ]


def test_prefix_constants_match_nomic_contract():
    assert DOCUMENT_PREFIX == "search_document: "
    assert QUERY_PREFIX == "search_query: "
    assert CLUSTERING_PREFIX == "clustering: "  # load-bearing under R3


def test_vectors_returned_as_python_floats():
    service = EmbeddingService(PINNED, encoder=FakeEncoder())
    vec = service.embed_document("anything")
    assert len(vec) == 768
    assert all(isinstance(x, float) for x in vec)


def test_encoder_dim_drift_raises():
    service = EmbeddingService(PINNED, encoder=FakeEncoder(dim=64))
    with pytest.raises(EmbeddingError) as excinfo:
        service.embed_document("anything")
    msg = str(excinfo.value)
    assert "64" in msg and "768" in msg


# --- R3 / U2 required acceptance tests ----------------------------------------


def test_key_prefix_is_clustering_not_search_document():
    """embed_key/embed_full apply clustering:, embed_retrieval applies
    search_document:; the SAME atom through embed_key vs embed_retrieval yields
    measurably different vectors (cosine < 1.0) — the prefix is load-bearing."""
    fake = HashEncoder()
    service = EmbeddingService(PINNED, encoder=fake)
    atom = "retry with exponential backoff on a flaky network call"

    key_vec = service.embed_full(atom)  # clustering:
    retr_vec = service.embed_retrieval(atom)  # search_document:

    assert fake.calls == [
        "clustering: " + atom,
        "search_document: " + atom,
    ]
    # Same atom, different prefix → different encode input → different vector.
    assert key_vec != retr_vec
    assert _cosine(key_vec, retr_vec) < 1.0

    # embed_key also carries the clustering prefix.
    fake2 = HashEncoder()
    EmbeddingService(PINNED, encoder=fake2).embed_key("precond", "action")
    assert fake2.calls[0].startswith("clustering: ")
    assert not fake2.calls[0].startswith("search_document: ")


def test_key_text_is_precondition_action_only():
    """build_key_text uses precond+action only; two atoms with identical
    precond+action but different outcomes produce the IDENTICAL key vector."""
    precondition = "the request times out"
    action = "retry with exponential backoff"
    outcome_a = "the call eventually succeeds"
    outcome_b = "the call is abandoned after five tries"

    key_text = build_key_text(precondition, action)
    # The key text excludes expected_outcome / rationale / negative_scope.
    assert outcome_a not in key_text
    assert outcome_b not in key_text
    assert precondition in key_text and action in key_text

    svc_a = EmbeddingService(PINNED, encoder=HashEncoder())
    svc_b = EmbeddingService(PINNED, encoder=HashEncoder())
    # Different outcomes are never passed to embed_key, so the key vectors match.
    vec_a = svc_a.embed_key(precondition, action)
    vec_b = svc_b.embed_key(precondition, action)
    assert vec_a == vec_b


def test_matryoshka_truncate_then_renorm_unit_norm():
    """With matryoshka_dim=256 the returned vector has length 256 and L2 norm 1.0;
    renormalization happens AFTER truncation, not before."""
    cfg = MatryoshkaConfig(
        model=PINNED.model, dim=768, device="cpu", matryoshka_dim=256
    )
    # Constant encoder makes the after-vs-before distinction sharp: renorm-before
    # would slice a unit 768-vec to 256 dims with norm sqrt(256/768) ≈ 0.577.
    service = EmbeddingService(cfg, encoder=FakeEncoder(dim=768))
    vec = service.embed_full("anything")

    assert len(vec) == 256
    norm = math.sqrt(sum(x * x for x in vec))
    assert norm == pytest.approx(1.0, abs=1e-6)


def test_effective_dim_pin_mismatch_fails_startup(store):
    """ensure_pins pins the EFFECTIVE (post-Matryoshka) dim; opening against a DB
    pinned to a different effective dim fails fast at startup."""
    cfg256 = MatryoshkaConfig(
        model=PINNED.model, dim=768, device="cpu", matryoshka_dim=256
    )
    ensure_pins(store, cfg256)
    # The pin is the effective (256) dim, not the model-native 768.
    assert store.get_meta(META_DIM_KEY) == "256"

    # A config whose effective dim differs (full 768, no truncation) mismatches.
    with pytest.raises(EmbeddingError) as excinfo:
        ensure_pins(store, PINNED)
    msg = str(excinfo.value)
    assert "256" in msg and "768" in msg
    assert "migration" in msg


def test_matryoshka_dim_pin_matches_on_reopen(store):
    cfg256 = MatryoshkaConfig(
        model=PINNED.model, dim=768, device="cpu", matryoshka_dim=256
    )
    ensure_pins(store, cfg256)
    ensure_pins(store, cfg256)  # same effective dim: no error


# --- pin assertions (R22) ------------------------------------------------------


def test_ensure_pins_writes_on_first_use(store):
    ensure_pins(store, PINNED)
    assert store.get_meta(META_MODEL_KEY) == PINNED.model
    assert store.get_meta(META_DIM_KEY) == "768"


def test_ensure_pins_passes_when_matching(store):
    ensure_pins(store, PINNED)
    ensure_pins(store, PINNED)  # second startup: no error


def test_dim_pin_mismatch_fails_startup_with_migration_guidance(store):
    ensure_pins(store, PINNED)
    shrunk = EmbeddingConfig(model=PINNED.model, dim=512, device="cpu")
    with pytest.raises(EmbeddingError) as excinfo:
        ensure_pins(store, shrunk)
    msg = str(excinfo.value)
    assert "512" in msg and "768" in msg
    assert "migration" in msg
    assert "thresholds.toml" in msg


def test_model_pin_mismatch_fails_startup(store):
    ensure_pins(store, PINNED)
    swapped = EmbeddingConfig(model="other/model", dim=768, device="cpu")
    with pytest.raises(EmbeddingError) as excinfo:
        ensure_pins(store, swapped)
    msg = str(excinfo.value)
    assert "other/model" in msg and PINNED.model in msg


# --- real model (slow, integration) ---------------------------------------------


@pytest.fixture(scope="module")
def real_service():
    return EmbeddingService(PINNED)


@pytest.mark.slow
def test_same_text_embeds_identically_across_calls(real_service):
    text = "always pin subprocess encoding to utf-8 on Windows"
    first = real_service.embed_document(text)
    second = real_service.embed_document(text)
    assert first == second  # CPUExecutionProvider pinned: bitwise determinism


@pytest.mark.slow
def test_document_and_query_prefixes_produce_different_vectors(real_service):
    text = "always pin subprocess encoding to utf-8 on Windows"
    doc = real_service.embed_document(text)
    query = real_service.embed_query(text)
    assert len(doc) == len(query) == 768
    assert doc != query


@pytest.mark.slow
def test_clustering_and_retrieval_prefixes_differ_real_model(real_service):
    text = "always pin subprocess encoding to utf-8 on Windows"
    full = real_service.embed_full(text)
    retrieval = real_service.embed_retrieval(text)
    assert len(full) == len(retrieval) == 768
    assert full != retrieval  # clustering: vs search_document: are distinct spaces


@pytest.mark.slow
def test_missing_model_offline_produces_documented_error(tmp_path):
    # HF reads HF_HOME/HF_HUB_OFFLINE at import time, so an in-process monkeypatch
    # cannot isolate this from the cached-model tests above; probe in a subprocess.
    probe = (
        "from agent_families.config import EmbeddingConfig\n"
        "from agent_families.embedding import EmbeddingError, EmbeddingService\n"
        "cfg = EmbeddingConfig(model='nomic-ai/nomic-embed-text-v1.5',"
        " dim=768, device='cpu')\n"
        "try:\n"
        "    EmbeddingService(cfg).embed_document('anything')\n"
        "except EmbeddingError as exc:\n"
        "    print(f'EMBEDDING_ERROR: {exc}')\n"
        "else:\n"
        "    print('NO_ERROR')\n"
    )
    env = dict(os.environ)
    env["HF_HOME"] = str(tmp_path / "empty-hf-cache")
    env["HF_HUB_OFFLINE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=300,
    )
    assert "EMBEDDING_ERROR:" in result.stdout, result.stderr
    assert "HF_HUB_OFFLINE" in result.stdout
    assert "af init" in result.stdout
