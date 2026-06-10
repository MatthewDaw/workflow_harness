"""plan-002 U8: toy-spec corpus and end-to-end acceptance (R19, e2e of R1–R18).

The full pipeline — spec file → MSG synthesis → planner session → plan lints →
persisted canonical plan → orchestrator → worker/gate/verifier ticket loop →
settlement — driven over the committed corpus in ``agent-families/specs/``
with the U3 scripted-agent fake. Fully offline and CI-safe: zero quota, no
``claude`` on PATH; the gate runs real subprocess commands (tiny ``python -c``
programs, the U6 precedent).

Each spec carries a machine-readable ``Expected outcome`` json block (R19);
:func:`read_expected` parses it and :func:`assert_expected` checks the run
against it, so the corpus and the suite cannot drift apart silently.

Run-assembly note (recorded deviation): U5's ``run_planning`` persists the
canonical ``trace_tkt`` rows itself, while ``Orchestrator.run``'s planning arm
insists on inserting fresh ticket rows (raising on duplicates) — U8 is the
first unit to wire both. The faithful assembly is therefore the documented run
lifecycle itself: ``create_run`` → ``run_planning`` (run status ``planning``,
plan + traceability persisted) → write the post-planning checkpoint document
(exactly the shape ``_drive`` saves after planning, R3) → ``Orchestrator.resume``
re-enters at the recorded state and executes. ``planner_fn`` is wired to fail
loudly — planning must never re-run.

The resume scenario uses REAL subprocess termination (reserved for this one
coarse e2e by U4): a child orchestrator process blocks at the ``after_gate``
crash seam mid-run, the test kills it, and a fresh orchestrator resumes over
the same durable store + session-script cursor to ``success``.

## Conformance

Scenario / invariant -> test mapping (plan-002 U8 Test scenarios, 1:1):

- trivial spec: one ticket, ``success``, full trace chain queryable
  (run -> ticket -> spans -> CHK with repro envelopes):
  ``test_trivial_spec_success_with_queryable_trace_chain``
- multi-ticket spec: dependency order respected, ``success``:
  ``test_multi_ticket_dependency_order_respected``
- ambiguous spec: ``assumptions[]`` non-empty in run report:
  ``test_ambiguous_spec_surfaces_assumptions_in_run_report``
- escalation spec: scripted persistent failure -> ticket escalated, dependent
  blocked, run ``partial``:
  ``test_escalation_spec_partial_with_blocked_dependent``
- resume e2e: kill the multi-ticket run mid-flight, resume to ``success``:
  ``test_resume_after_real_subprocess_kill_midflight``
- R19 corpus integrity: every committed spec exists, chunks into MSG
  paragraphs, and carries a parseable expected-outcome assertion block:
  ``test_corpus_spec_carries_expected_outcome_assertions``
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

from agent_families.pipeline.orchestrator import Orchestrator, checkpoint_key
from agent_families.pipeline.planning import (
    chunk_spec,
    plan_report,
    run_planning,
)
from agent_families.pipeline.sessions import (
    planner_profile,
    verifier_profile,
    worker_profile,
)
from agent_families.pipeline.ticket_loop import TicketLoop, TicketLoopConfig
from agent_families.pipeline.workspace import Workspace
from agent_families.store import RUN_TERMINAL_STATUSES, Store
from agent_families.pipeline.gate import GateCommand

PY = sys.executable
SPECS_DIR = Path(__file__).resolve().parents[1] / "specs"
SPEC_NAMES = (
    "01-trivial.md",
    "02-multi-ticket.md",
    "03-ambiguous.md",
    "04-escalation.md",
)

# Scenario parameters for the documented scripted episodes (test data, not
# system tunables — live runs route their budgets per the README procedure).
RALPH_CAP = 2
PLANNER_CAP = 1
MAX_RETRIES = 1
SIZE_BUDGET = 4


# --- canonical-id helpers (U5 mints these shapes at persistence) ---------------


def tkt(run_id: int, index: int) -> str:
    return f"TKT-r{run_id}-{index:03d}"


def ac(run_id: int, index: int) -> str:
    return f"AC-r{run_id}-{index:03d}"


def msg_for(run_id: int, paragraphs: list[str], marker: str) -> str:
    """The MSG id of the spec paragraph containing ``marker`` (real provenance)."""
    for index, paragraph in enumerate(paragraphs):
        if marker in paragraph:
            return f"MSG-r{run_id}-p{index:03d}"
    raise AssertionError(f"no spec paragraph mentions {marker!r}")


# --- session-script scaffolding (the U3 fake; U5/U6 helper shapes) --------------


def envelope(output: dict, *, cost_usd: float = 0.04, num_turns: int = 2) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1200,
        "num_turns": num_turns,
        "result": "done",
        "total_cost_usd": cost_usd,
        "usage": {"input_tokens": 80, "output_tokens": 40},
        "structured_output": output,
    }


def planner_step(plan: dict) -> dict:
    return {
        "role": "planner",
        "envelope": envelope(plan, cost_usd=0.02, num_turns=1),
    }


def worker_step(summary: str, **fields) -> dict:
    return {"role": "worker", "envelope": envelope({"summary": summary}), **fields}


def verifier_step(output: dict) -> dict:
    return {"role": "verifier", "envelope": envelope(output)}


def repro(command="npm test", cwd=".", timeout=60, expected_exit=0) -> dict:
    return {
        "command": command,
        "cwd": cwd,
        "timeout": timeout,
        "expected_exit": expected_exit,
    }


def chk(ac_id: str, result="pass", evidence="check executed", **repro_kw) -> dict:
    return {
        "ac": ac_id,
        "result": result,
        "evidence": evidence,
        "repro": repro(**repro_kw),
    }


def verdict(*checks: dict, v="pass") -> dict:
    return {"verdict": v, "checks": list(checks)}


def requirement(local_id: str, text: str, source_msg: str) -> dict:
    return {"id": local_id, "text": text, "source_msg": source_msg}


def ticket(
    local_id: str,
    title: str,
    covers: list[str],
    depends_on: list[str],
    files: list[str],
    acs: list[dict],
) -> dict:
    return {
        "id": local_id,
        "title": title,
        "description": f"Implement: {title}.",
        "covers": covers,
        "depends_on": depends_on,
        "files": files,
        "acceptance_criteria": acs,
    }


def new_file_diff(path: str, line: str) -> str:
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        "@@ -0,0 +1 @@\n"
        f"+{line}\n"
    )


DIFFS = {
    "counter.diff": new_file_diff("src/counter.ts", "export const bookmarkCount = () => 0"),
    "api.diff": new_file_diff("src/api.ts", "export const listBookmarks = () => []"),
    "ui.diff": new_file_diff("src/ui.ts", "export const renderDashboard = () => null"),
    "signin.diff": new_file_diff("src/signin.ts", "export const signIn = () => true"),
}


def gate_cmd(code: str) -> GateCommand:
    return GateCommand(
        name="test", argv=(PY, "-c", code), timeout_s=60.0, failure_kind="gate_test"
    )


PASS_GATE = (gate_cmd("raise SystemExit(0)"),)
FAIL_GATE = (
    gate_cmd("import sys; sys.stderr.write('1 test failed\\n'); sys.exit(1)"),
)


# --- per-spec scripted episodes (steps in exact consumption order) ---------------
# One script per run, shared by the planner and the ticket loop: the durable
# cursor consumes steps sequentially, so a wrong execution order trips the
# fake's role check and fails loudly.


def trivial_steps(run_id: int, paragraphs: list[str]) -> list[dict]:
    plan = {
        "requirements": [
            requirement(
                "R-counter",
                "the home page shows a stored-bookmark counter",
                msg_for(run_id, paragraphs, "counter"),
            )
        ],
        "tickets": [
            ticket(
                "T-counter",
                "Bookmark counter widget",
                covers=["R-counter"],
                depends_on=[],
                files=["src/counter.ts"],
                acs=[
                    {
                        "id": "A-counter",
                        "text": "the home page shows the stored-bookmark count",
                        "req": "R-counter",
                    }
                ],
            )
        ],
        "assumptions": [],
    }
    return [
        planner_step(plan),
        worker_step("implemented the counter widget", apply_diff="counter.diff"),
        verifier_step(verdict(chk(ac(run_id, 0), command="npm run build"))),
    ]


def multi_ticket_steps(run_id: int, paragraphs: list[str]) -> list[dict]:
    plan = {
        "requirements": [
            requirement(
                "R-ui",
                "the dashboard lists bookmarks from the endpoint",
                msg_for(run_id, paragraphs, "dashboard"),
            ),
            requirement(
                "R-api",
                "a bookmarks API endpoint returns stored bookmarks",
                msg_for(run_id, paragraphs, "API"),
            ),
        ],
        "tickets": [
            # The DEPENDENT ticket is listed first, so its canonical id
            # (TKT-...-000) sorts BEFORE its dependency's (TKT-...-001):
            # execution order must come from the DAG, not the lexicographic
            # tie-break (R2).
            ticket(
                "T-ui",
                "Bookmarks dashboard",
                covers=["R-ui"],
                depends_on=["T-api"],
                files=["src/ui.ts"],
                acs=[{"id": "A-ui", "text": "dashboard lists bookmarks", "req": "R-ui"}],
            ),
            ticket(
                "T-api",
                "Bookmarks API endpoint",
                covers=["R-api"],
                depends_on=[],
                files=["src/api.ts"],
                acs=[{"id": "A-api", "text": "endpoint returns JSON", "req": "R-api"}],
            ),
        ],
        "assumptions": [],
    }
    # AC counter is minted in planner-output order: A-ui -> AC-000, A-api -> AC-001.
    return [
        planner_step(plan),
        worker_step("implemented the API endpoint", apply_diff="api.diff"),
        verifier_step(verdict(chk(ac(run_id, 1)))),
        worker_step("implemented the dashboard", apply_diff="ui.diff"),
        verifier_step(verdict(chk(ac(run_id, 0)))),
    ]


def ambiguous_steps(run_id: int, paragraphs: list[str]) -> list[dict]:
    plan = {
        "requirements": [
            requirement(
                "R-signin",
                "users can sign in",
                msg_for(run_id, paragraphs, "sign in"),
            )
        ],
        "tickets": [
            ticket(
                "T-signin",
                "Sign-in flow",
                covers=["R-signin"],
                depends_on=[],
                files=["src/signin.ts"],
                acs=[{"id": "A-signin", "text": "a user can sign in", "req": "R-signin"}],
            )
        ],
        "assumptions": [
            "Sign-in is username-only; no password, magic link, or SSO.",
            "A failed sign-in attempt re-shows the form with a generic error.",
        ],
    }
    return [
        planner_step(plan),
        worker_step("implemented username-only sign-in", apply_diff="signin.diff"),
        verifier_step(verdict(chk(ac(run_id, 0)))),
    ]


def escalation_steps(run_id: int, paragraphs: list[str]) -> list[dict]:
    plan = {
        "requirements": [
            requirement(
                "R-health",
                "a health endpoint reports build status",
                msg_for(run_id, paragraphs, "health endpoint"),
            ),
            requirement(
                "R-status",
                "the status page renders the health output",
                msg_for(run_id, paragraphs, "status page"),
            ),
        ],
        "tickets": [
            ticket(
                "T-health",
                "Health endpoint",
                covers=["R-health"],
                depends_on=[],
                files=["src/health.ts"],
                acs=[{"id": "A-health", "text": "health reports build status", "req": "R-health"}],
            ),
            ticket(
                "T-status",
                "Status page",
                covers=["R-status"],
                depends_on=["T-health"],
                files=["src/status.ts"],
                acs=[{"id": "A-status", "text": "status page renders health", "req": "R-status"}],
            ),
        ],
        "assumptions": [],
    }
    # The worker never satisfies the FAIL_GATE: one worker step per cap slot,
    # the verifier is never reached, the ticket escalates (R2).
    return [
        planner_step(plan),
        worker_step("attempt 1: health endpoint"),
        worker_step("attempt 2: still failing the gate"),
    ]


# --- run assembly (the documented created -> planning -> executing lifecycle) ----


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


def script_path_for(base: Path) -> Path:
    return base / "script" / "session-script.json"


def write_script(base: Path, steps: list[dict]) -> Path:
    script = script_path_for(base)
    script.parent.mkdir(parents=True, exist_ok=True)
    for name, diff in DIFFS.items():
        (script.parent / name).write_text(diff, encoding="utf-8", newline="\n")
    script.write_text(
        json.dumps({"steps": steps}, indent=2), encoding="utf-8", newline="\n"
    )
    return script


def attach_orchestrator(
    store: Store, base: Path, *, gate, cap: int, crash_at=None
) -> Orchestrator:
    """The real U6 stages on the U4 seams; planning must never re-run."""
    loop = TicketLoop(
        TicketLoopConfig(
            worker_profile=worker_profile(
                model="sonnet", max_turns=20, timeout_s=60.0,
                test_commands=("npm test",),
            ),
            verifier_profile=verifier_profile(
                model="sonnet", max_turns=10, timeout_s=60.0,
                check_commands=("npm run build",),
            ),
            gate_commands=gate,
            transcript_dir=base / "transcripts",
            max_retries=MAX_RETRIES,
            mode="scripted",
            script_path=script_path_for(base),
        )
    )

    def planning_already_done(ctx):
        raise AssertionError(
            "planner_fn must never run: planning happened in run_planning and"
            " the orchestrator resumes from the post-planning checkpoint"
        )

    return Orchestrator(
        store,
        planner_fn=planning_already_done,
        worker_fn=loop.worker,
        gate_fn=loop.gate,
        verifier_fn=loop.verifier,
        ralph_cap=cap,
        crash_at=crash_at,
    )


def build_pipeline(
    base: Path, spec_path: Path, steps_fn, *, gate, cap: int, crash_at=None
):
    """Assemble one run end to end, stopping at the post-planning checkpoint.

    Returns ``(store, orchestrator, run_id)``; the caller drives execution via
    ``orchestrator.resume(run_id)`` (the documented R3 re-entry).
    """
    store = Store(base / "library.db")
    store.migrate()
    ws = make_ws(base / "ws")
    run_id = store.create_run(f"specs/{spec_path.name}", store.current_snapshot_id())
    paragraphs = chunk_spec(spec_path.read_text(encoding="utf-8"))
    script = write_script(base, steps_fn(run_id, paragraphs))
    run_planning(
        store,
        run_id,
        spec_path,
        planner_profile(model="sonnet", max_turns=4, timeout_s=60.0),
        transcript_dir=base / "transcripts",
        cap=PLANNER_CAP,
        max_retries=MAX_RETRIES,
        size_budget=SIZE_BUDGET,
        mode="scripted",
        script_path=script,
    )
    document = plan_report(store, run_id)
    # The post-planning checkpoint document — exactly what _drive saves after
    # the planning stage (R3); resume re-enters at this recorded state.
    store.set_meta(
        checkpoint_key(run_id),
        json.dumps(
            {
                "workspace_root": str(ws.root),
                "planned": True,
                "tickets": [
                    {"id": t["id"], "depends_on": list(t["depends_on"])}
                    for t in document["tickets"]
                ],
                "iterations_used": {},
                "gate_passed": {},
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    orch = attach_orchestrator(store, base, gate=gate, cap=cap, crash_at=crash_at)
    return store, orch, run_id


def run_spec(base: Path, spec_name: str, steps_fn, *, gate, cap: int = RALPH_CAP):
    store, orch, run_id = build_pipeline(
        base, SPECS_DIR / spec_name, steps_fn, gate=gate, cap=cap
    )
    return store, orch.resume(run_id), run_id


# --- the R19 expected-outcome contract ---------------------------------------------


def read_expected(spec_path: Path) -> dict:
    text = spec_path.read_text(encoding="utf-8")
    match = re.search(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    assert match is not None, (
        f"{spec_path.name} carries no expected-outcome json block (R19)"
    )
    return json.loads(match.group(1))


def assert_expected(store: Store, result, run_id: int, spec_name: str) -> dict:
    """Assert the run against the spec's committed expected-outcome block."""
    expected = read_expected(SPECS_DIR / spec_name)
    assert result.status == expected["terminal"]
    assert store.get_run(run_id)["status"] == expected["terminal"]
    statuses = list(result.ticket_statuses.values())
    assert len(statuses) == expected["tickets"]
    assert statuses.count("done") == expected["done"]
    assert statuses.count("escalated") == expected["escalated"]
    assert statuses.count("blocked") == expected["blocked"]
    assumptions = plan_report(store, run_id)["assumptions"]
    assert len(assumptions) >= expected["min_assumptions"]
    return expected


@pytest.mark.parametrize("name", SPEC_NAMES)
def test_corpus_spec_carries_expected_outcome_assertions(name):
    spec = SPECS_DIR / name
    assert spec.exists(), f"corpus spec missing: {spec} (R19)"
    assert chunk_spec(spec.read_text(encoding="utf-8")), "spec yields no MSG paragraphs"
    expected = read_expected(spec)
    assert expected["terminal"] in RUN_TERMINAL_STATUSES
    for key in ("tickets", "done", "escalated", "blocked", "min_assumptions"):
        assert isinstance(expected[key], int), f"expected[{key!r}] must be an int"


# --- scenario 1: trivial spec ---------------------------------------------------------


def test_trivial_spec_success_with_queryable_trace_chain(tmp_path):
    store, result, run_id = run_spec(
        tmp_path, "01-trivial.md", trivial_steps, gate=PASS_GATE
    )
    assert_expected(store, result, run_id, "01-trivial.md")
    assert result.ticket_statuses == {tkt(run_id, 0): "done"}

    # Full trace chain in ONE query: run -> spans -> ticket -> AC -> CHK (R16/R14).
    chain = store.conn.execute(
        "SELECT r.status AS run_status, s.agent AS agent, s.status AS span_status,"
        "       t.id AS ticket_id, t.status AS ticket_status,"
        "       c.repro_command AS repro, c.result AS chk_result"
        " FROM runs r"
        " JOIN trace_span s ON s.run_id = r.id AND s.ticket_id IS NOT NULL"
        " JOIN trace_tkt t ON t.id = s.ticket_id"
        " JOIN trace_ac a ON a.ticket_id = t.id"
        " JOIN trace_chk c ON c.ac_id = a.id"
        " WHERE r.id = ? ORDER BY s.rowid",
        (run_id,),
    ).fetchall()
    assert [row["agent"] for row in chain] == ["worker", "verifier"]
    assert all(row["run_status"] == "success" for row in chain)
    assert all(row["ticket_status"] == "done" for row in chain)
    assert all(row["span_status"] == "completed" for row in chain)
    assert all(row["chk_result"] == "pass" for row in chain)
    # CHK rows carry the structured repro envelope, workspace-relative (R14).
    stored = json.loads(chain[0]["repro"])
    assert stored == repro(command="npm run build")
    assert not Path(stored["cwd"]).is_absolute()

    # Planner included: three finalized spans, none left running (R16/R3).
    spans = store.conn.execute(
        "SELECT agent, status, ticket_id, files_json FROM trace_span"
        " WHERE run_id = ? ORDER BY rowid",
        (run_id,),
    ).fetchall()
    assert [s["agent"] for s in spans] == ["planner", "worker", "verifier"]
    assert all(s["status"] == "completed" for s in spans)
    assert spans[0]["ticket_id"] is None  # planner spans have no ticket
    assert json.loads(spans[1]["files_json"]) == ["src/counter.ts"]  # R11

    # REQ provenance points at a synthesized MSG row of THIS run (R9).
    req = store.conn.execute(
        "SELECT source_msg_id FROM trace_req WHERE id = ?",
        (f"REQ-r{run_id}-000",),
    ).fetchone()
    assert req["source_msg_id"].startswith(f"MSG-r{run_id}-p")

    # Settlement aggregated span costs into the run row (R16).
    run = store.get_run(run_id)
    assert run["total_cost_usd"] == pytest.approx(0.02 + 0.04 + 0.04)
    assert run["total_turns"] == 1 + 2 + 2
    assert run["cost_partial_spans"] == 0

    # The worker diff landed and the orchestrator committed everything (R11).
    ws_root = tmp_path / "ws"
    assert (ws_root / "src" / "counter.ts").exists()
    porcelain = subprocess.run(
        ["git", "-C", str(ws_root), "status", "--porcelain"],
        capture_output=True, encoding="utf-8", check=True,
    )
    assert porcelain.stdout.strip() == ""


# --- scenario 2: multi-ticket dependency order ------------------------------------------


def test_multi_ticket_dependency_order_respected(tmp_path):
    store, result, run_id = run_spec(
        tmp_path, "02-multi-ticket.md", multi_ticket_steps, gate=PASS_GATE
    )
    assert_expected(store, result, run_id, "02-multi-ticket.md")
    ui, api = tkt(run_id, 0), tkt(run_id, 1)

    # The canonical plan carries the dependency edge (ui depends on api).
    document = plan_report(store, run_id)
    deps = {t["id"]: t["depends_on"] for t in document["tickets"]}
    assert deps == {ui: [api], api: []}

    # Execution order came from the DAG, not the lexicographic tie-break:
    # api (TKT-...-001) ran to done strictly before ui (TKT-...-000) started.
    transitions = [
        (row["ticket_id"], row["to_status"])
        for row in store.conn.execute(
            "SELECT ticket_id, to_status FROM ticket_status_transitions"
            " WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    ]
    assert transitions == [
        (api, "in_progress"),
        (api, "done"),
        (ui, "in_progress"),
        (ui, "done"),
    ]

    # Span order tells the same story (R16): api's worker/verifier before ui's.
    ticket_spans = [
        (row["agent"], row["ticket_id"])
        for row in store.conn.execute(
            "SELECT agent, ticket_id FROM trace_span"
            " WHERE run_id = ? AND ticket_id IS NOT NULL ORDER BY rowid",
            (run_id,),
        ).fetchall()
    ]
    assert ticket_spans == [
        ("worker", api), ("verifier", api), ("worker", ui), ("verifier", ui),
    ]
    assert (tmp_path / "ws" / "src" / "api.ts").exists()
    assert (tmp_path / "ws" / "src" / "ui.ts").exists()


# --- scenario 3: ambiguous spec -> assumptions[] -----------------------------------------


def test_ambiguous_spec_surfaces_assumptions_in_run_report(tmp_path):
    store, result, run_id = run_spec(
        tmp_path, "03-ambiguous.md", ambiguous_steps, gate=PASS_GATE
    )
    assert_expected(store, result, run_id, "03-ambiguous.md")
    report = plan_report(store, run_id)
    assert report["assumptions"] == [
        "Sign-in is username-only; no password, magic link, or SSO.",
        "A failed sign-in attempt re-shows the form with a generic error.",
    ]
    # Persisted with the plan document, not just surfaced (R10).
    raw = store.get_meta(f"plan:run:{run_id}")
    assert "username-only" in raw


# --- scenario 4: engineered escalation ----------------------------------------------------


def test_escalation_spec_partial_with_blocked_dependent(tmp_path):
    store, result, run_id = run_spec(
        tmp_path, "04-escalation.md", escalation_steps, gate=FAIL_GATE
    )
    assert_expected(store, result, run_id, "04-escalation.md")
    health, status_page = tkt(run_id, 0), tkt(run_id, 1)
    assert result.ticket_statuses == {health: "escalated", status_page: "blocked"}

    # Every cap slot produced a typed gate failure with a repro command (R12/R13).
    failures = store.conn.execute(
        "SELECT ticket_id, repro_command FROM failure_records"
        " WHERE failure_kind = 'gate_test' ORDER BY id"
    ).fetchall()
    assert len(failures) == RALPH_CAP
    assert all(f["ticket_id"] == health for f in failures)
    assert all(f["repro_command"].strip() for f in failures)

    # The blocked dependent was never attempted: no in_progress transition,
    # no spans, no script steps consumed beyond the planner + cap workers.
    attempted = store.conn.execute(
        "SELECT COUNT(*) AS n FROM ticket_status_transitions"
        " WHERE ticket_id = ? AND to_status = 'in_progress'",
        (status_page,),
    ).fetchone()["n"]
    assert attempted == 0
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_span WHERE ticket_id = ?",
        (status_page,),
    ).fetchone()["n"] == 0


# --- scenario 5: resume after a REAL subprocess kill mid-flight ----------------------------

CHILD_DRIVER = """\
import sys, time
from pathlib import Path

sys.path.insert(0, {tests_dir})
import test_pipeline_e2e as e2e

base = Path(sys.argv[1])
sentinel = base / "killpoint.ready"


def block_at_first_gate_pass(step):
    if step == "after_gate":
        sentinel.write_text("ready", encoding="utf-8")
        time.sleep(600)  # the parent test kills this process here


store, orch, run_id = e2e.build_pipeline(
    base,
    e2e.SPECS_DIR / "02-multi-ticket.md",
    e2e.multi_ticket_steps,
    gate=e2e.PASS_GATE,
    cap=e2e.RALPH_CAP,
    crash_at=block_at_first_gate_pass,
)
(base / "run-id.txt").write_text(str(run_id), encoding="utf-8")
orch.resume(run_id)
"""


def test_resume_after_real_subprocess_kill_midflight(tmp_path):
    child = tmp_path / "child_driver.py"
    child.write_text(
        CHILD_DRIVER.format(tests_dir=repr(str(Path(__file__).parent))),
        encoding="utf-8",
        newline="\n",
    )
    log_path = tmp_path / "child.log"
    sentinel = tmp_path / "killpoint.ready"
    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(child), str(tmp_path)],
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + 120
        try:
            while not sentinel.exists():
                if proc.poll() is not None:
                    pytest.fail(
                        "child orchestrator exited before the kill point:\n"
                        + log_path.read_text(encoding="utf-8")
                    )
                if time.monotonic() > deadline:
                    pytest.fail("child orchestrator never reached the kill point")
                time.sleep(0.05)
        finally:
            proc.kill()  # REAL subprocess termination (the U4 reservation)
            proc.wait(timeout=30)

    run_id = int((tmp_path / "run-id.txt").read_text(encoding="utf-8"))
    store = Store(tmp_path / "library.db")
    ui, api = tkt(run_id, 0), tkt(run_id, 1)

    # Killed mid-flight: the run is non-terminal, the first ticket in flight.
    assert store.get_run(run_id)["status"] == "executing"
    statuses = {
        row["id"]: row["status"]
        for row in store.conn.execute("SELECT id, status FROM trace_tkt").fetchall()
    }
    assert statuses == {api: "in_progress", ui: "pending"}

    # A FRESH orchestrator resumes over the same durable store and session
    # script (its cursor sidecar survived the kill) and completes the run.
    orch = attach_orchestrator(store, tmp_path, gate=PASS_GATE, cap=RALPH_CAP)
    result = orch.resume(run_id)
    assert result.status == "success"
    assert result.ticket_statuses == {api: "done", ui: "done"}

    # The post-gate resume skipped straight to the verifier without
    # double-charging the cap (R3): one iteration per ticket.
    state = json.loads(store.get_meta(checkpoint_key(run_id)))
    assert state["iterations_used"] == {api: 1, ui: 1}
    assert store.orphan_spans(run_id) == []
