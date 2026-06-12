"""NLI bridge — wires agent-families' store-free NLI classifier into this service.

Imports and re-exports :func:`agent_families.nli.classify` and
:class:`agent_families.nli.NliResult` so the rest of the learning service uses
a single, consistent import path.  The underlying implementation lives in
``agent-families/src/agent_families/nli.py`` and is NOT duplicated here.

Explicitly NOT imported (SQLite-coupled, banned from the live loop):
  - agent_families.store
  - agent_families.vecindex
  - agent_families.derive_skills (offline training only)

Usage::

    from learning_service.nli import classify, NliResult

    result = classify("Use snake_case for identifiers.", "Use camelCase for variables.")
    # result.label  in {"contradiction", "entailment", "neutral"}
    # result.confidence  float
"""

from __future__ import annotations

# --- import the store-free core only -------------------------------------------

from agent_families.nli import (  # noqa: F401 — re-exported API
    NliError,
    NliFixtureMissing,
    NliResult,
    NliUnavailable,
    classify,
    preflight,
    request_hash,
    reset_encoder_cache,
    write_fixture,
)

__all__ = [
    "NliError",
    "NliFixtureMissing",
    "NliResult",
    "NliUnavailable",
    "classify",
    "preflight",
    "request_hash",
    "reset_encoder_cache",
    "write_fixture",
]
