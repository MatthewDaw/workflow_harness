"""Harness gate: deterministic check commands with enforced timeouts (plan-002 U6).

R12 — the gate (tsc, eslint, vitest in production; any configured command list
here) runs after every worker iteration as a cheap checker that short-circuits
before the verifier. Each command carries an ORCHESTRATOR-enforced timeout; a
command that fails or times out becomes a typed failure in the §7 shape
(failure_kind / location / expected / observed / repro_command — R13). The
gate never writes the store: it returns :class:`GateOutcome` and the ticket
loop (ticket_loop.py) persists failure records and ledger entries, keeping the
orchestrator the sole store writer (R6).

All commands run even after a failure: the full failure set is the no-progress
detector's input (R17) — a richer set hashes more honestly than a first-failure
prefix. Iteration accounting (a gate bounce consumes a full Ralph iteration) is
the orchestrator's job (U4), not this module's.

:func:`canonical_failure_set_hash` is the canonical hash over a set of typed
failure records, invariant to ordering — three identical gate bounces hash
identically, which is exactly the signal the shadow no-progress detector logs
per iteration (U7 consumes this function; the producer owns the canon).

Windows KTDs honored: utf-8 subprocess decoding with ``errors="replace"``;
``repro_command`` is the ``list2cmdline`` rendering of the argv (what a user
pastes into a shell to reproduce). Command lists and their timeouts are
caller-supplied — nothing here hardcodes a tunable.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# The §7 fields that participate in the canonical failure-set hash. Protocol
# constant, not a tunable: changing it invalidates every stored hash.
FAILURE_SET_FIELDS = (
    "failure_kind",
    "location",
    "expected",
    "observed",
    "repro_command",
)

# Gate commands map onto the store's gate failure kinds (R12/R13).
GATE_FAILURE_KINDS = ("gate_typecheck", "gate_lint", "gate_test")

HASH_PREFIX = "sha256:"

# Output kept on a typed failure record — enough to act on, bounded so ledger
# renderings stay readable. Protocol constant (truncation marker is appended).
_OBSERVED_TAIL_CHARS = 2000


class GateError(Exception):
    """Gate misuse or a command that could not be spawned at all."""


@dataclass(frozen=True)
class GateCommand:
    """One gate check: name, argv, enforced timeout, and its failure kind."""

    name: str
    argv: tuple[str, ...]
    timeout_s: float
    failure_kind: str

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise GateError("GateCommand.name must be non-empty")
        if not self.argv or not all(str(a).strip() for a in self.argv):
            raise GateError(
                f"GateCommand({self.name}): argv must be a non-empty tuple of"
                " non-empty strings"
            )
        if self.timeout_s <= 0:
            raise GateError(
                f"GateCommand({self.name}): timeout_s must be positive,"
                f" got {self.timeout_s}"
            )
        if self.failure_kind not in GATE_FAILURE_KINDS:
            raise GateError(
                f"GateCommand({self.name}): failure_kind must be one of"
                f" {GATE_FAILURE_KINDS}, got '{self.failure_kind}'"
            )

    @property
    def repro_command(self) -> str:
        """The shell-pasteable rendering of this command (Windows quoting)."""
        return subprocess.list2cmdline(list(self.argv))


@dataclass(frozen=True)
class GateFailure:
    """One typed gate failure in the §7 record shape (R13)."""

    failure_kind: str
    location: str
    expected: str
    observed: str
    repro_command: str

    def as_record(self) -> dict:
        return {
            "failure_kind": self.failure_kind,
            "location": self.location,
            "expected": self.expected,
            "observed": self.observed,
            "repro_command": self.repro_command,
        }


@dataclass(frozen=True)
class GateOutcome:
    """The gate's verdict for one iteration: pass, or the typed failure set."""

    passed: bool
    failures: tuple[GateFailure, ...] = ()
    failure_set_hash: str | None = None


def _tail(text: str) -> str:
    text = (text or "").strip()
    if len(text) <= _OBSERVED_TAIL_CHARS:
        return text
    return "…(truncated)…" + text[-_OBSERVED_TAIL_CHARS:]


def _combined_output(stdout: str | None, stderr: str | None) -> str:
    parts = [p.strip() for p in (stdout, stderr) if p and p.strip()]
    return _tail("\n".join(parts)) or "(no output)"


def run_gate(
    workspace_root: Path | str, commands: Iterable[GateCommand]
) -> GateOutcome:
    """Run every gate command in the workspace; collect typed failures (R12).

    Commands run sequentially in ``workspace_root`` with utf-8 capture; a
    nonzero exit or a timeout becomes one :class:`GateFailure`. All commands
    run regardless of earlier failures (the full set feeds the no-progress
    detector). A command that cannot be spawned at all is a :class:`GateError`
    — that's a harness/config problem, not an agent failure.
    """
    workspace_root = Path(workspace_root)
    if not workspace_root.is_dir():
        raise GateError(f"gate workspace does not exist: {workspace_root}")
    commands = tuple(commands)
    if not commands:
        raise GateError(
            "run_gate needs at least one GateCommand — an empty gate would"
            " pass everything vacuously (R12)"
        )
    failures: list[GateFailure] = []
    for command in commands:
        try:
            proc = subprocess.run(
                list(command.argv),
                cwd=str(workspace_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=command.timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            failures.append(
                GateFailure(
                    failure_kind=command.failure_kind,
                    location=command.name,
                    expected=f"exit 0 within {command.timeout_s}s",
                    observed=(
                        f"timed out after {command.timeout_s}s and was killed;"
                        f" partial output: "
                        + _combined_output(
                            exc.stdout if isinstance(exc.stdout, str) else None,
                            exc.stderr if isinstance(exc.stderr, str) else None,
                        )
                    ),
                    repro_command=command.repro_command,
                )
            )
            logger.warning(
                "gate command '%s' timed out after %.1fs",
                command.name,
                command.timeout_s,
            )
            continue
        except OSError as exc:
            raise GateError(
                f"gate command '{command.name}' could not be spawned"
                f" ({command.repro_command}): {exc}"
            ) from exc
        if proc.returncode != 0:
            failures.append(
                GateFailure(
                    failure_kind=command.failure_kind,
                    location=command.name,
                    expected="exit 0",
                    observed=(
                        f"exit {proc.returncode}: "
                        + _combined_output(proc.stdout, proc.stderr)
                    ),
                    repro_command=command.repro_command,
                )
            )
            logger.info(
                "gate command '%s' failed (exit %d)", command.name, proc.returncode
            )
    if not failures:
        return GateOutcome(passed=True)
    return GateOutcome(
        passed=False,
        failures=tuple(failures),
        failure_set_hash=canonical_failure_set_hash(failures),
    )


def canonical_failure_set_hash(
    records: Iterable[GateFailure | Mapping],
) -> str:
    """Order-invariant sha256 over a SET of typed failure records (R17 feed).

    Each record is reduced to the §7 fields (:data:`FAILURE_SET_FIELDS`),
    canonically JSON-serialized, sorted, and hashed — identical failure sets
    hash identically regardless of record order; that stability is what makes
    the no-progress detector's per-iteration comparison meaningful.
    """
    canon = sorted(
        json.dumps(
            {
                field: str(record[field])
                for field in FAILURE_SET_FIELDS
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for record in (
            r.as_record() if isinstance(r, GateFailure) else r for r in records
        )
    )
    digest = hashlib.sha256("\n".join(canon).encode("utf-8")).hexdigest()
    return HASH_PREFIX + digest
