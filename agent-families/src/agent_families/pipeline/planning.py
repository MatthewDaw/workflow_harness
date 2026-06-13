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

Phase 007 amendments (plan-007 U2 — greenfield-mode planner contract):

- Assumptions are typed (KTD2): each contract item is an object
  ``{claim, risk_if_wrong (low|med|high), cheapest_test}`` (a legacy bare
  string is accepted via a union ``type`` and normalized to a med-risk claim
  for cross-unit fixture compatibility). They persist to the **``trace_assume``
  ledger** (keyed by ``run_id``) as the single source of truth;
  :func:`check_plan_assumptions` is subsumed onto those rows — it ranks them
  through :func:`rank_questions` (risk-weight, KTD7) and settles each row
  (``answered`` → ``confirmed`` + ``confirmed_by_msg``; over-budget stays
  ``open``). The plan document keeps a rendered view so :func:`plan_report`
  is unchanged for existing consumers.
- A typed PROPOSAL artifact (KTD4/R10): the contract gains an optional
  ``proposals[]`` persisted to ``trace_proposal`` — legal but unexercised
  until the founder exists (Phase D); ``linked_assume_id`` remaps a
  planner-local assumption id to the canonical ASSUME id.
- REQ provenance is MSG-or-ASSUME (KTD2): a requirement may carry
  ``source_assume`` instead of ``source_msg``; the trace_req DB CHECK enforces
  exactly-one at persistence (the plan-dict provenance lint is U3).

Phase 007 amendments (plan-007 U3 — two enforcement seams for the ledger):

- :func:`lint_req_provenance` (R2): a pure plan-dict lint enforcing the
  exactly-one REQ provenance rule at the plan-output level (mirroring the
  trace_req DB CHECK), wired into the Ralph loop through the ``plan_lint``
  failure channel so the planner self-corrects before persistence fails.
- :func:`assumption_gate` (KTD7/R2): the pre-increment gate — NOT a plan lint —
  demanding the top ``k_effective`` riskiest ASSUMEs be ``confirmed``, where
  ``k_effective = min(config_k, floor(question_budget / 2))`` so confirmations
  can never consume the whole question budget. A bounce emits typed
  :class:`AssumptionGateFinding` records; the host re-enters planning.

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
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_families.pipeline.sessions import RoleProfile, run_session
from agent_families.store import ASSUME_RISKS

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

# The REQ-provenance lint (007 U3 / R2): NOT part of the R10 PLAN_LINTS list —
# it is the "pure lint" seam of U3's two-seam lint architecture, a plan-dict
# check that mirrors U1's trace_req exactly-one DB CHECK so the planner gets
# typed Ralph feedback before persistence fails. Kept beside PLAN_LINTS (not
# inside it) so the R10 list stays byte-stable; run_planning merges its findings
# into the same plan_lint failure channel.
LINT_REQ_PROVENANCE = "req_provenance"

SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"

# Ticket kinds (003 R11/KTD Q4): `bug` is a tag carried on otherwise-ordinary
# tickets; omitted means "feature".
TICKET_KINDS = ("feature", "bug")
DEFAULT_TICKET_KIND = "feature"

# Per-assumption verification statuses written by the plan-checker (003 R13).
# These name the SHAPE of the ``assumption_checks`` records surfaced on the plan
# document — the human-readable verification outcome. They are distinct from the
# ``trace_assume.status`` column (007 KTD2: open|confirmed|invalidated), which is
# the queryable ledger state: ``check_plan_assumptions`` maps ``answered`` →
# ``confirmed`` (with ``confirmed_by_msg``) on the row while still returning the
# ``verified`` record below; over-budget/unavailable leaves the row ``open``.
ASSUMPTION_VERIFIED = "verified"
ASSUMPTION_UNVERIFIED = "unverified"

# trace_assume row statuses (007 KTD2) the ledger is settled into.
ASSUME_STATUS_OPEN = "open"
ASSUME_STATUS_CONFIRMED = "confirmed"
ASSUME_STATUS_INVALIDATED = "invalidated"

# The planner prompt-set version stamped on planner spans (007 U2). The typed
# ASSUME object + proposals[] contract is an instrument event (DESIGN §17): the
# schema description below changed, so the version is bumped from the implicit
# v1 (None) baseline. The orchestrator may override per call; absent an override,
# run_planning stamps this constant so the contract revision is queryable.
PLANNER_PROMPT_SET_VERSION = "planner-2-assume-typed"

# --- the planner structured-output contract (R9/R10) --------------------------
# Stays within the judge validator's schema subset (type/enum/required/
# properties/additionalProperties/items); id-shape and reference integrity are
# the lints' job, riding the Ralph feedback loop rather than the schema-retry
# path — a dangling ref is a PLANNING failure, not a malformed response.

# REQ provenance is MSG-or-ASSUME (007 KTD2 / R2): ``source_msg`` is no longer
# mandatory — a requirement may instead trace to a planner assumption via
# ``source_assume`` (the assumption's planner-local id, remapped to a canonical
# ASSUME id at persistence). The exactly-one rule is enforced by the trace_req DB
# CHECK (U1) at persistence and, at the plan-dict level, by the U3 provenance
# lint; the schema validator's subset cannot express XOR, so both fields are
# optional here and the contract is tightened downstream.
_REQUIREMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "source_msg": {"type": "string"},
        "source_assume": {"type": "string"},
    },
    "required": ["id", "text"],
    "additionalProperties": False,
}

# Typed ASSUME object (007 KTD2): the planner's assumptions become typed rows
# ``{claim, risk_if_wrong, cheapest_test}`` with an optional planner-local ``id``
# so REQs/proposals can link to them. The validator accepts a union ``type`` and
# only enforces ``required``/``additionalProperties`` when the item is an object,
# so a legacy bare string still validates and is normalized to a med-risk claim
# at persistence (cross-unit fixture compatibility — see the module Conformance
# note). A well-formed object MUST carry all three typed fields.
_ASSUMPTION_SCHEMA = {
    "type": ["string", "object"],
    "properties": {
        "id": {"type": "string"},
        "claim": {"type": "string"},
        "risk_if_wrong": {"type": "string", "enum": list(ASSUME_RISKS)},
        "cheapest_test": {"type": "string"},
    },
    "required": ["claim", "risk_if_wrong", "cheapest_test"],
    "additionalProperties": False,
}

# Typed PROPOSAL artifact (007 KTD4/R10): a decision the planner surfaces with
# options and a recommendation, optionally tied to the assumption it resolves.
# Legal but unexercised in Phase B (the explorer answers questions; nothing
# consumes proposals until the founder exists in Phase D) — persisted to
# trace_proposal so the contract and store are ready.
_PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "topic": {"type": "string"},
        "options": {"type": "array", "items": {"type": "string"}},
        "recommended": {"type": "string"},
        "linked_assume_id": {"type": "string"},
    },
    "required": ["id", "topic", "options", "recommended"],
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
        "assumptions": {"type": "array", "items": _ASSUMPTION_SCHEMA},
        # optional (007 U2): the typed PROPOSAL artifact; absent → no proposals.
        "proposals": {"type": "array", "items": _PROPOSAL_SCHEMA},
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
    injected_skills: str = "",
) -> str:
    """The hardcoded Phase 1 planner prompt over the synthesized MSG listing.

    ``carry_in`` (003 R11): (msg_id, content) pairs of explorer UAT feedback
    from the previous increment — bug REQs are extracted from them with full
    MSG provenance and their tickets tagged ``kind: "bug"``, prepended before
    the new work.

    ``injected_skills`` (004 R2/R3): the retrieved library section
    (library.retrieval.render_injection_section). Empty by default — when empty
    the prompt is byte-identical to its pre-retrieval form, so the assembly seam
    adds nothing until a caller wires retrieval in.
    """
    injection_block = (
        f"\n\n{injected_skills}\n" if injected_skills else ""
    )
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
        " rather than asked about — each an object with claim (the assumption),"
        " risk_if_wrong (one of low, med, high), and cheapest_test (the cheapest"
        " way to check it). Empty array if none.\n"
        "- proposals: OPTIONAL decisions you surface with options and a"
        " recommendation, each with a unique id, topic, options (strings),"
        " recommended, and an optional linked_assume_id naming the assumption"
        " id it resolves. Omit or use an empty array if you have none."
        f"{carry_block}{injection_block}\n\n"
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


# --- file-ownership data (R10 warn lint; promoted to enforcement in Plan 5) ------


def file_ownership_conflicts(
    ticket_files: Mapping[str, Sequence[str]],
) -> dict[str, tuple[str, ...]]:
    """Files claimed by more than one ticket — the file-ownership lint's datum.

    Maps each contested file to the ticket ids that claim it, in first-seen
    order (the order the warn lint renders). Empty when every file has a single
    owner. In Phase 1 this datum was recorded as a *warning* with no consumer; in
    Plan 5 the rehearsal wave scheduler promotes it to **enforcement** — two
    tickets sharing a file are serialized into separate fan-out waves so their
    one-shot artifacts can never conflict on merge (005 R8). Exposing it as one
    named function keeps the lint and the scheduler reading the same definition.
    """
    owners: dict[str, list[str]] = {}
    for ticket_id, files in ticket_files.items():
        for file in files:
            owners.setdefault(file, []).append(ticket_id)
    return {
        file: tuple(ids) for file, ids in owners.items() if len(ids) > 1
    }


def file_ownership_conflict_pairs(
    tickets: Sequence[dict],
) -> list[tuple[str, str]]:
    """Ticket *pairs* that claim a shared file — the pairs view of the ownership
    lint, consumed by the rehearsal wave scheduler (005 R8 — same overlap datum as
    :func:`file_ownership_conflicts`, expressed as serialization pairs rather than
    the file→owners map). Each ``ticket`` is a plan-document ticket (``id`` + a
    ``files`` list); pairs are ordered (a < b), deduplicated, and sorted; deterministic.
    """
    owners: dict[str, list[str]] = {}
    for ticket in tickets:
        for file in ticket.get("files", ()):
            owners.setdefault(file, []).append(ticket["id"])
    pairs: set[tuple[str, str]] = set()
    for owner_ids in owners.values():
        unique = sorted(set(owner_ids))
        for i in range(len(unique)):
            for j in range(i + 1, len(unique)):
                pairs.add((unique[i], unique[j]))
    return sorted(pairs)


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
        # source_msg is now optional (007 KTD2 — a REQ may instead trace to an
        # ASSUME). When present it must reference a synthesized MSG; the
        # exactly-one provenance rule itself is the U3 provenance lint's job.
        if "source_msg" in req and req["source_msg"] not in msg_ids:
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

    # file_ownership: WARN level in Phase 1 — recorded, never blocking. The same
    # datum is promoted to enforcement by Plan 5's rehearsal wave scheduler
    # (file_ownership_conflicts is the shared definition).
    conflicts = file_ownership_conflicts(
        {ticket["id"]: ticket["files"] for ticket in tickets}
    )
    for file, owner_ids in conflicts.items():
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


# --- the REQ-provenance lint (007 U3 / R2; the pure-lint seam) ---------------------


def lint_req_provenance(plan: dict) -> list[LintFinding]:
    """Every REQ carries exactly one of ``source_msg`` / ``source_assume`` (007 U3).

    REQ provenance is MSG-or-ASSUME (KTD2): a requirement traces to a source
    message OR to a planner assumption, never both and never neither. This pure
    plan-dict lint mirrors U1's ``trace_req`` exactly-one DB CHECK at the
    plan-output level, so the planner receives typed Ralph feedback (via the
    ``plan_lint`` failure channel in :func:`run_planning`) *before* the DB CHECK
    would reject the insert — the same datum, an earlier and friendlier failure.

    This is the "pure lint" half of U3's two-seam lint architecture; the
    assumption gate (:func:`assumption_gate`) is the orchestrator-seam half.
    Returns one :class:`LintFinding` per violating REQ; an empty list means every
    requirement's provenance is well-formed (so a zero-ASSUME, all-MSG brownfield
    plan passes unchanged).
    """
    findings: list[LintFinding] = []
    for req in plan["requirements"]:
        rid = req["id"]
        has_msg = bool(req.get("source_msg"))
        has_assume = bool(req.get("source_assume"))
        if has_msg == has_assume:  # both present, or neither — the XOR violation
            observed = (
                "both source_msg and source_assume set"
                if has_msg
                else "neither source_msg nor source_assume set"
            )
            findings.append(
                LintFinding(
                    LINT_REQ_PROVENANCE,
                    SEVERITY_ERROR,
                    f"requirement {rid}",
                    "exactly one of source_msg / source_assume"
                    " (MSG-or-ASSUME provenance)",
                    observed,
                )
            )
    return findings


# --- typed ASSUME / PROPOSAL contract helpers (007 U2) ----------------------------


@dataclass(frozen=True)
class NormalizedAssumption:
    """A planner assumption flattened to the trace_assume column shape.

    ``local_id`` is the planner-local handle (object form only) other planner
    artifacts link to before canonical ids are minted; ``None`` for the legacy
    bare-string form, which cannot be linked.
    """

    local_id: str | None
    claim: str
    risk_if_wrong: str
    cheapest_test: str


def normalize_assumption(item: str | Mapping) -> NormalizedAssumption:
    """Flatten one contract assumption (string OR typed object) to ASSUME fields.

    A legacy bare string normalizes to a med-risk claim with no cheapest test
    (007 U2 cross-unit fixture compatibility); a typed object passes its fields
    through. The contract schema has already validated the object's shape.
    """
    if isinstance(item, str):
        return NormalizedAssumption(
            local_id=None, claim=item, risk_if_wrong="med", cheapest_test=""
        )
    return NormalizedAssumption(
        local_id=item.get("id"),
        claim=item["claim"],
        risk_if_wrong=item["risk_if_wrong"],
        cheapest_test=item.get("cheapest_test", ""),
    )


# Risk ordering for the question-ranking seam (KTD7) — highest risk first.
_RISK_RANK = {"high": 0, "med": 1, "low": 2}


def _candidate_risk(candidate) -> str:
    """Extract ``risk_if_wrong`` from a candidate (dict, sqlite Row, or object)."""
    try:
        # dicts and sqlite3.Row both index by column/key name
        return candidate["risk_if_wrong"]
    except (TypeError, KeyError, IndexError):
        return getattr(candidate, "risk_if_wrong", "med")


def rank_questions(candidates: Sequence) -> list:
    """Order assumption candidates for confirmation (007 KTD7, v1 seam).

    v1 is pure risk-weight ordering — high before med before low — with stable
    ties (input order preserved). A real EVPI estimator (and, post-U4, the
    DEC-category weight) replaces the key later without touching callers. The
    seam exists so the gate and the assumption-conversion consume one definition
    of "which question matters most."
    """
    return sorted(
        candidates, key=lambda c: _RISK_RANK.get(_candidate_risk(c), 1)
    )


# --- persistence (orchestrator-minted canonical ids; one transaction) -------------


def plan_meta_key(run_id: int) -> str:
    return f"plan:run:{run_id}"


def _persist_plan(
    store: Store, run_id: int, plan: dict, warnings: tuple[LintFinding, ...]
) -> dict:
    """Write the linted plan in one transaction and return the canonical document.

    Canonical REQ/TKT/AC/ASSUME/PROP ids are minted here in planner-output order
    — the planner is an untrusted producer; its local ids exist only inside its
    own output and are remapped on every link. Typed assumptions become
    trace_assume rows (007 KTD2: the queryable ledger, the single source of truth
    that check_plan_assumptions settles), and proposals become trace_proposal
    rows (KTD4); the plan document keeps a rendered view of each so plan_report
    still surfaces them.
    """
    req_map: dict[str, str] = {}
    tkt_map: dict[str, str] = {}
    assume_map: dict[str, str] = {}  # planner-local assumption id → canonical
    run = store.get_run(run_id)
    canonical_reqs: list[dict] = []
    canonical_tkts: list[dict] = []
    canonical_props: list[dict] = []
    with store.transaction():
        # ASSUME rows first — REQs and proposals may link to them (007 KTD2).
        for i, item in enumerate(plan["assumptions"]):
            norm = normalize_assumption(item)
            aid = f"ASSUME-r{run_id}-{i:03d}"
            store.conn.execute(
                "INSERT INTO trace_assume (id, run_id, claim, risk_if_wrong,"
                " cheapest_test) VALUES (?, ?, ?, ?, ?)",
                (aid, run_id, norm.claim, norm.risk_if_wrong, norm.cheapest_test),
            )
            if norm.local_id is not None:
                assume_map[norm.local_id] = aid
        for i, req in enumerate(plan["requirements"]):
            cid = f"REQ-r{run_id}-{i:03d}"
            req_map[req["id"]] = cid
            source_assume = req.get("source_assume")
            if source_assume:
                assume_cid = assume_map.get(source_assume)
                if assume_cid is None:
                    raise PlanningError(
                        f"requirement {req['id']} sources unknown assumption"
                        f" '{source_assume}' — no assumption carries that id"
                    )
                store.conn.execute(
                    "INSERT INTO trace_req (id, source_assume_id) VALUES (?, ?)",
                    (cid, assume_cid),
                )
                canonical_reqs.append(
                    {"id": cid, "text": req["text"], "source_assume": assume_cid}
                )
            else:
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
        for i, proposal in enumerate(plan.get("proposals", [])):
            pid = f"PROP-r{run_id}-{i:03d}"
            linked_local = proposal.get("linked_assume_id")
            linked_cid = assume_map.get(linked_local) if linked_local else None
            store.conn.execute(
                "INSERT INTO trace_proposal (id, run_id, topic, options_json,"
                " recommended, linked_assume_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    pid,
                    run_id,
                    proposal["topic"],
                    json.dumps(proposal["options"], ensure_ascii=False),
                    proposal["recommended"],
                    linked_cid,
                ),
            )
            canonical_props.append(
                {
                    "id": pid,
                    "topic": proposal["topic"],
                    "options": list(proposal["options"]),
                    "recommended": proposal["recommended"],
                    "linked_assume_id": linked_cid,
                }
            )
        document = {
            "run_id": run_id,
            "spec_ref": run["spec_ref"],
            "requirements": canonical_reqs,
            "tickets": canonical_tkts,
            # rendered view — original shape passes through (legacy strings stay
            # strings) so existing report consumers are unaffected; trace_assume
            # is the queryable source of truth (007 KTD2).
            "assumptions": list(plan["assumptions"]),
            "proposals": canonical_props,
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
    """One planner assumption (its ``claim``) rendered as a verified-oracle question."""
    return (
        f"The plan for the current increment assumed: {assumption} — Is this"
        " assumption correct for the target app? Verify it against the live"
        " UI and answer concretely."
    )


def load_assume_rows(store: Store, run_id: int) -> list:
    """The run's trace_assume ledger rows in id order (007 KTD2)."""
    return store.conn.execute(
        "SELECT id, run_id, claim, basis, risk_if_wrong, cheapest_test, status,"
        " confirmed_by_msg FROM trace_assume WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()


def check_plan_assumptions(store: Store, run_id: int, ask) -> list[dict]:
    """Convert the run's ASSUME ledger into verification questions (003 R13 / 007 KTD2).

    Subsumed onto ``trace_assume`` rows (007 KTD2): the ledger — not the plan
    document — is the single source of truth. Rows are ranked by risk through
    the :func:`rank_questions` seam (KTD7: confirm top-risk first) before
    conversion, so under a tight question budget the riskiest assumptions are
    the ones spent on.

    ``ask`` is the verified-oracle round-trip bound by the orchestrator —
    ``lambda q: explorer.ask_question(store, episode_id, q, mentions, ...)``
    — returning an object exposing ``outcome`` (``answered`` |
    ``answer_unavailable`` | ``budget_exhausted``), ``answer``, and
    ``answer_msg_id``. Conversions consume question budget INSIDE ``ask`` (the
    budget trains elicitation; free verification would untrain it — KTD Q6).
    Outcomes settle both the human-readable record AND the ledger row:

    - ``answered`` → record ``verified`` + answer; row → ``confirmed`` with
      ``confirmed_by_msg`` = the answer MSG;
    - ``budget_exhausted`` / ``answer_unavailable`` → record ``unverified``
      (typed risk, not a blocker); the row stays ``open``.

    The records are persisted onto the plan document (``assumption_checks``)
    and surfaced by :func:`plan_report`.
    """
    rows = rank_questions(load_assume_rows(store, run_id))
    records: list[dict] = []
    for index, row in enumerate(rows):
        claim = row["claim"]
        # ask() runs (and commits) its own round-trip; the ledger UPDATE rides a
        # separate short transaction after it so the write lock is never held
        # across a live LLM call (the original conversion's lock discipline).
        outcome = ask(build_assumption_question(claim))
        kind = outcome.outcome
        if kind == "answered":
            answer_msg = getattr(outcome, "answer_msg_id", None)
            with store.transaction():
                store.conn.execute(
                    "UPDATE trace_assume SET status = ?, confirmed_by_msg = ?"
                    " WHERE id = ?",
                    (ASSUME_STATUS_CONFIRMED, answer_msg, row["id"]),
                )
            record = {
                "index": index,
                "assume_id": row["id"],
                "assumption": claim,
                "status": ASSUMPTION_VERIFIED,
                "reason": None,
                "answer": outcome.answer,
            }
        elif kind in ("budget_exhausted", "answer_unavailable"):
            record = {
                "index": index,
                "assume_id": row["id"],
                "assumption": claim,
                "status": ASSUMPTION_UNVERIFIED,
                "reason": kind,
                "answer": None,
            }
            logger.info(
                "assumption %s recorded unverified (%s) — typed risk, not a"
                " blocker (003 R13); ledger row stays open: %s",
                row["id"],
                kind,
                claim,
            )
        else:
            raise PlanningError(
                f"ask returned unknown outcome {kind!r} for assumption"
                f" {row['id']} (expected answered / answer_unavailable /"
                " budget_exhausted)"
            )
        records.append(record)
    document = plan_report(store, run_id)
    document["assumption_checks"] = records
    store.set_meta(
        plan_meta_key(run_id),
        json.dumps(document, sort_keys=True, ensure_ascii=False),
    )
    return records


# --- the pre-increment assumption gate (007 U3 / R2; the orchestrator seam) --------

# The gate's typed-failure label (the §7 location prefix). The gate is NOT a plan
# lint and deliberately does not borrow the ``plan_lint`` failure-records kind:
# it fires at a different seam (plan-accepted → increment-execute) and emits a
# typed in-memory record the host re-plans on (see Deviations).
GATE_LABEL = "assumption_gate"


@dataclass(frozen=True)
class AssumptionGateFinding:
    """One top-risk ASSUME left unconfirmed at the gate (the §7 typed shape).

    Mirrors :class:`LintFinding`'s location/expected/observed triple so the host
    can render it through the same feedback channel, but it is a distinct type
    because the gate is not a plan lint (007 KTD7).
    """

    assume_id: str
    risk_if_wrong: str
    location: str
    expected: str
    observed: str

    def as_dict(self) -> dict:
        return {
            "assume_id": self.assume_id,
            "risk_if_wrong": self.risk_if_wrong,
            "location": self.location,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class AssumptionGateResult:
    """The gate's verdict: pass, or a typed bounce naming every unconfirmed top-k
    ASSUME. ``k_effective`` is recorded so the host can log/telemeter the
    budget-scaled threshold that was applied."""

    passed: bool
    k_effective: int
    failures: tuple[AssumptionGateFinding, ...]


def k_effective(config_k: int, question_budget: int) -> int:
    """The gate's budget-scaled confirmation count (007 KTD7).

    ``k_effective = min(config_k, floor(question_budget / 2))`` — confirmations
    can never consume more than half the increment's question budget, so the
    gate can never starve the elicitation it is meant to protect. With a tiny
    budget (``< 2``) ``k_effective`` is ``0`` and the gate never blocks.
    """
    if config_k < 0:
        raise PlanningError(f"gate k must be a non-negative count, got {config_k}")
    if question_budget < 0:
        raise PlanningError(
            f"question_budget must be non-negative, got {question_budget}"
        )
    return min(config_k, question_budget // 2)


def assumption_gate(
    store: Store, run_id: int, *, config_k: int, question_budget: int
) -> AssumptionGateResult:
    """Gate increment execution on the top-risk assumptions being confirmed (007 KTD7).

    Fires between plan acceptance and increment execution — the seam
    :func:`check_plan_assumptions` already occupies post-persist — NOT as a plan
    lint. The run's ASSUME ledger is ranked by risk through the
    :func:`rank_questions` seam; the top ``k_effective`` riskiest assumptions
    must each be ``confirmed``. Any of them still ``open`` (or ``invalidated``)
    is a bounce: the result carries a typed :class:`AssumptionGateFinding` per
    offender, and the host re-enters the planning Ralph loop (confirmations ride
    the question budget through :func:`check_plan_assumptions`). ``k`` auto-scales
    so the gate can never consume the whole budget (:func:`k_effective`).

    ``config_k`` and ``question_budget`` are caller-supplied (the orchestrator
    routes ``config_k`` from ``[greenfield] gate_k`` and ``question_budget`` from
    the increment's question cap) — nothing is hardcoded here.
    """
    k = k_effective(config_k, question_budget)
    ranked = rank_questions(load_assume_rows(store, run_id))
    top = ranked[:k]
    failures = tuple(
        AssumptionGateFinding(
            assume_id=row["id"],
            risk_if_wrong=row["risk_if_wrong"],
            location=f"{GATE_LABEL}: assumption {row['id']}",
            expected="confirmed before increment execution"
            " (top-risk assumptions, KTD7)",
            observed=f"status '{row['status']}' (risk {row['risk_if_wrong']})",
        )
        for row in top
        if row["status"] != ASSUME_STATUS_CONFIRMED
    )
    return AssumptionGateResult(passed=not failures, k_effective=k, failures=failures)


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
    assign_provider: Callable[[], str] | None = None,
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
    # plan-010 U1 (R2): the assign stage fills the existing inert
    # ``injected_skills=`` planner seam. ``None`` (the default) → ``""``, keeping
    # the planner prompt byte-identical to today (the seam was inert).
    injected_skills = assign_provider() if assign_provider is not None else ""
    base_prompt = build_planner_prompt(
        spec_path.name, messages, size_budget, carry_in=carry_in,
        injected_skills=injected_skills,
    )

    # The typed-ASSUME/proposals contract is an instrument event (007 U2): absent
    # a caller override, stamp the bumped planner prompt-set version on every span.
    stamped_version = (
        prompt_set_version if prompt_set_version is not None
        else PLANNER_PROMPT_SET_VERSION
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
            prompt_set_version=stamped_version,
            mode=mode,
            script_path=script_path,
        )
        plan = result.output
        # The R10 deterministic lints plus the U3 REQ-provenance lint (a separate
        # seam, charged through the same plan_lint failure channel so the planner
        # gets typed feedback before the trace_req DB CHECK fires at persistence).
        findings = lint_plan(plan, msg_ids, size_budget=size_budget)
        findings = findings + lint_req_provenance(plan)
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
