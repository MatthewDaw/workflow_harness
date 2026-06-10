"""plan-002 U6: the harness gate — typed failures, timeouts, failure-set hash.

Fully offline: gate commands are tiny ``python -c`` programs (the command list
is a caller-supplied tunable, R12 — production wires npm scripts in U8).

## Conformance

Scenario / invariant -> test mapping (plan-002 U6, gate slice of R12/R13):

- gate commands run with orchestrator-enforced timeouts; pass when all exit 0:
  ``test_gate_passes_when_all_commands_pass``,
  ``test_gate_command_timeout_is_enforced_and_typed``
- gate failures are typed failure records in the §7 shape with a repro
  command: ``test_gate_failure_is_typed_with_repro_command``
- all commands run, full failure set collected (no first-failure prefix):
  ``test_gate_runs_all_commands_and_collects_every_failure``
- identical failure sets hash identically across iterations (the no-progress
  detector's signal): ``test_identical_failure_sets_hash_identically``
- distinct failure sets hash differently:
  ``test_distinct_failure_sets_hash_differently``
- the hash is order-invariant (a SET, not a list):
  ``test_failure_set_hash_is_order_invariant``
- Windows KTD — utf-8 subprocess decoding:
  ``test_gate_output_decodes_utf8``
- config validation (timeouts positive, kinds from the store enum, non-empty
  argv/gate): ``test_gate_command_validation``, ``test_run_gate_guards``
"""

from __future__ import annotations

import sys
import time

import pytest

from agent_families.pipeline.gate import (
    GATE_FAILURE_KINDS,
    GateCommand,
    GateError,
    GateFailure,
    canonical_failure_set_hash,
    run_gate,
)
from agent_families.store import FAILURE_KINDS

PY = sys.executable


def cmd(name="test", code="raise SystemExit(0)", timeout_s=60.0, kind="gate_test"):
    return GateCommand(
        name=name, argv=(PY, "-c", code), timeout_s=timeout_s, failure_kind=kind
    )


FAIL_CODE = "import sys; sys.stderr.write('1 type error in app.ts\\n'); sys.exit(1)"


# --- pass/fail and the §7 record shape (R12/R13) -----------------------------------


def test_gate_passes_when_all_commands_pass(tmp_path):
    outcome = run_gate(tmp_path, [cmd("typecheck"), cmd("lint"), cmd("test")])
    assert outcome.passed
    assert outcome.failures == ()
    assert outcome.failure_set_hash is None


def test_gate_failure_is_typed_with_repro_command(tmp_path):
    command = cmd("typecheck", FAIL_CODE, kind="gate_typecheck")
    outcome = run_gate(tmp_path, [command])
    assert not outcome.passed
    (failure,) = outcome.failures
    assert failure.failure_kind == "gate_typecheck"
    assert failure.location == "typecheck"
    assert failure.expected == "exit 0"
    assert "exit 1" in failure.observed
    assert "1 type error in app.ts" in failure.observed
    # the repro command is the shell-pasteable rendering of the argv
    assert failure.repro_command == command.repro_command
    assert "type error" in failure.repro_command  # the -c payload rides along
    # every gate kind is a valid store failure kind (R13 enum)
    assert failure.failure_kind in FAILURE_KINDS


def test_gate_runs_all_commands_and_collects_every_failure(tmp_path):
    outcome = run_gate(
        tmp_path,
        [
            cmd("typecheck", FAIL_CODE, kind="gate_typecheck"),
            cmd("lint", "raise SystemExit(0)", kind="gate_lint"),
            cmd("test", "import sys; print('2 failed'); sys.exit(1)"),
        ],
    )
    assert not outcome.passed
    assert [f.location for f in outcome.failures] == ["typecheck", "test"]
    assert {f.failure_kind for f in outcome.failures} == {
        "gate_typecheck",
        "gate_test",
    }


def test_gate_command_timeout_is_enforced_and_typed(tmp_path):
    started = time.monotonic()
    outcome = run_gate(
        tmp_path,
        [cmd("test", "import time; time.sleep(60)", timeout_s=0.5)],
    )
    elapsed = time.monotonic() - started
    assert elapsed < 30  # killed at the enforced timeout, not the sleep
    assert not outcome.passed
    (failure,) = outcome.failures
    assert "timed out after 0.5s" in failure.observed
    assert failure.expected == "exit 0 within 0.5s"
    assert failure.failure_kind == "gate_test"


def test_gate_output_decodes_utf8(tmp_path):
    # the child writes raw utf-8 bytes; the gate must decode them (Windows KTD)
    code = (
        "import sys; sys.stdout.buffer.write('na\\u00efve \\u2713 fail\\n'"
        ".encode('utf-8')); sys.exit(1)"
    )
    outcome = run_gate(tmp_path, [cmd("test", code)])
    assert "naïve ✓ fail" in outcome.failures[0].observed


# --- canonical failure-set hash (feeds the U7 no-progress detector) -----------------


def test_identical_failure_sets_hash_identically(tmp_path):
    commands = [cmd("typecheck", FAIL_CODE, kind="gate_typecheck")]
    hashes = [run_gate(tmp_path, commands).failure_set_hash for _ in range(3)]
    assert hashes[0] is not None
    assert hashes[0].startswith("sha256:")
    assert hashes == [hashes[0]] * 3


def test_distinct_failure_sets_hash_differently(tmp_path):
    first = run_gate(tmp_path, [cmd("test", FAIL_CODE)])
    second = run_gate(
        tmp_path,
        [cmd("test", "import sys; sys.stderr.write('other error\\n'); sys.exit(2)")],
    )
    assert first.failure_set_hash != second.failure_set_hash


def test_failure_set_hash_is_order_invariant():
    a = GateFailure("gate_test", "test", "exit 0", "exit 1: boom", "npm test")
    b = GateFailure(
        "gate_lint", "lint", "exit 0", "exit 1: unused var", "npm run lint"
    )
    assert canonical_failure_set_hash([a, b]) == canonical_failure_set_hash([b, a])
    # dict records (e.g. store rows) hash identically to dataclass records
    assert canonical_failure_set_hash(
        [a.as_record(), b.as_record()]
    ) == canonical_failure_set_hash([b, a])
    assert canonical_failure_set_hash([a]) != canonical_failure_set_hash([a, b])


# --- validation guards -----------------------------------------------------------------


def test_gate_command_validation():
    with pytest.raises(GateError, match="timeout_s"):
        cmd(timeout_s=0)
    with pytest.raises(GateError, match="failure_kind"):
        cmd(kind="verifier_check")
    with pytest.raises(GateError, match="argv"):
        GateCommand(name="x", argv=(), timeout_s=1.0, failure_kind="gate_test")
    with pytest.raises(GateError, match="name"):
        GateCommand(
            name=" ", argv=(PY, "-c", "pass"), timeout_s=1.0, failure_kind="gate_test"
        )
    assert set(GATE_FAILURE_KINDS) <= set(FAILURE_KINDS)


def test_run_gate_guards(tmp_path):
    with pytest.raises(GateError, match="at least one"):
        run_gate(tmp_path, [])
    with pytest.raises(GateError, match="does not exist"):
        run_gate(tmp_path / "missing", [cmd()])
    with pytest.raises(GateError, match="spawned"):
        run_gate(
            tmp_path,
            [
                GateCommand(
                    name="ghost",
                    argv=(str(tmp_path / "no-such-binary.exe"),),
                    timeout_s=5.0,
                    failure_kind="gate_test",
                )
            ],
        )
