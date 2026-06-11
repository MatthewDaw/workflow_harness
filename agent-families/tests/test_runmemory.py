"""Run-scoped working memory tests (plan-004 U3, R5/R6).

Fully offline: the induction judge call and the ``add_idea`` gauntlet are driven
through their injected seams with scripted fakes (the U5 ``judge_fn`` precedent),
the store and vector index are exercised directly, and no embedding model, real
judge, or subprocess ever runs — the suite passes with zero quota and no
``claude`` on PATH.

## Conformance

Each plan-004 U3 test scenario maps to a behavioral test here:

- induction fires on verifier-pass ONLY ->
  ``test_induction_fires_on_verifier_pass_only`` (the failed path NEVER calls the
  judge), ``test_induction_writes_a_live_workflow_on_pass``,
  ``test_induction_no_lesson_writes_no_workflow``.
- workflow injected into a later ticket's ledger ABOVE library skills under ONE
  budget -> ``test_injection_ranks_workflows_above_library_under_one_budget``,
  ``test_injection_whole_workflows_only_overflow_dropped``.
- cost counted against the increment ceiling ->
  ``test_induction_cost_charged_to_increment``.
- settlement kills the table -> ``test_settlement_kills_run_memory``.
- nomination excludes a workflow whose source ticket is implicated in a failed
  SCEN (the Phase-2 UAT-divergence tag consumed) ->
  ``test_nomination_excludes_uat_divergent_workflow``,
  ``test_nomination_excludes_unaccepted_run``,
  ``test_implicated_tickets_walks_the_failed_scen_chain``.
- survivor submits through ``add_idea`` with a batch tag ->
  ``test_survivor_submits_through_add_idea_with_batch_tag``.

Verification (plan-004 U3): a two-ticket fake increment shows ticket 2's prompt
containing ticket 1's workflow ->
``test_two_ticket_increment_ticket2_prompt_contains_ticket1_workflow``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent_families.library.retrieval import (
    RetrievalParams,
    count_tokens,
    retrieve,
)
from agent_families.pipeline import runmemory as rm
from agent_families.pipeline.ticket_loop import build_worker_prompt
from agent_families.store import Store
from agent_families.vecindex import VecIndex

DIM = 4
QUERY = [1.0, 0.0, 0.0, 0.0]
NEAR = [1.0, 0.0, 0.0, 0.0]


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    try:
        yield s
    finally:
        s.close()


def _raising_judge(*args, **kwargs):
    raise AssertionError(
        "the induction judge seam must not be consulted unless the verifier passed"
    )


def _judge_returning(output, *, cost=0.01):
    def _judge(prompt, schema, model, **kwargs):
        return SimpleNamespace(output=output, cost_usd=cost)

    return _judge


INDUCE = {
    "induce": True,
    "precondition": "adding a model field",
    "action": "write the migration then the form",
    "expected_outcome": "the field persists and renders",
}
NO_LESSON = {
    "induce": False,
    "precondition": "",
    "action": "",
    "expected_outcome": "",
}

TICKET = {
    "id": "TKT-1",
    "title": "Add tags",
    "description": "Let users tag bookmarks",
    "acceptance_criteria": [{"id": "AC-1", "text": "a tag persists"}],
}


# --- trace-fixture builders (the SCEN -> ... -> TKT chain) --------------------


def _feat(store, fid="FEAT-1"):
    store.conn.execute(
        "INSERT INTO trace_feat (id, evidence_ref, target, status)"
        " VALUES (?, 'ref', 'linkding', 'confirmed')",
        (fid,),
    )


def _mention(store, msg="MSG-1", feat="FEAT-1"):
    store.conn.execute(
        "INSERT INTO trace_msg (id, content) VALUES (?, 'asked')", (msg,)
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)",
        (msg, feat),
    )


def _req(store, rid="REQ-1", msg="MSG-1"):
    store.conn.execute(
        "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)", (rid, msg)
    )


def _tkt(store, tid, *, status="done", inc="INC-1", covers=None):
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status) VALUES (?, ?, ?)",
        (tid, inc, status),
    )
    if covers is not None:
        store.conn.execute(
            "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)",
            (tid, covers),
        )


def _scen(store, episode_id, *, feat="FEAT-1", sid="SCEN-1", result="fail"):
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, result, evidence, episode_id,"
        " snapshot_id) VALUES (?, ?, ?, 'judged_different', ?, 0)",
        (sid, feat, result, episode_id),
    )


# --- R5: induction -----------------------------------------------------------


def test_induction_fires_on_verifier_pass_only(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    # The verifier did NOT pass: the judge seam must never be consulted, no row.
    result = rm.induce_workflow(
        store,
        episode_id=ep,
        ticket=TICKET,
        verifier_passed=False,
        judge_fn=_raising_judge,
    )
    assert result.induced is False
    assert result.workflow_id is None
    assert result.cost_usd is None
    assert store.live_workflows(ep) == []


def test_induction_writes_a_live_workflow_on_pass(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    result = rm.induce_workflow(
        store,
        episode_id=ep,
        ticket=TICKET,
        verifier_passed=True,
        judge_fn=_judge_returning(INDUCE),
    )
    assert result.induced is True
    rows = store.live_workflows(ep)
    assert len(rows) == 1
    assert rows[0]["id"] == result.workflow_id
    assert rows[0]["status"] == "live"
    assert rows[0]["precondition"] == INDUCE["precondition"]
    assert rows[0]["action"] == INDUCE["action"]
    assert rows[0]["expected_outcome"] == INDUCE["expected_outcome"]
    assert rows[0]["source_ticket_id"] == "TKT-1"


def test_induction_no_lesson_writes_no_workflow(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    result = rm.induce_workflow(
        store,
        episode_id=ep,
        ticket=TICKET,
        verifier_passed=True,
        judge_fn=_judge_returning(NO_LESSON),
    )
    assert result.induced is False
    assert result.workflow_id is None
    assert store.live_workflows(ep) == []


def test_induction_cost_charged_to_increment(store):
    from agent_families.pipeline.tripwires import run_totals

    ep = store.create_episode("linkding", "sha256:x", 0)
    run = store.create_run("spec", 0, episode_id=ep, increment_index=1)
    _tkt(store, "TKT-1", status="done")
    result = rm.induce_workflow(
        store,
        episode_id=ep,
        ticket=TICKET,
        verifier_passed=True,
        run_id=run,
        judge_fn=_judge_returning(INDUCE, cost=0.07),
    )
    # The induction cost is a trace_span charged to the increment's run, so it
    # aggregates into the run total (and thence runs.total_cost_usd at
    # settlement -> the episode cost ceiling, R5).
    assert result.cost_span_id is not None
    totals = run_totals(store, run)
    assert totals.cost_usd == pytest.approx(0.07)


def test_induction_without_run_charges_nothing(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    result = rm.induce_workflow(
        store,
        episode_id=ep,
        ticket=TICKET,
        verifier_passed=True,
        judge_fn=_judge_returning(INDUCE, cost=0.07),
    )
    # No increment to charge: still a workflow, but no cost span minted.
    assert result.induced is True
    assert result.cost_span_id is None


# --- R6: injection above library skills, one budget --------------------------


def _seed_library_skill(store, vec, name, vector):
    """A one-member active library skill (the U5 registration write shape)."""
    family_id = store.create_family("worker")
    agent_id = store.create_agent(family_id, "generalist")
    skill_id = store.create_skill(agent_id, name, "A library skill.")
    with store.transaction():
        insight_id = store.insert_insight(
            precondition="lib precondition",
            action="lib action",
            expected_outcome="lib outcome",
            content_hash=f"hash-{name}",
            status="quarantined",
        )
        vec.insert(insight_id, vector)
        store.append_member(skill_id, insight_id)
    with store.queue_operation("promote", "seed") as snap:
        store.set_status(insight_id, "active", snap)
    return family_id, skill_id


def test_injection_ranks_workflows_above_library_under_one_budget(store):
    vec = VecIndex(store, DIM)
    vec.migrate()
    family_id, skill_id = _seed_library_skill(store, vec, "elicitation", NEAR)
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    rm.induce_workflow(
        store, episode_id=ep, ticket=TICKET, verifier_passed=True,
        judge_fn=_judge_returning(INDUCE),
    )

    def retrieve_library(budget):
        return retrieve(
            store, query_vector=QUERY, family_id=family_id,
            params=RetrievalParams(budget_tokens=budget, relevance_floor=0.5),
        )

    budget = 10_000
    result = rm.compose_injection(
        store, episode_id=ep, budget_tokens=budget,
        retrieve_library=retrieve_library,
    )
    # Both the workflow and the library skill are present...
    assert "Workflow W" in result.section
    assert "Skill: elicitation" in result.section
    assert result.library is not None and skill_id in result.library.skills
    # ...and the workflow is ranked ABOVE the library skill (fresher).
    assert result.section.index("Workflow W") < result.section.index(
        "Skill: elicitation"
    )
    # One budget: total injected tokens never exceed it.
    assert result.injected_token_count <= budget


def test_injection_whole_workflows_only_overflow_dropped(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    _tkt(store, "TKT-2", status="done")
    for tid in ("TKT-1", "TKT-2"):
        rm.induce_workflow(
            store, episode_id=ep, ticket={**TICKET, "id": tid},
            verifier_passed=True, judge_fn=_judge_returning(INDUCE),
        )
    rows = store.live_workflows(ep)
    one_block_tokens = count_tokens(rm.render_workflow_block(rows[0]))
    # A budget that fits exactly one workflow block (header + one block).
    budget = count_tokens(rm._RUN_MEMORY_HEADER) + one_block_tokens
    result = rm.compose_injection(store, episode_id=ep, budget_tokens=budget)

    assert len(result.workflow_ids) == 1
    # The dropped workflow is logged whole (rank + size), never truncated.
    assert len(result.workflow_drops) == 1
    drop = result.workflow_drops[0]
    assert drop.reason == rm.DROP_BUDGET
    assert drop.rank > 0 and drop.token_count > 0
    # The injected block appears intact.
    assert rm.render_workflow_block(rows[0]) in result.section


def test_compose_rejects_nonpositive_budget(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    with pytest.raises(rm.RunMemoryError, match="budget_tokens"):
        rm.compose_injection(store, episode_id=ep, budget_tokens=0)


def test_compose_with_no_workflows_and_no_library_is_empty(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    result = rm.compose_injection(store, episode_id=ep, budget_tokens=1000)
    assert result.workflow_ids == ()
    assert result.section == ""
    assert result.injected_token_count == 0


# --- Verification: two-ticket increment, ticket 2 sees ticket 1's workflow ----


def test_two_ticket_increment_ticket2_prompt_contains_ticket1_workflow(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    # Ticket 1 passes verification -> its workflow is induced into run memory.
    rm.induce_workflow(
        store, episode_id=ep,
        ticket={**TICKET, "id": "TKT-1", "title": "Add tags"},
        verifier_passed=True, judge_fn=_judge_returning(INDUCE),
    )
    # Ticket 2's worker prompt is assembled with the run-memory injection.
    injection = rm.compose_injection(store, episode_id=ep, budget_tokens=10_000)
    ticket2 = {
        "id": "TKT-2", "title": "Filter by tag",
        "description": "Filter the list",
        "files": ["src/filter.ts"],
        "acceptance_criteria": [{"id": "AC-2", "text": "filtering works"}],
    }
    prompt = build_worker_prompt(
        ticket2, "(no prior iterations)", injected_skills=injection.section
    )
    # The loop is closed within the episode: ticket 2 sees ticket 1's workflow.
    assert "Workflow W" in prompt
    assert INDUCE["action"] in prompt


# --- R6: nomination filter (UAT-accepted AND unimplicated) -------------------


def _accepted_run(store, ep, index):
    run = store.create_run("spec", 0, episode_id=ep, increment_index=index)
    store.set_run_acceptance(run, "accepted")
    return run


def test_implicated_tickets_walks_the_failed_scen_chain(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store, "FEAT-1")
    _mention(store, "MSG-1", "FEAT-1")
    _req(store, "REQ-1", "MSG-1")
    _tkt(store, "TKT-bad", status="done", covers="REQ-1")
    _tkt(store, "TKT-good", status="done")  # covers nothing failed
    _scen(store, ep, feat="FEAT-1", sid="SCEN-1", result="fail")
    assert rm.implicated_tickets(store, ep) == {"TKT-bad"}


def test_nomination_excludes_uat_divergent_workflow(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    run = _accepted_run(store, ep, 1)
    # The chain: TKT-bad covers a REQ whose FEAT's scenario FAILED, yet its run
    # was UAT-accepted -> the UAT-divergence case the filter must remove.
    _feat(store, "FEAT-1")
    _mention(store, "MSG-1", "FEAT-1")
    _req(store, "REQ-1", "MSG-1")
    _tkt(store, "TKT-bad", status="done", covers="REQ-1")
    _tkt(store, "TKT-good", status="done")
    _scen(store, ep, feat="FEAT-1", sid="SCEN-1", result="fail")
    with store.transaction():
        good = store.insert_workflow(
            ep, precondition="p", action="good action", expected_outcome="o",
            run_id=run, source_ticket_id="TKT-good",
        )
        store.insert_workflow(
            ep, precondition="p", action="bad action", expected_outcome="o",
            run_id=run, source_ticket_id="TKT-bad",
        )
    survivors = rm.nominate_workflows(store, ep)
    assert [w["id"] for w in survivors] == [good]
    assert survivors[0]["source_ticket_id"] == "TKT-good"


def test_nomination_excludes_unaccepted_run(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    rejected = store.create_run("spec", 0, episode_id=ep, increment_index=1)
    store.set_run_acceptance(rejected, "rejected")
    _tkt(store, "TKT-x", status="done")
    with store.transaction():
        store.insert_workflow(
            ep, precondition="p", action="a", expected_outcome="o",
            run_id=rejected, source_ticket_id="TKT-x",
        )
    # The run was UAT-rejected -> not nominated even though unimplicated.
    assert rm.nominate_workflows(store, ep) == []


def test_nomination_requires_a_source_ticket_and_run(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    with store.transaction():
        store.insert_workflow(
            ep, precondition="p", action="a", expected_outcome="o",
        )  # no run, no source ticket
    assert rm.nominate_workflows(store, ep) == []


# --- R6: submission through add_idea, batch-tagged ---------------------------


def test_survivor_submits_through_add_idea_with_batch_tag(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    run = _accepted_run(store, ep, 1)
    _tkt(store, "TKT-good", status="done")
    with store.transaction():
        wid = store.insert_workflow(
            ep, precondition="p", action="good action", expected_outcome="o",
            run_id=run, source_ticket_id="TKT-good",
        )

    calls = []

    def fake_add_idea(store_, vec_, embedder_, config_, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(code="registered", batch_id=99)

    submissions = rm.submit_nominations(
        store, None, None, None, ep,
        batch_label="b-ep1", add_idea_fn=fake_add_idea,
    )
    assert len(submissions) == 1
    assert submissions[0].workflow_id == wid
    # The survivor flowed through add_idea with its fields AND the batch tag.
    assert len(calls) == 1
    assert calls[0]["batch_label"] == "b-ep1"
    assert calls[0]["action"] == "good action"


def test_submission_skips_excluded_workflows(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    run = _accepted_run(store, ep, 1)
    _feat(store, "FEAT-1")
    _mention(store, "MSG-1", "FEAT-1")
    _req(store, "REQ-1", "MSG-1")
    _tkt(store, "TKT-bad", status="done", covers="REQ-1")
    _scen(store, ep, feat="FEAT-1", sid="SCEN-1", result="fail")
    with store.transaction():
        store.insert_workflow(
            ep, precondition="p", action="bad action", expected_outcome="o",
            run_id=run, source_ticket_id="TKT-bad",
        )

    calls = []

    def fake_add_idea(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(code="registered", batch_id=1)

    submissions = rm.submit_nominations(
        store, None, None, None, ep,
        batch_label="b-ep1", add_idea_fn=fake_add_idea,
    )
    # The only workflow is UAT-divergent -> nothing submitted.
    assert submissions == []
    assert calls == []


# --- R5: settlement kills the table ------------------------------------------


def test_settlement_kills_run_memory(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _tkt(store, "TKT-1", status="done")
    rm.induce_workflow(
        store, episode_id=ep, ticket=TICKET, verifier_passed=True,
        judge_fn=_judge_returning(INDUCE),
    )
    assert len(store.live_workflows(ep)) == 1
    retired = rm.settle_run_memory(store, ep)
    assert retired == 1
    # The within-episode memory is gone — nothing leaks forward.
    assert store.live_workflows(ep) == []
    dead = store.conn.execute(
        "SELECT status FROM workflows WHERE episode_id = ?", (ep,)
    ).fetchall()
    assert [r["status"] for r in dead] == ["dead"]
