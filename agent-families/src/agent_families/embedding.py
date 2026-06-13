"""Embedding service: pinned local model, literal task prefixes, pin assertions.

`nomic-ai/nomic-embed-text-v1.5` through sentence-transformers' ONNX backend with
CPUExecutionProvider pinned (DESIGN §13, KTD): CPU ONNX output is reproducible
enough for record/replay fixture stability across machines. Task prefixes are
prepended literally by call-site intent. Under R3 (plan 008) the prefixes are all
load-bearing:

  * ``clustering:`` — the **key** and **full** vectors that decide an insight's
    identity in the derive graph (``embed_key`` / ``embed_full``). The key vector
    is computed over precondition+action only so that a rule and its negation
    collide on the same identity instead of hiding behind whole-atom cosine.
  * ``search_document:`` — the **retrieval** vector indexed for search
    (``embed_retrieval``; the former ``embed_document``).
  * ``search_query:`` — an incoming query at routing time (``embed_query``).

Startup discipline (R22): the embedding model and *effective* dimension are pinned
in the store's meta table; :func:`ensure_pins` asserts they match thresholds.toml
before any embedding work. With Matryoshka truncation enabled the effective dim is
the truncated dim, not the model's native dim. Cosine thresholds are not portable
across models or dims, so a pin mismatch is a deliberate-migration error, never
silently absorbed.

First-run model download (~0.5 GB, honors ``HF_HOME``) is owned by ``af init``
(U9); a missing model surfaces the documented fix instead of a raw HF traceback.
"""

from __future__ import annotations

import math

from agent_families.config import EmbeddingConfig

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
# Load-bearing under R3: the identity prefix for key/full clustering vectors.
CLUSTERING_PREFIX = "clustering: "

META_MODEL_KEY = "embedding_model"
META_DIM_KEY = "embedding_dim"

_ONNX_PROVIDER = "CPUExecutionProvider"


class EmbeddingError(Exception):
    """Raised on model-load failure, dimension drift, or pin mismatch."""


def build_key_text(precondition: str, action: str) -> str:
    """Deterministic key text for the identity (key) vector: precond+action only.

    The key vector exists so a rule and its negation collide on the rule's
    identity. Expected-outcome, rationale, and negative-scope are deliberately
    excluded — two atoms with the same precondition+action but opposite outcomes
    must produce the *same* key vector so NLI (not cosine) renders the verdict.
    """
    return f"{precondition.strip()} -> {action.strip()}"


def _effective_dim(config: EmbeddingConfig) -> int:
    """The pinned/asserted dim: the Matryoshka-truncated dim when configured."""
    matryoshka = getattr(config, "matryoshka_dim", None)
    return matryoshka if matryoshka else config.dim


def _l2_normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0.0:
        return vector
    return [x / norm for x in vector]


class EmbeddingService:
    """Lazy-loading encoder with the prefix discipline baked into its API.

    ``encoder`` is injectable for unit tests; production call sites construct
    from config alone and the real model loads on first use.

    When ``config.matryoshka_dim`` is set the raw model vector is truncated to that
    many leading dimensions and L2-renormalized (Matryoshka representation, DESIGN
    §13); the *effective* dim — what every vector is checked and pinned against — is
    the truncated dim.
    """

    def __init__(self, config: EmbeddingConfig, encoder=None) -> None:
        self.model_name = config.model
        self.dim = config.dim
        self.device = config.device
        self.matryoshka_dim = getattr(config, "matryoshka_dim", None)
        self.effective_dim = _effective_dim(config)
        self._encoder = encoder

    def ensure_ready(self) -> None:
        """Load (downloading if needed) without embedding anything; used by init."""
        self._load()

    def embed_key(self, precondition: str, action: str) -> list[float]:
        """Embed the identity (key) vector over precond+action (`clustering:`)."""
        return self._embed(CLUSTERING_PREFIX, build_key_text(precondition, action))

    def embed_full(self, text: str) -> list[float]:
        """Embed the whole atom for clustering (`clustering:` prefix)."""
        return self._embed(CLUSTERING_PREFIX, text)

    def embed_retrieval(self, text: str) -> list[float]:
        """Embed library content for the retrieval index (`search_document:`)."""
        return self._embed(DOCUMENT_PREFIX, text)

    def embed_document(self, text: str) -> list[float]:
        """Back-compat alias for :meth:`embed_retrieval`.

        The standalone retrieval/key/full call-site split lives in U6 (add_idea)
        and U-tripwires migrations, which are outside this unit's file scope; this
        alias keeps existing indexers green until those call sites migrate.
        """
        return self.embed_retrieval(text)

    def embed_query(self, text: str) -> list[float]:
        """Embed an incoming idea for routing/dedup (`search_query:` prefix)."""
        return self._embed(QUERY_PREFIX, text)

    # --- internals -----------------------------------------------------------

    def _load(self):
        if self._encoder is not None:
            return self._encoder
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise EmbeddingError(
                "sentence-transformers is not installed; run `uv sync` in"
                " agent-families/."
            ) from exc
        try:
            self._encoder = SentenceTransformer(
                self.model_name,
                backend="onnx",
                device=self.device,
                # Not required since transformers >= 5.5; belt-and-suspenders (KTD).
                trust_remote_code=True,
                model_kwargs={"provider": _ONNX_PROVIDER},
            )
        except Exception as exc:
            raise EmbeddingError(
                f"could not load embedding model '{self.model_name}': {exc}\n"
                "If this is a cache miss while offline (HF_HUB_OFFLINE=1), the model"
                " must be pre-fetched first: unset HF_HUB_OFFLINE and run `af init`"
                " once with network access (~0.5 GB download, honors HF_HOME)."
            ) from exc
        return self._encoder

    def _embed(self, prefix: str, text: str) -> list[float]:
        encoder = self._load()
        raw = encoder.encode(prefix + text)
        vector = [float(x) for x in raw]
        # Matryoshka: truncate to the leading dims, THEN L2-renormalize, before the
        # effective-dim guard. Renorm after truncation keeps the truncated vector
        # unit-norm (renorm before would leave the slice sub-unit).
        if self.matryoshka_dim:
            vector = _l2_normalize(vector[: self.matryoshka_dim])
        if len(vector) != self.effective_dim:
            raise EmbeddingError(
                f"model '{self.model_name}' returned a {len(vector)}-dim vector but"
                f" config pins the effective dim = {self.effective_dim}. The pinned"
                " dim changes only on a full re-embed migration (thresholds.toml"
                " [embedding] provenance)."
            )
        return vector


def ensure_pins(store, config: EmbeddingConfig) -> None:
    """Assert the store's embedding pins match config, writing them on first use (R22).

    The dim pinned is the *effective* dim (Matryoshka-truncated when configured),
    so a DB built at one effective dim cannot be reopened against another.

    A mismatch is fatal with migration guidance: cosine thresholds were calibrated
    against the pinned model, so an embedder/dim change re-embeds every insight and
    recalibrates every cosine threshold (DESIGN §13) — it never happens implicitly.
    """
    effective_dim = _effective_dim(config)
    stored_model = store.get_meta(META_MODEL_KEY)
    stored_dim = store.get_meta(META_DIM_KEY)
    if stored_model is None and stored_dim is None:
        store.set_meta(META_MODEL_KEY, config.model)
        store.set_meta(META_DIM_KEY, str(effective_dim))
        return
    mismatches = []
    if stored_model != config.model:
        mismatches.append(
            f"  model: library is pinned to '{stored_model}',"
            f" thresholds.toml says '{config.model}'"
        )
    if stored_dim != str(effective_dim):
        mismatches.append(
            f"  dim: library is pinned to {stored_dim},"
            f" thresholds.toml says {effective_dim}"
        )
    if mismatches:
        raise EmbeddingError(
            "embedding pin mismatch between the library database and thresholds.toml:\n"
            + "\n".join(mismatches)
            + "\nChanging the embedder or dimension is a deliberate migration: it"
            " re-embeds every insight and recalibrates every cosine threshold"
            " against the hand-labeled pair set (DESIGN §13). Either restore"
            " thresholds.toml to the stored pins or perform that migration."
        )
