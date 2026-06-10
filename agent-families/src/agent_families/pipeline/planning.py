"""Planning stage (plan-002 U5): toy spec → MSG → planner session → linted plan.

R9 — MSG bootstrap without an explorer (KTD Q4): the orchestrator chunks the
spec into paragraph MSG rows so the planner's extraction writes REQ rows with
real ``source → MSG`` joins from day one. ``mentions`` stays empty — FEAT does
not exist until Phase 2. The U1 schema gives ``trace_msg`` no source column, so
provenance is encoded structurally: the MSG id is ``MSG-r{run}-p{index}`` (the
paragraph index) and the spec file is the run row's ``spec_ref`` — recorded
here as the unit's smallest faithful adaptation.

R10 — planning is a Ralph loop over the ``run_session`` seam (U3): each
iteration the planner emits one structured-output plan; the deterministic lints
below either accept it (persisted in one transaction) or feed typed failures
back (``plan_lint`` failure records + a feedback prompt) and consume one
iteration of the cap. Cap exhaustion sets the run terminal ``plan_failed`` and
raises :class:`PlanFailed` — nothing downstream runs. Judged plan checks stay
deferred; the contract carries ``assumptions[]``, persisted with the plan and
surfaced by :func:`plan_report`.

Phase 2 amendments (plan-003 U5):

- :func:`check_plan_assumptions` closes Phase 1's assumption-verification
  seam (003 R13): the plan-checker converts the planner's ``assumptions[]``
  into questions through an injected ``ask`` callable (the explorer's
  verified-oracle round-trip, bound by the orchestrator). Conversions ride
  the question budget INSIDE ``ask``; an over-budget assumption is recorded
  ``unverified`` on the plan — a typed risk, never a blocker.
- UAT carry-in (003 R11): ``run_planning`` accepts ``carry_in_msg_ids`` —
  explorer-authored UAT feedback MSG rows already persisted by
  ``explorer.run_uat``. They join the planner prompt and the valid
  ``source_msg`` set, so bug REQs carry real MSG provenance; tickets fixing
  them are ordinary tickets tagged ``kind: "bug"`` (a tag in the plan
  document, not a ticket type — KTD Q4; §12 queries never special-case).

The lint list (1:1 with R10, :data:`PLAN_LINTS`):

- ``req_set`` — non-empty REQ extraction, unique REQ ids, valid MSG provenance
- ``req_coverage`` — the REQ coverage matrix: every REQ covered by >= 1 ticket,
  ``covers`` referencing only extracted REQs (a zero-ticket plan fires here)
- ``dag_acyclic`` — well-formed ticket id set, known ``depends_on`` refs, no cycles
- ``ac_links`` — >= 1 AC per ticket, unique AC ids, each AC linked to a REQ its
  ticket covers
- ``size_budget`` — unit-of-work budget: at most ``size_budget`` files per ticket
- ``file_ownership`` — partitioning at WARN level in Phase 1: overlaps are
  recorded with the plan and the plan is accepted (no consumer by design)

The orchestrator is the sole store writer (R6): the planner is an untrusted
producer whose only channel is validated structured output; canonical REQ/TKT/AC
ids are minted HERE at persistence, never taken from the planner. The persisted
plan document (meta key ``plan:run:{run_id}``) carries the canonicalized ticket
DAG (``depends_on``), files, ACs, assumptions, and warnings — the artifact the
orchestrator (U4) consumes for topological execution.

Tunables (cap, size budget) are caller-supplied per the U3 precedent — routing
them from ``thresholds.toml`` is the orchestrator's job; nothing is hardcoded
here.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_families.pipeline.sessions import RoleProfile, run_session

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# The deterministic lint list (R10), enumerated 1:1 in tests.
LINT_REQ_SET = "req_set"
LINT_REQ_COVERAGE = "req_coverage"
LINT_DAG_ACYCLIC = "dag_acyclic"
LINT_AC_LINKS = "ac_links"
LINT_SIZE_BUDGET = "size_budget"
LINT_FILE_OWNERSHIP = "file_ownership"
PLAN_LINTS = (
    LINT_REQ_SET,
    LINT_REQ_COVERAGE,
    LINT_DAG_ACYCLIC,
    LINT_AC_LINKS,
    LINT_SIZE_BUDGET,
    LINT_FILE_OWNERSHIP,
)

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

# Ticket kinds (003 R11/KTD Q4): `bug` is a tag carried on otherwise-ordinary
# tickets; omitted means "feature".
TICKET_KINDS = ("feature", "bug")
DEFAULT_TICKET_KIND = "feature"

# Per-assumption verification statuses written by the plan-checker (003 R13).
ASSUMPTION_VERIFIED = "verified"
ASSUMPTION_UNVERIFIED = "unverified"

# --- the planner structured-output contract (R9/R10) --------------------------
# Stays within the judge validator's schema subset (type/enum/required/
# properties/additionalProperties/items); id-shape and reference integrity are
# the lints' job, riding the Ralph feedback loop rather than the schema-retry
# path — a dangling ref is a PLANNING failure, not a malformed response.

_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "source_msg": {"type": "string"},
    },
    "required": ["id", "text", "source_msg"],
    "additionalProperties": False,
}

_AC_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "req": {"type": "string"},
    },
    "required": ["id", "text", "req"],
    "additionalProperties": False,
}

_TICKET_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "covers": {"type": "array", "items": {"type": "string"}},
        "depends_on": {"type": "array", "items": {"type": "string"}},
        "files": {"type": "array", "items": {"type": "string"}},
        "acceptance_criteria": {"type": "array", "items": _AC_SCHEMA},
        # optional (003 R11): bug tickets are ordinary tickets with a tag
        "kind": {"type": "string", "enum": list(TICKET_KINDS)},
    },
    "required": [
        "id",
        "title",
        "description",
        "covers",
        "depends_on",
        "files",
        "acceptance_criteria",
    ],
    "additionalProperties": False,
}

PLANNER_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "requirements": {"type": "array", "items": _REQUIREMENT_SCHEMA},
        "tickets": {"type": "array", "items": _TICKET_SCHEMA},
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["requirements", "tickets", "assumptions"],
    "additionalProperties": False,
}


class PlanningError(Exception):
    """Planning-stage misuse or invariant breach with an actionable message."""


class PlanFailed(PlanningError):
    """The planner cap is exhausted — the run is terminal ``plan_failed`` (R10)."""

    def __init__(self, message: str, failures: tuple[LintFinding, ...] = ()) -> None:
        self.failures = failures
        super().__init__(message)


@dataclass(frozen=True)
class LintFinding:
    """One typed lint outcome in the §7 failure shape (location/expected/observed)."""

    lint: str
    severity: str
    location: str
    expected: str
    observed: str

    def as_dict(self) -> dict:
        return {
            "lint": self.lint,
            "severity": self.severity,
            "location": self.location,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class SpecMessage:
    """One synthesized MSG row: id encodes run + paragraph index (provenance)."""

    msg_id: str
    paragraph_index: int
    content: str


@dataclass(frozen=True)
class PlanningResult:
    """An accepted plan: persisted traceability plus the canonical plan document."""

    run_id: int
    iterations: int
    msg_ids: tuple[str, ...]
    plan: dict
    assumptions: tuple[str, ...]
    warnings: tuple[LintFinding, ...]


# --- MSG synthesis (R9) --------------------------------------------------------


def chunk_spec(text: str) -> list[str]:
    """Blank-line-separated paragraphs, stripped, empties dropped."""
    return [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]


def _msg_id(run_id: int, paragraph_index: int) -> str:
    return f"MSG-r{run_id}-p{paragraph_index:03d}"


def synthesize_messages(
    store: Store, run_id: int, spec_path: str | Path
) -> list[SpecMessage]:
    """Chunk the toy spec into MSG rows for ``run_id`` — idempotent per run.

    Source provenance rides the id (paragraph index) and the run row's
    ``spec_ref`` (spec file); ``mentions`` stays empty (R9 — FEAT is Phase 2).
    A resume re-entry finds the existing rows and returns them unchanged.
    """
    existing = store.conn.execute(
        "SELECT id, content FROM trace_msg WHERE id LIKE ? ORDER BY id",
        (f"MSG-r{run_id}-p%",),
    ).fetchall()
    if existing:
        return [
            SpecMessage(
                msg_id=row["id"],
                paragraph_index=int(row["id"].rsplit("p", 1)[1]),
                content=row["content"],
            )
            for row in existing
        ]

    spec_path = Path(spec_path)
    if not spec_path.exists():
        raise PlanningError(f"spec file not found: {spec_path}")
    paragraphs = chunk_spec(spec_path.read_text(encoding="utf-8"))
    if not paragraphs:
        raise PlanningError(
            f"spec {spec_path.name} contains no paragraphs to synthesize MSG"
            " rows from — an empty spec cannot be planned"
        )
    messages = [
        SpecMessage(msg_id=_msg_id(run_id, i), paragraph_index=i, content=p)
        for i, p in enumerate(paragraphs)
    ]
    with store.transaction():
        for message in messages:
            store.conn.execute(
                "INSERT INTO trace_msg (id, content) VALUES (?, ?)",
                (message.msg_id, message.content),
            )
    return messages


# --- prompts (deterministic, hardcoded per Phase 1; no volatile data) -----------


def build_planner_prompt(
    spec_name: str,
    messages: list[SpecMessage],
    size_budget: int,
    *,
    carry_in: Sequence[tuple[str, str]] = (),
) -> str:
    """The hardcoded Phase 1 planner prompt over the synthesized MSG listing.

    ``carry_in`` (003 R11): (msg_id, content) pairs of explorer UAT feedback
    from the previous increment — bug REQs are extracted from them with full
    MSG provenance and their tickets tagged ``kind: "bug"``, prepended before
    the new work.
    """
    carry_block = (
        "\n\nUAT feedback from the previous increment (carry-in): extract a"
        " requirement from each feedback message below (source_msg = its MSG"
        " id) and cover it with a ticket whose kind is 'bug', ordered BEFORE"
        " the new work:\n"
        + "\n".join(f"[{mid}] {content}" for mid, content in carry_in)
        if carry_in
        else ""
    )
    msg_block = "\n".join(f"[{m.msg_id}] {m.content}" for m in messages)
    return (
        "You are the planner for a software delivery pipeline. The spec"
        f" '{spec_name}' has been chunked into the source messages below, each"
        " tagged with its MSG id.\n"
        "Produce a plan as a single JSON object with:\n"
        "- requirements: extracted requirements, each with a unique id, its"
        " text, and source_msg set to the MSG id it was extracted from.\n"
        "- tickets: units of work, each with a unique id, title, description,"
        " covers (the requirement ids it implements — every requirement must"
        " be covered by at least one ticket), depends_on (ticket ids; the"
        " dependency graph must be acyclic), files (the workspace-relative"
        f" files it owns — at most {size_budget} per ticket; avoid assigning"
        " the same file to multiple tickets), and acceptance_criteria (at"
        " least one per ticket, each with a unique id, its text, and req set"
        " to a requirement id the ticket covers).\n"
        "- assumptions: anything the spec leaves ambiguous that you decided"
        " rather than asked about, as plain strings (empty array if none)."
        f"{carry_block}\n\n"
        f"Source messages:\n{msg_block}"
    )


def build_lint_feedback_prompt(
    base_prompt: str, errors: list[LintFinding]
) -> str:
    """Typed lint failures fed back for the next planner Ralph iteration (R10)."""
    lines = "\n".join(
        f"- [{f.lint}] {f.location}: expected {f.expected}; observed {f.observed}"
        for f in errors
    )
    return (
        f"{base_prompt}\n\n"
        "Your previous plan failed these deterministic plan lints:\n"
        f"{lines}\n"
        "Produce a corrected plan that resolves every failure while keeping"
        " everything that was already valid."
    )


# --- deterministic plan lints (R10) ----------------------------------------------


def lint_plan(plan: dict, msg_ids: set[str], *, size_budget: int) -> list[LintFinding]:
    """Run every deterministic lint; returns all findings (errors AND warns)."""
    if size_budget < 1:
        raise PlanningError(
            f"size_budget must be a positive ticket-file budget, got {size_budget}"
        )
    findings: list[LintFinding] = []
    requirements = plan["requirements"]
    tickets = plan["tickets"]

    # req_set: non-empty extraction, unique ids, valid MSG provenance (R9).
    if not requirements:
        findings.append(
            LintFinding(
                LINT_REQ_SET,
                SEVERITY_ERROR,
                "requirements",
                "at least one requirement extracted from the spec",
                "empty requirements array",
            )
        )
    req_ids: set[str] = set()
    for req in requirements:
        rid = req["id"]
        if rid in req_ids:
            findings.append(
                LintFinding(
                    LINT_REQ_SET,
                    SEVERITY_ERROR,
                    f"requirement {rid}",
                    "unique requirement ids",
                    f"duplicate requirement id '{rid}'",
                )
            )
        req_ids.add(rid)
        if req["source_msg"] not in msg_ids:
            findings.append(
                LintFinding(
                    LINT_REQ_SET,
                    SEVERITY_ERROR,
                    f"requirement {rid}",
                    "source_msg referencing a synthesized MSG row",
                    f"unknown source_msg '{req['source_msg']}'",
                )
            )

    # dag_acyclic: well-formed ticket id set, known refs, no cycles.
    ticket_ids: set[str] = set()
    for ticket in tickets:
        tid = ticket["id"]
        if tid in ticket_ids:
            findings.append(
                LintFinding(
                    LINT_DAG_ACYCLIC,
                    SEVERITY_ERROR,
                    f"ticket {tid}",
                    "unique ticket ids (the DAG's node set)",
                    f"duplicate ticket id '{tid}'",
                )
            )
        ticket_ids.add(tid)
    edges: dict[str, list[str]] = {ticket["id"]: [] for ticket in tickets}
    for ticket in tickets:
        tid = ticket["id"]
        for dep in ticket["depends_on"]:
            if dep not in ticket_ids:
                findings.append(
                    LintFinding(
                        LINT_DAG_ACYCLIC,
                        SEVERITY_ERROR,
                        f"ticket {tid}",
                        "depends_on referencing ticket ids in this plan",
                        f"unknown dependency '{dep}'",
                    )
                )
            else:
                edges[tid].append(dep)
    cyclic = _cycle_members(edges)
    if cyclic:
        findings.append(
            LintFinding(
                LINT_DAG_ACYCLIC,
                SEVERITY_ERROR,
                "tickets",
                "an acyclic ticket dependency graph",
                f"dependency cycle among tickets: {', '.join(sorted(cyclic))}",
            )
        )

    # req_coverage: the coverage matrix — every REQ covered, covers refs known.
    covered: set[str] = set()
    for ticket in tickets:
        tid = ticket["id"]
        for ref in ticket["covers"]:
            if ref not in req_ids:
                findings.append(
                    LintFinding(
                        LINT_REQ_COVERAGE,
                        SEVERITY_ERROR,
                        f"ticket {tid}",
                        "covers referencing extracted requirement ids",
                        f"unknown requirement '{ref}'",
                    )
                )
            else:
                covered.add(ref)
    for req in requirements:
        rid = req["id"]
        if rid not in covered:
            findings.append(
                LintFinding(
                    LINT_REQ_COVERAGE,
                    SEVERITY_ERROR,
                    f"requirement {rid}",
                    "every requirement covered by at least one ticket",
                    f"requirement '{rid}' is covered by no ticket",
                )
            )

    # ac_links: AC presence and links — each AC tied to a REQ its ticket covers.
    ac_ids: set[str] = set()
    for ticket in tickets:
        tid = ticket["id"]
        criteria = ticket["acceptance_criteria"]
        if not criteria:
            findings.append(
                LintFinding(
                    LINT_AC_LINKS,
                    SEVERITY_ERROR,
                    f"ticket {tid}",
                    "at least one acceptance criterion per ticket",
                    "no acceptance criteria",
                )
            )
        ticket_covers = set(ticket["covers"])
        for ac in criteria:
            if ac["id"] in ac_ids:
                findings.append(
                    LintFinding(
                        LINT_AC_LINKS,
                        SEVERITY_ERROR,
                        f"acceptance criterion {ac['id']}",
                        "unique acceptance-criterion ids",
                        f"duplicate acceptance-criterion id '{ac['id']}'",
                    )
                )
            ac_ids.add(ac["id"])
            if ac["req"] not in ticket_covers:
                findings.append(
                    LintFinding(
                        LINT_AC_LINKS,
                        SEVERITY_ERROR,
                        f"acceptance criterion {ac['id']} on ticket {tid}",
                        "req set to a requirement id the ticket covers",
                        f"'{ac['req']}' is not covered by ticket '{tid}'",
                    )
                )

    # size_budget: the unit-of-work budget (caller-routed tunable).
    for ticket in tickets:
        tid = ticket["id"]
        if len(ticket["files"]) > size_budget:
            findings.append(
                LintFinding(
                    LINT_SIZE_BUDGET,
                    SEVERITY_ERROR,
                    f"ticket {tid}",
                    f"at most {size_budget} files per ticket (unit-of-work budget)",
                    f"{len(ticket['files'])} files",
                )
            )

    # file_ownership: WARN level in Phase 1 — recorded, never blocking.
    owners: dict[str, list[str]] = {}
    for ticket in tickets:
        for file in ticket["files"]:
            owners.setdefault(file, []).append(ticket["id"])
    for file, owner_ids in owners.items():
        if len(owner_ids) > 1:
            findings.append(
                LintFinding(
                    LINT_FILE_OWNERSHIP,
                    SEVERITY_WARN,
                    f"file {file}",
                    "each file owned by a single ticket",
                    f"'{file}' is claimed by tickets: {', '.join(owner_ids)}",
                )
            )
    return findings


def _cycle_members(edges: dict[str, list[str]]) -> list[str]:
    """Kahn's algorithm over the (known-ref) dependency edges; returns the node
    ids left on cycles, empty when the graph is a DAG. Deterministic."""
    indegree = {node: 0 for node in edges}
    for deps in edges.values():
        for dep in deps:
            indegree[dep] += 1
    queue = [node for node in edges if indegree[node] == 0]
    settled = 0
    while queue:
        node = queue.pop()
        settled += 1
        for dep in edges[node]:
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)
    if settled == len(edges):
        return []
    return [node for node in edges if indegree[node] > 0]


# --- persistence (orchestrator-minted canonical ids; one transaction) -------------


def plan_meta_key(run_id: int) -> str:
    return f"plan:run:{run_id}"


def _persist_plan(
    store: Store, run_id: int, plan: dict, warnings: tuple[LintFinding, ...]
) -> dict:
    """Write the linted plan in one transaction and return the canonical document.

    Canonical REQ/TKT/AC ids are minted here in planner-output order — the
    planner is an untrusted producer; its local ids exist only inside its own
    output and are remapped on every link.
    """
    req_map: dict[str, str] = {}
    tkt_map: dict[str, str] = {}
    run = store.get_run(run_id)
    canonical_reqs: list[dict] = []
    canonical_tkts: list[dict] = []
    with store.transaction():
        for i, req in enumerate(plan["requirements"]):
            cid = f"REQ-r{run_id}-{i:03d}"
            req_map[req["id"]] = cid
            store.conn.execute(
                "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)",
                (cid, req["source_msg"]),
            )
            canonical_reqs.append(
                {"id": cid, "text": req["text"], "source_msg": req["source_msg"]}
            )
        for i, ticket in enumerate(plan["tickets"]):
            tkt_map[ticket["id"]] = f"TKT-r{run_id}-{i:03d}"
        ac_counter = 0
        for ticket in plan["tickets"]:
            ctid = tkt_map[ticket["id"]]
            store.conn.execute("INSERT INTO trace_tkt (id) VALUES (?)", (ctid,))
            covers = [req_map[ref] for ref in dict.fromkeys(ticket["covers"])]
            for req_cid in covers:
                store.conn.execute(
                    "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)",
                    (ctid, req_cid),
                )
            canonical_acs: list[dict] = []
            for ac in ticket["acceptance_criteria"]:
                acid = f"AC-r{run_id}-{ac_counter:03d}"
                ac_counter += 1
                store.conn.execute(
                    "INSERT INTO trace_ac (id, ticket_id, req_id) VALUES (?, ?, ?)",
                    (acid, ctid, req_map[ac["req"]]),
                )
                canonical_acs.append(
                    {"id": acid, "text": ac["text"], "req": req_map[ac["req"]]}
                )
            canonical_tkts.append(
                {
                    "id": ctid,
                    "title": ticket["title"],
                    "description": ticket["description"],
                    "kind": ticket.get("kind", DEFAULT_TICKET_KIND),
                    "covers": covers,
                    "depends_on": [
                        tkt_map[dep] for dep in dict.fromkeys(ticket["depends_on"])
                    ],
                    "files": list(ticket["files"]),
                    "acceptance_criteria": canonical_acs,
                }
            )
        document = {
            "run_id": run_id,
            "spec_ref": run["spec_ref"],
            "requirements": canonical_reqs,
            "tickets": canonical_tkts,
            "assumptions": list(plan["assumptions"]),
            "warnings": [w.as_dict() for w in warnings],
        }
        store.set_meta(
            plan_meta_key(run_id),
            json.dumps(document, sort_keys=True, ensure_ascii=False),
        )
    return document


def plan_report(store: Store, run_id: int) -> dict:
    """The run's persisted plan document — assumptions and warnings surfaced (R10)."""
    raw = store.get_meta(plan_meta_key(run_id))
    if raw is None:
        raise PlanningError(
            f"run {run_id} has no persisted plan (planning has not succeeded)"
        )
    return json.loads(raw)


# --- the plan-checker's assumption conversion (003 R13; closes the Phase 1 seam) --


def build_assumption_question(assumption: str) -> str:
    """One planner assumption rendered as a verified-oracle question."""
    return (
        f"The plan for the current increment assumed: {assumption} — Is this"
        " assumption correct for the target app? Verify it against the live"
        " UI and answer concretely."
    )


def check_plan_assumptions(store: Store, run_id: int, ask) -> list[dict]:
    """Convert the persisted plan's ``assumptions[]`` into questions (003 R13).

    ``ask`` is the verified-oracle round-trip bound by the orchestrator —
    ``lambda q: explorer.ask_question(store, episode_id, q, mentions, ...)``
    — returning an object exposing ``outcome`` (``answered`` |
    ``answer_unavailable`` | ``budget_exhausted``) and ``answer``.
    Conversions consume question budget INSIDE ``ask`` (the budget trains
    elicitation; free verification would untrain it — KTD Q6). Outcomes:

    - ``answered`` → the assumption is ``verified``, the answer recorded;
    - ``budget_exhausted`` → ``unverified`` (typed risk, not a blocker);
    - ``answer_unavailable`` → ``unverified`` (the slot was refunded and the
      tuple queued for human review by the round-trip itself).

    The records are persisted onto the plan document (``assumption_checks``)
    and surfaced by :func:`plan_report`.
    """
    document = plan_report(store, run_id)
    records: list[dict] = []
    for index, assumption in enumerate(document.get("assumptions", [])):
        outcome = ask(build_assumption_question(assumption))
        kind = outcome.outcome
        if kind == "answered":
            record = {
                "index": index,
                "assumption": assumption,
                "status": ASSUMPTION_VERIFIED,
                "reason": None,
                "answer": outcome.answer,
            }
        elif kind in ("budget_exhausted", "answer_unavailable"):
            record = {
                "index": index,
                "assumption": assumption,
                "status": ASSUMPTION_UNVERIFIED,
                "reason": kind,
                "answer": None,
            }
            logger.info(
                "assumption %d recorded unverified (%s) — typed risk, not a"
                " blocker (003 R13): %s",
                index,
                kind,
                assumption,
            )
        else:
            raise PlanningError(
                f"ask returned unknown outcome {kind!r} for assumption"
                f" {index} (expected answered / answer_unavailable /"
                " budget_exhausted)"
            )
        records.append(record)
    document["assumption_checks"] = records
    store.set_meta(
        plan_meta_key(run_id),
        json.dumps(document, sort_keys=True, ensure_ascii=False),
    )
    return records


# --- the planning Ralph loop (R10) --------------------------------------------------


def run_planning(
    store: Store,
    run_id: int,
    spec_path: str | Path,
    profile: RoleProfile,
    *,
    transcript_dir: str | Path,
    cap: int,
    max_retries: int,
    size_budget: int,
    family: str | None = None,
    prompt_set_version: str | None = None,
    mode: str | None = None,
    script_path: str | Path | None = None,
    carry_in_msg_ids: Sequence[str] = (),
) -> PlanningResult:
    """Drive the planner Ralph loop: spec → MSG → session → lints → plan.

    ``carry_in_msg_ids`` (003 R11): ids of already-persisted UAT feedback
    MSG rows; they join the prompt and the valid ``source_msg`` set so bug
    REQs trace to them.

    Lint errors become ``plan_lint`` failure records charged to the iteration's
    span and feed the next iteration's prompt; each lint bounce consumes one of
    ``cap`` iterations. Cap exhaustion flips the run to ``plan_failed`` and
    raises :class:`PlanFailed` with nothing persisted. Session-level failures
    (schema violations after retries, quota — R4's distinct exception, timeouts)
    propagate untouched for the orchestrator's checkpoint discipline.

    On acceptance the run stays in ``planning`` — advancing to ``executing`` is
    the orchestrator's transition (U4).
    """
    if cap < 1:
        raise PlanningError(f"planner cap must be a positive iteration count, got {cap}")
    run = store.get_run(run_id)
    if run is None:
        raise PlanningError(f"run {run_id} does not exist")
    store.set_run_status(run_id, "planning")

    spec_path = Path(spec_path)
    transcript_dir = Path(transcript_dir)
    messages = synthesize_messages(store, run_id, spec_path)
    carry_in: list[tuple[str, str]] = []
    for mid in dict.fromkeys(carry_in_msg_ids):
        row = store.conn.execute(
            "SELECT id, content FROM trace_msg WHERE id = ?", (mid,)
        ).fetchone()
        if row is None:
            raise PlanningError(
                f"carry-in message {mid} does not exist — UAT feedback must"
                " be persisted (explorer.run_uat) before planning carries"
                " it in (003 R11)"
            )
        carry_in.append((row["id"], row["content"]))
    msg_ids = {m.msg_id for m in messages} | {mid for mid, _ in carry_in}
    base_prompt = build_planner_prompt(
        spec_path.name, messages, size_budget, carry_in=carry_in
    )

    prompt = base_prompt
    last_errors: list[LintFinding] = []
    for iteration in range(1, cap + 1):
        result = run_session(
            prompt,
            PLANNER_OUTPUT_SCHEMA,
            profile,
            transcript_path=transcript_dir / f"planning-iter-{iteration:03d}.jsonl",
            max_retries=max_retries,
            store=store,
            run_id=run_id,
            family=family,
            ralph_iteration=iteration,
            prompt_set_version=prompt_set_version,
            mode=mode,
            script_path=script_path,
        )
        plan = result.output
        findings = lint_plan(plan, msg_ids, size_budget=size_budget)
        errors = [f for f in findings if f.severity == SEVERITY_ERROR]
        warnings = tuple(f for f in findings if f.severity == SEVERITY_WARN)
        if not errors:
            for warning in warnings:
                logger.warning(
                    "plan lint warn (recorded, not blocking): [%s] %s: %s",
                    warning.lint,
                    warning.location,
                    warning.observed,
                )
            document = _persist_plan(store, run_id, plan, warnings)
            return PlanningResult(
                run_id=run_id,
                iterations=iteration,
                msg_ids=tuple(m.msg_id for m in messages),
                plan=document,
                assumptions=tuple(plan["assumptions"]),
                warnings=warnings,
            )
        for finding in errors:
            store.insert_failure_record(
                failure_kind="plan_lint",
                location=f"{finding.lint}: {finding.location}",
                expected=finding.expected,
                observed=finding.observed,
                run_id=run_id,
                span_id=result.span_id,
            )
        logger.info(
            "planning iteration %d/%d bounced on %d lint error(s)",
            iteration,
            cap,
            len(errors),
        )
        last_errors = errors
        prompt = build_lint_feedback_prompt(base_prompt, errors)

    store.set_run_status(run_id, "plan_failed")
    raise PlanFailed(
        f"planning cap of {cap} iteration(s) exhausted for run {run_id};"
        " run is terminal plan_failed. Last lint failures: "
        + "; ".join(f"[{f.lint}] {f.location}: {f.observed}" for f in last_errors),
        failures=tuple(last_errors),
    )
