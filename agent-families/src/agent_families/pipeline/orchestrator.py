"""Orchestrator core: the run/ticket state machine with checkpoint/resume (plan-002 U4).

Run state machine (R1, HTD): ``created -> planning -> executing ->
settled(success | partial)``, with ``plan_failed`` exiting from planning and
``aborted_quota | aborted_error`` exits from any state via checkpoint. One run
row per invocation, keyed by toy-spec ref and the library snapshot in force
(recorded even though Phase 1 prompts read no skills, DESIGN section 15).

Ticket execution (R2): deterministic topological order with a stable
tie-break (lexicographic ticket ID, plan KTD). An escalated ticket (Ralph cap
exhausted) FIRST resets the workspace to its start tag — independents build
from the last verified-good state, never the escalated ticket's committed
failing code — THEN marks its transitive dependents ``blocked``; independent
subtrees continue. Run outcome is ``success`` iff every ticket is ``done``,
else ``partial``.

Checkpoint/resume (R3): resume granularity is the Ralph-iteration boundary,
never mid-session (KTD: a live session cannot be resumed across orchestrator
death). Durable state is the store — runs, trace_tkt statuses, spans, plus
one checkpoint document per run in the ``meta`` bookkeeping table under
:func:`checkpoint_key` carrying the ticket DAG, the per-ticket iteration
counters, and the sub-iteration gate flag — and workspace git history with
one orchestrator-owned commit per iteration (U2's discipline). The gate
outcome persists sub-iteration, so a post-gate resume skips straight to the
verifier without double-charging the cap. :meth:`Orchestrator.resume` aborts
orphaned spans (inserted ``running`` at spawn, never finalized — dead
sessions), discards their partial transcripts (every file path listed in the
span's ``artifact_refs_json``), resets the workspace via U2's
:func:`~agent_families.pipeline.workspace.reset_hard` (``git reset --hard
HEAD`` + ``git clean -fd``; HEAD IS the last iteration commit because the
orchestrator owns every commit), and re-enters the loop at the same iteration
index without consuming cap: iteration counters advance only when an
iteration completes.

Quota (R4): :class:`SessionQuotaExhausted` from any stage checkpoints the run
and exits with terminal ``aborted_quota``, consuming no Ralph iterations and
producing no agent-attributed failure records. Other session/workspace errors
exit ``aborted_error``. Both abort terminals are resumable — resume re-enters
at the recorded state. (The plan's configurable sleep-until-window
alternative is the caller's loop: call :meth:`Orchestrator.resume` when the
quota window reopens.)

Stage seams: planner/worker/gate/verifier are injected callables — plan-002
U5 (planning stage) and U6 (ticket loop, real gate, verifier, ledger,
failure records) supply the real ones; U4 tests drive scripted stubs and an
injectable stub gate, exactly per the plan's approach. Per the tunables
discipline the Ralph cap is caller-supplied with no default: the Phase 0
config loader is frozen this wave, so routing the cap from ``thresholds.toml``
lands with the units that own real stage wiring (U6/U8) — nothing in this
module hardcodes a tunable.

``crash_at`` is the plan's test-only kill-point injection seam: a callable
invoked with a step name (:data:`CRASH_STEPS`) at every checkpoint boundary;
tests raise from it to simulate process death deterministically. Real
subprocess termination is reserved for the one coarse e2e (U8).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_families.pipeline.sessions import SessionError, SessionQuotaExhausted
from agent_families.pipeline.workspace import (
    Workspace,
    WorkspaceError,
    commit_iteration,
    reset_hard,
    reset_to_ticket_start,
    tag_ticket_start,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)

# Run terminals a resume cannot re-enter (the abort terminals CAN be resumed).
SETTLED_STATUSES = ("success", "partial", "plan_failed")

# The named kill points the crash_at seam fires at, in loop order.
CRASH_STEPS = (
    "planning_start",
    "executing_start",
    "ticket_start",
    "after_worker",
    "after_commit",
    "after_gate",
    "after_gate_fail",
    "after_verdict_fail",
    "after_ticket_done",
    "after_escalation",
)


class OrchestratorError(Exception):
    """Orchestrator misuse or invariant violation, with an actionable message."""


class PlanFailed(OrchestratorError):
    """Planning cap exhausted (R10) — raised by the planning stage (U5);
    mapped to the run terminal ``plan_failed``."""


def checkpoint_key(run_id: int) -> str:
    """The ``meta`` key of a run's checkpoint document (R3 substrate)."""
    return f"pipeline:run:{int(run_id)}:checkpoint"


# --- stage contracts -----------------------------------------------------------


@dataclass(frozen=True)
class TicketSpec:
    """One executable ticket: its ID and the ticket IDs it depends on."""

    ticket_id: str
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class GateResult:
    """Harness-gate outcome for one iteration (real gate lands in U6)."""

    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class VerifierResult:
    """Verifier verdict for one iteration (real verifier lands in U6)."""

    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RunContext:
    """What the planning stage sees (U5 consumes this seam)."""

    store: Store
    run_id: int
    spec_ref: str
    workspace: Workspace


@dataclass(frozen=True)
class TicketContext:
    """What the per-ticket stages see for one Ralph iteration."""

    store: Store
    run_id: int
    ticket_id: str
    ralph_iteration: int
    workspace: Workspace


PlannerFn = Callable[[RunContext], Sequence[TicketSpec]]
WorkerFn = Callable[[TicketContext], None]
GateFn = Callable[[TicketContext], GateResult]
VerifierFn = Callable[[TicketContext], VerifierResult]


@dataclass(frozen=True)
class RunResult:
    """A run's terminal outcome (terminal status is also durable in ``runs``)."""

    run_id: int
    status: str
    ticket_statuses: dict[str, str]
    detail: str = ""


# --- the durable checkpoint document (R3) ----------------------------------------


@dataclass
class _RunState:
    """The checkpoint document: everything resume needs beyond the row tables.

    Run and ticket STATUSES are authoritative in their own tables; this
    document carries the ticket DAG (the planner's output shape has no Phase 1
    table), per-ticket iteration counters (cap accounting — counters advance
    only when an iteration completes), and the sub-iteration gate flag (a
    post-gate resume skips straight to the verifier, R3).
    """

    workspace_root: str
    planned: bool = False
    tickets: tuple[TicketSpec, ...] = ()
    iterations_used: dict[str, int] = field(default_factory=dict)
    gate_passed: dict[str, bool] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace_root": self.workspace_root,
                "planned": self.planned,
                "tickets": [
                    {"id": t.ticket_id, "depends_on": list(t.depends_on)}
                    for t in self.tickets
                ],
                "iterations_used": self.iterations_used,
                "gate_passed": self.gate_passed,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> _RunState:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise OrchestratorError(f"corrupt run checkpoint document: {exc}") from exc
        return cls(
            workspace_root=data["workspace_root"],
            planned=bool(data["planned"]),
            tickets=tuple(
                TicketSpec(ticket_id=t["id"], depends_on=tuple(t["depends_on"]))
                for t in data["tickets"]
            ),
            iterations_used={k: int(v) for k, v in data["iterations_used"].items()},
            gate_passed={k: bool(v) for k, v in data["gate_passed"].items()},
        )


# --- planner-output validation ------------------------------------------------------


def _validate_ticket_specs(tickets: tuple[TicketSpec, ...]) -> None:
    """Structural sanity for the executable DAG (U5's plan lints run upstream
    with retry; by the time tickets reach the orchestrator these are bugs)."""
    seen: set[str] = set()
    for ticket in tickets:
        if not ticket.ticket_id.startswith("TKT-"):
            raise OrchestratorError(
                f"ticket id {ticket.ticket_id!r} must be 'TKT-' prefixed"
                " (trace_tkt CHECK constraint)"
            )
        if ticket.ticket_id in seen:
            raise OrchestratorError(f"duplicate ticket id {ticket.ticket_id!r}")
        seen.add(ticket.ticket_id)
    for ticket in tickets:
        for dep in ticket.depends_on:
            if dep == ticket.ticket_id:
                raise OrchestratorError(
                    f"ticket {ticket.ticket_id} depends on itself"
                )
            if dep not in seen:
                raise OrchestratorError(
                    f"ticket {ticket.ticket_id} has unknown dependency {dep!r}"
                )
    # Kahn's algorithm: an executable plan must be acyclic.
    indegree = {t.ticket_id: len(set(t.depends_on)) for t in tickets}
    dependents: dict[str, list[str]] = {}
    for ticket in tickets:
        for dep in set(ticket.depends_on):
            dependents.setdefault(dep, []).append(ticket.ticket_id)
    queue = [tid for tid, deg in indegree.items() if deg == 0]
    processed = 0
    while queue:
        current = queue.pop()
        processed += 1
        for nxt in dependents.get(current, ()):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if processed != len(tickets):
        cyclic = sorted(tid for tid, deg in indegree.items() if deg > 0)
        raise OrchestratorError(f"dependency cycle among tickets: {cyclic}")


# --- the orchestrator ------------------------------------------------------------------


class Orchestrator:
    """The run loop: durable state machine, honest resume, injected stages."""

    def __init__(
        self,
        store: Store,
        *,
        planner_fn: PlannerFn,
        worker_fn: WorkerFn,
        gate_fn: GateFn,
        verifier_fn: VerifierFn,
        ralph_cap: int,
        crash_at: Callable[[str], None] | None = None,
    ) -> None:
        if ralph_cap <= 0:
            raise OrchestratorError(
                f"ralph_cap must be a positive iteration budget, got {ralph_cap}"
            )
        self.store = store
        self.planner_fn = planner_fn
        self.worker_fn = worker_fn
        self.gate_fn = gate_fn
        self.verifier_fn = verifier_fn
        self.ralph_cap = int(ralph_cap)
        self.crash_at = crash_at

    # --- entry points -------------------------------------------------------------

    def run(self, spec_ref: str, workspace: Workspace) -> RunResult:
        """Start a new run against an instantiated workspace (R1)."""
        if not spec_ref.strip():
            raise OrchestratorError("spec_ref must be non-empty")
        root = Path(workspace.root)
        if not (root / ".git").exists():
            raise OrchestratorError(
                f"workspace {root} is not a git repository; instantiate it"
                " first (U2 instantiate_workspace)"
            )
        run_id = self.store.create_run(spec_ref, self.store.current_snapshot_id())
        state = _RunState(workspace_root=str(root))
        self._save_state(run_id, state)
        logger.info("run %d created: spec_ref=%s", run_id, spec_ref)
        return self._drive(run_id, spec_ref, workspace, state)

    def resume(self, run_id: int) -> RunResult:
        """Re-enter a checkpointed run at its recorded state (R3).

        Orphan spans are aborted and their partial transcripts discarded; the
        workspace is reset to the last iteration commit (HEAD) with untracked
        partial work cleaned; the loop re-enters at the same iteration index
        without consuming cap.
        """
        run = self.store.get_run(run_id)
        if run is None:
            raise OrchestratorError(f"run {run_id} does not exist")
        if run["status"] in SETTLED_STATUSES:
            raise OrchestratorError(
                f"run {run_id} already settled ({run['status']}); nothing to resume"
            )
        raw = self.store.get_meta(checkpoint_key(run_id))
        if raw is None:
            raise OrchestratorError(
                f"run {run_id} has no checkpoint document; it cannot be resumed"
            )
        state = _RunState.from_json(raw)
        root = Path(state.workspace_root)
        if not root.is_dir():
            raise OrchestratorError(
                f"checkpointed workspace is gone: {root} (workspaces are"
                " retained after their run, R18 — do not delete mid-run)"
            )
        workspace = Workspace(root=root)
        orphans = self.store.orphan_spans(run_id)
        for span in orphans:
            self._discard_artifacts(span["artifact_refs_json"])
            self.store.finalize_span(span["id"], "aborted", cost_partial=True)
        if orphans:
            logger.warning(
                "resume run %d: aborted %d orphan span(s), partial transcripts"
                " discarded",
                run_id,
                len(orphans),
            )
        # Back to the last iteration commit; clean drops partial session work
        # (no -x: the template .gitignore keeps node_modules safe, R3).
        reset_hard(workspace, "HEAD")
        logger.info("run %d resuming (status was %s)", run_id, run["status"])
        return self._drive(run_id, run["spec_ref"], workspace, state)

    # --- the drive loop ----------------------------------------------------------------

    def _drive(
        self, run_id: int, spec_ref: str, workspace: Workspace, state: _RunState
    ) -> RunResult:
        try:
            if not state.planned:
                self.store.set_run_status(run_id, "planning")
                self._crash_point("planning_start")
                tickets = tuple(
                    self.planner_fn(
                        RunContext(
                            store=self.store,
                            run_id=run_id,
                            spec_ref=spec_ref,
                            workspace=workspace,
                        )
                    )
                )
                _validate_ticket_specs(tickets)
                with self.store.transaction():
                    for ticket in tickets:
                        existing = self.store.conn.execute(
                            "SELECT 1 FROM trace_tkt WHERE id = ?",
                            (ticket.ticket_id,),
                        ).fetchone()
                        if existing is not None:
                            raise OrchestratorError(
                                f"ticket id {ticket.ticket_id} already exists in"
                                " trace_tkt; the planner must mint fresh ticket"
                                " IDs per run"
                            )
                        self.store.conn.execute(
                            "INSERT INTO trace_tkt (id, status)"
                            " VALUES (?, 'pending')",
                            (ticket.ticket_id,),
                        )
                    state.planned = True
                    state.tickets = tickets
                    self._save_state(run_id, state)
            self.store.set_run_status(run_id, "executing")
            self._crash_point("executing_start")
            while (ticket_id := self._next_runnable(state)) is not None:
                self._run_ticket(run_id, ticket_id, workspace, state)
            return self._settle(run_id, state)
        except PlanFailed as exc:
            return self._abort(run_id, state, "plan_failed", str(exc))
        except SessionQuotaExhausted as exc:
            # R4: no iteration consumed, no agent-attributed failure record —
            # counters only ever advance at iteration completion.
            return self._abort(run_id, state, "aborted_quota", str(exc))
        except (SessionError, WorkspaceError) as exc:
            return self._abort(run_id, state, "aborted_error", str(exc))

    def _next_runnable(self, state: _RunState) -> str | None:
        """Deterministic topo order: in-progress first (resume re-entry), then
        the lexicographically smallest pending ticket with all deps done (R2)."""
        statuses = self._ticket_statuses(state)
        in_progress = sorted(t for t, s in statuses.items() if s == "in_progress")
        if in_progress:
            return in_progress[0]
        pending = sorted(t for t, s in statuses.items() if s == "pending")
        if not pending:
            return None
        deps = {t.ticket_id: t.depends_on for t in state.tickets}
        for ticket_id in pending:
            if all(statuses[d] == "done" for d in deps[ticket_id]):
                return ticket_id
        # Unreachable with a validated DAG (escalation blocks dependents
        # transitively); kept as a loud invariant instead of a silent hang.
        raise OrchestratorError(
            f"dependency deadlock: tickets {pending} can never become runnable"
        )

    def _run_ticket(
        self, run_id: int, ticket_id: str, workspace: Workspace, state: _RunState
    ) -> None:
        if self._ticket_statuses(state)[ticket_id] == "pending":
            # Tag BEFORE the status flip: a crash between the two re-tags
            # idempotently; flipping first would lose the escalation reset
            # target. Never re-tagged on resume — HEAD has moved.
            tag_ticket_start(workspace, ticket_id)
            with self.store.transaction():
                self.store.set_ticket_status(ticket_id, "in_progress", run_id)
        self._crash_point("ticket_start")

        used = state.iterations_used.get(ticket_id, 0)
        while used < self.ralph_cap:
            iteration = used + 1
            ctx = TicketContext(
                store=self.store,
                run_id=run_id,
                ticket_id=ticket_id,
                ralph_iteration=iteration,
                workspace=workspace,
            )
            if not state.gate_passed.get(ticket_id, False):
                self.worker_fn(ctx)
                self._crash_point("after_worker")
                commit_iteration(workspace, ticket_id, iteration)
                self._crash_point("after_commit")
                gate = self.gate_fn(ctx)
                if not gate.passed:
                    # R12: a gate bounce consumes a full Ralph iteration.
                    logger.info(
                        "gate bounce: run=%d ticket=%s iteration=%d (%s)",
                        run_id,
                        ticket_id,
                        iteration,
                        gate.detail,
                    )
                    used = iteration
                    state.iterations_used[ticket_id] = used
                    self._save_state(run_id, state)
                    self._crash_point("after_gate_fail")
                    continue
                # Sub-iteration persist: a post-gate resume goes straight to
                # the verifier without double-charging the cap (R3).
                state.gate_passed[ticket_id] = True
                self._save_state(run_id, state)
                self._crash_point("after_gate")
            verdict = self.verifier_fn(ctx)
            used = iteration
            state.iterations_used[ticket_id] = used
            state.gate_passed[ticket_id] = False
            if verdict.passed:
                with self.store.transaction():
                    self.store.set_ticket_status(ticket_id, "done", run_id)
                    self._save_state(run_id, state)
                self._crash_point("after_ticket_done")
                return
            self._save_state(run_id, state)
            self._crash_point("after_verdict_fail")
        self._escalate(run_id, ticket_id, workspace, state)

    def _escalate(
        self, run_id: int, ticket_id: str, workspace: Workspace, state: _RunState
    ) -> None:
        """Cap exhausted (R2): reset the workspace FIRST so independents build
        from the last verified-good state, then escalate + block dependents."""
        reset_to_ticket_start(workspace, ticket_id)
        dependents = sorted(self._transitive_dependents(ticket_id, state))
        statuses = self._ticket_statuses(state)
        with self.store.transaction():
            self.store.set_ticket_status(ticket_id, "escalated", run_id)
            for dependent in dependents:
                if statuses[dependent] == "pending":
                    self.store.set_ticket_status(dependent, "blocked", run_id)
            state.gate_passed[ticket_id] = False
            self._save_state(run_id, state)
        logger.warning(
            "ticket %s escalated at cap %d; blocked dependents: %s",
            ticket_id,
            self.ralph_cap,
            ", ".join(dependents) or "(none)",
        )
        self._crash_point("after_escalation")

    # --- settlement (R1/R16) ---------------------------------------------------------

    def _settle(self, run_id: int, state: _RunState) -> RunResult:
        statuses = self._ticket_statuses(state)
        status = (
            "success"
            if all(s == "done" for s in statuses.values())
            else "partial"
        )
        self._finalize_run(run_id, status)
        logger.info("run %d settled: %s", run_id, status)
        return RunResult(run_id=run_id, status=status, ticket_statuses=statuses)

    def _abort(
        self, run_id: int, state: _RunState, status: str, detail: str
    ) -> RunResult:
        logger.warning("run %d aborted: %s (%s)", run_id, status, detail)
        self._finalize_run(run_id, status)
        return RunResult(
            run_id=run_id,
            status=status,
            ticket_statuses=self._ticket_statuses(state),
            detail=detail,
        )

    def _finalize_run(self, run_id: int, status: str) -> None:
        """Set the terminal and aggregate span cost/turn totals into the run
        row; settlement sums include cost_partial spans and report their count
        (R16)."""
        totals = self.store.conn.execute(
            "SELECT SUM(cost_usd) AS cost_usd,"
            " SUM(input_tokens) AS input_tokens,"
            " SUM(output_tokens) AS output_tokens,"
            " SUM(num_turns) AS num_turns,"
            " SUM(duration_ms) AS duration_ms,"
            " SUM(CASE WHEN cost_partial = 1 THEN 1 ELSE 0 END) AS partials"
            " FROM trace_span WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        with self.store.transaction():
            self.store.conn.execute(
                "UPDATE runs SET total_cost_usd = ?, total_input_tokens = ?,"
                " total_output_tokens = ?, total_turns = ?, total_duration_ms = ?,"
                " cost_partial_spans = ? WHERE id = ?",
                (
                    totals["cost_usd"],
                    totals["input_tokens"],
                    totals["output_tokens"],
                    totals["num_turns"],
                    totals["duration_ms"],
                    totals["partials"] or 0,
                    run_id,
                ),
            )
            self.store.set_run_status(run_id, status)

    # --- plumbing -------------------------------------------------------------------

    def _ticket_statuses(self, state: _RunState) -> dict[str, str]:
        out: dict[str, str] = {}
        for ticket in state.tickets:
            row = self.store.conn.execute(
                "SELECT status FROM trace_tkt WHERE id = ?", (ticket.ticket_id,)
            ).fetchone()
            if row is None:
                raise OrchestratorError(
                    f"checkpoint names ticket {ticket.ticket_id} but trace_tkt"
                    " has no such row (store/checkpoint divergence)"
                )
            out[ticket.ticket_id] = row["status"]
        return out

    def _transitive_dependents(
        self, ticket_id: str, state: _RunState
    ) -> set[str]:
        dependents_of: dict[str, set[str]] = {}
        for ticket in state.tickets:
            for dep in ticket.depends_on:
                dependents_of.setdefault(dep, set()).add(ticket.ticket_id)
        out: set[str] = set()
        frontier = [ticket_id]
        while frontier:
            current = frontier.pop()
            for nxt in dependents_of.get(current, ()):
                if nxt not in out:
                    out.add(nxt)
                    frontier.append(nxt)
        return out

    def _save_state(self, run_id: int, state: _RunState) -> None:
        # Single-statement upsert: joins an open transaction (status flip +
        # checkpoint travel atomically) or autocommits standalone.
        self.store.set_meta(checkpoint_key(run_id), state.to_json())

    @staticmethod
    def _discard_artifacts(refs_json: str | None) -> None:
        """Delete the partial artifacts (transcripts) of an orphan span (R3)."""
        try:
            refs = json.loads(refs_json or "[]")
        except json.JSONDecodeError:
            return
        if not isinstance(refs, list):
            return
        for ref in refs:
            if not isinstance(ref, str):
                continue
            path = Path(ref)
            try:
                if path.is_file():
                    path.unlink()
            except OSError:
                logger.warning("could not discard orphan artifact %s", path)

    def _crash_point(self, step: str) -> None:
        if self.crash_at is not None:
            self.crash_at(step)
