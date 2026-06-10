"""plan-002 U6: the per-ticket Ralph loop — worker, gate, verifier (R11–R15).

Fully offline and fake-driven: sessions ride the U3 scripted fake, the gate
runs REAL commands (tiny ``python -c`` programs — the plan reserves the real
gate for U6's tests), the workspace is a small real git repo, and the
orchestrator-driven scenarios use the actual U4 ``Orchestrator`` with this
module's stages plugged into its seams.

## Conformance

Scenario / invariant -> test mapping (plan-002 U6 Test scenarios, 1:1):

- gate failure consumes an iteration and writes a typed record with a repro
  command (R12/R13): ``test_gate_failure_consumes_iteration_with_typed_record``
- three identical scripted gate failures show identical failure-set hashes
  (feeds the no-progress detector):
  ``test_three_identical_gate_failures_hash_identically``
- verifier PASS missing one AC's CHK -> rejected as contract violation,
  retried, charged to infra not cap (R14):
  ``test_verifier_pass_missing_chk_rejected_retried_charged_to_infra``
- verifier PASS with full coverage closes the ticket and finalizes spans:
  ``test_trivial_ticket_end_to_end_with_fakes``
- repro envelope with absolute path rejected by validation (R14):
  ``test_validate_verdict_rejects`` (parametrized) +
  ``test_absolute_repro_path_rejected_through_the_session_retry_path``
- dev server: readiness probe gates verifier start / teardown on verdict
  (R15): ``test_devserver_bracketing_around_verifier`` (port retry and
  hung-server kill are covered in test_devserver.py)
- escalation path: cap exhaustion marks ticket escalated and dependents
  blocked (R2 arm driven by this loop's failures):
  ``test_escalation_blocks_dependents``
- escalation followed by an independent ticket whose gate passes (workspace
  reset to the ticket-start tag): ``test_escalated_code_does_not_poison_gate``

R11 clause coverage: ledger rendered read-only from store rows
(``test_render_ledger_formats_failures_and_summaries``), the worker reads it
in its prompt (``test_prompt_builders``), ``files_touched`` derived
mechanically from the iteration diff
(``test_worker_derives_files_touched_and_appends_ledger``); the
commit-per-iteration discipline itself is U4's (test_orchestrator.py).
R8 containment over the worker transcript:
``test_worker_containment_violation_raises``.
R14 verdict-fail evidence: ``test_verifier_fail_records_verifier_check_failures``;
zero-AC guard: ``test_verifier_requires_acceptance_criteria``.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from agent_families.pipeline.devserver import DevServerConfig
from agent_families.pipeline.gate import GateCommand
from agent_families.pipeline.orchestrator import (
    Orchestrator,
    TicketContext,
    TicketSpec,
    checkpoint_key,
)
from agent_families.pipeline.planning import plan_meta_key
from agent_families.pipeline.sessions import (
    verifier_profile,
    worker_profile,
)
from agent_families.pipeline.ticket_loop import (
    ENTRY_CHK,
    ENTRY_GATE_FAILURE,
    ENTRY_VERIFIER_FAILURE,
    ENTRY_WORKER,
    TicketLoop,
    TicketLoopConfig,
    TicketLoopError,
    build_verifier_prompt,
    build_worker_prompt,
    iteration_files,
    render_ledger,
    ticket_document,
    validate_verdict,
)
from agent_families.pipeline.workspace import ContainmentViolation, Workspace
from agent_families.store import Store

PY = sys.executable


# --- workspace / store scaffolding (the U4 test pattern) ----------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def make_ws(root: Path) -> Workspace:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text(
        "export const answer = 42\n", encoding="utf-8", newline="\n"
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.autocrlf", "false"),
        ("add", "-A"),
        ("commit", "-m", "init"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    return Workspace(root=root)


def seed_acs(store: Store, ticket_id: str, n_acs: int = 1) -> list[str]:
    """MSG/REQ/AC rows for an EXISTING trace_tkt row (FK order matters)."""
    msg_id = f"MSG-seed-{ticket_id}"
    ac_ids = []
    with store.transaction():
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_msg (id, content) VALUES (?, 'seed')",
            (msg_id,),
        )
        for i in range(n_acs):
            req_id = f"REQ-seed-{ticket_id}-{i}"
            ac_id = f"AC-{ticket_id}-{i}"
            store.conn.execute(
                "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)",
                (req_id, msg_id),
            )
            store.conn.execute(
                "INSERT INTO trace_ac (id, ticket_id, req_id) VALUES (?, ?, ?)",
                (ac_id, ticket_id, req_id),
            )
            ac_ids.append(ac_id)
    return ac_ids


def direct_setup(tmp_path: Path, n_acs: int = 1):
    """Store + workspace + run + one seeded ticket, for direct stage calls."""
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    run_id = store.create_run("specs/toy.md", 0)
    store.conn.execute("INSERT INTO trace_tkt (id) VALUES ('TKT-A')")
    seed_acs(store, "TKT-A", n_acs)
    return store, ws, run_id


def ctx_for(store, ws, run_id, ticket_id="TKT-A", iteration=1) -> TicketContext:
    return TicketContext(
        store=store,
        run_id=run_id,
        ticket_id=ticket_id,
        ralph_iteration=iteration,
        workspace=ws,
    )


# --- session-script scaffolding (the U3 fake) ----------------------------------------


def envelope(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1200,
        "num_turns": 2,
        "result": "done",
        "total_cost_usd": 0.04,
        "usage": {"input_tokens": 80, "output_tokens": 40},
        "structured_output": output,
    }


def worker_step(summary="implemented the ticket", **fields) -> dict:
    return {"role": "worker", "envelope": envelope({"summary": summary}), **fields}


def verifier_step(output: dict, **fields) -> dict:
    return {"role": "verifier", "envelope": envelope(output), **fields}


def repro(command="npm test", cwd=".", timeout=60, expected_exit=0) -> dict:
    return {
        "command": command,
        "cwd": cwd,
        "timeout": timeout,
        "expected_exit": expected_exit,
    }


def chk(ac: str, result="pass", evidence="check executed", **repro_kw) -> dict:
    return {"ac": ac, "result": result, "evidence": evidence, "repro": repro(**repro_kw)}


def verdict(*checks: dict, v="pass") -> dict:
    return {"verdict": v, "checks": list(checks)}


NEW_FILE_DIFF = """\
diff --git a/src/feature.ts b/src/feature.ts
new file mode 100644
--- /dev/null
+++ b/src/feature.ts
@@ -0,0 +1 @@
+export const feature = true
"""

MARKER_DIFF = """\
diff --git a/marker.txt b/marker.txt
new file mode 100644
--- /dev/null
+++ b/marker.txt
@@ -0,0 +1 @@
+done
"""

POISON_DIFF = """\
diff --git a/poison.txt b/poison.txt
new file mode 100644
--- /dev/null
+++ b/poison.txt
@@ -0,0 +1 @@
+failing code
"""

DIFFS = {"new-file.diff": NEW_FILE_DIFF, "marker.diff": MARKER_DIFF,
         "poison.diff": POISON_DIFF}


def gate_cmd(code: str, name="test", kind="gate_test", timeout_s=60.0) -> GateCommand:
    return GateCommand(
        name=name, argv=(PY, "-c", code), timeout_s=timeout_s, failure_kind=kind
    )


PASS_GATE = (gate_cmd("raise SystemExit(0)"),)
FAIL_GATE = (
    gate_cmd("import sys; sys.stderr.write('1 test failed\\n'); sys.exit(1)"),
)
# passes only once the worker has produced marker.txt (cwd = workspace root)
MARKER_GATE = (
    gate_cmd("import sys, os; sys.exit(0 if os.path.exists('marker.txt') else 1)"),
)
# fails while the escalated ticket's poison.txt is present
POISON_GATE = (
    gate_cmd("import sys, os; sys.exit(1 if os.path.exists('poison.txt') else 0)"),
)


def make_loop(
    tmp_path: Path,
    steps: list[dict],
    *,
    gate=PASS_GATE,
    devserver=None,
    max_retries=1,
) -> TicketLoop:
    script_dir = tmp_path / "script"
    script_dir.mkdir(parents=True, exist_ok=True)
    for name, diff in DIFFS.items():
        (script_dir / name).write_text(diff, encoding="utf-8", newline="\n")
    script = script_dir / "session-script.json"
    script.write_text(
        json.dumps({"steps": steps}, indent=2), encoding="utf-8", newline="\n"
    )
    config = TicketLoopConfig(
        worker_profile=worker_profile(
            model="sonnet", max_turns=20, timeout_s=60.0, test_commands=("npm test",)
        ),
        verifier_profile=verifier_profile(
            model="sonnet", max_turns=10, timeout_s=60.0,
            check_commands=("npm run build",),
        ),
        gate_commands=gate,
        transcript_dir=tmp_path / "transcripts",
        max_retries=max_retries,
        devserver=devserver,
        mode="scripted",
        script_path=script,
    )
    return TicketLoop(config)


def seeding_worker(loop: TicketLoop, store: Store, n_acs=1):
    """Wrap the real worker stage to seed AC rows on first touch per ticket —
    trace_ac rows can only exist after the orchestrator registers trace_tkt,
    which happens after the (stubbed) planner returns."""
    seeded: set[str] = set()

    def worker(ctx: TicketContext) -> None:
        if ctx.ticket_id not in seeded:
            seeded.add(ctx.ticket_id)
            seed_acs(store, ctx.ticket_id, n_acs)
        loop.worker(ctx)

    return worker


def run_orchestrated(store, ws, loop, tickets, *, cap, n_acs=1):
    orch = Orchestrator(
        store,
        planner_fn=lambda c: list(tickets),
        worker_fn=seeding_worker(loop, store, n_acs),
        gate_fn=loop.gate,
        verifier_fn=loop.verifier,
        ralph_cap=cap,
    )
    return orch.run("specs/toy.md", ws)


def checkpoint(store: Store, run_id: int) -> dict:
    return json.loads(store.get_meta(checkpoint_key(run_id)))


def failure_rows(store: Store, kind: str) -> list:
    return store.conn.execute(
        "SELECT * FROM failure_records WHERE failure_kind = ? ORDER BY id", (kind,)
    ).fetchall()


# --- the loop end-to-end on a trivial ticket (Verification clause) --------------------


def test_trivial_ticket_end_to_end_with_fakes(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    loop = make_loop(
        tmp_path,
        [
            worker_step("added the feature module", apply_diff="new-file.diff"),
            verifier_step(verdict(chk("AC-TKT-A-0", command="npm run build"))),
        ],
    )
    result = run_orchestrated(store, ws, loop, (TicketSpec("TKT-A"),), cap=2)
    assert result.status == "success"
    assert result.ticket_statuses == {"TKT-A": "done"}
    # full trace chain queryable: run -> ticket -> spans -> CHK (R16/R14)
    spans = store.conn.execute(
        "SELECT agent, status, ticket_id, files_json FROM trace_span"
        " ORDER BY rowid"  # insertion order: span ids are uuids
    ).fetchall()
    assert [s["agent"] for s in spans] == ["worker", "verifier"]
    assert all(s["status"] == "completed" for s in spans)  # finalized, none running
    assert all(s["ticket_id"] == "TKT-A" for s in spans)
    # files_touched derived mechanically from the iteration diff (R11)
    assert json.loads(spans[0]["files_json"]) == ["src/feature.ts"]
    # the diff actually landed and was committed by the orchestrator
    assert (ws.root / "src" / "feature.ts").exists()
    # CHK row carries the structured repro envelope, workspace-relative (R14)
    chk_row = store.conn.execute("SELECT * FROM trace_chk").fetchone()
    assert chk_row["ac_id"] == "AC-TKT-A-0"
    assert chk_row["result"] == "pass"
    stored = json.loads(chk_row["repro_command"])
    assert stored == repro(command="npm run build")
    assert not Path(stored["cwd"]).is_absolute()
    # ledger trail: worker summary + verifier chk entries
    kinds = [
        r["entry_kind"]
        for r in store.conn.execute(
            "SELECT entry_kind FROM ledger_entries ORDER BY id"
        ).fetchall()
    ]
    assert kinds == [ENTRY_WORKER, ENTRY_CHK]


# --- gate failure: typed record + full-iteration charge (R12/R13) ----------------------


def test_gate_failure_consumes_iteration_with_typed_record(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    loop = make_loop(
        tmp_path,
        [
            worker_step("first try, forgot the marker"),
            worker_step("added the marker", apply_diff="marker.diff"),
            verifier_step(verdict(chk("AC-TKT-A-0"))),
        ],
        gate=MARKER_GATE,
    )
    result = run_orchestrated(store, ws, loop, (TicketSpec("TKT-A"),), cap=3)
    assert result.status == "success"
    # the bounce charged a FULL iteration: two used, not one (R12)
    assert checkpoint(store, result.run_id)["iterations_used"] == {"TKT-A": 2}
    (record,) = failure_rows(store, "gate_test")
    assert record["ticket_id"] == "TKT-A"
    assert record["expected"] == "exit 0"
    assert "exit 1" in record["observed"]
    assert "marker.txt" in record["repro_command"]  # §7 repro command (R13)
    ledger = render_ledger(store, "TKT-A")
    assert "[gate_test]" in ledger
    assert "repro:" in ledger


def test_three_identical_gate_failures_hash_identically(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    loop = make_loop(tmp_path, [], gate=FAIL_GATE)
    details = [
        loop.gate(ctx_for(store, ws, run_id, iteration=i)).detail
        for i in (1, 2, 3)
    ]
    assert details[0].startswith("sha256:")
    assert details == [details[0]] * 3  # the no-progress detector's signal
    assert len(failure_rows(store, "gate_test")) == 3
    entries = store.conn.execute(
        "SELECT entry_kind, ralph_iteration FROM ledger_entries ORDER BY id"
    ).fetchall()
    assert [e["entry_kind"] for e in entries] == [ENTRY_GATE_FAILURE] * 3
    assert [e["ralph_iteration"] for e in entries] == [1, 2, 3]


# --- verdict-completeness lint (R14) ------------------------------------------------

AC_IDS = ["AC-1", "AC-2"]


@pytest.mark.parametrize(
    ("output", "match"),
    [
        (verdict(chk("AC-1")), "lacks a CHK"),
        (verdict(chk("AC-1"), chk("AC-9")), "unknown acceptance criterion"),
        (verdict(chk("AC-1"), chk("AC-2", result="fail")), "failing"),
        (verdict(chk("AC-1", cwd="C:\\evil"), chk("AC-2")), "absolute"),
        (verdict(chk("AC-1", cwd="/tmp/x"), chk("AC-2")), "absolute"),
        (verdict(chk("AC-1", cwd="../outside"), chk("AC-2")), "inside the workspace"),
        (
            verdict(chk("AC-1", command="type C:\\secrets.txt"), chk("AC-2")),
            "absolute",
        ),
        (verdict(chk("AC-1", command="  "), chk("AC-2")), "non-empty"),
        (verdict(chk("AC-1", timeout=0), chk("AC-2")), "timeout"),
        (verdict(chk("AC-1", expected_exit=-1), chk("AC-2")), "expected_exit"),
    ],
    ids=[
        "missing-chk-for-ac",
        "unknown-ac",
        "pass-with-failing-chk",
        "absolute-cwd-windows",
        "absolute-cwd-posix",
        "cwd-escapes-workspace",
        "absolute-path-in-command",
        "empty-command",
        "nonpositive-timeout",
        "negative-expected-exit",
    ],
)
def test_validate_verdict_rejects(output, match):
    violation = validate_verdict(output, AC_IDS)
    assert violation is not None
    assert match in violation


def test_validate_verdict_accepts_complete_verdicts():
    assert validate_verdict(verdict(chk("AC-1"), chk("AC-2")), AC_IDS) is None
    # a FAIL verdict with failing checks and full coverage is evidence-complete
    failing = verdict(chk("AC-1", result="fail"), chk("AC-2"), v="fail")
    assert validate_verdict(failing, AC_IDS) is None


def test_verifier_pass_missing_chk_rejected_retried_charged_to_infra(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    loop = make_loop(
        tmp_path,
        [
            worker_step(),
            # attempt 1: PASS covering only one of two ACs -> contract violation
            verifier_step(verdict(chk("AC-TKT-A-0"))),
            # the retry (same Ralph iteration) covers both
            verifier_step(verdict(chk("AC-TKT-A-0"), chk("AC-TKT-A-1"))),
        ],
    )
    # cap=1 PROVES the violation was charged to infra, not the worker's cap:
    # the retry happened inside the single allowed iteration.
    result = run_orchestrated(
        store, ws, loop, (TicketSpec("TKT-A"),), cap=1, n_acs=2
    )
    assert result.status == "success"
    assert checkpoint(store, result.run_id)["iterations_used"] == {"TKT-A": 1}
    (violation,) = failure_rows(store, "contract_violation")
    assert "lacks a CHK" in violation["observed"]
    assert "AC-TKT-A-1" in violation["observed"]
    # only the accepted attempt's checks persisted
    rows = store.conn.execute(
        "SELECT ac_id FROM trace_chk ORDER BY ac_id"
    ).fetchall()
    assert [r["ac_id"] for r in rows] == ["AC-TKT-A-0", "AC-TKT-A-1"]


def test_absolute_repro_path_rejected_through_the_session_retry_path(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    loop = make_loop(
        tmp_path,
        [
            verifier_step(verdict(chk("AC-TKT-A-0", cwd="C:\\evil\\dir"))),
            verifier_step(verdict(chk("AC-TKT-A-0"))),
        ],
    )
    result = loop.verifier(ctx_for(store, ws, run_id))
    assert result.passed
    (violation,) = failure_rows(store, "contract_violation")
    assert "absolute" in violation["observed"]


def test_verifier_fail_records_verifier_check_failures(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    loop = make_loop(
        tmp_path,
        [
            verifier_step(
                verdict(
                    chk(
                        "AC-TKT-A-0",
                        result="fail",
                        evidence="login page returned 500",
                        command="npx playwright test login.spec.ts",
                        cwd=".",
                    ),
                    v="fail",
                )
            ),
        ],
    )
    result = loop.verifier(ctx_for(store, ws, run_id))
    assert not result.passed
    chk_row = store.conn.execute("SELECT * FROM trace_chk").fetchone()
    assert chk_row["result"] == "fail"
    (record,) = failure_rows(store, "verifier_check")
    assert record["location"] == "AC-TKT-A-0"
    assert record["observed"] == "login page returned 500"
    stored = json.loads(record["repro_command"])  # the structured envelope (R14)
    assert stored["command"] == "npx playwright test login.spec.ts"
    assert stored["cwd"] == "."
    kinds = [
        r["entry_kind"]
        for r in store.conn.execute(
            "SELECT entry_kind FROM ledger_entries ORDER BY id"
        ).fetchall()
    ]
    assert kinds == [ENTRY_CHK, ENTRY_VERIFIER_FAILURE]


def test_verifier_requires_acceptance_criteria(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    run_id = store.create_run("specs/toy.md", 0)
    store.conn.execute("INSERT INTO trace_tkt (id) VALUES ('TKT-A')")  # no ACs
    loop = make_loop(tmp_path, [])
    with pytest.raises(TicketLoopError, match="no acceptance criteria"):
        loop.verifier(ctx_for(store, ws, run_id))


# --- escalation paths (R2 arm driven by this loop) --------------------------------------


def test_escalation_blocks_dependents(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    loop = make_loop(
        tmp_path,
        [worker_step("attempt 1"), worker_step("attempt 2")],
        gate=FAIL_GATE,
    )
    tickets = (TicketSpec("TKT-A"), TicketSpec("TKT-B", ("TKT-A",)))
    result = run_orchestrated(store, ws, loop, tickets, cap=2)
    assert result.status == "partial"
    assert result.ticket_statuses == {"TKT-A": "escalated", "TKT-B": "blocked"}
    assert len(failure_rows(store, "gate_test")) == 2


def test_escalated_code_does_not_poison_gate(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    loop = make_loop(
        tmp_path,
        [
            worker_step("A plants failing code", apply_diff="poison.diff"),
            worker_step("B implements cleanly"),
            verifier_step(verdict(chk("AC-TKT-B-0"))),
        ],
        gate=POISON_GATE,
    )
    # A and B are INDEPENDENT: A escalates at cap=1, then B must build from
    # the reset workspace — A's committed poison.txt would fail B's gate if
    # the reset-to-start-tag did not happen (R2).
    tickets = (TicketSpec("TKT-A"), TicketSpec("TKT-B"))
    result = run_orchestrated(store, ws, loop, tickets, cap=1)
    assert result.status == "partial"
    assert result.ticket_statuses == {"TKT-A": "escalated", "TKT-B": "done"}
    assert not (ws.root / "poison.txt").exists()


# --- worker stage: containment, files_touched, ledger (R8/R11) ---------------------------


def test_worker_derives_files_touched_and_appends_ledger(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    loop = make_loop(
        tmp_path, [worker_step("built the feature", apply_diff="new-file.diff")]
    )
    loop.worker(ctx_for(store, ws, run_id))
    span = store.conn.execute(
        "SELECT files_json, status FROM trace_span"
    ).fetchone()
    assert span["status"] == "completed"
    assert json.loads(span["files_json"]) == ["src/feature.ts"]
    entry = store.conn.execute("SELECT * FROM ledger_entries").fetchone()
    assert entry["entry_kind"] == ENTRY_WORKER
    assert entry["content"] == "built the feature"
    assert entry["ralph_iteration"] == 1
    assert "[iter 1] worker_summary: built the feature" in render_ledger(
        store, "TKT-A"
    )


def test_worker_containment_violation_raises(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    outside = str(tmp_path / "outside" / "evil.ts")
    loop = make_loop(
        tmp_path,
        [
            worker_step(
                "sneaky write",
                transcript_events=[
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "name": "Write",
                                    "input": {"file_path": outside},
                                }
                            ]
                        },
                    }
                ],
            )
        ],
    )
    with pytest.raises(ContainmentViolation, match="evil.ts"):
        loop.worker(ctx_for(store, ws, run_id))


def test_iteration_files_covers_modified_and_untracked(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    (ws.root / "src" / "index.ts").write_text(
        "export const answer = 43\n", encoding="utf-8", newline="\n"
    )
    (ws.root / "new.txt").write_text("x\n", encoding="utf-8", newline="\n")
    assert iteration_files(ws.root) == ["new.txt", "src/index.ts"]


# --- dev server bracketing (R15) ---------------------------------------------------------


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connectable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.25):
            return True
    except OSError:
        return False


DEV_SERVER_PY = """\
import socket, sys

port = int(sys.argv[1])
marker = sys.argv[2]
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    server.bind(("127.0.0.1", port))
except OSError:
    sys.exit(1)
server.listen(5)
with open(marker, "w") as fh:
    fh.write("started\\n")
while True:
    conn, _ = server.accept()
    conn.close()
"""


def test_devserver_bracketing_around_verifier(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    script = tmp_path / "fake-dev-server.py"
    script.write_text(DEV_SERVER_PY, encoding="utf-8", newline="\n")
    marker = tmp_path / "server-started.txt"
    port = free_port()
    loop = make_loop(
        tmp_path,
        [verifier_step(verdict(chk("AC-TKT-A-0")))],
        devserver=DevServerConfig(
            command=(PY, str(script), "{port}", str(marker)),
            port=port,
            port_attempts=3,
            readiness_timeout_s=10.0,
        ),
    )
    result = loop.verifier(ctx_for(store, ws, run_id))
    assert result.passed
    # the readiness probe gated the verifier start: the server HAD bound
    assert marker.exists()
    # ...and teardown on verdict: nothing is listening any more (R15)
    assert not connectable(port)


# --- rendering, documents, prompts (R11/R14 plumbing) --------------------------------------


def test_render_ledger_formats_failures_and_summaries(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    assert render_ledger(store, "TKT-A") == "(no prior iterations)"
    loop = make_loop(tmp_path, [worker_step("first pass")], gate=FAIL_GATE)
    loop.worker(ctx_for(store, ws, run_id))
    loop.gate(ctx_for(store, ws, run_id))
    ledger = render_ledger(store, "TKT-A")
    assert "worker_summary: first pass" in ledger
    # the §7 failure fields ride into the rendered view (R11)
    assert "expected: exit 0" in ledger
    assert "observed: exit 1" in ledger
    assert "repro:" in ledger
    assert "1 test failed" in ledger


def test_ticket_document_prefers_plan_and_falls_back_to_trace_rows(tmp_path):
    store, ws, run_id = direct_setup(tmp_path)
    # no plan document: fall back to trace_ac rows
    doc = ticket_document(store, run_id, "TKT-A")
    assert doc["id"] == "TKT-A"
    assert [ac["id"] for ac in doc["acceptance_criteria"]] == ["AC-TKT-A-0"]
    # with U5's persisted plan document, the canonical ticket entry wins
    store.set_meta(
        plan_meta_key(run_id),
        json.dumps(
            {
                "run_id": run_id,
                "tickets": [
                    {
                        "id": "TKT-A",
                        "title": "Login flow",
                        "description": "Build it",
                        "covers": ["REQ-1"],
                        "depends_on": [],
                        "files": ["src/login.ts"],
                        "acceptance_criteria": [
                            {"id": "AC-TKT-A-0", "text": "user can log in",
                             "req": "REQ-1"}
                        ],
                    }
                ],
            }
        ),
    )
    doc = ticket_document(store, run_id, "TKT-A")
    assert doc["title"] == "Login flow"
    assert doc["files"] == ["src/login.ts"]


def test_prompt_builders(tmp_path):
    ticket = {
        "id": "TKT-A",
        "title": "Login flow",
        "description": "Build the login page",
        "covers": ["REQ-1"],
        "depends_on": [],
        "files": ["src/login.ts"],
        "acceptance_criteria": [
            {"id": "AC-1", "text": "user can log in", "req": "REQ-1"}
        ],
    }
    ledger = "[iter 1] gate_failure: [gate_test] test — expected: exit 0"
    worker_prompt = build_worker_prompt(ticket, ledger)
    assert "TKT-A" in worker_prompt
    assert "src/login.ts" in worker_prompt
    assert ledger in worker_prompt  # the worker READS the ledger (R11)
    assert "read-only" in worker_prompt  # ...and is told it appends nothing
    verifier_prompt = build_verifier_prompt(ticket, "http://127.0.0.1:4173")
    assert "AC-1" in verifier_prompt
    assert "http://127.0.0.1:4173" in verifier_prompt
    # R14: never anchor on the worker's unit-suite results
    assert "never anchor" in verifier_prompt
    assert "absolute paths" in verifier_prompt
    assert "never start or stop" in verifier_prompt  # R15: orchestrator's server
    no_server = build_verifier_prompt(ticket, None)
    assert "http://" not in no_server


def test_config_validation(tmp_path):
    profile_kw = dict(model="sonnet", max_turns=5, timeout_s=30.0)
    with pytest.raises(TicketLoopError, match="gate_commands"):
        TicketLoopConfig(
            worker_profile=worker_profile(**profile_kw, test_commands=("npm test",)),
            verifier_profile=verifier_profile(
                **profile_kw, check_commands=("npm run build",)
            ),
            gate_commands=(),
            transcript_dir=tmp_path,
            max_retries=1,
        )
