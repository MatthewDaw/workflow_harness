"""Judge bridge — wires agent-families' store-free judge runner into this service.

Imports and re-exports :func:`agent_families.judge.run_judge`,
:class:`agent_families.judge.JudgeResult`, and the shared schema constants so
the rest of the learning service uses a single, consistent import path.  The
underlying implementation lives in
``agent-families/src/agent_families/judge.py`` and is NOT duplicated here.

Explicitly NOT imported (SQLite-coupled, banned from the live loop):
  - agent_families.store
  - agent_families.vecindex
  - agent_families.derive_skills (offline training only)

Usage::

    from learning_service.judge import run_judge, RESOLVE_EDGE_SCHEMA

    result = run_judge(
        prompt="Is insight A consistent with insight B? ...",
        schema=RESOLVE_EDGE_SCHEMA,
        model="claude-sonnet-4-5",
        max_retries=2,
    )
    # result.output["outcome"]  in {"corroborate", "refine", "contradicts", "unrelated"}
"""

from __future__ import annotations

from agent_families.judge import (  # noqa: F401 — re-exported API
    ADMISSION_GATE_SCHEMA,
    GATE_OUTCOMES,
    JudgeError,
    JudgeFixtureMissing,
    JudgeResult,
    JudgeSchemaViolation,
    JudgeUnavailable,
    OUTCOMES,
    RESOLVE_EDGE_OUTCOMES,
    RESOLVE_EDGE_SCHEMA,
    preflight,
    request_hash,
    reset_preflight_cache,
    run_judge,
    write_fixture,
)

__all__ = [
    "ADMISSION_GATE_SCHEMA",
    "GATE_OUTCOMES",
    "JudgeError",
    "JudgeFixtureMissing",
    "JudgeResult",
    "JudgeSchemaViolation",
    "JudgeUnavailable",
    "OUTCOMES",
    "RESOLVE_EDGE_OUTCOMES",
    "RESOLVE_EDGE_SCHEMA",
    "preflight",
    "request_hash",
    "reset_preflight_cache",
    "run_judge",
    "write_fixture",
]
