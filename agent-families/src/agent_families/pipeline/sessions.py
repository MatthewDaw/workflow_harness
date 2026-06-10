"""Live-session seam: ``run_session`` + the scripted-agent fake (plan-002 U3).

The second invocation class beside Phase 0's single-shot ``run_judge`` (KTD
"two seams, not one"): multi-turn, tool-enabled ``claude -p`` sessions.
Request-hash replay is impossible for filesystem-nondeterministic sessions, so
the offline discipline here is a **scripted fake** (R7), selected via env var,
that mirrors this seam's interface exactly — it applies known diffs to the
workspace and emits known result envelopes which flow through the SAME
classification, structured-output validation, retry, and span bookkeeping as a
live session. The offline suite needs zero quota and no ``claude`` on PATH.

Live invocation discipline (Phase 0 KTDs carried forward): spawn via the
Windows node-entrypoint rule (no cmd.exe argv re-parsing), prompt via stdin,
utf-8 everywhere; ``--output-format stream-json`` is captured line-by-line to
a JSONL transcript file (R5) while per-message usage is tallied, so a session
killed on timeout — which never emits a final envelope — still finalizes its
span with best-effort cost/turn fields flagged ``cost_partial`` (R16).

Spans (R16): every invocation — each retry attempt is its own invocation —
registers a span ``running`` at spawn and finalizes it on exit (``completed``
/ ``error`` / ``timeout``). The orchestrator is the sole store writer (R6):
the span hooks run in THIS process; agents never receive the store DB path,
and the per-role permission profile is the only tool surface an agent sees.

Per-role tunables (model, max-turns, wall-clock timeout, the worker's
unit-test commands, the verifier's check commands) are supplied by the caller
— this module hardcodes none of them. Routing them from ``thresholds.toml``
is the orchestrator's job (U4); the Phase 0 config loader is frozen this wave.

Quota classification (R4) — PROBE PENDING (plan-002 KTD Q8): the
quota-exhaustion envelope shape under the June 15, 2026 billing change cannot
be forced cheaply offline, so the plan's documented fallback applies —
classify on the error envelope's ``subtype``/``result`` text (and stderr on a
nonzero exit) using :data:`QUOTA_SUBTYPES` / :data:`QUOTA_TEXT_MARKERS`.
Probe procedure (live, when a real exhaustion is observed): copy the captured
result envelope over ``tests/fixtures/sessions/quota-envelope.json``, extend
the marker constants if the wording differs, and record the finding under
``## Probe findings`` in PROGRESS.md.

Live smoke procedure (documented per U3 verification, NOT in CI): with the
claude CLI installed and logged in on the subscription, from agent-families/::

    uv run python -c "from agent_families.pipeline.sessions import *; \
        p = planner_profile(model='sonnet', max_turns=2, timeout_s=300.0); \
        s = {'type': 'object', 'properties': {'answer': {'type': 'string'}}, \
             'required': ['answer'], 'additionalProperties': False}; \
        r = run_session('Reply with answer set to ok.', s, p, mode='live', \
                        transcript_path='smoke-session.jsonl', max_retries=1); \
        print(r.output, r.cost_usd, r.num_turns)"

then inspect ``smoke-session.jsonl`` (stream captured line-by-line) and note
the observed cost. Expected: a few cents on the default planner profile.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agent_families import judge as _judge
from agent_families.judge import validate_against_schema

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

MODE_ENV = "AF_SESSION_MODE"
SCRIPT_ENV = "AF_SESSION_SCRIPT"
MODES = ("live", "scripted")

# Reap grace after a kill — process-hygiene constant, not a behavior tunable
# (the behavior tunable is the per-role wall-clock timeout, caller-supplied).
_KILL_REAP_S = 10.0

# --- R4 quota/rate-limit classification (fallback markers; probe pending) ----
# Protocol constants, not tunables: candidate subtypes plus lowercase result/
# stderr substrings drawn from the documented claude -p error envelope shapes
# (June 2026 library verification). Compared case-insensitively; subtypes are
# also matched with underscores normalized to spaces so unlisted variants like
# "usage_limit" still classify. Refresh from the live probe (module docstring).
QUOTA_SUBTYPES = frozenset(
    {
        "error_rate_limit",
        "rate_limit_error",
        "usage_limit_reached",
        "quota_exceeded",
    }
)
QUOTA_TEXT_MARKERS = (
    "rate limit",
    "rate-limit",
    "usage limit",
    "quota",
    "out of extra usage",
    "credit balance",
    "limit will reset",
)


class SessionError(Exception):
    """Base for every session-seam failure."""


class SessionUnavailable(SessionError):
    """The claude CLI is missing/broken or returned a non-quota error envelope."""


class SessionTimeout(SessionError):
    """The session exceeded its wall-clock timeout and was killed (R5)."""


class SessionQuotaExhausted(SessionError):
    """Quota/rate-limit exhaustion (R4) — the distinct, checkpoint-triggering exit.

    Consumes no Ralph iterations and produces no agent-attributed failure
    records; the orchestrator checkpoints and exits ``aborted_quota``.
    """


class SessionSchemaViolation(SessionError):
    """Structured output still violated the contract after all retries (R6)."""


class SessionScriptError(SessionError):
    """The scripted fake was misused or its script is exhausted/malformed (R7)."""


# --- per-role permission profiles (R6 / DESIGN §8) ----------------------------

_READ_TOOLS = ("Read", "Glob", "Grep", "LS")
_WRITE_TOOLS = ("Write", "Edit", "MultiEdit")


@dataclass(frozen=True)
class RoleProfile:
    """One role's session envelope: model, turn/time budget, and tool surface.

    ``tools`` maps to ``--tools`` (the judge precedent: ``""`` = no tools at
    all); ``allowed_tools`` maps to ``--allowedTools`` with one specifier per
    argv element. Values come from config via the caller — nothing here is
    defaulted from constants.
    """

    role: str
    model: str
    max_turns: int
    timeout_s: float
    tools: str | None = None
    allowed_tools: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise SessionError("RoleProfile.role must be non-empty")
        if not self.model.strip():
            raise SessionError(f"RoleProfile({self.role}): model must be non-empty")
        if self.max_turns <= 0:
            raise SessionError(
                f"RoleProfile({self.role}): max_turns must be positive,"
                f" got {self.max_turns}"
            )
        if self.timeout_s <= 0:
            raise SessionError(
                f"RoleProfile({self.role}): timeout_s must be positive,"
                f" got {self.timeout_s}"
            )


def planner_profile(*, model: str, max_turns: int, timeout_s: float) -> RoleProfile:
    """§8: the planner has no execution or file-write tools — structured output
    is its only channel (spec content rides the prompt)."""
    return RoleProfile(
        role="planner",
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tools="",
        allowed_tools=None,
    )


def worker_profile(
    *,
    model: str,
    max_turns: int,
    timeout_s: float,
    test_commands: tuple[str, ...],
) -> RoleProfile:
    """§8: the worker writes only inside its workspace (session cwd; enforced
    post-hoc by the R8 containment assert until the Windows directory-scoping
    probe hardens U6) and may run ONLY its own unit-test commands — Bash is
    never granted unscoped."""
    if not test_commands or not all(c.strip() for c in test_commands):
        raise SessionError(
            "worker_profile requires at least one non-empty test command"
            " (the worker's only permitted Bash surface, §8)"
        )
    allowed = (
        *_READ_TOOLS,
        *_WRITE_TOOLS,
        *(f"Bash({command}:*)" for command in test_commands),
    )
    return RoleProfile(
        role="worker",
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tools=None,
        allowed_tools=allowed,
    )


def verifier_profile(
    *,
    model: str,
    max_turns: int,
    timeout_s: float,
    check_commands: tuple[str, ...],
) -> RoleProfile:
    """§8: the verifier executes build/integration/browser checks and writes
    nothing — no file-write tools; Bash is scoped to the named check commands
    (the orchestrator manages the dev server itself, R15)."""
    if not check_commands or not all(c.strip() for c in check_commands):
        raise SessionError(
            "verifier_profile requires at least one non-empty check command"
        )
    allowed = (
        *_READ_TOOLS,
        *(f"Bash({command}:*)" for command in check_commands),
    )
    return RoleProfile(
        role="verifier",
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tools=None,
        allowed_tools=allowed,
    )


def build_session_argv(profile: RoleProfile, schema_json: str) -> list[str]:
    """Assemble the live argv: stream-json capture + the role's tool surface.

    The prompt rides stdin (never argv — Phase 0 Windows KTD); the schema is a
    single argv element; ``--verbose`` is required by ``-p`` + stream-json.
    """
    argv = _judge.resolve_claude_argv() + [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--json-schema",
        schema_json,
        "--max-turns",
        str(profile.max_turns),
        "--model",
        profile.model,
        "--no-session-persistence",
        "--strict-mcp-config",
    ]
    if profile.tools is not None:
        argv += ["--tools", profile.tools]
    if profile.allowed_tools is not None:
        argv += ["--allowedTools", *profile.allowed_tools]
    return argv


# --- envelope classification (R4) ---------------------------------------------


def _text_mentions_quota(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in QUOTA_TEXT_MARKERS)


def classify_envelope(envelope: dict) -> str:
    """``ok`` | ``quota`` | ``error`` — R4's envelope classification (fallback
    markers until the live probe lands; see module docstring)."""
    if not envelope.get("is_error"):
        return "ok"
    subtype = str(envelope.get("subtype") or "")
    if subtype in QUOTA_SUBTYPES or _text_mentions_quota(subtype.replace("_", " ")):
        return "quota"
    if _text_mentions_quota(str(envelope.get("result") or "")):
        return "quota"
    return "error"


# --- stream capture -------------------------------------------------------------


@dataclass
class _StreamTally:
    """Best-effort accounting accumulated from per-message stream usage (R16)."""

    input_tokens: int = 0
    output_tokens: int = 0
    assistant_turns: int = 0
    saw_usage: bool = False
    result_envelope: dict | None = None

    def feed(self, event: dict) -> None:
        if event.get("type") == "assistant":
            self.assistant_turns += 1
            message = event.get("message")
            usage = message.get("usage") if isinstance(message, dict) else None
            if isinstance(usage, dict):
                self.saw_usage = True
                self.input_tokens += int(usage.get("input_tokens") or 0)
                self.output_tokens += int(usage.get("output_tokens") or 0)
        elif event.get("type") == "result":
            self.result_envelope = event

    def partial_costs(self, duration_ms: int) -> dict:
        """Span cost fields for a session that never emitted a final envelope."""
        return {
            "num_turns": self.assistant_turns or None,
            "duration_ms": duration_ms,
            "cost_usd": None,
            "input_tokens": self.input_tokens if self.saw_usage else None,
            "output_tokens": self.output_tokens if self.saw_usage else None,
        }


@dataclass
class _LiveOutcome:
    kind: str  # "ok" | "timeout" | "exit_error"
    tally: _StreamTally
    duration_ms: int
    returncode: int | None = None
    stderr: str = ""

    @property
    def envelope(self) -> dict | None:
        return self.tally.result_envelope


def _envelope_costs(envelope: dict) -> dict:
    usage = envelope.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    return {
        "num_turns": envelope.get("num_turns"),
        "duration_ms": envelope.get("duration_ms"),
        "cost_usd": envelope.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def _pump_lines(stream, sink: Callable[[str], None]) -> None:
    for raw in stream:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        sink(line)


def _write_stdin(proc: subprocess.Popen, prompt: str) -> None:
    if proc.stdin is None:
        return
    try:
        try:
            proc.stdin.write(prompt)
        except TypeError:
            # binary stdin (test doubles): same bytes, utf-8 per the KTD
            proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
    except OSError:
        # session died before consuming the prompt; exit handling decides
        logger.warning("session stdin closed early; prompt may be unread")


def _invoke_live(
    prompt: str,
    schema_json: str,
    profile: RoleProfile,
    transcript_path: Path,
    cwd: Path | None,
) -> _LiveOutcome:
    """One live invocation: spawn, stream-capture, enforce wall-clock, classify exit."""
    _judge.preflight()
    argv = build_session_argv(profile, schema_json)
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    tally = _StreamTally()
    stderr_chunks: list[str] = []
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            cwd=str(cwd) if cwd is not None else None,
        )
    except OSError as exc:
        raise SessionUnavailable(
            f"claude CLI at {argv[0]} could not be executed: {exc}"
        ) from exc

    with transcript_path.open("a", encoding="utf-8", newline="\n") as fh:

        def _capture(line: str) -> None:
            fh.write(line if line.endswith("\n") else line + "\n")
            stripped = line.strip()
            if not stripped:
                return
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                return  # non-JSON noise: captured in the transcript, not parsed
            if isinstance(event, dict):
                tally.feed(event)

        readers = [
            threading.Thread(
                target=_pump_lines, args=(proc.stdout, _capture), daemon=True
            ),
            threading.Thread(
                target=_pump_lines, args=(proc.stderr, stderr_chunks.append),
                daemon=True,
            ),
        ]
        for reader in readers:
            reader.start()
        _write_stdin(proc, prompt)

        timed_out = False
        try:
            proc.wait(timeout=profile.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            logger.warning(
                "session exceeded %.1fs wall clock; killing (role=%s)",
                profile.timeout_s,
                profile.role,
            )
            try:
                proc.kill()
                proc.wait(timeout=_KILL_REAP_S)
            except Exception:  # noqa: BLE001 - reap is best-effort post-kill
                pass
        for reader in readers:
            reader.join(timeout=_KILL_REAP_S)
        # Final drain: output written before a kill can still sit in the pipe
        # after the readers saw EOF — it belongs in the transcript and the
        # partial-cost tally (R16).
        for stream, sink in ((proc.stdout, _capture), (proc.stderr, stderr_chunks.append)):
            if stream is None:
                continue
            try:
                _pump_lines(stream, sink)
            except (OSError, ValueError):
                pass  # stream already closed; nothing left to drain

    duration_ms = int((time.monotonic() - started) * 1000)
    if timed_out:
        return _LiveOutcome(kind="timeout", tally=tally, duration_ms=duration_ms)
    if proc.returncode != 0:
        return _LiveOutcome(
            kind="exit_error",
            tally=tally,
            duration_ms=duration_ms,
            returncode=proc.returncode,
            stderr="".join(stderr_chunks),
        )
    return _LiveOutcome(kind="ok", tally=tally, duration_ms=duration_ms)


# --- the scripted fake (R7) ------------------------------------------------------


def _cursor_path(script_path: Path) -> Path:
    return script_path.with_name(script_path.name + ".cursor")


def reset_session_script(script_path: str | Path) -> None:
    """Rewind a session script to step 0 (test/e2e setup hook)."""
    cursor = _cursor_path(Path(script_path))
    if cursor.exists():
        cursor.unlink()


def _load_script_steps(script_path: Path) -> list[dict]:
    if not script_path.exists():
        raise SessionScriptError(f"session script not found: {script_path}")
    try:
        data = json.loads(script_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SessionScriptError(
            f"session script is not valid JSON: {script_path}: {exc}"
        ) from exc
    steps = data.get("steps") if isinstance(data, dict) else None
    if not isinstance(steps, list) or not all(isinstance(s, dict) for s in steps):
        raise SessionScriptError(
            f"session script must be an object with a 'steps' array of objects:"
            f" {script_path}"
        )
    return steps


def _consume_step(script_path: Path) -> dict:
    """Pop the next script step; the cursor sidecar makes consumption durable
    across orchestrator processes (resume/e2e kill tests)."""
    steps = _load_script_steps(script_path)
    cursor = _cursor_path(script_path)
    consumed = 0
    if cursor.exists():
        try:
            consumed = int(cursor.read_text(encoding="utf-8").strip() or "0")
        except ValueError as exc:
            raise SessionScriptError(
                f"corrupt script cursor {cursor}; delete it or call"
                " reset_session_script()"
            ) from exc
    if consumed >= len(steps):
        raise SessionScriptError(
            f"session script exhausted: all {len(steps)} step(s) of {script_path}"
            " were consumed. Add steps or reset_session_script() to rewind."
        )
    cursor.write_text(str(consumed + 1), encoding="utf-8", newline="\n")
    return steps[consumed]


def _apply_diff(workspace_root: Path, diff_path: Path) -> None:
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(workspace_root),
            "apply",
            "--whitespace=nowarn",
            str(diff_path),
        ],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise SessionScriptError(
            f"scripted diff {diff_path} failed to apply in {workspace_root}"
            f" (exit {proc.returncode}): {proc.stderr.strip()}"
        )


def _scripted_attempt(
    script_path: Path,
    profile: RoleProfile,
    workspace_root: Path | None,
    transcript_path: Path,
) -> dict:
    """One scripted invocation: apply the step's diff, synthesize the transcript,
    return its envelope — which then rides the SAME post-processing as live."""
    step = _consume_step(script_path)
    expected_role = step.get("role")
    if expected_role is not None and expected_role != profile.role:
        raise SessionScriptError(
            f"script step expects role '{expected_role}' but the session was"
            f" invoked as role '{profile.role}' ({script_path})"
        )
    diff_ref = step.get("apply_diff")
    if diff_ref:
        if workspace_root is None:
            raise SessionScriptError(
                "script step carries apply_diff but run_session received no"
                " workspace_root"
            )
        diff_path = (script_path.parent / diff_ref).resolve()
        if not diff_path.exists():
            raise SessionScriptError(
                f"script step diff not found: {diff_path} (relative to"
                f" {script_path.parent})"
            )
        _apply_diff(Path(workspace_root), diff_path)
    envelope = step.get("envelope")
    if not isinstance(envelope, dict):
        raise SessionScriptError(
            f"script step has no 'envelope' object: {script_path}"
        )
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    with transcript_path.open("a", encoding="utf-8", newline="\n") as fh:
        for event in step.get("transcript_events", []):
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        fh.write(json.dumps(envelope, ensure_ascii=False) + "\n")
    return envelope


# --- mode/script resolution -------------------------------------------------------


def _resolve_mode(mode: str | None) -> str:
    resolved = mode or os.environ.get(MODE_ENV) or "live"
    if resolved not in MODES:
        raise SessionError(
            f"invalid session mode '{resolved}' (from {MODE_ENV} or argument);"
            f" valid modes: {', '.join(MODES)}"
        )
    return resolved


def _resolve_script_path(script_path: str | Path | None) -> Path:
    resolved = script_path or os.environ.get(SCRIPT_ENV)
    if not resolved:
        raise SessionScriptError(
            f"scripted session mode needs a script: pass script_path or set"
            f" {SCRIPT_ENV}"
        )
    return Path(resolved)


# --- the seam ----------------------------------------------------------------------


@dataclass(frozen=True)
class SessionResult:
    """A contract-valid session outcome plus its envelope bookkeeping."""

    output: dict
    envelope: dict
    span_id: str
    transcript_path: Path
    attempts: int
    num_turns: int | None
    duration_ms: int | None
    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None


def run_session(
    prompt: str,
    schema: dict,
    profile: RoleProfile,
    *,
    transcript_path: str | Path,
    max_retries: int,
    workspace_root: str | Path | None = None,
    store: Store | None = None,
    run_id: int | None = None,
    family: str | None = None,
    agent: str | None = None,
    ticket_id: str | None = None,
    ralph_iteration: int | None = None,
    parent_span: str | None = None,
    prompt_set_version: str | None = None,
    extra_validate: Callable[[dict], str | None] | None = None,
    mode: str | None = None,
    script_path: str | Path | None = None,
) -> SessionResult:
    """Run one agentic session through the seam and return validated output.

    Every live agentic invocation flows through here (R5). ``workspace_root``
    is the session's cwd live, and the diff target for the scripted fake. With
    ``store``, each attempt registers a span ``running`` at spawn and finalizes
    it on exit (R16) — the store is written ONLY in this process (R6).

    Raises :class:`SessionTimeout` (killed at the profile's wall clock, span
    ``timeout`` with partial costs), :class:`SessionQuotaExhausted` (R4's
    distinct exit), :class:`SessionUnavailable` (spawn/exit/envelope errors),
    and :class:`SessionSchemaViolation` after ``max_retries`` feedback retries
    — each retry is a fresh invocation with its own span, judge-style (R6).
    """
    resolved_mode = _resolve_mode(mode)
    script = (
        _resolve_script_path(script_path) if resolved_mode == "scripted" else None
    )
    transcript_path = Path(transcript_path)
    workspace = Path(workspace_root) if workspace_root is not None else None
    schema_json = json.dumps(
        schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

    current_prompt = prompt
    violation: str | None = None
    output: dict | None = None
    for attempt in range(1, max_retries + 2):
        span_id = f"SPAN-{uuid.uuid4().hex}"
        if store is not None:
            store.insert_span(
                span_id,
                run_id=run_id,
                family=family,
                agent=agent or profile.role,
                ticket_id=ticket_id,
                ralph_iteration=ralph_iteration,
                parent_span=parent_span,
                model_version=profile.model,
                prompt_set_version=prompt_set_version,
            )

        if resolved_mode == "scripted":
            envelope = _scripted_attempt(script, profile, workspace, transcript_path)
        else:
            outcome = _invoke_live(
                current_prompt, schema_json, profile, transcript_path, workspace
            )
            if outcome.kind == "timeout":
                if store is not None:
                    store.finalize_span(
                        span_id,
                        "timeout",
                        cost_partial=True,
                        **outcome.tally.partial_costs(outcome.duration_ms),
                    )
                raise SessionTimeout(
                    f"session (role={profile.role}) exceeded {profile.timeout_s}s"
                    f" and was killed; partial transcript: {transcript_path}"
                )
            if outcome.kind == "exit_error":
                if store is not None:
                    store.finalize_span(
                        span_id,
                        "error",
                        cost_partial=True,
                        **outcome.tally.partial_costs(outcome.duration_ms),
                    )
                stderr = outcome.stderr.strip()
                if _text_mentions_quota(stderr):
                    raise SessionQuotaExhausted(
                        f"claude exited {outcome.returncode} with a"
                        f" quota/rate-limit message: {stderr[:200]}"
                    )
                raise SessionUnavailable(
                    f"claude exited {outcome.returncode}: {stderr[:500]}"
                )
            if outcome.envelope is None:
                if store is not None:
                    store.finalize_span(
                        span_id,
                        "error",
                        cost_partial=True,
                        **outcome.tally.partial_costs(outcome.duration_ms),
                    )
                raise SessionUnavailable(
                    "session stream ended with no result envelope; transcript:"
                    f" {transcript_path}"
                )
            envelope = outcome.envelope

        costs = _envelope_costs(envelope)
        classification = classify_envelope(envelope)
        if classification != "ok":
            if store is not None:
                store.finalize_span(span_id, "error", **costs)
            subtype = envelope.get("subtype", "unknown")
            snippet = str(envelope.get("result", ""))[:200]
            if classification == "quota":
                raise SessionQuotaExhausted(
                    f"quota/rate-limit exhaustion (subtype={subtype}). {snippet}"
                )
            raise SessionUnavailable(
                f"session failed: envelope is_error=true, subtype={subtype}."
                f" {snippet}"
            )

        output = envelope.get("structured_output")
        if output is None:
            violation = "envelope contains no structured_output"
        else:
            violation = validate_against_schema(output, schema)
            if violation is None and extra_validate is not None:
                violation = extra_validate(output)
        # The invocation itself completed; a contract violation is the caller's
        # to charge (U6: infra, not the worker's cap).
        if store is not None:
            store.finalize_span(span_id, "completed", **costs)
        if violation is None:
            logger.info(
                "session ok: role=%s span=%s attempts=%d cost_usd=%s turns=%s",
                profile.role,
                span_id,
                attempt,
                costs["cost_usd"],
                costs["num_turns"],
            )
            return SessionResult(
                output=output,
                envelope=envelope,
                span_id=span_id,
                transcript_path=transcript_path,
                attempts=attempt,
                num_turns=costs["num_turns"],
                duration_ms=costs["duration_ms"],
                cost_usd=costs["cost_usd"],
                input_tokens=costs["input_tokens"],
                output_tokens=costs["output_tokens"],
            )
        logger.warning(
            "session structured-output violation (attempt %d/%d): %s",
            attempt,
            max_retries + 1,
            violation,
        )
        current_prompt = _judge._feedback_prompt(prompt, output, violation)
    raise SessionSchemaViolation(
        f"session structured output violated the contract after"
        f" {max_retries + 1} attempts; last violation: {violation}"
    )
