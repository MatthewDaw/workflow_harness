"""Ticket loop: worker, harness gate, verifier (plan-002 U6, R11–R15).

This module supplies the REAL per-ticket stage callables the U4 orchestrator
drives (its ``worker_fn`` / ``gate_fn`` / ``verifier_fn`` seams). One Ralph
iteration is: worker session → orchestrator commit (U4) → containment assert →
gate → verifier (dev server managed around it) — with the orchestrator owning
all iteration accounting, escalation, and resets.

R11 — the worker reads an APPEND-ONLY ledger rendered here from store rows
(:func:`render_ledger`); the worker appends nothing — it is an untrusted
producer whose only channel is validated structured output (§7), and the
orchestrator process is the sole store writer (R6). ``SPAN.files_touched`` is
derived mechanically from the iteration diff: the pre-commit
``git status --porcelain`` view, which is byte-for-byte what U4's
``commit_iteration`` commits immediately after the worker stage returns.

R12 — the gate (gate.py) runs after every worker iteration; failures become
typed failure records (R13) plus ledger entries, and the returned
``GateResult.detail`` carries the canonical failure-set hash (the no-progress
detector's per-iteration signal, U7). The bounce-consumes-a-full-iteration
accounting lives in the orchestrator (one counter).

R14 — the verifier executes AC-derived checks ITSELF (its prompt forbids
anchoring on the worker's unit-suite results); every CHK row stores a
structured repro envelope ``{command, cwd, timeout, expected_exit}`` with a
workspace-relative cwd and no absolute paths. The **verdict-completeness
lint** (:func:`validate_verdict`) rejects any verdict lacking a CHK per AC and
any PASS verdict containing failing CHKs; violations ride ``run_session``'s
retry-then-fail path and are recorded as ``contract_violation`` failure
records charged to infra — never the worker's cap (the retry happens INSIDE
one Ralph iteration).

R15 — the orchestrator manages the dev server (devserver.py) around the
verifier session: readiness probe gates the verifier start, teardown on
verdict (success or failure), hard timeout on a hung server. The verifier only
drives the browser.

Every tunable (profiles, gate commands and timeouts, retry budget, dev-server
envelope) arrives via :class:`TicketLoopConfig`, caller-supplied per the U3/U5
precedent — routing them from ``thresholds.toml`` is the run-assembly wiring's
job (U8); nothing here hardcodes one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING

from agent_families.pipeline.devserver import DevServer, DevServerConfig
from agent_families.pipeline.gate import GateCommand, run_gate
from agent_families.pipeline.orchestrator import (
    GateResult,
    TicketContext,
    VerifierResult,
)
from agent_families.pipeline.planning import plan_meta_key
from agent_families.pipeline.sessions import RoleProfile, run_session
from agent_families.pipeline.workspace import assert_containment

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# Ledger entry kinds written by this module (free-text column; constants keep
# renderings and queries aligned).
ENTRY_WORKER = "worker_summary"
ENTRY_GATE_FAILURE = "gate_failure"
ENTRY_CHK = "verifier_chk"
ENTRY_VERIFIER_FAILURE = "verifier_failure"

# Windows drive / UNC prefixes anywhere in a repro command — the R14 "no
# absolute paths" guard (posix-style flags like --fix are indistinguishable
# from posix paths inside an arbitrary command string, so cwd carries the
# strict relative check and the command carries the drive/UNC scan).
_WIN_ABS_IN_COMMAND = re.compile(r"(?:^|[\s\"'=])(?:[A-Za-z]:[\\/]|\\\\)")


class TicketLoopError(Exception):
    """Ticket-loop misuse or invariant breach with an actionable message."""


# --- structured-output contracts (R11/R14) -----------------------------------
# Stay within the judge validator's subset (type/enum/required/properties/
# additionalProperties/items), like U5's planner contract.

WORKER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
    "additionalProperties": False,
}

_REPRO_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {"type": "string"},
        "cwd": {"type": "string"},
        "timeout": {"type": "number"},
        "expected_exit": {"type": "integer"},
    },
    "required": ["command", "cwd", "timeout", "expected_exit"],
    "additionalProperties": False,
}

_CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "ac": {"type": "string"},
        "result": {"type": "string", "enum": ["pass", "fail"]},
        "evidence": {"type": "string"},
        "repro": _REPRO_SCHEMA,
    },
    "required": ["ac", "result", "evidence", "repro"],
    "additionalProperties": False,
}

VERIFIER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "checks": {"type": "array", "items": _CHECK_SCHEMA},
    },
    "required": ["verdict", "checks"],
    "additionalProperties": False,
}


# --- ledger rendering (R11: orchestrator-rendered, read-only to the worker) ----


def render_ledger(store: Store, ticket_id: str) -> str:
    """The ticket's append-only history as prompt text.

    A rendered VIEW over store rows (ledger entries joined to their failure
    records) — never a second writer surface; the §7 failure fields (expected /
    observed / repro) ride along so the next iteration can act on them.
    """
    rows = store.conn.execute(
        "SELECT le.ralph_iteration, le.entry_kind, le.content,"
        "       fr.failure_kind, fr.location, fr.expected, fr.observed,"
        "       fr.repro_command"
        " FROM ledger_entries le"
        " LEFT JOIN failure_records fr ON fr.id = le.failure_record_id"
        " WHERE le.ticket_id = ? ORDER BY le.id",
        (ticket_id,),
    ).fetchall()
    if not rows:
        return "(no prior iterations)"
    lines: list[str] = []
    for row in rows:
        prefix = f"[iter {row['ralph_iteration']}] {row['entry_kind']}"
        if row["failure_kind"] is not None:
            lines.append(
                f"{prefix}: [{row['failure_kind']}] {row['location']}"
                f" — expected: {row['expected']}; observed: {row['observed']};"
                f" repro: {row['repro_command']}"
            )
        else:
            lines.append(f"{prefix}: {row['content']}")
    return "\n".join(lines)


def ticket_document(store: Store, run_id: int, ticket_id: str) -> dict:
    """The ticket's plan-document entry (title/description/files/ACs).

    Reads the run's persisted plan (U5's canonical document); when a run has
    no plan document (stub planners), falls back to the trace rows so prompts
    still carry the authoritative AC ids.
    """
    raw = store.get_meta(plan_meta_key(run_id))
    if raw is not None:
        document = json.loads(raw)
        for ticket in document.get("tickets", ()):
            if ticket["id"] == ticket_id:
                return ticket
    acs = store.conn.execute(
        "SELECT id, req_id FROM trace_ac WHERE ticket_id = ? ORDER BY id",
        (ticket_id,),
    ).fetchall()
    return {
        "id": ticket_id,
        "title": "",
        "description": "",
        "covers": [],
        "depends_on": [],
        "files": [],
        "acceptance_criteria": [
            {"id": row["id"], "text": "", "req": row["req_id"]} for row in acs
        ],
    }


# --- prompts (deterministic, hardcoded per Phase 1; no volatile data) -----------


def build_worker_prompt(
    ticket: dict, ledger: str, *, injected_skills: str = ""
) -> str:
    files = ", ".join(ticket["files"]) if ticket["files"] else "(unassigned)"
    acs = "\n".join(
        f"- {ac['id']}: {ac['text']}".rstrip(": ")
        for ac in ticket["acceptance_criteria"]
    )
    # 004 R2/R3: the retrieved library section (empty by default → byte-identical
    # to the pre-retrieval prompt; the assembly seam is inert until wired).
    injection_block = f"{injected_skills}\n\n" if injected_skills else ""
    return (
        "You are the worker implementing exactly one ticket inside its"
        " workspace (your working directory).\n"
        f"Ticket {ticket['id']}: {ticket['title']}\n"
        f"{ticket['description']}\n"
        f"Files this ticket owns: {files}\n"
        f"Acceptance criteria:\n{acs}\n\n"
        "Rules: write only inside the workspace; run only your own unit-test"
        " commands; do not touch files owned by other tickets. When you stop,"
        " emit structured output with a 'summary' of what you changed and"
        " why.\n\n"
        f"{injection_block}"
        f"Ticket ledger (prior iterations, read-only):\n{ledger}"
    )


def build_verifier_prompt(
    ticket: dict, base_url: str | None, *, injected_skills: str = ""
) -> str:
    acs = "\n".join(
        f"- {ac['id']}: {ac['text']}".rstrip(": ")
        for ac in ticket["acceptance_criteria"]
    )
    injection_block = f"{injected_skills}\n" if injected_skills else ""
    server_line = (
        f"The dev server is already running at {base_url} — drive it for"
        " browser checks; never start or stop it yourself.\n"
        if base_url is not None
        else ""
    )
    return (
        "You are the verifier for one ticket. Execute the acceptance-criteria"
        " checks YOURSELF using your allowed check commands — never anchor on"
        " the worker's own unit-test results.\n"
        f"Ticket {ticket['id']}: {ticket['title']}\n"
        f"Acceptance criteria:\n{acs}\n"
        f"{injection_block}"
        f"{server_line}"
        "Emit structured output: 'verdict' (pass only if every check passed)"
        " and 'checks' with AT LEAST ONE entry per acceptance criterion."
        " Each check carries: 'ac' (the criterion id), 'result' (pass|fail),"
        " 'evidence' (what you observed), and 'repro' — an envelope"
        " {command, cwd, timeout, expected_exit} that re-executes the check:"
        " cwd is workspace-relative ('.' for the root) and neither cwd nor"
        " command may contain absolute paths."
    )


# --- verdict-completeness lint (R14) ----------------------------------------------


def _abs_path_violation(cwd: str) -> str | None:
    if not cwd.strip():
        return "repro.cwd must be non-empty ('.' for the workspace root)"
    if (
        PureWindowsPath(cwd).is_absolute()
        or cwd.startswith(("/", "\\"))
        or (len(cwd) >= 2 and cwd[1] == ":")
    ):
        return f"repro.cwd must be workspace-relative, got absolute path '{cwd}'"
    if ".." in PureWindowsPath(cwd).parts:
        return f"repro.cwd must stay inside the workspace, got '{cwd}'"
    return None


def validate_verdict(output: dict, ac_ids: list[str]) -> str | None:
    """First R14 violation in a verifier verdict, or None when evidence-complete.

    Deterministic and orchestrator-side: a CHK per AC is what "done" MEANS in
    Phase 1; a PASS with partial coverage or failing CHKs is MAST's
    incorrect-verification failure mode, rejected mechanically (KTD Q6).
    """
    known = set(ac_ids)
    covered: set[str] = set()
    for index, check in enumerate(output["checks"]):
        where = f"checks[{index}]"
        if check["ac"] not in known:
            return (
                f"{where} references unknown acceptance criterion"
                f" '{check['ac']}' (this ticket's ACs: {', '.join(ac_ids)})"
            )
        covered.add(check["ac"])
        repro = check["repro"]
        if not repro["command"].strip():
            return f"{where}.repro.command must be non-empty"
        if _WIN_ABS_IN_COMMAND.search(repro["command"]):
            return (
                f"{where}.repro.command must not contain absolute paths,"
                f" got: {repro['command']}"
            )
        cwd_violation = _abs_path_violation(repro["cwd"])
        if cwd_violation is not None:
            return f"{where}.{cwd_violation}"
        if repro["timeout"] <= 0:
            return f"{where}.repro.timeout must be positive, got {repro['timeout']}"
        if repro["expected_exit"] < 0:
            return (
                f"{where}.repro.expected_exit must be >= 0,"
                f" got {repro['expected_exit']}"
            )
    missing = sorted(known - covered)
    if missing:
        return (
            "verdict lacks a CHK for acceptance criteria: "
            + ", ".join(missing)
            + " (every AC needs at least one check, R14)"
        )
    if output["verdict"] == "pass":
        failing = [c["ac"] for c in output["checks"] if c["result"] == "fail"]
        if failing:
            return (
                "verdict is 'pass' but these checks failed: "
                + ", ".join(failing)
                + " (a PASS may not contain failing CHKs, R14)"
            )
    return None


# --- files_touched derivation (R11) ------------------------------------------------


def iteration_files(workspace_root: Path | str) -> list[str]:
    """The iteration diff's file set: pre-commit ``git status --porcelain``.

    The orchestrator commits immediately after the worker stage (U4), so the
    not-yet-committed working-tree delta IS the iteration diff. Used only for
    ``files_touched`` derivation — never containment (R8: a workspace-internal
    diff is structurally incapable of seeing outside writes).
    """
    proc = subprocess.run(
        ["git", "-C", str(workspace_root), "status", "--porcelain"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise TicketLoopError(
            f"git status failed in {workspace_root} (exit {proc.returncode}):"
            f" {proc.stderr.strip()}"
        )
    files: set[str] = set()
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename: the new name is what the iteration touched
            path = path.split(" -> ", 1)[1]
        files.add(path.strip().strip('"').replace("\\", "/"))
    return sorted(files)


# --- configuration ---------------------------------------------------------------


@dataclass(frozen=True)
class TicketLoopConfig:
    """Everything the three stages need, caller-supplied (no hidden tunables)."""

    worker_profile: RoleProfile
    verifier_profile: RoleProfile
    gate_commands: tuple[GateCommand, ...]
    transcript_dir: Path
    max_retries: int
    devserver: DevServerConfig | None = None
    harness_repo: Path | None = None
    family: str | None = None
    prompt_set_version: str | None = None
    mode: str | None = None
    script_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.gate_commands:
            raise TicketLoopError(
                "TicketLoopConfig.gate_commands must name at least one gate"
                " command (an empty gate passes everything vacuously, R12)"
            )
        if self.max_retries < 0:
            raise TicketLoopError(
                f"max_retries must be >= 0, got {self.max_retries}"
            )


class TicketLoop:
    """The real worker/gate/verifier stages, shaped for the U4 orchestrator:

    ``Orchestrator(store, planner_fn=..., worker_fn=loop.worker,
    gate_fn=loop.gate, verifier_fn=loop.verifier, ...)``
    """

    def __init__(self, config: TicketLoopConfig) -> None:
        self.config = config

    # --- worker (R11, R8) ------------------------------------------------------

    def worker(self, ctx: TicketContext) -> None:
        """One worker session: ledger-fed prompt, containment assert,
        files_touched derivation, ledger append. The commit is U4's, right
        after this returns."""
        cfg = self.config
        ledger = render_ledger(ctx.store, ctx.ticket_id)
        ticket = ticket_document(ctx.store, ctx.run_id, ctx.ticket_id)
        result = run_session(
            build_worker_prompt(ticket, ledger),
            WORKER_OUTPUT_SCHEMA,
            cfg.worker_profile,
            transcript_path=self._transcript(ctx, "worker"),
            max_retries=cfg.max_retries,
            workspace_root=ctx.workspace.root,
            store=ctx.store,
            run_id=ctx.run_id,
            family=cfg.family,
            ticket_id=ctx.ticket_id,
            ralph_iteration=ctx.ralph_iteration,
            prompt_set_version=cfg.prompt_set_version,
            mode=cfg.mode,
            script_path=cfg.script_path,
        )
        # R8: post-iteration containment assert over the session transcript —
        # raises ContainmentViolation (a WorkspaceError; the orchestrator's
        # aborted_error arm) before anything is recorded as progress.
        assert_containment(
            result.transcript_path,
            ctx.workspace.root,
            harness_repo=cfg.harness_repo,
        )
        files = iteration_files(ctx.workspace.root)
        with ctx.store.transaction():
            # files_json only — finalize_span would null the already-settled
            # cost fields, so this is a targeted column update (R11).
            ctx.store.conn.execute(
                "UPDATE trace_span SET files_json = ? WHERE id = ?",
                (json.dumps(files), result.span_id),
            )
            ctx.store.append_ledger_entry(
                ticket_id=ctx.ticket_id,
                entry_kind=ENTRY_WORKER,
                run_id=ctx.run_id,
                ralph_iteration=ctx.ralph_iteration,
                span_id=result.span_id,
                content=result.output["summary"],
            )

    # --- gate (R12, R13) ----------------------------------------------------------

    def gate(self, ctx: TicketContext) -> GateResult:
        """Run the harness gate; persist typed failures + ledger entries on a
        bounce. ``detail`` carries the canonical failure-set hash (U7 feed)."""
        outcome = run_gate(ctx.workspace.root, self.config.gate_commands)
        if outcome.passed:
            return GateResult(passed=True)
        with ctx.store.transaction():
            for failure in outcome.failures:
                record_id = ctx.store.insert_failure_record(
                    failure_kind=failure.failure_kind,
                    location=failure.location,
                    expected=failure.expected,
                    observed=failure.observed,
                    repro_command=failure.repro_command,
                    run_id=ctx.run_id,
                    ticket_id=ctx.ticket_id,
                )
                ctx.store.append_ledger_entry(
                    ticket_id=ctx.ticket_id,
                    entry_kind=ENTRY_GATE_FAILURE,
                    run_id=ctx.run_id,
                    ralph_iteration=ctx.ralph_iteration,
                    failure_record_id=record_id,
                    content=f"[{failure.failure_kind}] {failure.location}",
                )
        logger.info(
            "gate bounced ticket %s iteration %d: %d typed failure(s),"
            " failure-set %s",
            ctx.ticket_id,
            ctx.ralph_iteration,
            len(outcome.failures),
            outcome.failure_set_hash,
        )
        return GateResult(passed=False, detail=outcome.failure_set_hash or "")

    # --- verifier (R14, R15) ----------------------------------------------------

    def verifier(self, ctx: TicketContext) -> VerifierResult:
        """One verifier session bracketed by the managed dev server; persists
        CHK rows (repro envelopes) and typed ``verifier_check`` failures."""
        cfg = self.config
        ac_ids = [
            row["id"]
            for row in ctx.store.conn.execute(
                "SELECT id FROM trace_ac WHERE ticket_id = ? ORDER BY id",
                (ctx.ticket_id,),
            ).fetchall()
        ]
        if not ac_ids:
            raise TicketLoopError(
                f"ticket {ctx.ticket_id} has no acceptance criteria in"
                " trace_ac — a verdict without ACs is vacuous (R14); the plan"
                " lints guarantee at least one AC per ticket (U5 ac_links)"
            )
        ticket = ticket_document(ctx.store, ctx.run_id, ctx.ticket_id)
        server = DevServer(cfg.devserver) if cfg.devserver is not None else None
        try:
            base_url = None
            if server is not None:
                server.start()  # readiness probe gates the verifier start (R15)
                base_url = server.url
            result = run_session(
                build_verifier_prompt(ticket, base_url),
                VERIFIER_OUTPUT_SCHEMA,
                cfg.verifier_profile,
                transcript_path=self._transcript(ctx, "verifier"),
                max_retries=cfg.max_retries,
                workspace_root=ctx.workspace.root,
                store=ctx.store,
                run_id=ctx.run_id,
                family=cfg.family,
                ticket_id=ctx.ticket_id,
                ralph_iteration=ctx.ralph_iteration,
                prompt_set_version=cfg.prompt_set_version,
                extra_validate=self._verdict_validator(ctx, ac_ids),
                mode=cfg.mode,
                script_path=cfg.script_path,
            )
        finally:
            if server is not None:
                server.stop()  # teardown on verdict, pass or fail (R15)
        verdict = result.output["verdict"]
        checks = result.output["checks"]
        with ctx.store.transaction():
            for check in checks:
                chk_id = f"CHK-{uuid.uuid4().hex}"
                envelope = json.dumps(
                    check["repro"], sort_keys=True, separators=(",", ":")
                )
                ctx.store.conn.execute(
                    "INSERT INTO trace_chk (id, ac_id, result, repro_command,"
                    " evidence) VALUES (?, ?, ?, ?, ?)",
                    (chk_id, check["ac"], check["result"], envelope,
                     check["evidence"]),
                )
                ctx.store.append_ledger_entry(
                    ticket_id=ctx.ticket_id,
                    entry_kind=ENTRY_CHK,
                    run_id=ctx.run_id,
                    ralph_iteration=ctx.ralph_iteration,
                    span_id=result.span_id,
                    chk_id=chk_id,
                    content=f"{check['ac']}: {check['result']}",
                )
                if check["result"] == "fail":
                    record_id = ctx.store.insert_failure_record(
                        failure_kind="verifier_check",
                        location=check["ac"],
                        expected=(
                            f"check passes (exit {check['repro']['expected_exit']})"
                        ),
                        observed=check["evidence"],
                        repro_command=envelope,
                        run_id=ctx.run_id,
                        ticket_id=ctx.ticket_id,
                        span_id=result.span_id,
                    )
                    ctx.store.append_ledger_entry(
                        ticket_id=ctx.ticket_id,
                        entry_kind=ENTRY_VERIFIER_FAILURE,
                        run_id=ctx.run_id,
                        ralph_iteration=ctx.ralph_iteration,
                        failure_record_id=record_id,
                        chk_id=chk_id,
                        content=f"{check['ac']} failed",
                    )
        passed = verdict == "pass"
        return VerifierResult(
            passed=passed,
            detail=f"{len(checks)} check(s) over {len(ac_ids)} AC(s)",
        )

    # --- plumbing -------------------------------------------------------------------

    def _transcript(self, ctx: TicketContext, role: str) -> Path:
        return (
            self.config.transcript_dir
            / f"{ctx.ticket_id}-i{ctx.ralph_iteration:03d}-{role}.jsonl"
        )

    def _verdict_validator(
        self, ctx: TicketContext, ac_ids: list[str]
    ) -> Callable[[dict], str | None]:
        """R14 lint as run_session ``extra_validate``: violations ride the
        retry-then-fail path and are recorded as contract violations charged
        to INFRA — the retry happens inside the same Ralph iteration, so the
        worker's cap is never touched."""

        def _validate(output: dict) -> str | None:
            violation = validate_verdict(output, ac_ids)
            if violation is not None:
                ctx.store.insert_failure_record(
                    failure_kind="contract_violation",
                    location=f"verifier verdict for {ctx.ticket_id}",
                    expected=(
                        "a CHK per AC with workspace-relative repro envelopes"
                        " (verdict-completeness lint, R14)"
                    ),
                    observed=violation,
                    run_id=ctx.run_id,
                    ticket_id=ctx.ticket_id,
                )
                logger.warning(
                    "verifier contract violation (infra-charged, retrying):"
                    " %s",
                    violation,
                )
            return violation

        return _validate
