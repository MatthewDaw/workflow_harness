"""plan-002 U5: the planning stage — MSG synthesis, planner contract, plan lints.

Fully offline: the planner rides the U3 scripted-agent fake (zero quota, no
``claude`` on PATH).

Scenario map (plan-002 U5 Test scenarios, 1:1):

- valid plan persists full traceability joins (REQ→MSG, TKT→REQ, AC→TKT
  queryable): ``test_valid_plan_persists_full_traceability_joins``
- cyclic DAG output → typed lint failure → corrected on scripted iteration 2:
  ``test_cyclic_dag_typed_failure_then_corrected_on_iteration_2``
- empty REQ extraction → lint failure not crash:
  ``test_empty_req_extraction_is_lint_failure_not_crash``
- zero-ticket plan → coverage lint fires:
  ``test_zero_ticket_plan_fires_coverage_lint``
- overlapping file ownership → warn recorded, plan accepted:
  ``test_overlapping_file_ownership_warn_recorded_plan_accepted``
- assumptions[] persisted and surfaced in run report:
  ``test_assumptions_persisted_and_surfaced_in_run_report``
- cap exhaustion → plan_failed, nothing downstream runs:
  ``test_cap_exhaustion_plan_failed_nothing_downstream``

Verification clause — lint outcomes enumerated 1:1 with the lint list (R10):
``req_set`` / ``req_coverage`` / ``dag_acyclic`` / ``ac_links`` /
``size_budget`` / ``file_ownership`` each have dedicated ``test_lint_*`` tests,
and ``test_plan_lints_enumerate_the_r10_list`` pins the list itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_families.pipeline.planning import (
    LINT_AC_LINKS,
    LINT_DAG_ACYCLIC,
    LINT_FILE_OWNERSHIP,
    LINT_REQ_COVERAGE,
    LINT_REQ_PROVENANCE,
    LINT_REQ_SET,
    LINT_SIZE_BUDGET,
    PLAN_LINTS,
    PLANNER_OUTPUT_SCHEMA,
    PLANNER_PROMPT_SET_VERSION,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    AssumptionGateFinding,
    PlanFailed,
    PlanningError,
    assumption_gate,
    build_lint_feedback_prompt,
    build_planner_prompt,
    check_plan_assumptions,
    chunk_spec,
    k_effective,
    lint_plan,
    lint_req_provenance,
    normalize_assumption,
    plan_report,
    rank_questions,
    run_planning,
    synthesize_messages,
)
from agent_families.pipeline.sessions import SessionSchemaViolation, planner_profile
from agent_families.store import Store

SPEC = """\
# Toy bookmarks app

Users can log in with a username.

Users can view a dashboard listing their bookmarks.
"""

SIZE_BUDGET = 4


# --- helpers -------------------------------------------------------------------


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def make_run(store: Store, spec_ref: str = "specs/toy-spec.md") -> int:
    return store.create_run(spec_ref, store.current_snapshot_id())


def msg(run_id: int, index: int) -> str:
    return f"MSG-r{run_id}-p{index:03d}"


def valid_plan(run_id: int) -> dict:
    """Two REQs, two tickets with a dependency edge, one AC each — lints clean."""
    return {
        "requirements": [
            {
                "id": "REQ-1",
                "text": "login with a username",
                "source_msg": msg(run_id, 1),
            },
            {
                "id": "REQ-2",
                "text": "dashboard lists bookmarks",
                "source_msg": msg(run_id, 2),
            },
        ],
        "tickets": [
            {
                "id": "TKT-1",
                "title": "login",
                "description": "build the login flow",
                "covers": ["REQ-1"],
                "depends_on": [],
                "files": ["src/login.ts"],
                "acceptance_criteria": [
                    {"id": "AC-1", "text": "a user can log in", "req": "REQ-1"}
                ],
            },
            {
                "id": "TKT-2",
                "title": "dashboard",
                "description": "build the dashboard",
                "covers": ["REQ-2"],
                "depends_on": ["TKT-1"],
                "files": ["src/dashboard.ts"],
                "acceptance_criteria": [
                    {
                        "id": "AC-2",
                        "text": "the dashboard lists bookmarks",
                        "req": "REQ-2",
                    }
                ],
            },
        ],
        "assumptions": ["username-only auth; no password required"],
    }


def planner_step(plan: dict) -> dict:
    """One scripted-fake step whose envelope carries the plan as structured output."""
    return {
        "role": "planner",
        "envelope": {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 1200,
            "num_turns": 1,
            "result": "planned",
            "total_cost_usd": 0.02,
            "usage": {"input_tokens": 200, "output_tokens": 90},
            "structured_output": plan,
        },
    }


def plan_session(
    store: Store,
    tmp_path: Path,
    run_id: int,
    plans: list[dict],
    *,
    cap: int = 3,
    size_budget: int = SIZE_BUDGET,
):
    spec = tmp_path / "toy-spec.md"
    if not spec.exists():
        spec.write_text(SPEC, encoding="utf-8", newline="\n")
    script = tmp_path / "planner-script.json"
    script.write_text(
        json.dumps({"steps": [planner_step(p) for p in plans]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return run_planning(
        store,
        run_id,
        spec,
        planner_profile(model="sonnet", max_turns=1, timeout_s=60.0),
        transcript_dir=tmp_path / "transcripts",
        cap=cap,
        max_retries=0,
        size_budget=size_budget,
        mode="scripted",
        script_path=script,
    )


def findings_for(findings, lint: str):
    return [f for f in findings if f.lint == lint]


def lint(plan: dict, run_id: int = 1, size_budget: int = SIZE_BUDGET):
    msg_ids = {msg(run_id, i) for i in range(3)}
    return lint_plan(plan, msg_ids, size_budget=size_budget)


# --- MSG synthesis (R9) -----------------------------------------------------------


def test_synthesize_messages_chunks_paragraphs_with_provenance(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    spec = tmp_path / "toy-spec.md"
    spec.write_text(SPEC, encoding="utf-8", newline="\n")
    messages = synthesize_messages(store, run_id, spec)
    # one MSG per blank-line-separated paragraph, index encoded in the id
    assert [m.msg_id for m in messages] == [msg(run_id, i) for i in range(3)]
    assert [m.paragraph_index for m in messages] == [0, 1, 2]
    assert messages[1].content == "Users can log in with a username."
    # rows landed; the spec file rides the run row (provenance split per U5)
    rows = store.conn.execute("SELECT id, content FROM trace_msg ORDER BY id").fetchall()
    assert [r["id"] for r in rows] == [m.msg_id for m in messages]
    assert store.get_run(run_id)["spec_ref"] == "specs/toy-spec.md"
    # mentions stay EMPTY — FEAT does not exist until Phase 2 (R9)
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg_mentions").fetchone()["n"] == 0


def test_synthesize_messages_is_idempotent_per_run(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    spec = tmp_path / "toy-spec.md"
    spec.write_text(SPEC, encoding="utf-8", newline="\n")
    first = synthesize_messages(store, run_id, spec)
    again = synthesize_messages(store, run_id, spec)  # resume re-entry
    assert [m.msg_id for m in again] == [m.msg_id for m in first]
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_msg").fetchone()["n"] == 3


def test_synthesize_messages_runs_do_not_collide(tmp_path):
    store = make_store(tmp_path)
    run_a, run_b = make_run(store), make_run(store)
    spec = tmp_path / "toy-spec.md"
    spec.write_text(SPEC, encoding="utf-8", newline="\n")
    a = synthesize_messages(store, run_a, spec)
    b = synthesize_messages(store, run_b, spec)
    assert {m.msg_id for m in a}.isdisjoint({m.msg_id for m in b})


def test_empty_spec_is_an_actionable_error(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    spec = tmp_path / "empty.md"
    spec.write_text("\n\n   \n", encoding="utf-8", newline="\n")
    with pytest.raises(PlanningError, match="no paragraphs"):
        synthesize_messages(store, run_id, spec)
    with pytest.raises(PlanningError, match="not found"):
        synthesize_messages(store, run_id, tmp_path / "missing.md")


def test_chunk_spec_drops_blank_blocks():
    assert chunk_spec("a\n\n\n  \nb\n\nc") == ["a", "b", "c"]


# --- deterministic lints, 1:1 with the R10 list ------------------------------------


def test_plan_lints_enumerate_the_r10_list():
    assert PLAN_LINTS == (
        LINT_REQ_SET,
        LINT_REQ_COVERAGE,
        LINT_DAG_ACYCLIC,
        LINT_AC_LINKS,
        LINT_SIZE_BUDGET,
        LINT_FILE_OWNERSHIP,
    )


def test_lint_clean_plan_has_no_findings():
    assert lint(valid_plan(1)) == []


def test_lint_req_set_fires_on_empty_extraction():
    plan = valid_plan(1)
    plan["requirements"] = []
    plan["tickets"] = []
    found = findings_for(lint(plan), LINT_REQ_SET)
    assert len(found) == 1
    assert found[0].severity == SEVERITY_ERROR
    assert "empty" in found[0].observed


def test_lint_req_set_fires_on_unknown_source_msg_and_duplicate_ids():
    plan = valid_plan(1)
    plan["requirements"][1]["source_msg"] = "MSG-r1-p999"
    plan["requirements"].append(dict(plan["requirements"][0]))
    observed = [f.observed for f in findings_for(lint(plan), LINT_REQ_SET)]
    assert any("unknown source_msg 'MSG-r1-p999'" in o for o in observed)
    assert any("duplicate requirement id 'REQ-1'" in o for o in observed)


def test_lint_req_coverage_fires_on_uncovered_requirement():
    plan = valid_plan(1)
    plan["tickets"][1]["covers"] = ["REQ-1"]  # REQ-2 now uncovered
    plan["tickets"][1]["acceptance_criteria"][0]["req"] = "REQ-1"
    found = findings_for(lint(plan), LINT_REQ_COVERAGE)
    assert [f.severity for f in found] == [SEVERITY_ERROR]
    assert "REQ-2" in found[0].observed


def test_lint_req_coverage_fires_on_unknown_cover_ref():
    plan = valid_plan(1)
    plan["tickets"][0]["covers"] = ["REQ-1", "REQ-99"]
    found = findings_for(lint(plan), LINT_REQ_COVERAGE)
    assert any("unknown requirement 'REQ-99'" in f.observed for f in found)


def test_lint_dag_acyclic_fires_on_cycle():
    plan = valid_plan(1)
    plan["tickets"][0]["depends_on"] = ["TKT-2"]  # TKT-1 <-> TKT-2
    found = findings_for(lint(plan), LINT_DAG_ACYCLIC)
    assert len(found) == 1
    assert "cycle" in found[0].observed
    assert "TKT-1" in found[0].observed and "TKT-2" in found[0].observed


def test_lint_dag_acyclic_fires_on_unknown_dep_and_duplicate_ticket_id():
    plan = valid_plan(1)
    plan["tickets"][0]["depends_on"] = ["TKT-99"]
    plan["tickets"][1]["id"] = "TKT-1"
    observed = [f.observed for f in findings_for(lint(plan), LINT_DAG_ACYCLIC)]
    assert any("unknown dependency 'TKT-99'" in o for o in observed)
    assert any("duplicate ticket id 'TKT-1'" in o for o in observed)


def test_lint_ac_links_fires_on_missing_acs():
    plan = valid_plan(1)
    plan["tickets"][0]["acceptance_criteria"] = []
    found = findings_for(lint(plan), LINT_AC_LINKS)
    assert len(found) == 1
    assert found[0].severity == SEVERITY_ERROR
    assert "no acceptance criteria" in found[0].observed


def test_lint_ac_links_fires_on_req_outside_ticket_covers_and_duplicate_ac_id():
    plan = valid_plan(1)
    plan["tickets"][1]["acceptance_criteria"][0]["req"] = "REQ-1"  # covers REQ-2
    plan["tickets"][1]["acceptance_criteria"].append(
        {"id": "AC-1", "text": "dup id", "req": "REQ-2"}
    )
    observed = [f.observed for f in findings_for(lint(plan), LINT_AC_LINKS)]
    assert any("'REQ-1' is not covered by ticket 'TKT-2'" in o for o in observed)
    assert any("duplicate acceptance-criterion id 'AC-1'" in o for o in observed)


def test_lint_size_budget_fires_over_the_unit_of_work_budget():
    plan = valid_plan(1)
    plan["tickets"][0]["files"] = [f"src/f{i}.ts" for i in range(5)]
    found = findings_for(lint(plan, size_budget=4), LINT_SIZE_BUDGET)
    assert len(found) == 1
    assert found[0].severity == SEVERITY_ERROR
    assert "5 files" in found[0].observed
    # the budget is a caller-routed tunable, never hardcoded
    assert findings_for(lint(plan, size_budget=5), LINT_SIZE_BUDGET) == []
    with pytest.raises(PlanningError, match="size_budget"):
        lint(plan, size_budget=0)


def test_lint_file_ownership_overlap_is_warn_level():
    plan = valid_plan(1)
    plan["tickets"][0]["files"] = ["src/shared.ts", "src/login.ts"]
    plan["tickets"][1]["files"] = ["src/shared.ts"]
    findings = lint(plan)
    found = findings_for(findings, LINT_FILE_OWNERSHIP)
    assert len(found) == 1
    assert found[0].severity == SEVERITY_WARN
    assert "src/shared.ts" in found[0].observed
    # warn-level: no error finding anywhere in this plan
    assert [f for f in findings if f.severity == SEVERITY_ERROR] == []


# --- the planning Ralph loop over the scripted fake ---------------------------------


def test_valid_plan_persists_full_traceability_joins(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    result = plan_session(store, tmp_path, run_id, [valid_plan(run_id)])
    assert result.iterations == 1
    assert result.msg_ids == tuple(msg(run_id, i) for i in range(3))

    # REQ → MSG queryable (R9 provenance bootstrap)
    rows = store.conn.execute(
        "SELECT r.id AS req_id, m.content AS content FROM trace_req r"
        " JOIN trace_msg m ON m.id = r.source_msg_id ORDER BY r.id"
    ).fetchall()
    assert [r["req_id"] for r in rows] == [
        f"REQ-r{run_id}-000",
        f"REQ-r{run_id}-001",
    ]
    assert "log in" in rows[0]["content"]
    assert "dashboard" in rows[1]["content"]

    # TKT → REQ queryable through the coverage matrix
    rows = store.conn.execute(
        "SELECT c.tkt_id AS tkt, c.req_id AS req FROM trace_tkt_covers c"
        " JOIN trace_tkt t ON t.id = c.tkt_id"
        " JOIN trace_req r ON r.id = c.req_id ORDER BY c.tkt_id"
    ).fetchall()
    assert [(r["tkt"], r["req"]) for r in rows] == [
        (f"TKT-r{run_id}-000", f"REQ-r{run_id}-000"),
        (f"TKT-r{run_id}-001", f"REQ-r{run_id}-001"),
    ]

    # AC → TKT queryable with the AC's own REQ link
    rows = store.conn.execute(
        "SELECT a.id AS ac, a.ticket_id AS tkt, a.req_id AS req FROM trace_ac a"
        " JOIN trace_tkt t ON t.id = a.ticket_id ORDER BY a.id"
    ).fetchall()
    assert [(r["ac"], r["tkt"], r["req"]) for r in rows] == [
        (f"AC-r{run_id}-000", f"TKT-r{run_id}-000", f"REQ-r{run_id}-000"),
        (f"AC-r{run_id}-001", f"TKT-r{run_id}-001", f"REQ-r{run_id}-001"),
    ]

    # tickets are born pending; the dependency edge is canonicalized in the doc
    statuses = store.conn.execute("SELECT status FROM trace_tkt").fetchall()
    assert {r["status"] for r in statuses} == {"pending"}
    assert result.plan["tickets"][1]["depends_on"] == [f"TKT-r{run_id}-000"]

    # acceptance leaves the run in planning — executing is U4's transition
    assert store.get_run(run_id)["status"] == "planning"


def test_cyclic_dag_typed_failure_then_corrected_on_iteration_2(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    cyclic = valid_plan(run_id)
    cyclic["tickets"][0]["depends_on"] = ["TKT-2"]
    result = plan_session(store, tmp_path, run_id, [cyclic, valid_plan(run_id)])
    assert result.iterations == 2

    # the bounce produced a typed plan_lint failure record naming the lint
    records = store.conn.execute("SELECT * FROM failure_records").fetchall()
    assert len(records) == 1
    assert records[0]["failure_kind"] == "plan_lint"
    assert "dag_acyclic" in records[0]["location"]
    assert "cycle" in records[0]["observed"]
    assert records[0]["run_id"] == run_id

    # each Ralph iteration was its own span, charged to the failure record's span
    spans = store.conn.execute(
        "SELECT id, ralph_iteration, agent, status FROM trace_span ORDER BY ralph_iteration"
    ).fetchall()
    assert [s["ralph_iteration"] for s in spans] == [1, 2]
    assert {s["agent"] for s in spans} == {"planner"}
    assert {s["status"] for s in spans} == {"completed"}
    assert records[0]["span_id"] == spans[0]["id"]

    # the corrected plan persisted
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_tkt").fetchone()["n"] == 2


def test_empty_req_extraction_is_lint_failure_not_crash(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    empty = {"requirements": [], "tickets": [], "assumptions": []}
    result = plan_session(store, tmp_path, run_id, [empty, valid_plan(run_id)])
    assert result.iterations == 2
    records = store.conn.execute("SELECT location FROM failure_records").fetchall()
    assert any("req_set" in r["location"] for r in records)


def test_zero_ticket_plan_fires_coverage_lint(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    zero_tickets = valid_plan(run_id)
    zero_tickets["tickets"] = []
    result = plan_session(store, tmp_path, run_id, [zero_tickets, valid_plan(run_id)])
    assert result.iterations == 2
    records = store.conn.execute(
        "SELECT location, observed FROM failure_records"
    ).fetchall()
    coverage = [r for r in records if "req_coverage" in r["location"]]
    assert len(coverage) == 2  # one per uncovered REQ
    assert any("covered by no ticket" in r["observed"] for r in coverage)


def test_overlapping_file_ownership_warn_recorded_plan_accepted(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    overlapping = valid_plan(run_id)
    overlapping["tickets"][1]["files"] = ["src/login.ts"]  # TKT-1 owns it too
    result = plan_session(store, tmp_path, run_id, [overlapping])

    # accepted on iteration 1 — warn level never blocks (R10 Phase 1)
    assert result.iterations == 1
    assert store.conn.execute("SELECT COUNT(*) AS n FROM trace_tkt").fetchone()["n"] == 2
    # no failure record: a warn is not a typed failure
    assert store.conn.execute("SELECT COUNT(*) AS n FROM failure_records").fetchone()["n"] == 0
    # the warn is recorded on the result AND with the persisted plan
    assert len(result.warnings) == 1
    assert result.warnings[0].lint == LINT_FILE_OWNERSHIP
    report = plan_report(store, run_id)
    assert len(report["warnings"]) == 1
    assert report["warnings"][0]["lint"] == LINT_FILE_OWNERSHIP
    assert "src/login.ts" in report["warnings"][0]["observed"]


def test_assumptions_persisted_and_surfaced_in_run_report(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    ambiguous = valid_plan(run_id)
    ambiguous["assumptions"] = [
        "username-only auth; no password required",
        "bookmarks are private to their owner",
    ]
    result = plan_session(store, tmp_path, run_id, [ambiguous])
    assert result.assumptions == tuple(ambiguous["assumptions"])
    report = plan_report(store, run_id)
    assert report["assumptions"] == ambiguous["assumptions"]
    assert report["run_id"] == run_id
    assert report["spec_ref"] == "specs/toy-spec.md"


def test_cap_exhaustion_plan_failed_nothing_downstream(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    bad = {"requirements": [], "tickets": [], "assumptions": []}
    with pytest.raises(PlanFailed, match="cap of 2"):
        plan_session(store, tmp_path, run_id, [bad, bad], cap=2)

    # the run is terminal plan_failed (R10)
    assert store.get_run(run_id)["status"] == "plan_failed"
    # nothing downstream runs: no plan rows, no plan document
    for table in ("trace_req", "trace_tkt", "trace_tkt_covers", "trace_ac"):
        n = store.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        assert n == 0, f"{table} should be empty after plan_failed"
    with pytest.raises(PlanningError, match="no persisted plan"):
        plan_report(store, run_id)
    # every bounced iteration left its typed failure trail
    records = store.conn.execute("SELECT failure_kind FROM failure_records").fetchall()
    assert [r["failure_kind"] for r in records] == ["plan_lint", "plan_lint"]


def test_run_planning_validates_cap_and_run(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    with pytest.raises(PlanningError, match="cap"):
        plan_session(store, tmp_path, run_id, [valid_plan(run_id)], cap=0)
    with pytest.raises(PlanningError, match="does not exist"):
        plan_session(store, tmp_path, 999, [valid_plan(999)])


# --- contract and prompt sanity ------------------------------------------------------


def test_planner_schema_carries_the_full_contract():
    props = PLANNER_OUTPUT_SCHEMA["properties"]
    assert set(PLANNER_OUTPUT_SCHEMA["required"]) == {
        "requirements",
        "tickets",
        "assumptions",
    }
    ticket = props["tickets"]["items"]
    assert set(ticket["required"]) == {
        "id",
        "title",
        "description",
        "covers",
        "depends_on",
        "files",
        "acceptance_criteria",
    }
    # 007 U2: REQ provenance is MSG-or-ASSUME — source_msg is no longer mandatory
    # (the exactly-one rule is the DB CHECK + the U3 lint).
    assert props["requirements"]["items"]["required"] == ["id", "text"]
    assert set(props["requirements"]["items"]["properties"]) == {
        "id",
        "text",
        "source_msg",
        "source_assume",
    }
    assert ticket["properties"]["acceptance_criteria"]["items"]["required"] == [
        "id",
        "text",
        "req",
    ]
    # 007 U2: assumptions are typed objects (string union for back-compat);
    # proposals are an optional typed array.
    assumption = props["assumptions"]["items"]
    assert assumption["type"] == ["string", "object"]
    assert set(assumption["required"]) == {"claim", "risk_if_wrong", "cheapest_test"}
    assert assumption["properties"]["risk_if_wrong"]["enum"] == ["low", "med", "high"]
    assert "proposals" not in PLANNER_OUTPUT_SCHEMA["required"]
    proposal = props["proposals"]["items"]
    assert set(proposal["required"]) == {"id", "topic", "options", "recommended"}


def test_planner_prompt_lists_messages_and_budget(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    spec = tmp_path / "toy-spec.md"
    spec.write_text(SPEC, encoding="utf-8", newline="\n")
    messages = synthesize_messages(store, run_id, spec)
    prompt = build_planner_prompt(spec.name, messages, 4)
    for m in messages:
        assert f"[{m.msg_id}]" in prompt
        assert m.content in prompt
    assert "at most 4 per ticket" in prompt
    assert str(tmp_path) not in prompt  # no absolute paths in prompts


def test_feedback_prompt_carries_typed_failures(tmp_path):
    plan = valid_plan(1)
    plan["tickets"][0]["depends_on"] = ["TKT-2"]
    errors = [f for f in lint(plan) if f.severity == SEVERITY_ERROR]
    feedback = build_lint_feedback_prompt("BASE", errors)
    assert feedback.startswith("BASE")
    assert "[dag_acyclic]" in feedback
    assert "expected an acyclic ticket dependency graph" in feedback


def test_planner_prompt_describes_typed_assumptions_and_proposals(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    spec = tmp_path / "toy-spec.md"
    spec.write_text(SPEC, encoding="utf-8", newline="\n")
    messages = synthesize_messages(store, run_id, spec)
    prompt = build_planner_prompt(spec.name, messages, 4)
    # the typed ASSUME object shape is described (schema description only)
    assert "risk_if_wrong" in prompt
    assert "cheapest_test" in prompt
    # proposals are described as OPTIONAL — no behavioral "always propose" push
    # (proposal-first is Phase D, world-gated; KTD7)
    assert "proposals" in prompt and "OPTIONAL" in prompt
    assert "linked_assume_id" in prompt


# --- 007 U2: typed ASSUME ledger, PROPOSAL artifact, ranked questions ---------------


def typed_assumption(
    claim: str, risk: str = "high", *, local_id: str | None = None,
    cheapest: str = "click the thing",
) -> dict:
    obj = {"claim": claim, "risk_if_wrong": risk, "cheapest_test": cheapest}
    if local_id is not None:
        obj["id"] = local_id
    return obj


class _FakeAskOutcome:
    """A duck-typed ask round-trip outcome (the explorer's QAOutcome surface)."""

    def __init__(self, outcome: str, answer=None, answer_msg_id=None) -> None:
        self.outcome = outcome
        self.answer = answer
        self.answer_msg_id = answer_msg_id


def test_normalize_assumption_string_and_object():
    norm = normalize_assumption("just a string")
    assert (norm.local_id, norm.claim, norm.risk_if_wrong, norm.cheapest_test) == (
        None,
        "just a string",
        "med",
        "",
    )
    obj = normalize_assumption(
        {"id": "A1", "claim": "c", "risk_if_wrong": "high", "cheapest_test": "t"}
    )
    assert (obj.local_id, obj.claim, obj.risk_if_wrong, obj.cheapest_test) == (
        "A1",
        "c",
        "high",
        "t",
    )


def test_typed_assumptions_persist_to_assume_rows(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [
        typed_assumption("auth is username-only", "high", cheapest="try logging in"),
        typed_assumption("bookmarks are private", "low", cheapest="check another user"),
    ]
    plan_session(store, tmp_path, run_id, [plan])
    rows = store.conn.execute(
        "SELECT id, run_id, claim, risk_if_wrong, cheapest_test, status,"
        " confirmed_by_msg FROM trace_assume ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == [
        f"ASSUME-r{run_id}-000",
        f"ASSUME-r{run_id}-001",
    ]
    assert [r["claim"] for r in rows] == [
        "auth is username-only",
        "bookmarks are private",
    ]
    assert [r["risk_if_wrong"] for r in rows] == ["high", "low"]
    assert [r["cheapest_test"] for r in rows] == [
        "try logging in",
        "check another user",
    ]
    # the ledger is the source of truth; rows are born open, unconfirmed
    assert {r["status"] for r in rows} == {"open"}
    assert {r["confirmed_by_msg"] for r in rows} == {None}
    assert {r["run_id"] for r in rows} == {run_id}


def test_string_assumption_normalized_to_med_risk_ledger_row(tmp_path):
    # a legacy bare-string fixture (cross-unit back-compat) still persists as a
    # med-risk ASSUME row, and the document keeps the original string shape.
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan_session(store, tmp_path, run_id, [valid_plan(run_id)])
    row = store.conn.execute(
        "SELECT claim, risk_if_wrong, cheapest_test FROM trace_assume"
    ).fetchone()
    assert row["claim"] == "username-only auth; no password required"
    assert row["risk_if_wrong"] == "med"
    assert row["cheapest_test"] == ""
    assert plan_report(store, run_id)["assumptions"] == [
        "username-only auth; no password required"
    ]


def test_typed_assumption_missing_field_fails_schema_cleanly(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    bad = valid_plan(run_id)
    bad["assumptions"] = [{"claim": "no risk field", "cheapest_test": "x"}]
    with pytest.raises(SessionSchemaViolation):
        plan_session(store, tmp_path, run_id, [bad], cap=1)


def test_typed_assumption_bad_risk_enum_fails_schema_cleanly(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    bad = valid_plan(run_id)
    bad["assumptions"] = [typed_assumption("c", "critical")]  # not low|med|high
    with pytest.raises(SessionSchemaViolation):
        plan_session(store, tmp_path, run_id, [bad], cap=1)


def test_proposals_persist_with_and_without_link(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [typed_assumption("deletes are soft", "high", local_id="A-del")]
    plan["proposals"] = [
        {
            "id": "P1",
            "topic": "deletion semantics",
            "options": ["soft", "hard"],
            "recommended": "soft",
            "linked_assume_id": "A-del",
        },
        {
            "id": "P2",
            "topic": "empty state",
            "options": ["blank", "cta"],
            "recommended": "cta",
        },
    ]
    plan_session(store, tmp_path, run_id, [plan])
    rows = store.conn.execute(
        "SELECT id, topic, options_json, recommended, linked_assume_id"
        " FROM trace_proposal ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == [
        f"PROP-r{run_id}-000",
        f"PROP-r{run_id}-001",
    ]
    assert json.loads(rows[0]["options_json"]) == ["soft", "hard"]
    # the linked proposal remaps the planner-local assume id to the canonical
    # ASSUME id (the REQ/TKT remap discipline); the unlinked one is NULL
    assert rows[0]["linked_assume_id"] == f"ASSUME-r{run_id}-000"
    assert rows[1]["linked_assume_id"] is None
    props = plan_report(store, run_id)["proposals"]
    assert [p["id"] for p in props] == [
        f"PROP-r{run_id}-000",
        f"PROP-r{run_id}-001",
    ]
    assert props[0]["linked_assume_id"] == f"ASSUME-r{run_id}-000"
    assert props[1]["linked_assume_id"] is None


def test_req_sourced_from_assumption_persists(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [
        typed_assumption("untagged bookmarks are listed", "high", local_id="A-untag")
    ]
    plan["requirements"].append(
        {"id": "REQ-3", "text": "list untagged", "source_assume": "A-untag"}
    )
    plan["tickets"].append(
        {
            "id": "TKT-3",
            "title": "untagged",
            "description": "list untagged",
            "covers": ["REQ-3"],
            "depends_on": [],
            "files": ["src/untagged.ts"],
            "acceptance_criteria": [
                {"id": "AC-3", "text": "untagged listed", "req": "REQ-3"}
            ],
        }
    )
    plan_session(store, tmp_path, run_id, [plan])
    # REQ provenance = MSG-or-ASSUME: the ASSUME-sourced REQ persists with
    # source_assume_id and NO source_msg (the trace_req exactly-one CHECK holds)
    assume_req = store.conn.execute(
        "SELECT source_msg_id, source_assume_id FROM trace_req WHERE id = ?",
        (f"REQ-r{run_id}-002",),
    ).fetchone()
    assert assume_req["source_msg_id"] is None
    assert assume_req["source_assume_id"] == f"ASSUME-r{run_id}-000"
    # the MSG-sourced REQs are unaffected
    msg_req = store.conn.execute(
        "SELECT source_msg_id, source_assume_id FROM trace_req WHERE id = ?",
        (f"REQ-r{run_id}-000",),
    ).fetchone()
    assert msg_req["source_assume_id"] is None
    assert msg_req["source_msg_id"] == msg(run_id, 1)


def test_req_sourcing_unknown_assumption_is_an_actionable_error(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["requirements"].append(
        {"id": "REQ-3", "text": "x", "source_assume": "A-nope"}
    )
    plan["tickets"].append(
        {
            "id": "TKT-3",
            "title": "x",
            "description": "x",
            "covers": ["REQ-3"],
            "depends_on": [],
            "files": ["src/x.ts"],
            "acceptance_criteria": [{"id": "AC-3", "text": "x", "req": "REQ-3"}],
        }
    )
    with pytest.raises(PlanningError, match="unknown assumption"):
        plan_session(store, tmp_path, run_id, [plan], cap=1)


def test_rank_questions_stable_under_ties():
    items = [
        {"id": "a", "risk_if_wrong": "med"},
        {"id": "b", "risk_if_wrong": "med"},
        {"id": "c", "risk_if_wrong": "med"},
    ]
    assert [i["id"] for i in rank_questions(items)] == ["a", "b", "c"]


def test_rank_questions_orders_by_risk_high_first():
    items = [
        {"id": "low1", "risk_if_wrong": "low"},
        {"id": "high1", "risk_if_wrong": "high"},
        {"id": "med1", "risk_if_wrong": "med"},
        {"id": "high2", "risk_if_wrong": "high"},
    ]
    # high before med before low; ties keep input order (high1 before high2)
    assert [i["id"] for i in rank_questions(items)] == [
        "high1",
        "high2",
        "med1",
        "low1",
    ]


def test_check_plan_assumptions_ranks_and_settles_ledger(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    # low FIRST in plan order so risk-ranking demonstrably REORDERS the questions
    plan["assumptions"] = [
        typed_assumption("low risk two", "low", local_id="L"),
        typed_assumption("high risk one", "high", local_id="H"),
    ]
    plan_session(store, tmp_path, run_id, [plan])
    # a real MSG for the confirmed_by_msg FK
    with store.transaction():
        store.conn.execute(
            "INSERT INTO trace_msg (id, content) VALUES (?, ?)",
            ("MSG-answer", "the answer"),
        )
    answers = iter(
        [
            _FakeAskOutcome("answered", answer="yes", answer_msg_id="MSG-answer"),
            _FakeAskOutcome("budget_exhausted"),
        ]
    )
    seen: list[str] = []

    def ask(question: str):
        seen.append(question)
        return next(answers)

    records = check_plan_assumptions(store, run_id, ask)
    # KTD7: the high-risk assumption is converted FIRST despite being second in
    # plan order — the budget is spent on the riskiest question
    assert "high risk one" in seen[0]
    assert "low risk two" in seen[1]
    assert records[0]["status"] == "verified"
    assert records[0]["answer"] == "yes"
    assert records[1]["status"] == "unverified"
    assert records[1]["reason"] == "budget_exhausted"
    # the ledger is settled: answered → confirmed with the answer MSG; the
    # over-budget row stays open (a typed risk, not a blocker)
    ledger = {
        r["claim"]: r
        for r in store.conn.execute(
            "SELECT claim, status, confirmed_by_msg FROM trace_assume"
        ).fetchall()
    }
    assert ledger["high risk one"]["status"] == "confirmed"
    assert ledger["high risk one"]["confirmed_by_msg"] == "MSG-answer"
    assert ledger["low risk two"]["status"] == "open"
    assert ledger["low risk two"]["confirmed_by_msg"] is None
    # surfaced on the plan document
    assert plan_report(store, run_id)["assumption_checks"] == records


def test_planner_prompt_set_version_stamped_and_overridable(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan_session(store, tmp_path, run_id, [valid_plan(run_id)])
    stamped = store.conn.execute(
        "SELECT DISTINCT prompt_set_version FROM trace_span WHERE agent = 'planner'"
        " AND run_id = ?",
        (run_id,),
    ).fetchall()
    assert [s["prompt_set_version"] for s in stamped] == [PLANNER_PROMPT_SET_VERSION]
    # an explicit caller override is respected (the orchestrator's channel)
    run2 = make_run(store)
    script = tmp_path / "ps-override-script.json"
    script.write_text(
        json.dumps({"steps": [planner_step(valid_plan(run2))]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    run_planning(
        store,
        run2,
        tmp_path / "toy-spec.md",
        planner_profile(model="sonnet", max_turns=1, timeout_s=60.0),
        transcript_dir=tmp_path / "transcripts-ps",
        cap=1,
        max_retries=0,
        size_budget=SIZE_BUDGET,
        mode="scripted",
        script_path=script,
        prompt_set_version="ps-custom",
    )
    overridden = store.conn.execute(
        "SELECT prompt_set_version FROM trace_span WHERE run_id = ?", (run2,)
    ).fetchone()
    assert overridden["prompt_set_version"] == "ps-custom"


# --- 007 U3: REQ-provenance lint + pre-increment assumption gate --------------------


def test_provenance_lint_xor_source():
    # MUST-test (007 U3): a REQ with BOTH source_msg and source_assume, or
    # NEITHER, fails the lint; exactly one passes. Mirrors U1's trace_req DB
    # CHECK at the plan-dict level.
    run_id = 1
    # exactly one (the clean valid plan, all source_msg) → passes
    assert lint_req_provenance(valid_plan(run_id)) == []

    # exactly one via source_assume → also passes
    via_assume = valid_plan(run_id)
    via_assume["requirements"][0]["source_assume"] = "A-x"
    del via_assume["requirements"][0]["source_msg"]
    assert lint_req_provenance(via_assume) == []

    # BOTH → a single typed error on the offending REQ
    both = valid_plan(run_id)
    both["requirements"][0]["source_assume"] = "A-x"  # already has source_msg
    findings = lint_req_provenance(both)
    assert [f.lint for f in findings] == [LINT_REQ_PROVENANCE]
    assert findings[0].severity == SEVERITY_ERROR
    assert findings[0].location == "requirement REQ-1"
    assert "both" in findings[0].observed

    # NEITHER → an orphan REQ fails
    neither = valid_plan(run_id)
    del neither["requirements"][0]["source_msg"]
    findings = lint_req_provenance(neither)
    assert [f.lint for f in findings] == [LINT_REQ_PROVENANCE]
    assert "neither" in findings[0].observed


def test_provenance_lint_zero_assume_all_msg_plan_passes():
    # the scenario's "zero-ASSUME all-MSG plan passes" — the brownfield shape is
    # untouched by the new lint.
    plan = valid_plan(7)
    assert all("source_msg" in r and "source_assume" not in r
               for r in plan["requirements"])
    assert lint_req_provenance(plan) == []


def test_run_planning_bounces_on_provenance_then_corrected(tmp_path):
    # the provenance lint rides the Ralph loop through the plan_lint failure
    # channel: a both-source REQ bounces iteration 1, the corrected plan lands
    # on iteration 2 (lint feedback end-to-end, U3 Verification).
    store = make_store(tmp_path)
    run_id = make_run(store)
    bad = valid_plan(run_id)
    bad["requirements"][0]["source_assume"] = "A-x"  # both → provenance error
    good = valid_plan(run_id)
    result = plan_session(store, tmp_path, run_id, [bad, good], cap=3)
    assert result.iterations == 2
    recs = store.conn.execute(
        "SELECT failure_kind, location FROM failure_records WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    assert any(
        r["failure_kind"] == "plan_lint" and LINT_REQ_PROVENANCE in r["location"]
        for r in recs
    )


def test_k_effective_caps_at_half_budget():
    # MUST-test (007 U3, KTD7): k_effective = min(config_k, floor(budget/2)).
    assert k_effective(2, 3) == 1            # budget 3 → floor(3/2)=1
    assert k_effective(8, 10) == 5           # budget 10, config_k 8 → min(8,5)=5
    assert k_effective(0, 100) == 0          # config_k 0 → gate never blocks
    # the gate can NEVER consume the whole question budget, at any budget
    for budget in range(0, 24):
        assert k_effective(99, budget) <= budget // 2


def test_gate_passes_when_no_top_risk_assume_is_open(tmp_path):
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [typed_assumption("auth is username-only", "high")]
    plan_session(store, tmp_path, run_id, [plan])
    # k_effective(2, 1) = min(2, 0) = 0 → tiny budget, the gate never blocks even
    # with an open high-risk assumption
    result = assumption_gate(store, run_id, config_k=2, question_budget=1)
    assert result.k_effective == 0
    assert result.passed
    assert result.failures == ()


def test_gate_bounces_unconfirmed_then_roundtrips(tmp_path):
    # MUST-test (007 U3): an unconfirmed high-risk ASSUME bounces with a TYPED
    # failure record; confirm → re-plan → passes (full round-trip).
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [
        typed_assumption("auth is username-only", "high", local_id="H"),
        typed_assumption("bookmarks are private", "low", local_id="L"),
    ]
    plan_session(store, tmp_path, run_id, [plan])

    # before confirmation the top-risk ASSUME is open → the gate bounces.
    bounce = assumption_gate(store, run_id, config_k=1, question_budget=10)
    assert bounce.k_effective == 1
    assert not bounce.passed
    # the riskiest assumption is the one demanded (risk-ranked top-k), not plan order
    assert [f.assume_id for f in bounce.failures] == [f"ASSUME-r{run_id}-000"]
    assert bounce.failures[0].risk_if_wrong == "high"
    # TYPED record (the §7 shape), not a free string
    assert isinstance(bounce.failures[0], AssumptionGateFinding)
    assert bounce.failures[0].as_dict()["assume_id"] == f"ASSUME-r{run_id}-000"

    # confirm it through the existing assumption-conversion flow — the elicitation
    # round-trip — then re-run the gate (the host's "re-plan").
    with store.transaction():
        store.conn.execute(
            "INSERT INTO trace_msg (id, content) VALUES (?, ?)",
            ("MSG-confirm", "yes, username-only auth"),
        )
    check_plan_assumptions(
        store,
        run_id,
        lambda q: _FakeAskOutcome(
            "answered", answer="yes", answer_msg_id="MSG-confirm"
        ),
    )
    cleared = assumption_gate(store, run_id, config_k=1, question_budget=10)
    assert cleared.passed
    assert cleared.failures == ()


def test_gate_invalidated_assumption_also_bounces(tmp_path):
    # "must be confirmed" — an invalidated top-risk assumption is not confirmed,
    # so it bounces just like an open one (re-planning is exactly right when a
    # load-bearing assumption was found false).
    store = make_store(tmp_path)
    run_id = make_run(store)
    plan = valid_plan(run_id)
    plan["assumptions"] = [typed_assumption("auth is username-only", "high")]
    plan_session(store, tmp_path, run_id, [plan])
    with store.transaction():
        store.conn.execute(
            "UPDATE trace_assume SET status = 'invalidated' WHERE id = ?",
            (f"ASSUME-r{run_id}-000",),
        )
    result = assumption_gate(store, run_id, config_k=2, question_budget=10)
    assert not result.passed
    assert "invalidated" in result.failures[0].observed


# --- ## Conformance (007 U2) --------------------------------------------------------
#
# U2 maps its Test scenarios to the tests above; the planner-contract refactor
# (ASSUME ledger, PROPOSAL artifact, ranked questions) is enforced behaviorally:
#
# - ambiguous fixture → ≥1 typed ASSUME persisted:
#     test_typed_assumptions_persist_to_assume_rows
# - an answered assumption question flips status with the MSG recorded:
#     test_check_plan_assumptions_ranks_and_settles_ledger (→ confirmed +
#     confirmed_by_msg) AND test_assumptions_converted_budget_counted_and_
#     overbudget_unverified in tests/test_explorer.py (live explorer round-trip)
# - legacy string-shaped transcripts: the contract is enforced for typed objects
#     (test_typed_assumption_missing_field_fails_schema_cleanly,
#     test_typed_assumption_bad_risk_enum_fails_schema_cleanly). DEVIATION (smallest
#     faithful adaptation, wave-scoped "do not touch other units' files" +
#     "suite must stay green"): bare strings are ACCEPTED via a union `type`
#     and normalized to a med-risk claim, rather than rejected, because the
#     string-assumption form is produced by 8 already-committed fixtures across
#     other units (test_explorer/test_episode/test_pipeline_e2e/test_rehearsal/
#     test_e2e_*). Back-compat is itself tested:
#     test_string_assumption_normalized_to_med_risk_ledger_row.
# - zero assumptions legal: test_valid_plan_persists_full_traceability_joins
#     (and every []-assumption fixture across the suite)
# - rank_questions stable under ties / orders by risk:
#     test_rank_questions_stable_under_ties, test_rank_questions_orders_by_risk_high_first
# - a proposal with no linked_assume_id persists (and a linked one remaps):
#     test_proposals_persist_with_and_without_link
# - REQ provenance = MSG-or-ASSUME: test_req_sourced_from_assumption_persists,
#     test_req_sourcing_unknown_assumption_is_an_actionable_error
# - prompt-set version bumped and stamped:
#     test_planner_prompt_set_version_stamped_and_overridable
# - brownfield regression (behaviour identical except assumption shape): the
#     whole pre-existing planning suite + tests/test_pipeline_e2e.py stay green.
#
# --- ## Conformance (007 U3) --------------------------------------------------------
#
# U3 ships two enforcement seams; the named MUST-tests (do not weaken) map 1:1 to
# the unit's required acceptance tests, and the remaining scenarios to the tests
# beside them:
#
# - test_k_effective_caps_at_half_budget — k_effective = min(config_k,
#     floor(question_budget/2)) EXACTLY (budget 3 → k=1; budget 10, config_k 8 →
#     k=5) and ≤ budget//2 at every budget: the gate can never consume the whole
#     question budget (KTD7). [REQUIRED]
# - test_provenance_lint_xor_source — a REQ with BOTH source_msg and
#     source_assume, or NEITHER, fails lint_req_provenance; exactly one passes
#     (mirrors U1's trace_req DB CHECK at the plan-dict level). [REQUIRED]
# - test_gate_bounces_unconfirmed_then_roundtrips — an unconfirmed high-risk
#     ASSUME bounces with a TYPED AssumptionGateFinding; confirm (via
#     check_plan_assumptions) → re-run gate → passes (full round-trip). [REQUIRED]
#
# Supporting scenarios:
# - Lint fixture pass + fail (orphan REQ) and zero-ASSUME all-MSG passes:
#     test_provenance_lint_xor_source, test_provenance_lint_zero_assume_all_msg_plan_passes
# - Lint feedback rides the Ralph loop (Verification: ambiguous spec exercises the
#     lint end-to-end): test_run_planning_bounces_on_provenance_then_corrected
# - Gate: confirmed/no-top-risk-open set passes; tiny budget never blocks:
#     test_gate_passes_when_no_top_risk_assume_is_open, the cleared arm of the
#     round-trip test; an invalidated top-risk assumption also bounces:
#     test_gate_invalidated_assumption_also_bounces
#
# DEVIATIONS (smallest faithful adaptations, recorded for the reviewer):
#  1. The provenance lint is a SEPARATE function (lint_req_provenance), beside —
#     not inside — lint_plan / PLAN_LINTS, so the R10 lint list stays byte-stable
#     (test_plan_lints_enumerate_the_r10_list unchanged). It is the "pure lint"
#     half of U3's two-seam architecture; the gate is the orchestrator-seam half.
#     run_planning merges its findings into the existing plan_lint failure channel.
#  2. The gate emits a TYPED in-memory record (AssumptionGateFinding, the §7
#     location/expected/observed shape) rather than a failure_records row: no
#     "assumption_gate" FAILURE_KIND exists and adding one would drift the
#     failure_records CHECK across already-migrated DBs (out of U3's wave scope).
#     The gate is "not a plan lint", so it deliberately does not reuse the
#     plan_lint kind. The host (the orchestrator/episode seam, wired by U7 which
#     owns the episode world branch and the increment-execution seam) decides
#     persistence and re-entry; U3 delivers the gate as a tested seam function,
#     exactly as check_plan_assumptions is itself an orchestrator-bound seam.
