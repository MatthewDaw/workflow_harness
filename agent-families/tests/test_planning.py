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
    LINT_REQ_SET,
    LINT_SIZE_BUDGET,
    PLAN_LINTS,
    PLANNER_OUTPUT_SCHEMA,
    SEVERITY_ERROR,
    SEVERITY_WARN,
    PlanFailed,
    PlanningError,
    build_lint_feedback_prompt,
    build_planner_prompt,
    chunk_spec,
    lint_plan,
    plan_report,
    run_planning,
    synthesize_messages,
)
from agent_families.pipeline.sessions import planner_profile
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
    assert props["requirements"]["items"]["required"] == ["id", "text", "source_msg"]
    assert ticket["properties"]["acceptance_criteria"]["items"]["required"] == [
        "id",
        "text",
        "req",
    ]


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
