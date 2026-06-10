"""Embedding service: pinned local model, literal task prefixes, pin assertions.

`nomic-ai/nomic-embed-text-v1.5` through sentence-transformers' ONNX backend with
CPUExecutionProvider pinned (DESIGN §13, KTD): CPU ONNX output is reproducible
enough for record/replay fixture stability across machines. Task prefixes are
prepended literally by call-site intent — `search_document:` at index time,
`search_query:` at routing time; `clustering:` is reserved for Phase 3 skill
splitting (constant only, no Phase 0 caller).

Startup discipline (R22): the embedding model and dimension are pinned in the
store's meta table; :func:`ensure_pins` asserts they match thresholds.toml before
any embedding work. Cosine thresholds are not portable across models, so a pin
mismatch is a deliberate-migration error, never silently absorbed.

First-run model download (~0.5 GB, honors ``HF_HOME``) is owned by ``af init``
(U9); a missing model surfaces the documented fix instead of a raw HF traceback.
"""

from __future__ import annotations

from agent_families.config import EmbeddingConfig

DOCUMENT_PREFIX = "search_document: "
QUERY_PREFIX = "search_query: "
# Reserved for Phase 3 skill splitting (DESIGN §13); nothing in Phase 0 uses it.
CLUSTERING_PREFIX = "clustering: "

META_MODEL_KEY = "embedding_model"
META_DIM_KEY = "embedding_dim"

_ONNX_PROVIDER = "CPUExecutionProvider"


class EmbeddingError(Exception):
    """Raised on model-load failure, dimension drift, or pin mismatch."""


class EmbeddingService:
    """Lazy-loading encoder with the prefix discipline baked into its API.

    ``encoder`` is injectable for unit tests; production call sites construct
    from config alone and the real model loads on first use.
    """

    def __init__(self, config: EmbeddingConfig, encoder=None) -> None:
        self.model_name = config.model
        self.dim = config.dim
        self.device = config.device
        self._encoder = encoder

    def ensure_ready(self) -> None:
        """Load (downloading if needed) without embedding anything; used by init."""
        self._load()

    def embed_document(self, text: str) -> list[float]:
        """Embed library content for indexing (`search_document:` prefix)."""
        return self._embed(DOCUMENT_PREFIX, text)

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
        if len(vector) != self.dim:
            raise EmbeddingError(
                f"model '{self.model_name}' returned a {len(vector)}-dim vector but"
                f" config pins dim = {self.dim}. The pinned dim changes only on a"
                " full re-embed migration (thresholds.toml [embedding] provenance)."
            )
        return vector


def ensure_pins(store, config: EmbeddingConfig) -> None:
    """Assert the store's embedding pins match config, writing them on first use (R22).

    A mismatch is fatal with migration guidance: cosine thresholds were calibrated
    against the pinned model, so an embedder/dim change re-embeds every insight and
    recalibrates every cosine threshold (DESIGN §13) — it never happens implicitly.
    """
    stored_model = store.get_meta(META_MODEL_KEY)
    stored_dim = store.get_meta(META_DIM_KEY)
    if stored_model is None and stored_dim is None:
        store.set_meta(META_MODEL_KEY, config.model)
        store.set_meta(META_DIM_KEY, str(config.dim))
        return
    mismatches = []
    if stored_model != config.model:
        mismatches.append(
            f"  model: library is pinned to '{stored_model}',"
            f" thresholds.toml says '{config.model}'"
        )
    if stored_dim != str(config.dim):
        mismatches.append(
            f"  dim: library is pinned to {stored_dim},"
            f" thresholds.toml says {config.dim}"
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
