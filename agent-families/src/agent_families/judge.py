"""Judge runner: the single seam owning every headless ``claude -p`` call (R6/R22/R23).

All judge traffic flows through :func:`run_judge`, which is fully testable offline
via a record/replay fixture cache:

- ``replay`` (default): responses come from JSON fixtures keyed by request hash;
  a missing fixture is a hard failure naming the hash. Zero subprocess calls,
  zero quota — this is what the offline test suite runs on.
- ``record``: the real CLI is invoked and the full response envelope is written
  to a fixture for deliberate refresh.
- ``passthrough``: the real CLI is invoked with no fixture interaction.

The mode comes from the ``AF_JUDGE_MODE`` env var (or an explicit argument); the
fixture directory from ``AF_JUDGE_FIXTURES``. Fixture keys are
``sha256(sorted-json(prompt, schema, model))``, so judge prompts must carry no
volatile data — no timestamps, no absolute paths, no full-precision floats (R23).

Invocation discipline (KTD): content via stdin, schema as a single argv element,
``--tools ""``, utf-8 everywhere, and on Windows the npm ``.cmd`` shim is resolved
to ``node.exe`` + the CLI entry script so argv never passes through cmd.exe
re-parsing (quote/%/^ mangling, ~8K argv cap).

Preflight (R22): cheap, quota-free — the executable must resolve and answer
``--version``; cached per process. Subscription-auth failures cannot be probed
for free, so they surface on the first real call via the envelope's
``is_error``/``subtype``, which this module raises as :class:`JudgeUnavailable`.

Manual record-mode smoke (documented, not in CI): with the claude CLI installed
and logged in on the subscription, set ``AF_JUDGE_MODE=record`` and
``AF_JUDGE_FIXTURES=tests/fixtures/judge``, call :func:`run_judge` once with a
real prompt/schema, inspect the new fixture, and commit it deliberately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

MODE_ENV = "AF_JUDGE_MODE"
FIXTURES_ENV = "AF_JUDGE_FIXTURES"
MODES = ("replay", "record", "passthrough")
DEFAULT_FIXTURES_DIR = Path("tests") / "fixtures" / "judge"

# The closed judge outcome enum (R6). Every judge schema's `outcome` field draws
# from this; per-call-type allowed SUBSETS are enforced by the caller (pipeline)
# through `extra_validate`, riding the same retry-then-fail path as a schema
# violation.
OUTCOMES = (
    "append_to_skill",
    "new_skill",
    "merge_discard",
    "contradiction_flag",
    "contradiction_supersede",
    "lint_reject",
    "rewrite_proposed",
    "no_placement",
)

# The admission gate (Operation 1) verdict enum (R10/R14 gate half). The gate is
# the standalone FRONT stage of the R3 ingest path: it turns messy raw input into
# one transferable schema'd atom (precondition/action/expected_outcome/rationale/
# negative_scope) or rejects/rewrites it — before any embedding, so the key vector
# is computed on the generalized text. These three verdicts are a gate-scoped
# subset; the per-call validator (pipeline.gate_outcome_validator) enforces the
# per-verdict field requirements.
GATE_OUTCOMES = ("admit", "lint_reject", "rewrite_proposed")

# The generalized atom Operation 1 emits (or proposes, on rewrite). negative_scope
# ("when NOT to apply", §5 Op.1) is mandatory; rationale ("because Z") is optional
# (KTD: adopted now as a nullable field). Required only when `atom` is present —
# a lint_reject carries no atom.
_GATE_ATOM_SCHEMA = {
    "type": "object",
    "properties": {
        "precondition": {"type": "string"},
        "action": {"type": "string"},
        "expected_outcome": {"type": "string"},
        "rationale": {"type": "string"},
        "negative_scope": {"type": "string"},
    },
    "required": ["precondition", "action", "expected_outcome", "negative_scope"],
    "additionalProperties": False,
}

# The admission-gate judge contract. `atom` is absent on a lint_reject (enforced
# application-side via gate_outcome_validator), so it is not top-level required.
ADMISSION_GATE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(GATE_OUTCOMES)},
        "atom": _GATE_ATOM_SCHEMA,
        "scope_tag": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["value", "justification"],
            "additionalProperties": False,
        },
        "reason": {"type": "string"},
    },
    "required": ["outcome", "scope_tag"],
    "additionalProperties": False,
}

# The NLI-fallback edge-resolution verdict enum (R12/R14, Operation 2). Local NLI
# (nli.py) renders the verdict for each key-collision candidate; only when its
# confidence is below threshold does the LLM judge run this Graphiti-style
# resolve_edge prompt as the FALLBACK. The four moves mirror the NLI labels plus
# an explicit `unrelated` (the key collided but the rules are independent):
#   corroborate <- entailment   contradicts <- contradiction
#   refine      <- same-key nuance (neutral)   unrelated <- different rule
# This is a SEPARATE, additive verdict surface from OUTCOMES: the R3 ingest
# gauntlet (plan 008 U6) classifies *edges*, it does not place skills. The legacy
# placement/merge OUTCOMES surface above is demoted (kept intact for the not-yet-
# migrated Phase-0 callers; the cut-over rides plans 008 U8/U9 — see the 008 U6
# Deviations note in PROGRESS.md).
RESOLVE_EDGE_OUTCOMES = ("corroborate", "refine", "contradicts", "unrelated")

RESOLVE_EDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(RESOLVE_EDGE_OUTCOMES)},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["outcome"],
    "additionalProperties": False,
}

_NPM_ENTRY_RELPATH = Path("node_modules") / "@anthropic-ai" / "claude-code" / "cli.js"
_SHIM_SUFFIXES = {".cmd", ".bat", ".ps1"}

_INSTALL_HINT = (
    "Install it with `npm install -g @anthropic-ai/claude-code`, then run `claude`"
    " once to log in with the subscription (no API key — subscription auth only)."
)


class JudgeError(Exception):
    """Base for every judge-runner failure."""


class JudgeUnavailable(JudgeError):
    """The claude CLI is missing, broken, or returned an error envelope."""


class JudgeSchemaViolation(JudgeError):
    """The judge's structured output still violated the schema after all retries."""


class JudgeFixtureMissing(JudgeError):
    """Replay mode found no recorded fixture for the request hash."""


@dataclass(frozen=True)
class JudgeResult:
    """A schema-valid judge response plus the envelope bookkeeping around it."""

    output: dict
    request_hash: str
    attempts: int
    cost_usd: float | None
    duration_ms: int | None
    envelope: dict


# --- request identity and fixtures ------------------------------------------


def request_hash(prompt: str, schema: dict, model: str) -> str:
    """Stable fixture key: sha256 over canonical JSON of (prompt, schema, model)."""
    canonical = json.dumps(
        {"model": model, "prompt": prompt, "schema": schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_fixture(
    fixtures_dir: Path, prompt: str, schema: dict, model: str, envelope: dict
) -> Path:
    """Persist a full response envelope under its request hash; returns the path.

    Written with sorted keys and ``\\n`` newlines so committed fixtures are
    byte-stable across platforms.
    """
    fixtures_dir = Path(fixtures_dir)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    h = request_hash(prompt, schema, model)
    path = fixtures_dir / f"{h}.json"
    body = json.dumps(
        {
            "request_hash": h,
            "request": {"model": model, "prompt": prompt, "schema": schema},
            "envelope": envelope,
        },
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    path.write_text(body + "\n", encoding="utf-8", newline="\n")
    return path


def _load_fixture(fixtures_dir: Path, h: str) -> dict:
    path = Path(fixtures_dir) / f"{h}.json"
    if not path.exists():
        raise JudgeFixtureMissing(
            f"no recorded judge fixture for request hash {h} in {fixtures_dir}"
            f" (mode=replay). Refresh fixtures deliberately with {MODE_ENV}=record."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["envelope"]


# --- executable resolution and preflight -------------------------------------


def resolve_claude_argv() -> list[str]:
    """Resolve the claude CLI to an argv prefix, bypassing cmd.exe where possible.

    On Windows the npm global install puts a ``claude.cmd`` shim on PATH; running
    it routes argv through cmd.exe re-parsing. When the shim's sibling
    ``node_modules`` entry script and ``node`` are both findable, invoke node
    directly; otherwise fall back to the shim.
    """
    path = shutil.which("claude")
    if path is None:
        raise JudgeUnavailable(
            "claude CLI not found on PATH; the judge cannot run. " + _INSTALL_HINT
        )
    resolved = Path(path)
    if resolved.suffix.lower() in _SHIM_SUFFIXES:
        entry = resolved.parent / _NPM_ENTRY_RELPATH
        node = shutil.which("node")
        if node is not None and entry.exists():
            return [node, str(entry)]
        logger.warning(
            "claude resolved to npm shim %s but node-entrypoint resolution failed;"
            " falling back to the shim (cmd.exe argv re-parsing risk)",
            resolved,
        )
    return [str(resolved)]


_preflight_ok = False


def preflight() -> None:
    """Verify the claude CLI is present and answers ``--version``; cached per process.

    Quota-free by design — auth problems surface on the first real call as an
    ``is_error`` envelope (raised as :class:`JudgeUnavailable` with its subtype).
    """
    global _preflight_ok
    if _preflight_ok:
        return
    argv = resolve_claude_argv()
    try:
        proc = subprocess.run(
            argv + ["--version"], capture_output=True, encoding="utf-8"
        )
    except OSError as exc:
        raise JudgeUnavailable(
            f"claude CLI at {argv[0]} could not be executed: {exc}. " + _INSTALL_HINT
        ) from exc
    if proc.returncode != 0:
        raise JudgeUnavailable(
            f"`claude --version` exited {proc.returncode}:"
            f" {(proc.stderr or '').strip()}. " + _INSTALL_HINT
        )
    _preflight_ok = True


def reset_preflight_cache() -> None:
    """Forget the cached preflight success (test hook)."""
    global _preflight_ok
    _preflight_ok = False


# --- minimal JSON-schema validation ------------------------------------------


def validate_against_schema(instance, schema: dict, path: str = "$") -> str | None:
    """Return the first violation message, or None if ``instance`` conforms.

    Hand-rolled for the subset the judge contract uses — type, enum, required,
    properties, additionalProperties, items — so the offline suite carries no
    extra dependency.
    """
    if "enum" in schema and instance not in schema["enum"]:
        return f"{path}: value {instance!r} is not one of the allowed enum values"
    if "type" in schema:
        allowed = schema["type"]
        if isinstance(allowed, str):
            allowed = [allowed]
        if not any(_type_ok(t, instance) for t in allowed):
            got = type(instance).__name__
            return f"{path}: expected type {'/'.join(allowed)}, got {got}"
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                return f"{path}: missing required key '{key}'"
        properties = schema.get("properties", {})
        for key, value in instance.items():
            if key in properties:
                violation = validate_against_schema(
                    value, properties[key], f"{path}.{key}"
                )
                if violation is not None:
                    return violation
            elif schema.get("additionalProperties") is False:
                return f"{path}: unexpected key '{key}' (additionalProperties is false)"
    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(instance):
            violation = validate_against_schema(item, schema["items"], f"{path}[{i}]")
            if violation is not None:
                return violation
    return None


def _type_ok(type_name: str, value) -> bool:
    if type_name == "object":
        return isinstance(value, dict)
    if type_name == "array":
        return isinstance(value, list)
    if type_name == "string":
        return isinstance(value, str)
    if type_name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if type_name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_name == "boolean":
        return isinstance(value, bool)
    if type_name == "null":
        return value is None
    return True


# --- invocation ---------------------------------------------------------------


def _build_argv(schema_json: str, model: str, bare: bool) -> list[str]:
    argv = resolve_claude_argv() + [
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        schema_json,
        "--tools",
        "",
        "--strict-mcp-config",
        "--max-turns",
        "1",
        "--no-session-persistence",
        "--model",
        model,
    ]
    if bare:
        # TODO(plan-001 Risks): --bare skips OAuth and may silently require an API
        # key, breaking the subscription-only constraint. It stays a config toggle,
        # off by default, until empirically verified against subscription auth.
        argv.append("--bare")
    return argv


def _invoke(
    prompt: str, schema_json: str, model: str, bare: bool, max_retries: int
) -> dict:
    """Run the CLI once, retrying a malformed envelope; returns the parsed envelope."""
    preflight()
    argv = _build_argv(schema_json, model, bare)
    snippet = ""
    for _attempt in range(max_retries + 1):
        try:
            proc = subprocess.run(
                argv, input=prompt, capture_output=True, encoding="utf-8"
            )
        except OSError as exc:
            raise JudgeUnavailable(
                f"claude CLI at {argv[0]} could not be executed: {exc}. "
                + _INSTALL_HINT
            ) from exc
        if proc.returncode != 0:
            raise JudgeUnavailable(
                f"claude exited {proc.returncode}: {(proc.stderr or '').strip()}"
            )
        try:
            envelope = json.loads(proc.stdout)
            if isinstance(envelope, dict):
                return envelope
        except json.JSONDecodeError:
            pass
        snippet = (proc.stdout or "")[:200]
        logger.warning("judge returned a malformed JSON envelope; retrying")
    raise JudgeUnavailable(
        f"claude returned a malformed JSON envelope after {max_retries + 1}"
        f" attempts; output started with: {snippet!r}"
    )


def _check_envelope(envelope: dict) -> None:
    if envelope.get("is_error"):
        subtype = envelope.get("subtype", "unknown")
        result = str(envelope.get("result", ""))[:200]
        raise JudgeUnavailable(
            f"judge call failed: envelope is_error=true, subtype={subtype}. {result}"
        )


def _feedback_prompt(original_prompt: str, offending_output, violation: str) -> str:
    rendered = (
        "null"
        if offending_output is None
        else json.dumps(offending_output, sort_keys=True, ensure_ascii=False)
    )
    return (
        f"{original_prompt}\n\n"
        "Your previous response violated the required JSON schema.\n"
        f"Violation: {violation}\n"
        f"Previous response: {rendered}\n"
        "Respond again with a single JSON object that conforms exactly to the schema."
    )


def _resolve_mode(mode: str | None) -> str:
    resolved = mode or os.environ.get(MODE_ENV) or "replay"
    if resolved not in MODES:
        raise JudgeError(
            f"invalid judge mode '{resolved}' (from {MODE_ENV} or argument);"
            f" valid modes: {', '.join(MODES)}"
        )
    return resolved


def _resolve_fixtures_dir(fixtures_dir: str | Path | None) -> Path:
    if fixtures_dir is not None:
        return Path(fixtures_dir)
    env = os.environ.get(FIXTURES_ENV)
    if env:
        return Path(env)
    return DEFAULT_FIXTURES_DIR


# --- the seam -----------------------------------------------------------------


def run_judge(
    prompt: str,
    schema: dict,
    model: str,
    *,
    max_retries: int,
    bare: bool = False,
    extra_validate: Callable[[dict], str | None] | None = None,
    mode: str | None = None,
    fixtures_dir: str | Path | None = None,
) -> JudgeResult:
    """Run one judge call through the record/replay seam and return validated output.

    ``max_retries`` comes from config (judge.max_retries) — a schema-violating
    response is fed back that many times before :class:`JudgeSchemaViolation`,
    with no side effects. ``extra_validate`` lets the caller enforce per-call-type
    allowed-outcome subsets and reference integrity (R6): returning a violation
    message routes through the same feedback-retry path. Each feedback retry has a
    distinct prompt, hence its own fixture key — the retry path replays offline.
    """
    resolved_mode = _resolve_mode(mode)
    fixtures = (
        _resolve_fixtures_dir(fixtures_dir)
        if resolved_mode in ("replay", "record")
        else None
    )
    schema_json = json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    current_prompt = prompt
    violation: str | None = None
    for attempt in range(1, max_retries + 2):
        h = request_hash(current_prompt, schema, model)
        if resolved_mode == "replay":
            envelope = _load_fixture(fixtures, h)
        else:
            envelope = _invoke(current_prompt, schema_json, model, bare, max_retries)
            if resolved_mode == "record":
                write_fixture(fixtures, current_prompt, schema, model, envelope)
        _check_envelope(envelope)
        output = envelope.get("structured_output")
        if output is None:
            violation = "envelope contains no structured_output"
        else:
            violation = validate_against_schema(output, schema)
            if violation is None and extra_validate is not None:
                violation = extra_validate(output)
        if violation is None:
            cost_usd = envelope.get("total_cost_usd")
            duration_ms = envelope.get("duration_ms")
            logger.info(
                "judge ok: hash=%s attempts=%d cost_usd=%s duration_ms=%s",
                h,
                attempt,
                cost_usd,
                duration_ms,
            )
            return JudgeResult(
                output=output,
                request_hash=h,
                attempts=attempt,
                cost_usd=cost_usd,
                duration_ms=duration_ms,
                envelope=envelope,
            )
        logger.warning(
            "judge schema violation (attempt %d/%d): %s",
            attempt,
            max_retries + 1,
            violation,
        )
        current_prompt = _feedback_prompt(prompt, output, violation)
    raise JudgeSchemaViolation(
        f"judge response violated the schema after {max_retries + 1} attempts;"
        f" last violation: {violation}"
    )
