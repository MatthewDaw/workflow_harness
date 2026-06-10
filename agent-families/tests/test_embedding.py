"""Embedding service: prefix discipline, dim guard, pin assertions, real model.

Unit tests inject a fake encoder (prefix/serialization logic needs no model);
the `slow`-marked integration tests load the real pinned model and are the
determinism/prefix-separation half of U3's verification.
"""

import os
import subprocess
import sys

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
    ensure_pins,
)
from agent_families.store import Store

PINNED = EmbeddingConfig(model="nomic-ai/nomic-embed-text-v1.5", dim=768, device="cpu")


class FakeEncoder:
    """Records every encode() input and returns a fixed-dim vector."""

    def __init__(self, dim=768):
        self.dim = dim
        self.calls = []

    def encode(self, text):
        self.calls.append(text)
        return [0.25] * self.dim


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


def test_prefix_constants_match_nomic_contract():
    assert DOCUMENT_PREFIX == "search_document: "
    assert QUERY_PREFIX == "search_query: "
    assert CLUSTERING_PREFIX == "clustering: "  # reserved for Phase 3 splitting


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
