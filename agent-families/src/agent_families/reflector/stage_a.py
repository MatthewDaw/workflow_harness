"""Reflector Stage A: deterministic attribution with honest sinks (plan-004 U5).

Per failed scenario, the §12.2 decision procedure runs as a sequence of lookups
over the traceability store — the same SCEN -> FEAT -> MSG -> REQ -> TKT -> AC ->
SPAN -> CHK join that ``af trace chain`` walks — producing an attribution
``{primary, contributing[]}`` and a *case file* (the implicated rows + evidence
refs) that Stage B (U6) reflects on.

The procedure is deterministic by construction (DESIGN §12.2): steps 1-6 are pure
lookups; step 7's discriminator is **mechanical re-execution** of the verifier's
stored CHK repro envelope (which is exactly why CHK persists a repro command);
only two narrow micro-judgments ever reach an LLM —

1. *elicitability* (step 1, when a FEAT was not communicated): table-driven from a
   fixed probe-question taxonomy; the LLM fires ONLY for out-of-taxonomy FEATs;
2. *answer-contradiction* (step 8): a lookup of Phase 2's stored oracle-checker
   verdicts (§9) — the LLM fires ONLY when no verdict was recorded at answer time.

Everything else — including the entire attribution decision — runs with zero
quota and no ``claude`` on PATH.

Honest sinks (R9): PLANNER/WORKER/VERIFIER attributions map to the four seeded
library families and become Stage B idea candidates. EXPLORER/GRADER attributions
have **no library family** — they sink to *instrument-health* records in the
Phase 2 review queue, never ideas.

Environment-owning seams (KTD "post-settlement re-execution owns its
environment"): step 7's repro re-execution and the coverage-instrumented scenario
re-run both happen against the *retained* episode workspace, so Stage A takes the
re-execution and coverage substrates as injected seams — the live wiring (clone
server restart, target reset-to-seed, vite-plugin-istanbul / V8 + c8 merge to
per-scenario file lists) supplies them; the offline suite injects scripted fakes.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence

from agent_families.judge import run_judge
from agent_families.store import Store

# --- attribution vocabulary -----------------------------------------------------
#
# The roles §12.2 attributes to. PLANNER/WORKER/VERIFIER are three of the four
# Phase-0-seeded families (the fourth, context-retriever, is never a §12.2 owner);
# EXPLORER/GRADER have no library family — `ROLE_FAMILY[role] is None` is exactly
# the instrument-health sink test (R9).
ATTRIBUTION_ROLES = ("planner", "worker", "verifier", "explorer", "grader")

ROLE_FAMILY: dict[str, str | None] = {
    "planner": "planner",
    "worker": "worker",
    "verifier": "verifier",
    "explorer": None,
    "grader": None,
}

# review_queue.kind for an explorer/grader attribution (R9 sink).
INSTRUMENT_HEALTH_KIND = "instrument_health"

# Fixed probe-question taxonomy (DESIGN §12.2). Each FEAT is tagged with a probe
# *category* at registration (seeded from the Phase 2 registry clusters); the
# category's elicitability is table-driven here, so step 1's micro-judgment is a
# lookup for in-taxonomy FEATs and the LLM fires only for out-of-taxonomy ones.
# Provenance: the categories below are the registry's standing question clusters
# (DESIGN §8 registration phase); `elicitable=True` means a planner asking the
# standard probe for this category would have surfaced the FEAT — so a missing
# mention is the PLANNER's elicitation gap, not the EXPLORER's prompt.
PROBE_TAXONOMY: dict[str, bool] = {
    # Discoverable from the request/spec the planner already holds, or via a
    # standard clarifying probe — non-elicitation is a planner gap.
    "core-crud": True,
    "auth-gated": True,
    "search-filter": True,
    "navigation": True,
    "settings": True,
    # Only observable by exploring the running app (hidden affordances, admin-
    # only surfaces, undocumented edge behavior) — a missing mention here is the
    # explorer's prompt-construction gap, not the planner's.
    "hidden-affordance": False,
    "admin-only": False,
    "edge-behavior": False,
}


class StageAError(Exception):
    """A broken Stage A precondition, with an actionable message."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the re-execution and coverage seams ----------------------------------------


@dataclass(frozen=True)
class ChkEnvelope:
    """A verifier check's stored repro envelope — step 7's re-execution input."""

    chk_id: str
    ac_id: str
    repro_command: str
    recorded_result: str
    evidence: str


# A repro runner re-executes a CHK envelope NOW against the retained workspace and
# returns True iff it still passes. The live runner brings up its own environment
# (KTD); the offline suite injects a scripted fake.
ReproRunner = Callable[[ChkEnvelope], bool]

# A coverage provider returns the source files a failing scenario exercises, from
# the coverage-instrumented re-run (this unit's substrate). Joined against
# SPAN.files_touched to locate a post-verification breaking ticket.
CoverageProvider = Callable[[str], Sequence[str]]


def read_merged_coverage(path: str) -> dict[str, list[str]]:
    """Read the template's merged per-scenario coverage map (the substrate step 7
    consumes): a JSON object ``{scen_id: [source_file, ...]}`` produced by the
    instrumented re-run's merge step (vite-plugin-istanbul / V8 browser-side + c8
    on the Hono server, merged and source-mapped). Returns a plain dict; a
    ``CoverageProvider`` over it is ``lambda sid: merged.get(sid, [])``."""
    with open(path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise StageAError(
            f"merged coverage at {path} must be a JSON object keyed by SCEN id"
        )
    out: dict[str, list[str]] = {}
    for sid, files in data.items():
        if not isinstance(files, list):
            raise StageAError(
                f"coverage entry for {sid} must be a list of source files"
            )
        out[sid] = sorted({str(f) for f in files})
    return out


# A judge seam for the two micro-judgments; defaults to the module ``run_judge``
# so a test can monkeypatch ``stage_a.run_judge`` to raise and prove the
# deterministic path never consults it.
JudgeFn = Callable[..., object]


# --- attribution data model -----------------------------------------------------


@dataclass(frozen=True)
class Cause:
    """One attributed cause: a role, the aspect of its contract that failed, and
    the implicated artifact refs (evidence)."""

    role: str
    aspect: str
    refs: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "role": self.role,
            "aspect": self.aspect,
            "refs": list(self.refs),
            "family": ROLE_FAMILY.get(self.role),
        }


@dataclass(frozen=True)
class CaseFile:
    """The implicated rows + evidence refs Stage A assembles for Stage B (§12.3)."""

    scen_id: str
    feat_id: str
    msg_ids: tuple[str, ...]
    req_ids: tuple[str, ...]
    tkt_ids: tuple[str, ...]
    ac_ids: tuple[str, ...]
    span_ids: tuple[str, ...]
    chk_ids: tuple[str, ...]
    qa_ids: tuple[int, ...]
    evidence: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "scen_id": self.scen_id,
            "feat_id": self.feat_id,
            "msg_ids": list(self.msg_ids),
            "req_ids": list(self.req_ids),
            "tkt_ids": list(self.tkt_ids),
            "ac_ids": list(self.ac_ids),
            "span_ids": list(self.span_ids),
            "chk_ids": list(self.chk_ids),
            "qa_ids": list(self.qa_ids),
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class Attribution:
    """A failed scenario's attribution: a primary owner (routing pressure) plus
    NTSB-style contributing factors, the assembled case file, and the
    instrument-health sink flag when the primary has no library family (R9)."""

    scen_id: str
    primary: Cause
    contributing: tuple[Cause, ...]
    case_file: CaseFile
    sink: str | None = None

    @property
    def is_instrument_health(self) -> bool:
        return self.sink == INSTRUMENT_HEALTH_KIND

    def to_dict(self) -> dict:
        return {
            "scen_id": self.scen_id,
            "primary": self.primary.to_dict(),
            "contributing": [c.to_dict() for c in self.contributing],
            "case_file": self.case_file.to_dict(),
            "sink": self.sink,
        }

    def to_json(self) -> str:
        """Byte-stable serialization (sorted keys) — the attribution-stability
        guarantee depends on this carrying no timestamps or volatile ordering."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False)


# --- the chain join (the same walk as `af trace chain`) -------------------------


@dataclass
class _Chain:
    scen: sqlite3.Row
    feat: sqlite3.Row | None
    msg_ids: list[str] = field(default_factory=list)
    req_rows: list[sqlite3.Row] = field(default_factory=list)
    tkt_rows: list[sqlite3.Row] = field(default_factory=list)
    ac_rows: list[sqlite3.Row] = field(default_factory=list)
    span_rows: list[sqlite3.Row] = field(default_factory=list)
    chk_rows: list[sqlite3.Row] = field(default_factory=list)
    qa_rows: list[sqlite3.Row] = field(default_factory=list)


def _placeholders(n: int) -> str:
    return ",".join("?" * n)


def _load_chain(store: Store, scen_id: str) -> _Chain:
    """Walk SCEN -> FEAT -> MSG -> REQ -> TKT -> AC -> SPAN -> CHK plus the Q&A
    log for the scenario's FEAT — the join `af trace chain` prints, here returned
    as rows for the decision procedure (the CLI owns the human-readable copy;
    this is the programmatic consumer the report only references by command)."""
    conn = store.conn
    scen = conn.execute(
        "SELECT * FROM trace_scen WHERE id = ?", (scen_id,)
    ).fetchone()
    if scen is None:
        raise StageAError(
            f"no scenario {scen_id!r}; settled scenarios are listed in"
            " `af episode report <episode>`"
        )
    feat_id = scen["feat_id"]
    feat = conn.execute(
        "SELECT * FROM trace_feat WHERE id = ?", (feat_id,)
    ).fetchone()

    msg_ids = [
        r["msg_id"]
        for r in conn.execute(
            "SELECT msg_id FROM trace_msg_mentions WHERE feat_id = ?"
            " ORDER BY msg_id",
            (feat_id,),
        ).fetchall()
    ]
    req_rows = (
        conn.execute(
            "SELECT * FROM trace_req WHERE source_msg_id IN"
            f" ({_placeholders(len(msg_ids))}) ORDER BY id",
            msg_ids,
        ).fetchall()
        if msg_ids
        else []
    )
    req_ids = [r["id"] for r in req_rows]
    tkt_rows = (
        conn.execute(
            "SELECT DISTINCT t.* FROM trace_tkt_covers c"
            " JOIN trace_tkt t ON t.id = c.tkt_id"
            f" WHERE c.req_id IN ({_placeholders(len(req_ids))}) ORDER BY t.id",
            req_ids,
        ).fetchall()
        if req_ids
        else []
    )
    ac_rows = (
        conn.execute(
            "SELECT * FROM trace_ac WHERE req_id IN"
            f" ({_placeholders(len(req_ids))}) ORDER BY id",
            req_ids,
        ).fetchall()
        if req_ids
        else []
    )
    tkt_ids = [r["id"] for r in tkt_rows]
    span_rows = (
        conn.execute(
            "SELECT * FROM trace_span WHERE ticket_id IN"
            f" ({_placeholders(len(tkt_ids))}) ORDER BY id",
            tkt_ids,
        ).fetchall()
        if tkt_ids
        else []
    )
    ac_ids = [r["id"] for r in ac_rows]
    chk_rows = (
        conn.execute(
            "SELECT * FROM trace_chk WHERE ac_id IN"
            f" ({_placeholders(len(ac_ids))}) ORDER BY id",
            ac_ids,
        ).fetchall()
        if ac_ids
        else []
    )
    # Q&A exchanges whose question mentions FEAT-y (step 8). The question_msg_id
    # join to trace_msg_mentions is the same mention edge the explorer wrote.
    qa_rows = conn.execute(
        "SELECT q.* FROM qa_log q"
        " JOIN trace_msg_mentions m ON m.msg_id = q.question_msg_id"
        " WHERE m.feat_id = ? ORDER BY q.id",
        (feat_id,),
    ).fetchall()

    return _Chain(
        scen=scen,
        feat=feat,
        msg_ids=msg_ids,
        req_rows=list(req_rows),
        tkt_rows=list(tkt_rows),
        ac_rows=list(ac_rows),
        span_rows=list(span_rows),
        chk_rows=list(chk_rows),
        qa_rows=list(qa_rows),
    )


# --- the two micro-judgments ----------------------------------------------------

_ELICITABILITY_SCHEMA = {
    "type": "object",
    "properties": {"elicitable": {"type": "boolean"}},
    "required": ["elicitable"],
    "additionalProperties": False,
}

_ANSWER_CONTRADICTION_SCHEMA = {
    "type": "object",
    "properties": {"contradicts": {"type": "boolean"}},
    "required": ["contradicts"],
    "additionalProperties": False,
}


def _resolve_elicitability(
    feat_id: str,
    probe_category: str | None,
    *,
    judge_fn: JudgeFn,
    judge_model: str,
    judge_max_retries: int,
    judge_mode: str | None,
    judge_fixtures_dir,
) -> bool:
    """Micro-judgment #1 — table-driven; the LLM fires only out-of-taxonomy."""
    if probe_category is not None and probe_category in PROBE_TAXONOMY:
        return PROBE_TAXONOMY[probe_category]
    prompt = (
        "You are the reflector's elicitability classifier. A feature was tested"
        " and failed, and it was NEVER mentioned in the planning prompt or Q&A."
        f" The feature's registry id is {feat_id}; its probe category"
        f" ({probe_category!r}) is outside the standing taxonomy.\n\n"
        "Decide ONE thing: could a planner asking the standard clarifying probes"
        " (from the request and a normal Q&A round) have surfaced this feature?"
        " If yes it is elicitable (a PLANNER elicitation gap); if it is only"
        " discoverable by exploring the running app, it is not (an EXPLORER"
        " prompt gap).\n\n"
        'Return structured output only: {"elicitable": true|false}.'
    )
    result = judge_fn(
        prompt,
        _ELICITABILITY_SCHEMA,
        judge_model,
        max_retries=judge_max_retries,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    return bool(result.output["elicitable"])


def _answer_contradicts(
    qa: sqlite3.Row,
    *,
    judge_fn: JudgeFn,
    judge_model: str,
    judge_max_retries: int,
    judge_mode: str | None,
    judge_fixtures_dir,
) -> bool:
    """Micro-judgment #2 — a LOOKUP of the stored oracle-checker verdict (§9); the
    LLM fires only when no verdict was recorded at answer time."""
    verdict = qa["checker_verdict"]
    if verdict is not None:
        return verdict == "fail"
    prompt = (
        "You are the reflector's answer-contradiction classifier. During an"
        " episode the explorer answered a pipeline question about the target"
        " app, and no grader-side oracle check was recorded at answer time.\n\n"
        f"## Question\n{qa['question']}\n\n"
        f"## Answer\n{qa['answer'] or ''}\n\n"
        "Decide ONE thing: does the answer contradict what the app actually"
        " does (an EXPLORER answering gap)? Default to false unless the answer is"
        " positively wrong.\n\n"
        'Return structured output only: {"contradicts": true|false}.'
    )
    result = judge_fn(
        prompt,
        _ANSWER_CONTRADICTION_SCHEMA,
        judge_model,
        max_retries=judge_max_retries,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    return bool(result.output["contradicts"])


# --- step 7: the mechanical discriminator ---------------------------------------


def _locate_breaking_ticket(
    store: Store, episode_id, chain: _Chain, coverage_files: Sequence[str]
) -> tuple[str | None, str | None]:
    """A regression after verification: the coverage trace of the failing scenario
    names the source files; join vs SPAN.files_touched (+ increment tags) to
    locate the breaking ticket. The breaker need not be one of the FEAT's covering
    tickets — a later increment's worker can regress it — so the join scans every
    span in the episode (via the runs->episode link) plus the chain's own spans.
    Returns ``(ticket_id, span_id)`` or ``(None, None)`` when no span touched the
    implicated files."""
    cover = {str(f) for f in coverage_files}
    if not cover:
        return (None, None)
    candidates: dict[str, sqlite3.Row] = {s["id"]: s for s in chain.span_rows}
    if episode_id is not None:
        for span in store.conn.execute(
            "SELECT s.* FROM trace_span s"
            " LEFT JOIN runs r ON r.id = s.run_id"
            " WHERE r.episode_id = ? OR s.episode = ?"
            " ORDER BY s.id",
            (episode_id, str(episode_id)),
        ).fetchall():
            candidates[span["id"]] = span
    hits: list[tuple[int, str, str, str]] = []
    for span in candidates.values():
        if span["ticket_id"] is None:
            continue
        try:
            touched = set(json.loads(span["files_json"] or "[]"))
        except (TypeError, json.JSONDecodeError):
            touched = set()
        if touched & cover:
            # increment_id tags order regressions; NULL sorts first (-1) so a
            # tagged (later) increment wins, then span id breaks ties.
            inc_key = _increment_sort_key(span["increment_id"])
            hits.append((inc_key, span["ticket_id"], span["id"], span["id"]))
    if not hits:
        return (None, None)
    # Latest increment is the most likely breaker; deterministic on (inc, span id).
    hits.sort(key=lambda h: (h[0], h[3]))
    _, ticket_id, span_id, _ = hits[-1]
    return (ticket_id, span_id)


def _increment_sort_key(increment_id: str | None) -> int:
    if not increment_id:
        return -1
    digits = "".join(ch for ch in increment_id if ch.isdigit())
    return int(digits) if digits else 0


# --- the decision procedure (§12.2) ---------------------------------------------


def attribute_failed_scenario(
    store: Store,
    scen_id: str,
    *,
    repro_runner: ReproRunner,
    coverage_provider: CoverageProvider,
    feat_probe_categories: Mapping[str, str] | None = None,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "sonnet",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> Attribution:
    """Run §12.2 steps 1-8 over the trace store for one failed scenario.

    Steps 1-6 are pure lookups; step 7 re-executes the stored CHK repro envelope
    via ``repro_runner`` (and, on a now-failing repro, locates the breaking ticket
    with ``coverage_provider``); steps 1 and 8 reach an LLM only for an
    out-of-taxonomy FEAT or a Q&A with no recorded checker verdict.
    """
    judge = judge_fn if judge_fn is not None else run_judge
    probe_cats = dict(feat_probe_categories or {})
    chain = _load_chain(store, scen_id)
    feat_id = chain.scen["feat_id"]
    if chain.scen["result"] != "fail":
        raise StageAError(
            f"{scen_id} did not fail (result={chain.scen['result']!r});"
            " Stage A attributes failed scenarios only"
        )

    evidence: list[str] = []
    if chain.scen["evidence"]:
        evidence.append(f"SCEN {scen_id}: {chain.scen['evidence']}")

    primary: Cause
    # ---- Step 1: COMMUNICATED? ------------------------------------------------
    if not chain.msg_ids:
        elicitable = _resolve_elicitability(
            feat_id,
            probe_cats.get(feat_id),
            judge_fn=judge,
            judge_model=judge_model,
            judge_max_retries=judge_max_retries,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        )
        if elicitable:
            primary = Cause("planner", "elicitation", (feat_id,))
        else:
            primary = Cause("explorer", "prompt", (feat_id,))
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    # ---- Step 2: EXTRACTED? ---------------------------------------------------
    if not chain.req_rows:
        primary = Cause("planner", "requirement_extraction",
                        tuple(chain.msg_ids))
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    req_ids = tuple(r["id"] for r in chain.req_rows)
    # ---- Step 3: COVERED? -----------------------------------------------------
    if not chain.tkt_rows:
        primary = Cause("planner", "coverage", req_ids)
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    # ---- Step 4: SPECIFIED? ---------------------------------------------------
    if not chain.ac_rows:
        primary = Cause("planner", "acceptance_criteria", req_ids)
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    # ---- Step 5: IMPLEMENTED? -------------------------------------------------
    # A covering ticket closed normally (status 'done') with a non-empty diff.
    implemented_span = _first_implemented_span(chain)
    if implemented_span is None:
        primary = Cause("worker", "implementation",
                        tuple(t["id"] for t in chain.tkt_rows))
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    # ---- Step 6: VERIFIED? ----------------------------------------------------
    passing_chk = next(
        (c for c in chain.chk_rows if c["result"] == "pass"), None
    )
    if passing_chk is None:
        primary = Cause("verifier", "incomplete_verification",
                        tuple(a["id"] for a in chain.ac_rows))
        return _finalize(store, scen_id, chain, primary, judge=judge,
                         judge_model=judge_model,
                         judge_max_retries=judge_max_retries,
                         judge_mode=judge_mode,
                         judge_fixtures_dir=judge_fixtures_dir,
                         evidence=evidence)

    # ---- Step 7: DISCRIMINATE (mechanical re-execution) -----------------------
    envelope = ChkEnvelope(
        chk_id=passing_chk["id"],
        ac_id=passing_chk["ac_id"],
        repro_command=passing_chk["repro_command"],
        recorded_result=passing_chk["result"],
        evidence=passing_chk["evidence"] or "",
    )
    still_passes = repro_runner(envelope)
    if still_passes:
        # AC satisfied-as-written but wrong/too weak — a PLANNER AC-quality gap.
        primary = Cause("planner", "ac_quality", (passing_chk["id"],))
    else:
        # Regression after verification — locate the breaking ticket mechanically.
        coverage_files = list(coverage_provider(scen_id))
        evidence.append(
            "coverage(" + scen_id + "): " + ", ".join(coverage_files)
        )
        ticket_id, span_id = _locate_breaking_ticket(
            store, chain.scen["episode_id"], chain, coverage_files
        )
        if ticket_id is not None:
            refs = tuple(r for r in (ticket_id, span_id, passing_chk["id"]) if r)
            primary = Cause("worker", "regression", refs)
        else:
            # No span touched the implicated files — the breaking change escaped
            # this scenario's covering tickets; the integration pass on the
            # increment owns it (VERIFIER), per step 7's second arm.
            primary = Cause("verifier", "integration", (passing_chk["id"],))

    return _finalize(store, scen_id, chain, primary, judge=judge,
                     judge_model=judge_model,
                     judge_max_retries=judge_max_retries,
                     judge_mode=judge_mode,
                     judge_fixtures_dir=judge_fixtures_dir,
                     evidence=evidence)


def _first_implemented_span(chain: _Chain) -> sqlite3.Row | None:
    done_tickets = {t["id"] for t in chain.tkt_rows if t["status"] == "done"}
    for span in chain.span_rows:
        if span["ticket_id"] not in done_tickets:
            continue
        try:
            touched = json.loads(span["files_json"] or "[]")
        except (TypeError, json.JSONDecodeError):
            touched = []
        if touched:
            return span
    return None


def _finalize(
    store: Store,
    scen_id: str,
    chain: _Chain,
    primary: Cause,
    *,
    judge: JudgeFn,
    judge_model: str,
    judge_max_retries: int,
    judge_mode: str | None,
    judge_fixtures_dir,
    evidence: list[str],
) -> Attribution:
    """Step 8 (answer check, additive contributing) + case-file assembly + the
    instrument-health sink decision."""
    contributing: list[Cause] = []
    # ---- Step 8: ANSWER CHECK (additive) --------------------------------------
    for qa in chain.qa_rows:
        if _answer_contradicts(
            qa,
            judge_fn=judge,
            judge_model=judge_model,
            judge_max_retries=judge_max_retries,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        ):
            ref = qa["question_msg_id"] or f"qa:{qa['id']}"
            cause = Cause("explorer", "answering", (ref,))
            # Don't double-count if the explorer is already the primary owner.
            if not (primary.role == "explorer" and primary.aspect == "answering"):
                contributing.append(cause)

    case_file = CaseFile(
        scen_id=scen_id,
        feat_id=chain.scen["feat_id"],
        msg_ids=tuple(chain.msg_ids),
        req_ids=tuple(r["id"] for r in chain.req_rows),
        tkt_ids=tuple(t["id"] for t in chain.tkt_rows),
        ac_ids=tuple(a["id"] for a in chain.ac_rows),
        span_ids=tuple(s["id"] for s in chain.span_rows),
        chk_ids=tuple(c["id"] for c in chain.chk_rows),
        qa_ids=tuple(q["id"] for q in chain.qa_rows),
        evidence=tuple(evidence),
    )
    sink = (
        INSTRUMENT_HEALTH_KIND
        if ROLE_FAMILY.get(primary.role) is None
        else None
    )
    return Attribution(
        scen_id=scen_id,
        primary=primary,
        contributing=tuple(contributing),
        case_file=case_file,
        sink=sink,
    )


# --- the Stage A pass -----------------------------------------------------------


@dataclass(frozen=True)
class StageAResult:
    """The episode's Stage A output: one attribution per failed scenario, plus the
    case files Stage B reflects on and the instrument-health records written."""

    episode_id: int
    attributions: tuple[Attribution, ...]
    instrument_health_ids: tuple[int, ...]

    @property
    def idea_candidates(self) -> tuple[Attribution, ...]:
        """Attributions with a library family — Stage B's input (R9)."""
        return tuple(a for a in self.attributions if not a.is_instrument_health)


def failed_scenarios(store: Store, episode_id: int) -> list[str]:
    """SCEN ids that failed in the episode (the Stage A worklist), id-ordered."""
    return [
        r["id"]
        for r in store.conn.execute(
            "SELECT id FROM trace_scen WHERE episode_id = ? AND result = 'fail'"
            " ORDER BY id",
            (episode_id,),
        ).fetchall()
    ]


def write_instrument_health(store: Store, episode_id: int, attr: Attribution) -> int:
    """Persist an explorer/grader attribution as an instrument-health review_queue
    record (R9) — never an idea. Returns the row id."""
    payload = json.dumps(
        {
            "scen_id": attr.scen_id,
            "primary": attr.primary.to_dict(),
            "contributing": [c.to_dict() for c in attr.contributing],
            "case_file": attr.case_file.to_dict(),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO review_queue (episode_id, kind, payload_json, status,"
            " created_at) VALUES (?, ?, ?, 'open', ?)",
            (episode_id, INSTRUMENT_HEALTH_KIND, payload, _utcnow()),
        )
    return cur.lastrowid


def run_stage_a(
    store: Store,
    episode_id: int,
    *,
    repro_runner: ReproRunner,
    coverage_provider: CoverageProvider,
    feat_probe_categories: Mapping[str, str] | None = None,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "sonnet",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> StageAResult:
    """Attribute every failed scenario in the episode, sinking explorer/grader
    attributions to instrument-health records and returning the rest as Stage B's
    idea candidates."""
    attributions: list[Attribution] = []
    health_ids: list[int] = []
    for scen_id in failed_scenarios(store, episode_id):
        attr = attribute_failed_scenario(
            store,
            scen_id,
            repro_runner=repro_runner,
            coverage_provider=coverage_provider,
            feat_probe_categories=feat_probe_categories,
            judge_fn=judge_fn,
            judge_model=judge_model,
            judge_max_retries=judge_max_retries,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        )
        attributions.append(attr)
        if attr.is_instrument_health:
            health_ids.append(write_instrument_health(store, episode_id, attr))
    return StageAResult(
        episode_id=episode_id,
        attributions=tuple(attributions),
        instrument_health_ids=tuple(health_ids),
    )
