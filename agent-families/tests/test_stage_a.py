"""plan-004 U5: Reflector Stage A — deterministic attribution with honest sinks
(R7, R8, R9).

Fully offline (the default suite): the §12.2 decision procedure runs as pure
lookups over a store-backed trace chain; step 7's repro re-execution and the
coverage substrate are injected as scripted seams (the live clone-restart /
target-reset / istanbul-c8 merge wiring is the production seam, exercised only by
the live cycle in U9). The judge seam is consulted ONLY for the two
micro-judgments and every deterministic fixture proves it is never touched.

## Conformance

Test-scenario / invariant (plan-004 U5) -> test:

- one fixture per §12.2 branch outcome (every step has a 1:1 fixture test):
  - communicated-no, elicitable -> PLANNER(elicitation):
    ``test_step1_communicated_no_elicitable_blames_planner``
  - communicated-no, not elicitable -> EXPLORER(prompt):
    ``test_step1_communicated_no_not_elicitable_blames_explorer``
  - extracted-no -> PLANNER(requirement extraction):
    ``test_step2_extracted_no_blames_planner``
  - covered-no -> PLANNER(coverage): ``test_step3_covered_no_blames_planner``
  - specified-no -> PLANNER(acceptance criteria):
    ``test_step4_specified_no_blames_planner``
  - implemented-no -> WORKER: ``test_step5_implemented_no_blames_worker``
  - verified-no -> VERIFIER: ``test_step6_verified_no_blames_verifier``
  - discriminate, repro-still-passes -> PLANNER(AC quality):
    ``test_step7_repro_still_passes_blames_ac_quality``
  - discriminate, repro-now-fails -> breaking ticket located by coverage trace:
    ``test_step7_repro_now_fails_locates_breaking_ticket`` (+
    ``test_step7_regression_with_no_coverage_hit_falls_to_verifier``)
  - answer-contradiction lookup -> EXPLORER(answering):
    ``test_step8_answer_contradiction_is_a_lookup``
- contributing[] populated on a multi-cause fixture:
  ``test_multi_cause_populates_contributing``
- instrument-health record written instead of an explorer idea:
  ``test_explorer_attribution_sinks_to_instrument_health``,
  ``test_instrument_health_excluded_from_idea_candidates``
- MUST-test: attribution is deterministic without an LLM (steps 1-6, judge raises):
  ``test_stage_a_is_deterministic_without_llm``
- MUST-test: step 7 mechanically re-executes the stored repro envelope (both arms):
  ``test_stage_a_repro_reexecution``
- MUST-test: identical fixture yields byte-identical attribution:
  ``test_stage_a_attribution_stable``
- the two micro-judgments reach an LLM only out-of-taxonomy / verdict-absent:
  ``test_out_of_taxonomy_feat_fires_the_elicitability_judge``,
  ``test_missing_oracle_verdict_fires_the_answer_judge``
- the coverage substrate reader (this unit's deliverable):
  ``test_read_merged_coverage_parses_per_scenario_file_lists``
- the episode pass attributes every failed scenario and sinks correctly:
  ``test_run_stage_a_over_an_episode``

## Deviations (smallest faithful adaptations; plan authoritative for WHAT)

- The U5 Verification names "the worked example from DESIGN §12.4", but DESIGN has
  no §12.4 (it runs §12.1 -> §12.2 -> §12.3 -> §13). The §12.2 decision procedure
  *is* the worked specification; the per-branch fixture tests above reproduce it
  step by step (the "worked example" intent), including the multi-cause /
  contributing case (``test_multi_cause_populates_contributing``).
- The coverage *substrate* (DESIGN §12.2 / R7) is delivered here as the merge
  *reader* + injected ``CoverageProvider`` seam (``read_merged_coverage`` and the
  step-7 join), unit-tested on a fixture coverage map. The output-stack template's
  instrumented build mode (vite-plugin-istanbul / V8 + c8) that *emits* that map
  lives in the Phase-1 template repo and is wired by the live cycle (U9); this
  wave is scoped to U5's two files only, so it is not modified here.
- The §12.2 join is replicated as ``_load_chain`` rather than imported from the
  CLI's ``af trace chain`` (``cli.py`` belongs to Phase 2 U9 and is out of this
  wave's edit scope); both walk the identical SCEN->...->CHK edges.
- Probe-category tagging is taken as a caller-supplied ``feat_probe_categories``
  mapping (the registry-cluster tags) rather than a new FEAT column — no schema
  change, and ``PROBE_TAXONOMY`` carries the fixed, provenance-commented table.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent_families import reflector
from agent_families.reflector import stage_a as sa
from agent_families.store import Store


# --- fixtures and chain builders ------------------------------------------------


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
        "the judge seam must not be consulted on a deterministic attribution path"
    )


def _scen(store, episode_id, *, feat="FEAT-1", sid="SCEN-1", result="fail"):
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, result, evidence, episode_id,"
        " snapshot_id) VALUES (?, ?, ?, 'judged_different', ?, 0)",
        (sid, feat, result, episode_id),
    )
    return sid


def _feat(store, fid="FEAT-1"):
    store.conn.execute(
        "INSERT INTO trace_feat (id, evidence_ref, target, status)"
        " VALUES (?, 'evidence-ref', 'linkding', 'confirmed')",
        (fid,),
    )


def _mention(store, msg="MSG-1", feat="FEAT-1", content="asked about the feature"):
    store.conn.execute(
        "INSERT INTO trace_msg (id, content) VALUES (?, ?)", (msg, content)
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)",
        (msg, feat),
    )


def _req(store, rid="REQ-1", msg="MSG-1"):
    store.conn.execute(
        "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)", (rid, msg)
    )


def _tkt(store, tid="TKT-1", *, status="done", inc="INC-1", covers="REQ-1"):
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status) VALUES (?, ?, ?)",
        (tid, inc, status),
    )
    if covers is not None:
        store.conn.execute(
            "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)",
            (tid, covers),
        )


def _ac(store, aid="AC-1", tid="TKT-1", rid="REQ-1"):
    store.conn.execute(
        "INSERT INTO trace_ac (id, ticket_id, req_id) VALUES (?, ?, ?)",
        (aid, tid, rid),
    )


def _span(store, sid="SPAN-1", tid="TKT-1", files='["src/app.ts"]'):
    store.conn.execute(
        "INSERT INTO trace_span (id, ticket_id, files_json, status)"
        " VALUES (?, ?, ?, 'completed')",
        (sid, tid, files),
    )


def _chk(store, cid="CHK-1", aid="AC-1", result="pass",
         repro="pytest tests/test_app.py"):
    store.conn.execute(
        "INSERT INTO trace_chk (id, ac_id, result, repro_command, evidence)"
        " VALUES (?, ?, ?, ?, 'green')",
        (cid, aid, result, repro),
    )


def _qa(store, episode_id, *, msg="MSG-1", verdict="fail", qid_answer="answer text"):
    store.conn.execute(
        "INSERT INTO qa_log (episode_id, question, question_msg_id, answer,"
        " checker_verdict, created_at) VALUES (?, 'q', ?, ?, ?, '2026-01-01T00:00:00+00:00')",
        (episode_id, msg, qid_answer, verdict),
    )


def _reaches_step7(store):
    """A chain where steps 1-6 all pass — the discriminator decides."""
    _feat(store)
    _mention(store)
    _req(store)
    _tkt(store, status="done")
    _ac(store)
    _span(store, files='["src/app.ts"]')
    _chk(store, result="pass")


NO_COVERAGE = lambda sid: []  # noqa: E731
PASS_REPRO = lambda env: True  # noqa: E731
FAIL_REPRO = lambda env: False  # noqa: E731

ELICITABLE = {"FEAT-1": "core-crud"}        # table -> elicitable=True
NOT_ELICITABLE = {"FEAT-1": "admin-only"}   # table -> elicitable=False


def _attr(store, sid="SCEN-1", **kw):
    kw.setdefault("repro_runner", PASS_REPRO)
    kw.setdefault("coverage_provider", NO_COVERAGE)
    kw.setdefault("judge_fn", _raising_judge)
    return sa.attribute_failed_scenario(store, sid, **kw)


# --- per-branch fixture tests (§12.2 steps 1-8) ---------------------------------


def test_step1_communicated_no_elicitable_blames_planner(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    attr = _attr(store, feat_probe_categories=ELICITABLE)
    assert attr.primary.role == "planner"
    assert attr.primary.aspect == "elicitation"
    assert attr.primary.refs == ("FEAT-1",)
    assert attr.sink is None


def test_step1_communicated_no_not_elicitable_blames_explorer(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    attr = _attr(store, feat_probe_categories=NOT_ELICITABLE)
    assert attr.primary.role == "explorer"
    assert attr.primary.aspect == "prompt"
    assert attr.is_instrument_health


def test_step2_extracted_no_blames_planner(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _mention(store)
    _scen(store, ep)
    attr = _attr(store)
    assert (attr.primary.role, attr.primary.aspect) == (
        "planner", "requirement_extraction")
    assert "MSG-1" in attr.primary.refs


def test_step3_covered_no_blames_planner(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _mention(store)
    _req(store)  # a REQ exists but no ticket covers it
    _scen(store, ep)
    attr = _attr(store)
    assert (attr.primary.role, attr.primary.aspect) == ("planner", "coverage")
    assert "REQ-1" in attr.primary.refs


def test_step4_specified_no_blames_planner(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _mention(store)
    _req(store)
    _tkt(store, status="done")  # ticket covers REQ-1, but no AC specified
    _scen(store, ep)
    attr = _attr(store)
    assert (attr.primary.role, attr.primary.aspect) == (
        "planner", "acceptance_criteria")


def test_step5_implemented_no_blames_worker(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _mention(store)
    _req(store)
    _tkt(store, status="escalated")  # never closed normally
    _ac(store)
    _span(store, files="[]")          # empty diff
    _scen(store, ep)
    attr = _attr(store)
    assert attr.primary.role == "worker"
    assert attr.primary.aspect == "implementation"


def test_step6_verified_no_blames_verifier(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _mention(store)
    _req(store)
    _tkt(store, status="done")
    _ac(store)
    _span(store, files='["src/app.ts"]')
    _chk(store, result="fail")  # implemented but never verified PASS
    _scen(store, ep)
    attr = _attr(store)
    assert attr.primary.role == "verifier"
    assert attr.primary.aspect == "incomplete_verification"


def test_step7_repro_still_passes_blames_ac_quality(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    attr = _attr(store, repro_runner=PASS_REPRO)
    assert attr.primary.role == "planner"
    assert attr.primary.aspect == "ac_quality"
    assert attr.primary.refs == ("CHK-1",)


def test_step7_repro_now_fails_locates_breaking_ticket(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    # A later increment's worker regressed a different file in this episode.
    run = store.create_run("spec", 0, episode_id=ep, increment_index=2)
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status)"
        " VALUES ('TKT-break', 'INC-2', 'done')"
    )
    store.conn.execute(
        "INSERT INTO trace_span (id, run_id, ticket_id, files_json, status,"
        " increment_id, episode) VALUES ('SPAN-break', ?, 'TKT-break',"
        " '[\"src/regression.ts\"]', 'completed', 'INC-2', ?)",
        (run, str(ep)),
    )
    coverage = {"SCEN-1": ["src/regression.ts"]}
    attr = _attr(
        store,
        repro_runner=FAIL_REPRO,
        coverage_provider=lambda sid: coverage[sid],
    )
    assert attr.primary.role == "worker"
    assert attr.primary.aspect == "regression"
    assert "TKT-break" in attr.primary.refs
    assert "SPAN-break" in attr.primary.refs


def test_step7_regression_with_no_coverage_hit_falls_to_verifier(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    # Repro fails but no span touched the coverage-named file: the integration
    # pass on the increment owns it (step 7's second arm, VERIFIER).
    attr = _attr(
        store,
        repro_runner=FAIL_REPRO,
        coverage_provider=lambda sid: ["src/nobody-touched-this.ts"],
    )
    assert attr.primary.role == "verifier"
    assert attr.primary.aspect == "integration"


def test_step8_answer_contradiction_is_a_lookup(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    _qa(store, ep, verdict="fail")  # stored oracle verdict -> pure lookup
    # Primary stays AC-quality (planner); the answer fault is a contributing cause.
    attr = _attr(store, repro_runner=PASS_REPRO)  # judge raises -> not consulted
    assert attr.primary.role == "planner"
    answering = [c for c in attr.contributing
                 if (c.role, c.aspect) == ("explorer", "answering")]
    assert len(answering) == 1
    assert "MSG-1" in answering[0].refs


def test_passing_oracle_verdict_adds_no_contributing(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    _qa(store, ep, verdict="pass")
    attr = _attr(store, repro_runner=PASS_REPRO)
    assert attr.contributing == ()


# --- multi-cause -----------------------------------------------------------------


def test_multi_cause_populates_contributing(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    run = store.create_run("spec", 0, episode_id=ep, increment_index=2)
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status)"
        " VALUES ('TKT-break', 'INC-2', 'done')"
    )
    store.conn.execute(
        "INSERT INTO trace_span (id, run_id, ticket_id, files_json, status,"
        " increment_id, episode) VALUES ('SPAN-break', ?, 'TKT-break',"
        " '[\"src/regression.ts\"]', 'completed', 'INC-2', ?)",
        (run, str(ep)),
    )
    _qa(store, ep, verdict="fail")
    attr = _attr(
        store,
        repro_runner=FAIL_REPRO,
        coverage_provider=lambda sid: ["src/regression.ts"],
    )
    # Primary: the breaking worker; contributing: the explorer's wrong answer.
    assert (attr.primary.role, attr.primary.aspect) == ("worker", "regression")
    assert any((c.role, c.aspect) == ("explorer", "answering")
               for c in attr.contributing)


# --- instrument-health sink (R9) ------------------------------------------------


def test_explorer_attribution_sinks_to_instrument_health(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    result = sa.run_stage_a(
        store, ep,
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        judge_fn=_raising_judge, feat_probe_categories=NOT_ELICITABLE,
    )
    assert len(result.instrument_health_ids) == 1
    rows = store.conn.execute(
        "SELECT kind, payload_json FROM review_queue WHERE episode_id = ?", (ep,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["kind"] == sa.INSTRUMENT_HEALTH_KIND
    payload = json.loads(rows[0]["payload_json"])
    assert payload["primary"]["role"] == "explorer"
    # An explorer attribution carries no library family -> never an idea.
    assert payload["primary"]["family"] is None


def test_instrument_health_excluded_from_idea_candidates(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    result = sa.run_stage_a(
        store, ep,
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        judge_fn=_raising_judge, feat_probe_categories=NOT_ELICITABLE,
    )
    assert result.idea_candidates == ()
    assert len(result.attributions) == 1


def test_planner_attribution_writes_no_instrument_health(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    result = sa.run_stage_a(
        store, ep,
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        judge_fn=_raising_judge, feat_probe_categories=ELICITABLE,
    )
    assert result.instrument_health_ids == ()
    assert len(result.idea_candidates) == 1
    n = store.conn.execute("SELECT COUNT(*) AS n FROM review_queue").fetchone()["n"]
    assert n == 0


# --- MUST-tests -----------------------------------------------------------------


# Each entry: (label, builder, probe_categories, expected_role, expected_aspect).
# Step 7's two arms are exercised by the dedicated repro MUST-test below; these
# six are the lookup-only steps 1-6 that must hold with the judge unavailable.
_DETERMINISTIC_BRANCHES = [
    ("communicated_elicitable", lambda s, ep: (_feat(s), _scen(s, ep)),
     ELICITABLE, "planner", "elicitation"),
    ("communicated_not_elicitable", lambda s, ep: (_feat(s), _scen(s, ep)),
     NOT_ELICITABLE, "explorer", "prompt"),
    ("extracted_no", lambda s, ep: (_feat(s), _mention(s), _scen(s, ep)),
     None, "planner", "requirement_extraction"),
    ("covered_no", lambda s, ep: (_feat(s), _mention(s), _req(s), _scen(s, ep)),
     None, "planner", "coverage"),
    ("specified_no",
     lambda s, ep: (_feat(s), _mention(s), _req(s), _tkt(s, status="done"),
                    _scen(s, ep)),
     None, "planner", "acceptance_criteria"),
    ("implemented_no",
     lambda s, ep: (_feat(s), _mention(s), _req(s), _tkt(s, status="escalated"),
                    _ac(s), _span(s, files="[]"), _scen(s, ep)),
     None, "worker", "implementation"),
    ("verified_no",
     lambda s, ep: (_feat(s), _mention(s), _req(s), _tkt(s, status="done"),
                    _ac(s), _span(s), _chk(s, result="fail"), _scen(s, ep)),
     None, "verifier", "incomplete_verification"),
]


@pytest.mark.parametrize("label,builder,cats,role,aspect", _DETERMINISTIC_BRANCHES)
def test_stage_a_is_deterministic_without_llm(
    store, monkeypatch, label, builder, cats, role, aspect
):
    """MUST: attribution steps 1-6 return the correct {primary, contributing} on
    every branch with the judge seam mocked to RAISE on call — the LLM is never
    the attributor; it may run only for the two micro-judgments (here suppressed
    by in-taxonomy categories / no Q&A)."""
    monkeypatch.setattr(sa, "run_judge", _raising_judge)
    ep = store.create_episode("linkding", "sha256:x", 0)
    builder(store, ep)
    # No judge_fn passed: the default module run_judge (now raising) would fire if
    # the path were not deterministic.
    attr = sa.attribute_failed_scenario(
        store, "SCEN-1",
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        feat_probe_categories=cats,
    )
    assert (attr.primary.role, attr.primary.aspect) == (role, aspect), label
    assert attr.contributing == ()


def test_stage_a_repro_reexecution(store, monkeypatch):
    """MUST: step 7 mechanically re-executes the stored CHK repro envelope — the
    discriminator is the re-execution result, not an LLM guess."""
    monkeypatch.setattr(sa, "run_judge", _raising_judge)
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)

    seen = []

    def recording_repro(env):
        seen.append(env)
        return True  # repro STILL passes -> AC-quality

    attr = sa.attribute_failed_scenario(
        store, "SCEN-1",
        repro_runner=recording_repro, coverage_provider=NO_COVERAGE,
    )
    # The stored envelope was actually handed to the runner (command + result).
    assert len(seen) == 1
    assert seen[0].chk_id == "CHK-1"
    assert seen[0].repro_command == "pytest tests/test_app.py"
    assert seen[0].recorded_result == "pass"
    assert (attr.primary.role, attr.primary.aspect) == ("planner", "ac_quality")

    # Same chain, repro now FAILS -> regression located by the coverage trace.
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status)"
        " VALUES ('TKT-break', 'INC-2', 'done')"
    )
    run = store.create_run("spec", 0, episode_id=ep, increment_index=2)
    store.conn.execute(
        "INSERT INTO trace_span (id, run_id, ticket_id, files_json, status,"
        " increment_id, episode) VALUES ('SPAN-break', ?, 'TKT-break',"
        " '[\"src/regression.ts\"]', 'completed', 'INC-2', ?)",
        (run, str(ep)),
    )
    attr2 = sa.attribute_failed_scenario(
        store, "SCEN-1",
        repro_runner=FAIL_REPRO,
        coverage_provider=lambda sid: ["src/regression.ts"],
    )
    assert (attr2.primary.role, attr2.primary.aspect) == ("worker", "regression")
    assert "TKT-break" in attr2.primary.refs


def test_stage_a_attribution_stable(store, monkeypatch):
    """MUST: the same trajectory fixture yields byte-identical attribution across
    repeated runs."""
    monkeypatch.setattr(sa, "run_judge", _raising_judge)
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    run = store.create_run("spec", 0, episode_id=ep, increment_index=2)
    store.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id, status)"
        " VALUES ('TKT-break', 'INC-2', 'done')"
    )
    store.conn.execute(
        "INSERT INTO trace_span (id, run_id, ticket_id, files_json, status,"
        " increment_id, episode) VALUES ('SPAN-break', ?, 'TKT-break',"
        " '[\"src/regression.ts\"]', 'completed', 'INC-2', ?)",
        (run, str(ep)),
    )
    _qa(store, ep, verdict="fail")
    kw = dict(
        repro_runner=FAIL_REPRO,
        coverage_provider=lambda sid: ["src/regression.ts"],
    )
    first = sa.attribute_failed_scenario(store, "SCEN-1", **kw).to_json()
    second = sa.attribute_failed_scenario(store, "SCEN-1", **kw).to_json()
    assert first == second


# --- the two micro-judgments only fire where §12.2 allows -----------------------


def test_out_of_taxonomy_feat_fires_the_elicitability_judge(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep)
    calls = []

    def fake_judge(prompt, schema, model, **kwargs):
        calls.append(prompt)
        return SimpleNamespace(output={"elicitable": True})

    attr = sa.attribute_failed_scenario(
        store, "SCEN-1",
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        feat_probe_categories={"FEAT-1": "totally-uncategorized"},
        judge_fn=fake_judge,
    )
    assert len(calls) == 1  # the LLM fired exactly once, for elicitability
    assert (attr.primary.role, attr.primary.aspect) == ("planner", "elicitation")


def test_missing_oracle_verdict_fires_the_answer_judge(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _reaches_step7(store)
    _scen(store, ep)
    _qa(store, ep, verdict=None)  # no oracle check ran at answer time
    calls = []

    def fake_judge(prompt, schema, model, **kwargs):
        calls.append(prompt)
        return SimpleNamespace(output={"contradicts": True})

    attr = sa.attribute_failed_scenario(
        store, "SCEN-1",
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        judge_fn=fake_judge,
    )
    assert len(calls) == 1  # the answer micro-judgment fired
    assert any((c.role, c.aspect) == ("explorer", "answering")
               for c in attr.contributing)


# --- the coverage substrate reader (this unit's deliverable) --------------------


def test_read_merged_coverage_parses_per_scenario_file_lists(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(
        json.dumps({"SCEN-1": ["src/b.ts", "src/a.ts", "src/a.ts"],
                    "SCEN-2": []}),
        encoding="utf-8",
    )
    merged = sa.read_merged_coverage(str(path))
    assert merged["SCEN-1"] == ["src/a.ts", "src/b.ts"]  # deduped + sorted
    assert merged["SCEN-2"] == []


def test_read_merged_coverage_rejects_non_object(tmp_path):
    path = tmp_path / "coverage.json"
    path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
    with pytest.raises(sa.StageAError):
        sa.read_merged_coverage(str(path))


# --- the episode pass -----------------------------------------------------------


def test_run_stage_a_over_an_episode(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    # SCEN-1: a planner AC-quality fault (idea candidate).
    _reaches_step7(store)
    _scen(store, ep, sid="SCEN-1")
    # SCEN-2: an un-elicitable explorer prompt gap (instrument-health sink).
    _feat(store, fid="FEAT-2")
    _scen(store, ep, feat="FEAT-2", sid="SCEN-2")
    # A passing scenario is not on the worklist.
    _feat(store, fid="FEAT-3")
    _scen(store, ep, feat="FEAT-3", sid="SCEN-3", result="pass")

    result = sa.run_stage_a(
        store, ep,
        repro_runner=PASS_REPRO, coverage_provider=NO_COVERAGE,
        judge_fn=_raising_judge,
        feat_probe_categories={"FEAT-1": "core-crud", "FEAT-2": "admin-only"},
    )
    assert {a.scen_id for a in result.attributions} == {"SCEN-1", "SCEN-2"}
    assert len(result.idea_candidates) == 1
    assert result.idea_candidates[0].scen_id == "SCEN-1"
    assert len(result.instrument_health_ids) == 1


def test_failed_scenarios_worklist_is_id_ordered(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep, sid="SCEN-2")
    _scen(store, ep, sid="SCEN-1")
    _scen(store, ep, sid="SCEN-3", result="pass")
    assert sa.failed_scenarios(store, ep) == ["SCEN-1", "SCEN-2"]


def test_attribute_rejects_a_passing_scenario(store):
    ep = store.create_episode("linkding", "sha256:x", 0)
    _feat(store)
    _scen(store, ep, result="pass")
    with pytest.raises(sa.StageAError):
        _attr(store)


def test_reflector_package_exposes_stage_a():
    assert reflector.__doc__ is not None
