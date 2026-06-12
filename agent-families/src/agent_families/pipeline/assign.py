"""The assign stage (plan-010 U1, R1–R3): per-job context assembly.

DESIGN §3 makes the runtime a fixed ``plan → assign → work → verify`` pipeline.
``assign`` is the only "routing" that remains, and it is **not** a router: it
gives each job its *context* — a whole-store, insight-level retrieval injection
and its file territory — without ever selecting a persona or consulting a
per-ticket family router. Knowledge specialization lives in the **index**
(retrieval), never here (R3).

The stage is a **thin assembly** over two pieces that already exist:

- ``library.retrieval`` — plan-009's whole-store insight-level
  :func:`~agent_families.library.retrieval.retrieve`, the per-stage query text
  builders (:func:`~agent_families.library.retrieval.planner_query` /
  ``worker_query`` / ``verifier_query``) and
  :func:`~agent_families.library.retrieval.render_injection_section`. The query
  text is embedded by a caller-supplied ``embed_query`` callable (the
  ``search_query:`` seam — :meth:`EmbeddingService.embed_query`), so this module
  stays offline and trivially testable. assign passes **no** ``family_id`` /
  ``working_agent_id`` to ``retrieve`` — reachability is the whole store
  (ownership ≠ reachability, R3 §4).
- :func:`agent_families.pipeline.planning.file_ownership_conflicts` — the
  existing file-ownership lint (planning.py:469-488), reused verbatim for the
  file-territory partition. No net-new allocation logic.

There is **no router and no persona machinery** in this module: it imports
neither the demoted family router's ``route`` entry point nor any
boundary-ticket / persona-selection seam, so mocking those to raise cannot
perturb ``assign`` (``R3``, ``test_no_router_or_persona_invoked``).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from agent_families.library.retrieval import (
    RetrievalParams,
    RetrievalResult,
    planner_query,
    render_injection_section,
    retrieve,
    verifier_query,
    worker_query,
)
from agent_families.pipeline.planning import file_ownership_conflicts
from agent_families.rendering import Renderer
from agent_families.store import Store

# The three stages whose existing inert ``injected_skills=`` seam assign fills
# (ticket_loop.py:417 worker, ticket_loop.py:521 verifier, planning.py:1232
# planner). These are *stages*, not personas or agents.
STAGE_PLANNER = "planner"
STAGE_WORKER = "worker"
STAGE_VERIFIER = "verifier"
STAGES = (STAGE_PLANNER, STAGE_WORKER, STAGE_VERIFIER)


class AssignError(Exception):
    """assign misuse with an actionable message."""


@dataclass(frozen=True)
class AssignedContext:
    """The per-job context assign hands a stage.

    ``injected_skills`` is the rendered, read-only retrieval section ready to
    drop into ``build_{worker,verifier,planner}_prompt(injected_skills=)`` — the
    empty string when retrieval returned nothing (the seam stays byte-identical
    to today, R2). ``file_territory`` is the file-ownership partition exactly as
    :func:`planning.file_ownership_conflicts` computes it (no net-new logic).
    ``retrieval`` is the full audit trail behind ``injected_skills`` (drops,
    candidates, pool) for telemetry; ``None`` when no retrieval was run.
    """

    injected_skills: str
    file_territory: dict[str, tuple[str, ...]] = field(default_factory=dict)
    retrieval: RetrievalResult | None = None


def _stage_query_text(
    stage: str,
    *,
    ticket: Mapping | None,
    typed_failures: Sequence[str],
    increment_request_msgs: Sequence[str],
    qa_transcript: Sequence[str],
) -> str:
    """Build the stage-appropriate query TEXT from the existing R2 builders.

    Embedding (the ``search_query:`` prefix) is the caller's ``embed_query`` —
    these builders stay string-only so the embed seam owns the prefix.
    """
    if stage == STAGE_WORKER:
        if ticket is None:
            raise AssignError("worker-stage assign requires the ticket document")
        return worker_query(dict(ticket))
    if stage == STAGE_VERIFIER:
        if ticket is None:
            raise AssignError("verifier-stage assign requires the ticket document")
        return verifier_query(dict(ticket), list(typed_failures))
    if stage == STAGE_PLANNER:
        return planner_query(list(increment_request_msgs), list(qa_transcript))
    raise AssignError(f"unknown stage '{stage}' (expected one of {STAGES})")


def assign(
    *,
    store: Store,
    stage: str,
    embed_query: Callable[[str], list[float]],
    params: RetrievalParams,
    ticket: Mapping | None = None,
    ticket_files: Mapping[str, Sequence[str]] | None = None,
    typed_failures: Sequence[str] = (),
    increment_request_msgs: Sequence[str] = (),
    qa_transcript: Sequence[str] = (),
    mode: str = "training",
    batch_id: int | None = None,
    snapshot_id: int | None = None,
    renderer: Renderer | None = None,
) -> AssignedContext:
    """Assemble a stage's per-job context: whole-store retrieval + file territory.

    The retrieval half:

    1. build the stage query text (R2 builders), embed it with ``embed_query``
       (the ``search_query:`` seam);
    2. :func:`retrieve` over the **whole store** — no family/agent scope (R3 §4);
    3. :func:`render_injection_section` → ``injected_skills`` (``""`` when nothing
       cleared the budget/floor, keeping the seam byte-identical to today).

    The territory half: :func:`planning.file_ownership_conflicts` over
    ``ticket_files`` (the same partition the plan lint and rehearsal scheduler
    read) — ``{}`` when no ticket set was supplied. No router, no persona.
    """
    if stage not in STAGES:
        raise AssignError(f"unknown stage '{stage}' (expected one of {STAGES})")

    query_text = _stage_query_text(
        stage,
        ticket=ticket,
        typed_failures=typed_failures,
        increment_request_msgs=increment_request_msgs,
        qa_transcript=qa_transcript,
    )
    query_vector = embed_query(query_text)
    result = retrieve(
        store,
        query_vector=query_vector,
        params=params,
        mode=mode,
        batch_id=batch_id,
        snapshot_id=snapshot_id,
        renderer=renderer,
        # R3 §4: NO family/agent scope — reachability is the whole store.
    )
    injected_skills = render_injection_section(result)

    file_territory = (
        file_ownership_conflicts(ticket_files) if ticket_files is not None else {}
    )

    return AssignedContext(
        injected_skills=injected_skills,
        file_territory=file_territory,
        retrieval=result,
    )
