"""plan-002 U4: orchestrator core — run/ticket state machine, checkpoint/resume.

Fully offline: stages are scripted stubs and an injectable stub gate per the
plan's approach (the real gate runs only in U6's tests and the U8 e2e); the
workspace is a small real git repo (U2's commit/tag/reset discipline runs for
real). Forced kills use the ``crash_at`` injection seam — a raised
``SimulatedCrash`` is process death: durable state stays mid-flight and a
FRESH orchestrator resumes, per R3's "resume = iteration boundary".

Scenario map (plan-002 U4 Test scenarios, 1:1):

- linear DAG executes in order:
  ``test_linear_dag_executes_in_dependency_order``
- diamond DAG with mid-failure blocks only dependents and yields ``partial``:
  ``test_diamond_dag_mid_failure_blocks_only_dependents_yields_partial``
- kill between gate and verifier -> resume completes the ticket without
  double-charging cap: ``test_kill_between_gate_and_verifier_*``
- kill mid-worker-session -> orphan span aborted, workspace reset, iteration
  replayed at the same index: ``test_kill_mid_worker_session_*``
- quota exception mid-run -> ``aborted_quota``, resume completes:
  ``test_quota_mid_run_*`` (+ the planning-stage variant)
- run with all tickets done -> ``success``:
  ``test_all_tickets_done_settles_success_with_cost_totals``
- deterministic re-run of the same fake script produces identical ticket
  ordering: ``test_deterministic_rerun_produces_identical_ticket_ordering``

Verification clause (kills at three distinct points): after_commit
(pre-gate), after_gate (pre-verifier), and mid-worker-session.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from agent_families.pipeline.orchestrator import (
    GateResult,
    Orchestrator,
    OrchestratorError,
    PlanFailed,
    RunResult,
    TicketSpec,
    VerifierResult,
    checkpoint_key,
)
from agent_families.pipeline.sessions import (
    SessionQuotaExhausted,
    SessionUnavailable,
)
from agent_families.pipeline.workspace import Workspace
from agent_families.store import Store


class SimulatedCrash(Exception):
    """Stands in for orchestrator process death at a crash_at kill point."""


# --- scaffolding -----------------------------------------------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def make_ws(root: Path) -> Workspace:
    """A small real git repo standing in for a U2-instantiated workspace."""
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


def build_orchestrator(
    store: Store,
    *,
    tickets: tuple[TicketSpec, ...] = (TicketSpec("TKT-A"),),
    planner=None,
    worker=None,
    gate=None,
    verifier=None,
    cap: int = 3,
    crash_at=None,
) -> Orchestrator:
    return Orchestrator(
        store,
        planner_fn=planner or (lambda ctx: list(tickets)),
        worker_fn=worker or (lambda ctx: None),
        gate_fn=gate or (lambda ctx: GateResult(passed=True)),
        verifier_fn=verifier or (lambda ctx: VerifierResult(passed=True)),
        ralph_cap=cap,
        crash_at=crash_at,
    )


def crash_on(step_name: str, times: int = 1):
    """A crash_at hook that raises at the named step the first ``times`` hits."""
    remaining = {"n": times}

    def hook(step: str) -> None:
        if step == step_name and remaining["n"] > 0:
            remaining["n"] -= 1
            raise SimulatedCrash(step)

    return hook


def checkpoint(store: Store, run_id: int) -> dict:
    raw = store.get_meta(checkpoint_key(run_id))
    assert raw is not None
    return json.loads(raw)


def only_run_id(store: Store) -> int:
    rows = store.conn.execute("SELECT id FROM runs").fetchall()
    assert len(rows) == 1
    return rows[0]["id"]


def finalized_span(ctx, *, cost=0.05, inp=100, out=50, turns=2, dur=1500) -> str:
    """Simulate one completed session: span registered running, finalized with
    costs (what run_session does for real, U3)."""
    span_id = f"SPAN-{uuid.uuid4().hex}"
    ctx.store.insert_span(
        span_id,
        run_id=ctx.run_id,
        ticket_id=ctx.ticket_id,
        ralph_iteration=ctx.ralph_iteration,
        agent="worker",
    )
    ctx.store.finalize_span(
        span_id,
        "completed",
        num_turns=turns,
        duration_ms=dur,
        cost_usd=cost,
        input_tokens=inp,
        output_tokens=out,
    )
    return span_id


DIAMOND = (
    TicketSpec("TKT-A"),
    TicketSpec("TKT-B", ("TKT-A",)),
    TicketSpec("TKT-C", ("TKT-A",)),
    TicketSpec("TKT-D", ("TKT-B", "TKT-C")),
)


# --- ordering and terminals (R1/R2) -------------------------------------------------


def test_linear_dag_executes_in_dependency_order(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    # planner emits the tickets scrambled; execution must follow the DAG
    tickets = (
        TicketSpec("TKT-C", ("TKT-B",)),
        TicketSpec("TKT-A"),
        TicketSpec("TKT-B", ("TKT-A",)),
    )
    order: list[tuple[str, int]] = []
    orch = build_orchestrator(
        store,
        tickets=tickets,
        worker=lambda ctx: order.append((ctx.ticket_id, ctx.ralph_iteration)),
    )
    result = orch.run("specs/linear.md", ws)
    assert result.status == "success"
    assert order == [("TKT-A", 1), ("TKT-B", 1), ("TKT-C", 1)]
    assert result.ticket_statuses == {
        "TKT-A": "done",
        "TKT-B": "done",
        "TKT-C": "done",
    }
    run_row = store.get_run(result.run_id)
    assert run_row["status"] == "success"
    assert run_row["spec_ref"] == "specs/linear.md"
    # R1: keyed by the library snapshot in force (0 = pre-mutation)
    assert run_row["snapshot_id"] == store.current_snapshot_id() == 0


def test_all_tickets_done_settles_success_with_cost_totals(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    tickets = (TicketSpec("TKT-A"), TicketSpec("TKT-B", ("TKT-A",)))
    orch = build_orchestrator(
        store, tickets=tickets, worker=lambda ctx: finalized_span(ctx)
    )
    result = orch.run("specs/two.md", ws)
    assert result.status == "success"
    # settlement aggregates span costs into the run row (R16)
    run_row = store.get_run(result.run_id)
    assert run_row["total_cost_usd"] == pytest.approx(0.10)
    assert run_row["total_input_tokens"] == 200
    assert run_row["total_output_tokens"] == 100
    assert run_row["total_turns"] == 4
    assert run_row["total_duration_ms"] == 3000
    assert run_row["cost_partial_spans"] == 0


def test_diamond_dag_mid_failure_blocks_only_dependents_yields_partial(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    order: list[str] = []
    c_saw_b_poison: list[bool] = []

    def worker(ctx):
        order.append(ctx.ticket_id)
        if ctx.ticket_id == "TKT-B":
            (ws.root / "b-poison.txt").write_text(
                "failing code\n", encoding="utf-8", newline="\n"
            )
        if ctx.ticket_id == "TKT-C":
            # R2: the escalated ticket's committed failing code must NOT
            # poison independents — workspace was reset to B's start tag.
            c_saw_b_poison.append((ws.root / "b-poison.txt").exists())

    def gate(ctx):
        if ctx.ticket_id == "TKT-B":
            return GateResult(passed=False, detail="tsc: b is broken")
        return GateResult(passed=True)

    orch = build_orchestrator(store, tickets=DIAMOND, worker=worker, gate=gate, cap=2)
    result = orch.run("specs/diamond.md", ws)
    assert result.status == "partial"
    assert result.ticket_statuses == {
        "TKT-A": "done",
        "TKT-B": "escalated",
        "TKT-C": "done",
        "TKT-D": "blocked",
    }
    # B burned its full cap; C still ran (independent subtree continues), in
    # the stable ID tie-break order: A, then B (escalates), then C.
    assert order == ["TKT-A", "TKT-B", "TKT-B", "TKT-C"]
    assert c_saw_b_poison == [False]
    # the escalation reset stuck: B's poison is gone from the final tree too
    assert not (ws.root / "b-poison.txt").exists()
    assert store.get_run(result.run_id)["status"] == "partial"


def test_gate_bounce_consumes_a_full_iteration(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    iterations: list[int] = []
    bounces = {"n": 1}

    def gate(ctx):
        if bounces["n"] > 0:
            bounces["n"] -= 1
            return GateResult(passed=False, detail="eslint")
        return GateResult(passed=True)

    orch = build_orchestrator(
        store,
        worker=lambda ctx: iterations.append(ctx.ralph_iteration),
        gate=gate,
        cap=3,
    )
    result = orch.run("specs/one.md", ws)
    assert result.status == "success"
    assert iterations == [1, 2]  # the bounce charged iteration 1 in full (R12)
    assert checkpoint(store, result.run_id)["iterations_used"] == {"TKT-A": 2}


def test_plan_failed_terminal_runs_nothing_downstream(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    worker_calls: list[str] = []

    def planner(ctx):
        raise PlanFailed("planning cap exhausted: REQ coverage lint kept failing")

    orch = build_orchestrator(
        store, planner=planner, worker=lambda ctx: worker_calls.append(ctx.ticket_id)
    )
    result = orch.run("specs/bad.md", ws)
    assert result.status == "plan_failed"
    assert "REQ coverage" in result.detail
    assert worker_calls == []
    assert store.get_run(result.run_id)["status"] == "plan_failed"
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_tkt").fetchone()["n"] == 0


# --- kill point 1: between gate and verifier (R3 sub-iteration persist) -------------


def test_kill_between_gate_and_verifier_resume_skips_to_verifier(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    worker_calls: list[int] = []
    verifier_calls: list[int] = []

    def worker(ctx):
        worker_calls.append(ctx.ralph_iteration)

    def verifier(ctx):
        verifier_calls.append(ctx.ralph_iteration)
        return VerifierResult(passed=True)

    orch = build_orchestrator(
        store, worker=worker, verifier=verifier, cap=1, crash_at=crash_on("after_gate")
    )
    with pytest.raises(SimulatedCrash):
        orch.run("specs/one.md", ws)
    run_id = only_run_id(store)
    assert store.get_run(run_id)["status"] == "executing"  # mid-flight, durable
    ck = checkpoint(store, run_id)
    assert ck["gate_passed"] == {"TKT-A": True}  # persisted sub-iteration
    assert ck["iterations_used"] == {}  # nothing charged yet

    # fresh orchestrator = fresh process; cap=1 proves no double-charge
    resumed = build_orchestrator(store, worker=worker, verifier=verifier, cap=1)
    result = resumed.resume(run_id)
    assert result.status == "success"
    assert worker_calls == [1]  # the worker did NOT re-run
    assert verifier_calls == [1]  # the verifier ran once, same iteration index
    assert checkpoint(store, run_id)["iterations_used"] == {"TKT-A": 1}


# --- kill point 2: after the iteration commit, before the gate ----------------------


def test_kill_after_commit_replays_iteration_at_same_index_without_consuming_cap(
    tmp_path,
):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    worker_calls: list[int] = []
    orch = build_orchestrator(
        store,
        worker=lambda ctx: worker_calls.append(ctx.ralph_iteration),
        cap=1,
        crash_at=crash_on("after_commit"),
    )
    with pytest.raises(SimulatedCrash):
        orch.run("specs/one.md", ws)
    run_id = only_run_id(store)
    assert checkpoint(store, run_id)["iterations_used"] == {}

    resumed = build_orchestrator(
        store, worker=lambda ctx: worker_calls.append(ctx.ralph_iteration), cap=1
    )
    result = resumed.resume(run_id)
    # cap=1 and the run still succeeds: the killed attempt consumed nothing;
    # the iteration replayed at the SAME index.
    assert result.status == "success"
    assert worker_calls == [1, 1]


# --- kill point 3: mid-worker-session (orphan span, partial work) --------------------


def test_kill_mid_worker_session_orphan_aborted_workspace_reset_iteration_replayed(
    tmp_path,
):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    transcript = tmp_path / "orphan-transcript.jsonl"
    transcript.write_text('{"type":"assistant"}\n', encoding="utf-8", newline="\n")
    worker_calls: list[int] = []
    replay_saw_partial: list[bool] = []
    first = {"armed": True}

    def worker(ctx):
        worker_calls.append(ctx.ralph_iteration)
        if first["armed"]:
            first["armed"] = False
            # a live session dies mid-flight: span left `running`, partial
            # transcript on disk, uncommitted partial work in the workspace
            ctx.store.insert_span(
                "SPAN-orphan",
                run_id=ctx.run_id,
                ticket_id=ctx.ticket_id,
                ralph_iteration=ctx.ralph_iteration,
            )
            ctx.store.conn.execute(
                "UPDATE trace_span SET artifact_refs_json = ? WHERE id = ?",
                (json.dumps([str(transcript)]), "SPAN-orphan"),
            )
            (ws.root / "partial-work.txt").write_text(
                "half-written\n", encoding="utf-8", newline="\n"
            )
            raise SimulatedCrash("mid-worker-session")
        replay_saw_partial.append((ws.root / "partial-work.txt").exists())

    orch = build_orchestrator(store, worker=worker, cap=1)
    with pytest.raises(SimulatedCrash):
        orch.run("specs/one.md", ws)
    run_id = only_run_id(store)
    assert store.orphan_spans(run_id) != []

    resumed = build_orchestrator(store, worker=worker, cap=1)
    result = resumed.resume(run_id)
    assert result.status == "success"
    # the orphan span was aborted, flagged cost_partial (R3/R16)
    row = store.conn.execute(
        "SELECT status, cost_partial FROM trace_span WHERE id = 'SPAN-orphan'"
    ).fetchone()
    assert row["status"] == "aborted"
    assert row["cost_partial"] == 1
    assert store.orphan_spans(run_id) == []
    # the partial transcript was discarded (R3)
    assert not transcript.exists()
    # the workspace was reset: the replay saw no partial work
    assert replay_saw_partial == [False]
    # the iteration replayed at the SAME index without consuming cap (cap=1)
    assert worker_calls == [1, 1]
    # settlement reports the partial-cost span count (R16)
    assert store.get_run(run_id)["cost_partial_spans"] == 1


# --- quota (R4) ------------------------------------------------------------------------


def test_quota_mid_run_aborts_quota_then_resume_completes(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    worker_calls: list[tuple[str, int]] = []
    quota = {"armed": True}

    def worker(ctx):
        worker_calls.append((ctx.ticket_id, ctx.ralph_iteration))
        if ctx.ticket_id == "TKT-B" and quota["armed"]:
            quota["armed"] = False
            raise SessionQuotaExhausted("usage limit reached; resets at 5pm")

    tickets = (TicketSpec("TKT-A"), TicketSpec("TKT-B", ("TKT-A",)))
    orch = build_orchestrator(store, tickets=tickets, worker=worker, cap=1)
    result = orch.run("specs/two.md", ws)
    assert result.status == "aborted_quota"
    assert "usage limit" in result.detail
    assert result.ticket_statuses == {"TKT-A": "done", "TKT-B": "in_progress"}
    assert store.get_run(result.run_id)["status"] == "aborted_quota"
    # R4: quota consumed no Ralph iteration and produced no agent-attributed
    # failure record
    assert checkpoint(store, result.run_id)["iterations_used"] == {"TKT-A": 1}
    failures = store.conn.execute("SELECT COUNT(*) AS n FROM failure_records").fetchone()
    assert failures["n"] == 0

    resumed = build_orchestrator(store, tickets=tickets, worker=worker, cap=1)
    final = resumed.resume(result.run_id)
    assert final.status == "success"
    # cap=1 still sufficed: the quota attempt was free; the iteration replayed
    assert worker_calls == [("TKT-A", 1), ("TKT-B", 1), ("TKT-B", 1)]


def test_quota_during_planning_aborts_then_resume_replans(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    planner_calls: list[str] = []
    quota = {"armed": True}

    def planner(ctx):
        planner_calls.append(ctx.spec_ref)
        if quota["armed"]:
            quota["armed"] = False
            raise SessionQuotaExhausted("rate limit during planning")
        return [TicketSpec("TKT-A")]

    orch = build_orchestrator(store, planner=planner)
    result = orch.run("specs/one.md", ws)
    assert result.status == "aborted_quota"
    assert result.ticket_statuses == {}
    assert checkpoint(store, result.run_id)["planned"] is False

    resumed = build_orchestrator(store, planner=planner)
    final = resumed.resume(result.run_id)
    assert final.status == "success"
    # resume re-entered the planning state with the durable spec_ref
    assert planner_calls == ["specs/one.md", "specs/one.md"]


def test_non_quota_session_error_aborts_error_and_is_resumable(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    flaky = {"armed": True}

    def worker(ctx):
        if flaky["armed"]:
            flaky["armed"] = False
            raise SessionUnavailable("claude exited 1: transient infra")

    orch = build_orchestrator(store, worker=worker)
    result = orch.run("specs/one.md", ws)
    assert result.status == "aborted_error"
    assert "transient infra" in result.detail
    resumed = build_orchestrator(store, worker=worker)
    assert resumed.resume(result.run_id).status == "success"


# --- determinism (R2 resume reproducibility) ---------------------------------------


def test_deterministic_rerun_produces_identical_ticket_ordering(tmp_path):
    def run_once(base: Path):
        store = make_store(base)
        ws = make_ws(base / "ws")
        order: list[tuple[str, int]] = []
        orch = build_orchestrator(
            store,
            tickets=DIAMOND,
            worker=lambda ctx: order.append((ctx.ticket_id, ctx.ralph_iteration)),
        )
        result = orch.run("specs/diamond.md", ws)
        transitions = [
            (row["ticket_id"], row["to_status"])
            for row in store.conn.execute(
                "SELECT ticket_id, to_status FROM ticket_status_transitions"
                " ORDER BY id"
            ).fetchall()
        ]
        return result.status, order, transitions

    first = run_once(tmp_path / "one")
    second = run_once(tmp_path / "two")
    assert first == second
    assert first[0] == "success"
    assert first[1] == [("TKT-A", 1), ("TKT-B", 1), ("TKT-C", 1), ("TKT-D", 1)]


# --- planner-output validation and guards --------------------------------------------


@pytest.mark.parametrize(
    ("tickets", "match"),
    [
        ((TicketSpec("TKT-A"), TicketSpec("TKT-A")), "duplicate"),
        ((TicketSpec("TKT-A", ("TKT-Z",)),), "unknown dependency"),
        ((TicketSpec("TKT-A", ("TKT-A",)),), "depends on itself"),
        (
            (TicketSpec("TKT-A", ("TKT-B",)), TicketSpec("TKT-B", ("TKT-A",))),
            "cycle",
        ),
        ((TicketSpec("OOPS-1"),), "TKT-"),
    ],
)
def test_invalid_planner_output_is_rejected(tmp_path, tickets, match):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    orch = build_orchestrator(store, tickets=tickets)
    with pytest.raises(OrchestratorError, match=match):
        orch.run("specs/bad.md", ws)
    # nothing was written: the registration transaction rolled back
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_tkt").fetchone()["n"] == 0


def test_planner_reusing_an_existing_ticket_id_is_rejected(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    store.conn.execute("INSERT INTO trace_tkt (id) VALUES ('TKT-A')")
    orch = build_orchestrator(store, tickets=(TicketSpec("TKT-A"),))
    with pytest.raises(OrchestratorError, match="fresh ticket IDs"):
        orch.run("specs/one.md", ws)


def test_resume_guards(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    orch = build_orchestrator(store)
    with pytest.raises(OrchestratorError, match="does not exist"):
        orch.resume(999)
    result = orch.run("specs/one.md", ws)
    assert result.status == "success"
    with pytest.raises(OrchestratorError, match="already settled"):
        orch.resume(result.run_id)
    # a run row with no checkpoint document is not resumable
    bare = store.create_run("specs/other.md", 0)
    with pytest.raises(OrchestratorError, match="checkpoint"):
        orch.resume(bare)


def test_constructor_and_run_input_validation(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OrchestratorError, match="ralph_cap"):
        build_orchestrator(store, cap=0)
    orch = build_orchestrator(store)
    with pytest.raises(OrchestratorError, match="spec_ref"):
        orch.run("   ", make_ws(tmp_path / "ws"))
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    with pytest.raises(OrchestratorError, match="not a git repository"):
        orch.run("specs/one.md", Workspace(root=not_a_repo))


def test_run_result_shape(tmp_path):
    store = make_store(tmp_path)
    ws = make_ws(tmp_path / "ws")
    result = build_orchestrator(store).run("specs/one.md", ws)
    assert isinstance(result, RunResult)
    assert result.detail == ""
    assert result.run_id == only_run_id(store)
