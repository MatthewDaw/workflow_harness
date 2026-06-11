"""Run-scoped working memory (plan-004 U3, R5/R6).

Within-episode learning with a clean nomination path. A worker that *passes*
verification has demonstrated a small reusable procedure; the episode captures it
as a **workflow** — an episode-scoped, typed ``{precondition, action,
expected_outcome}`` row that dies at settlement (:func:`settle_run_memory`) so the
within-episode memory never leaks across episodes (the two-tier memory split,
DESIGN §13).

R5 — **induction** is one single-shot structured judge call (the cheap tier per
§15) fired ONLY on verifier-pass (:func:`induce_workflow`). It produces a ``live``
workflow row and is **counted against the increment cost ceiling**: the call's
cost is recorded as a ``trace_span`` charged to the increment's run, so it rolls
into ``runs.total_cost_usd`` at run settlement exactly like every other session
cost (Phase 1 R16) — the episode cost ceiling (003 R4) therefore sees it.

R6 — **injection**: live workflows enter later workers' ledgers under the SAME
retrieval budget (R3), **ranked above library skills** because they are fresher
(:func:`compose_injection`). Workflows claim the budget first (whole blocks, never
truncated mid-workflow; overflow drop-logged); the library retrieval then runs on
the *remainder* — one budget, workflows on top. The library half is supplied by a
caller thunk (``retrieve_library``) so this module never re-implements U2's
retrieval; the run-assembly wiring (U8) binds the query vector / family / mode.

R6 — **nomination** is this plan's reflector responsibility end-to-end. At
settlement only workflows whose source ticket is **UAT-accepted AND unimplicated
in any failed SCEN chain** survive (:func:`nominate_workflows`): the Phase-2
UAT-divergence case (a ticket the explorer accepted at UAT but whose scenario then
failed) is exactly what the implication filter removes. Survivors submit through
the normal ``add_idea`` gauntlet, batch-tagged (:func:`submit_nominations`) — no
bypass, the same default-deny path every idea walks.

The judge and ``add_idea`` are injected seams (defaulting to the module
implementations), so the offline suite drives induction and submission with
scripted fakes — zero quota, no ``claude`` on PATH (the U5 ``judge_fn`` precedent).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from agent_families.library.retrieval import (
    RetrievalResult,
    count_tokens,
    render_injection_section,
)

if TYPE_CHECKING:
    import sqlite3

    from agent_families.config import Config
    from agent_families.embedding import EmbeddingService
    from agent_families.store import Store
    from agent_families.vecindex import VecIndex

# Drop reason for a workflow that did not fit the budget (R3 analog: whole
# workflows only — never truncate mid-workflow; every drop logged).
DROP_BUDGET = "budget"


class RunMemoryError(Exception):
    """Run-memory misuse or invariant breach with an actionable message."""


# --- induction (R5) ----------------------------------------------------------
# A single-shot structured judge call, fired on verifier-pass, that extracts ONE
# reusable workflow from the just-passed ticket (or declines). Stays within the
# judge validator's schema subset (type/enum/required/properties/
# additionalProperties), like the U5 micro-judgment schemas.

INDUCTION_SCHEMA = {
    "type": "object",
    "properties": {
        "induce": {"type": "boolean"},
        "precondition": {"type": "string"},
        "action": {"type": "string"},
        "expected_outcome": {"type": "string"},
    },
    "required": ["induce", "precondition", "action", "expected_outcome"],
    "additionalProperties": False,
}

# A judge seam for the induction call; defaults to the module ``run_judge`` so a
# test can inject a scripted fake (the U5 ``judge_fn`` precedent). Returns an
# object exposing ``.output`` (the structured dict) and ``.cost_usd``.
JudgeFn = Callable[..., object]


@dataclass(frozen=True)
class InductionResult:
    """The outcome of one induction attempt."""

    induced: bool
    workflow_id: int | None
    cost_usd: float | None
    cost_span_id: str | None


def build_induction_prompt(ticket: dict) -> str:
    """The cheap-tier induction prompt: extract ONE reusable workflow from a
    ticket that just passed verification, or decline with ``induce=false``."""
    acs = "\n".join(
        f"- {ac.get('id', '')}: {ac.get('text', '')}".rstrip(": ")
        for ac in ticket.get("acceptance_criteria", ())
    )
    return (
        "You are the reflector's run-memory inducer. A worker just IMPLEMENTED a"
        " ticket and it PASSED verification inside this episode. Capture at most"
        " ONE small, reusable procedure another ticket in the same episode could"
        " apply — a precondition (when it applies), an action (what to do), and"
        " the expected outcome (how you know it worked).\n\n"
        f"Ticket {ticket.get('id', '')}: {ticket.get('title', '')}\n"
        f"{ticket.get('description', '')}\n"
        f"Acceptance criteria:\n{acs}\n\n"
        "Only induce a workflow if there is a genuinely transferable lesson; a"
        " ticket with nothing reusable is a legal no-induction outcome. Return"
        ' structured output only: {"induce": true|false, "precondition": "...",'
        ' "action": "...", "expected_outcome": "..."}. When induce is false the'
        " three text fields may be empty strings."
    )


def induce_workflow(
    store: Store,
    *,
    episode_id: int,
    ticket: dict,
    verifier_passed: bool,
    run_id: int | None = None,
    source_ticket_id: str | None = None,
    judge_fn: JudgeFn | None = None,
    judge_model: str = "haiku",
    judge_max_retries: int = 0,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
) -> InductionResult:
    """Induce a run-memory workflow from a just-passed ticket (R5).

    Fires the single-shot judge call ONLY when ``verifier_passed`` — a failed or
    unverified ticket teaches no run-memory workflow. The call's cost (when the
    seam reports one and ``run_id`` is given) is charged to the increment's run as
    a ``trace_span`` so it counts against the episode cost ceiling. Returns the
    :class:`InductionResult`; a ``induce=false`` verdict still records the cost
    (the call was made) but writes no workflow row.
    """
    if not verifier_passed:
        # Induction fires on verifier-pass only — no call, no cost, no row.
        return InductionResult(
            induced=False, workflow_id=None, cost_usd=None, cost_span_id=None
        )

    from agent_families.judge import run_judge

    judge = judge_fn if judge_fn is not None else run_judge
    result = judge(
        build_induction_prompt(ticket),
        INDUCTION_SCHEMA,
        judge_model,
        max_retries=judge_max_retries,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    output = result.output
    cost_usd = getattr(result, "cost_usd", None)

    cost_span_id = _charge_increment(store, run_id, cost_usd)

    if not output["induce"]:
        return InductionResult(
            induced=False,
            workflow_id=None,
            cost_usd=cost_usd,
            cost_span_id=cost_span_id,
        )

    with store.transaction():
        workflow_id = store.insert_workflow(
            episode_id,
            precondition=output["precondition"],
            action=output["action"],
            expected_outcome=output["expected_outcome"],
            run_id=run_id,
            source_ticket_id=source_ticket_id or ticket.get("id"),
        )
    return InductionResult(
        induced=True,
        workflow_id=workflow_id,
        cost_usd=cost_usd,
        cost_span_id=cost_span_id,
    )


def _charge_increment(
    store: Store, run_id: int | None, cost_usd: float | None
) -> str | None:
    """Record the induction call's cost as a ``trace_span`` on the increment's
    run, so it aggregates into ``runs.total_cost_usd`` at settlement (R5 / Phase 1
    R16). No run or no reported cost → nothing to charge."""
    if run_id is None or cost_usd is None:
        return None
    span_id = f"SPAN-runmem-{uuid.uuid4().hex}"
    with store.transaction():
        store.insert_span(
            span_id, run_id=run_id, family="run_memory", agent="inducer"
        )
        store.finalize_span(span_id, "completed", cost_usd=cost_usd, num_turns=1)
    return span_id


# --- injection (R6) ----------------------------------------------------------


def render_workflow_block(row) -> str:
    """One live workflow as a self-contained, read-only prompt block."""
    return (
        f"## Workflow W{row['id']}\n"
        f"- Precondition: {row['precondition']}\n"
        f"- Action: {row['action']}\n"
        f"- Expected outcome: {row['expected_outcome']}"
    )


_RUN_MEMORY_HEADER = (
    "Run memory (workflows learned earlier in THIS episode — fresher than the"
    " library; apply before the library skills below):"
)


def render_run_memory_section(workflow_ids_blocks: list[str]) -> str:
    """Wrap rendered workflow blocks as a labelled section (empty when none)."""
    if not workflow_ids_blocks:
        return ""
    return _RUN_MEMORY_HEADER + "\n" + "\n".join(workflow_ids_blocks)


@dataclass(frozen=True)
class WorkflowDrop:
    """A live workflow that did not fit the budget — logged with rank and size."""

    workflow_id: int
    rank: int
    token_count: int
    reason: str  # DROP_BUDGET


@dataclass(frozen=True)
class InjectionResult:
    """The combined run-memory + library injection: workflows ranked on top."""

    workflow_ids: tuple[int, ...]
    workflow_section: str
    library: RetrievalResult | None
    section: str  # combined text, ready for build_worker_prompt(injected_skills=)
    injected_token_count: int
    budget_tokens: int
    workflow_drops: tuple[WorkflowDrop, ...]


def compose_injection(
    store: Store,
    *,
    episode_id: int,
    budget_tokens: int,
    retrieve_library: Callable[[int], RetrievalResult | None] | None = None,
) -> InjectionResult:
    """Compose the worker injection: live workflows first (R6 — ranked above
    library skills, fresher), the library on the remaining budget (R3 — one
    budget). ``retrieve_library`` is a thunk taking the leftover token budget and
    returning U2's :class:`RetrievalResult` (or ``None``); it is the seam that
    binds the query vector / family / mode without this module re-implementing
    retrieval. Whole workflows only — an over-budget workflow is dropped (logged),
    never truncated mid-block.
    """
    if budget_tokens <= 0:
        raise RunMemoryError(
            f"budget_tokens must be a positive token budget, got {budget_tokens}"
        )

    workflows = store.live_workflows(episode_id)
    included_ids: list[int] = []
    included_blocks: list[str] = []
    drops: list[WorkflowDrop] = []
    used = 0
    rank = 0
    for row in workflows:
        rank += 1
        block = render_workflow_block(row)
        tokens = count_tokens(block)
        if used + tokens <= budget_tokens:
            included_ids.append(row["id"])
            included_blocks.append(block)
            used += tokens
        else:
            drops.append(WorkflowDrop(row["id"], rank, tokens, DROP_BUDGET))

    workflow_section = render_run_memory_section(included_blocks)

    remaining = budget_tokens - used
    library: RetrievalResult | None = None
    if retrieve_library is not None and remaining > 0:
        library = retrieve_library(remaining)

    library_section = (
        render_injection_section(library) if library is not None else ""
    )
    parts = [p for p in (workflow_section, library_section) if p]
    section = "\n\n".join(parts)
    injected = used + (library.injected_token_count if library is not None else 0)

    return InjectionResult(
        workflow_ids=tuple(included_ids),
        workflow_section=workflow_section,
        library=library,
        section=section,
        injected_token_count=injected,
        budget_tokens=budget_tokens,
        workflow_drops=tuple(drops),
    )


# --- nomination + submission (R6) --------------------------------------------


def implicated_tickets(store: Store, episode_id: int) -> set[str]:
    """Tickets implicated in ANY failed SCEN chain in the episode.

    The SCEN -> FEAT -> MSG -> REQ -> TKT(covers) join (the same edges
    ``af trace chain`` and Stage A walk): a ticket covering a requirement that
    traces back to a failed scenario's feature is implicated, so a workflow it
    sourced must not seed the success channel (R6)."""
    rows = store.conn.execute(
        "SELECT DISTINCT c.tkt_id AS tkt_id"
        " FROM trace_scen s"
        " JOIN trace_msg_mentions m ON m.feat_id = s.feat_id"
        " JOIN trace_req r ON r.source_msg_id = m.msg_id"
        " JOIN trace_tkt_covers c ON c.req_id = r.id"
        " WHERE s.episode_id = ? AND s.result = 'fail'",
        (episode_id,),
    ).fetchall()
    return {row["tkt_id"] for row in rows}


def nominate_workflows(store: Store, episode_id: int) -> list[sqlite3.Row]:
    """The settlement nomination filter (R6): live workflows whose source ticket
    is **UAT-accepted AND unimplicated** in any failed SCEN chain.

    UAT acceptance is read from the source ticket's run (003 R2: UAT judges the
    delivered subset — ``runs.acceptance == 'accepted'``). A workflow with no
    source ticket or no accepted run is never nominated; one whose source ticket
    is implicated in a failed scenario is excluded even if its run was accepted
    (the UAT-divergence case)."""
    blocked = implicated_tickets(store, episode_id)
    survivors: list[sqlite3.Row] = []
    for row in store.live_workflows(episode_id):
        source_ticket_id = row["source_ticket_id"]
        if source_ticket_id is None or row["run_id"] is None:
            continue
        if source_ticket_id in blocked:
            continue
        accepted = store.conn.execute(
            "SELECT acceptance FROM runs WHERE id = ?", (row["run_id"],)
        ).fetchone()
        if accepted is None or accepted["acceptance"] != "accepted":
            continue
        survivors.append(row)
    return survivors


# An ``add_idea`` seam (defaults to the module function) so the offline suite can
# assert nomination flow without the placement judge.
AddIdeaFn = Callable[..., object]


@dataclass(frozen=True)
class NominationSubmission:
    """One nominated workflow's submission outcome."""

    workflow_id: int
    source_ticket_id: str
    result: object  # the AddIdeaResult (or the injected seam's return)


def submit_nominations(
    store: Store,
    vec: VecIndex,
    embedder: EmbeddingService,
    config: Config,
    episode_id: int,
    *,
    batch_label: str,
    scope_tag: str | None = None,
    judge_mode: str | None = None,
    judge_fixtures_dir=None,
    add_idea_fn: AddIdeaFn | None = None,
) -> list[NominationSubmission]:
    """Submit every nominated workflow through the normal ``add_idea`` gauntlet,
    batch-tagged (R6). Survivors only — the nomination filter has already removed
    UAT-divergent and unaccepted workflows. The submission is the standard
    default-deny path (no bypass); ``add_idea_fn`` is the injectable seam."""
    from agent_families.pipeline import add_idea

    submit = add_idea_fn if add_idea_fn is not None else add_idea
    submissions: list[NominationSubmission] = []
    for row in nominate_workflows(store, episode_id):
        result = submit(
            store,
            vec,
            embedder,
            config,
            precondition=row["precondition"],
            action=row["action"],
            expected_outcome=row["expected_outcome"],
            batch_label=batch_label,
            scope_tag=scope_tag,
            judge_mode=judge_mode,
            judge_fixtures_dir=judge_fixtures_dir,
        )
        submissions.append(
            NominationSubmission(
                workflow_id=row["id"],
                source_ticket_id=row["source_ticket_id"],
                result=result,
            )
        )
    return submissions


# --- settlement --------------------------------------------------------------


def settle_run_memory(store: Store, episode_id: int) -> int:
    """Kill the episode's run memory at settlement (R5): every ``live`` workflow
    flips to ``dead`` so the within-episode memory never leaks forward. Returns
    the number of workflows retired."""
    with store.transaction():
        return store.settle_workflows(episode_id)
