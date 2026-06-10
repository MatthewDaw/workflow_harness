"""plan-003 U7: grader settlement and report (R7 dual-app half, R20-R23).

Offline: judge comparisons replay recorded fixtures through the judge seam
(zero quota, zero subprocess); drivers/resolvers/http are scripted fakes; the
dev server in the hold-open tests is the tiny strict-bind python TCP script
(the test_devserver precedent). The live mini-settlement is docker-required
and additionally needs playwright installed.

## Conformance

Test-scenario / invariant -> test mapping (plan-003 U7):

- must-tier failure with panel disagreement escalates to panel and records
  judge metadata: ``test_low_confidence_must_tier_escalates_to_panel_and_records_metadata``
  (+ ``test_high_confidence_single_judgment_never_panels``,
  ``test_panel_tie_defaults_to_fail``)
- default-fail framing applied (prompt fixture asserts wording):
  ``test_comparison_prompt_default_fail_framing_binary_and_cot``
- deterministic assertions first — harness outcomes never reach a judge
  (R20): ``test_identical_final_states_pass_deterministically_no_judge``,
  ``test_clone_feature_absent_fails_deterministically``,
  ``test_clone_failed_execution_fails_deterministically``
- ``invalid`` scenarios excluded from denominator:
  ``test_target_invalid_verdict_excluded_from_denominator``
- UAT-accepted-but-scenario-failed lands in its report section with typed
  tag: ``test_uat_accepted_but_scenario_failed_lands_in_report_section``
- report rows carry runnable trace CLI strings:
  ``test_report_rows_carry_runnable_trace_cli_strings``
- SCEN keys complete (R22, incl. judge-input payload persistence):
  ``test_scen_rows_keyed_and_carry_judge_inputs``,
  ``test_scen_insert_without_keys_rejected``
- unreached-frontier bucket, full denominator (R23):
  ``test_unreached_frontier_bucket_full_denominator``
- baseline property checklist probes the clone's endpoints (R21):
  ``test_baseline_probes_check_clone_endpoints``,
  ``test_baseline_probe_validation``
- metamorphic tier runs target-free (R21):
  ``test_metamorphic_identity_and_presence_checks``,
  ``test_metamorphic_check_validation``
- clone dev server hold-open across UAT and settlement (R7):
  ``test_hold_open_defers_stop_until_release``,
  ``test_hold_open_requires_running_server``,
  ``test_run_settlement_requires_held_clone_server``
- report renders deterministically from a synthetic episode fixture (the U7
  verification): ``test_report_renders_deterministically``
- full settlement assembly over a store-backed episode:
  ``test_run_settlement_offline_end_to_end``
- live mini-settlement over a 3-scenario manifest set (docker-required):
  ``test_live_mini_settlement_three_scenarios``
"""

from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import sys

import pytest

from agent_families.grading.scenarios import (
    Observation,
    Resolution,
    ResolutionCache,
    ScenarioResult,
    StepEvidence,
    StepResult,
    parse_manifest,
)
from agent_families.grading.settle import (
    BaselineProbe,
    COMPARISON_SCHEMA,
    MetamorphicCheck,
    ScenarioVerdict,
    SettleConfig,
    SettleError,
    SettlementPlan,
    UAT_DIVERGENCE_TAG,
    a11y_diff,
    assemble_report,
    compare_scenario,
    comparison_prompt,
    panelist_prompt,
    render_report,
    run_baseline_probes,
    run_metamorphic_checks,
    run_settlement,
    scen_id,
    store_report,
    unreached_feat_ids,
    write_scen_rows,
)
from agent_families.judge import write_fixture
from agent_families.pipeline.devserver import (
    DevServer,
    DevServerConfig,
    DevServerError,
)
from agent_families.store import Store, StoreError

# --- a11y tree fixtures (static; the test_scenarios captured-once shapes) -------


def n(role: str, name: str = "", *children: dict) -> dict:
    node: dict = {"role": role, "name": name}
    if children:
        node["children"] = list(children)
    return node


def bookmark_row(title: str, url_text: str) -> dict:
    return n(
        "listitem",
        "",
        n("link", title),
        n("text", url_text),
        n("link", "Edit"),
    )


def app_tree(rows: list[dict], *, extra: list | None = None) -> dict:
    return n(
        "document",
        f"Bookmarks ({len(rows)})",
        n("banner", "", n("link", "linkding")),
        n("navigation", "", n("link", "Bookmarks"), n("link", "Tags (3)")),
        n(
            "main",
            "",
            n("searchbox", "Search"),
            n("button", "Add bookmark"),
            n("list", "", *rows),
            *(extra or []),
        ),
    )


ROWS_A = [bookmark_row("Python docs", "docs.python.org")]
ROWS_B = [
    bookmark_row("Rust book", "doc.rust-lang.org"),
    bookmark_row("Go tour", "go.dev"),
]

TREE_PLAIN = app_tree(ROWS_A)
TREE_MUTATED = app_tree(ROWS_B)  # content-only mutation of TREE_PLAIN
TREE_EXTRA_BUTTON = app_tree(ROWS_A, extra=[n("button", "Bulk edit")])


# --- scripted seams ----------------------------------------------------------------


class FakeDriver:
    """Scripted state-machine driver (the test_scenarios precedent)."""

    def __init__(self, states: list[tuple[dict, str]]):
        self.states = states
        self.idx = 0
        self.executed: list[dict] = []

    def observe(self) -> Observation:
        tree, url = self.states[min(self.idx, len(self.states) - 1)]
        return Observation(a11y_tree=tree, url=url)

    def execute(self, action: dict) -> None:
        self.executed.append(action)
        self.idx = min(self.idx + 1, len(self.states) - 1)

    def screenshot(self) -> bytes:
        return b"\x89PNG-fake"


def click(target: str) -> dict:
    return {"action": "click", "selector": f"text={target}", "args": []}


class ScriptedResolver:
    """ResolveFn fake: per-step scripted outcomes (lists pop per call);
    unscripted steps resolve to a click on their own text."""

    def __init__(self, script: dict | None = None):
        self.script = dict(script or {})
        self.calls: list[str] = []

    def __call__(self, step_text: str, a11y_tree: dict) -> Resolution:
        self.calls.append(step_text)
        entry = self.script.get(step_text)
        if entry is None:
            return Resolution("resolved", click(step_text), "scripted default")
        if isinstance(entry, list):
            return entry.pop(0)
        return entry


class FakeHttp:
    """Scripted (method, path-suffix) -> (status, body) http seam."""

    def __init__(self, script: dict[tuple[str, str], tuple[int, str]]):
        self.script = script
        self.calls: list[tuple[str, str]] = []

    def __call__(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url))
        for (m, suffix), response in self.script.items():
            if m == method and url.endswith(suffix):
                return response
        return 200, "ok"


def envelope(output: dict) -> dict:
    return {
        "structured_output": output,
        "is_error": False,
        "total_cost_usd": 0.0,
        "duration_ms": 1,
    }


def judgment(verdict: str, confidence: str, reasoning: str = "cot") -> dict:
    return {"reasoning": reasoning, "verdict": verdict, "confidence": confidence}


def make_manifest(
    steps,
    *,
    scenario_id="FEAT-1/add-bookmark",
    feat_id="FEAT-1",
    tier="must",
    title="Add a bookmark",
    expected="The bookmark appears in the list",
):
    return parse_manifest(
        {
            "scenario_id": scenario_id,
            "feat_id": feat_id,
            "title": title,
            "steps": steps,
            "expected_outcome": expected,
            "tier": tier,
        }
    )


def completed_result(
    app: str,
    final_tree: dict,
    *,
    scenario_id="FEAT-1/add-bookmark",
    feat_id="FEAT-1",
    stale=False,
) -> ScenarioResult:
    evidence = StepEvidence(
        a11y_tree=final_tree, url=f"http://{app}/bookmarks", screenshot=b"png"
    )
    return ScenarioResult(
        scenario_id=scenario_id,
        feat_id=feat_id,
        app=app,
        status="completed",
        steps=[
            StepResult(
                index=0,
                text="step",
                status="cached",
                action=None,
                resolver_calls=0,
                evidence=evidence,
                failure=None,
            )
        ],
        evidence_stale=stale,
    )


def broken_result(
    app: str, status: str, reason: str, *, scenario_id="FEAT-1/add-bookmark"
) -> ScenarioResult:
    return ScenarioResult(
        scenario_id=scenario_id,
        feat_id="FEAT-1",
        app=app,
        status=status,
        failure_reason=reason,
    )


def config_for(tmp_path, *, panel_size=3, model="sonnet") -> SettleConfig:
    return SettleConfig(
        model=model,
        max_retries=0,
        panel_size=panel_size,
        mode="replay",
        fixtures_dir=tmp_path,
    )


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    yield s
    s.close()


def add_feat(store: Store, fid: str, target: str = "linkding") -> None:
    store.conn.execute(
        "INSERT INTO trace_feat (id, evidence_ref, target, digest, status)"
        " VALUES (?, 'evidence/x.json', ?, 'sha256:abc', 'confirmed')",
        (fid, target),
    )


def mention_feat(store: Store, fid: str, msg_id: str = "MSG-e1-open") -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO trace_msg (id, content) VALUES (?, '')",
        (msg_id,),
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)",
        (msg_id, fid),
    )


# --- a11y diff and the deterministic tier (R20) -----------------------------------


def test_a11y_diff_normalizes_content_and_detects_structure():
    same = a11y_diff(TREE_PLAIN, TREE_MUTATED)
    assert same == {"target_only": [], "clone_only": []}, (
        "content-only mutation must produce an empty structural diff"
    )
    diff = a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)
    assert any("bulk edit" in s for s in diff["target_only"])
    assert diff["clone_only"] == []


def test_identical_final_states_pass_deterministically_no_judge(tmp_path):
    # fixtures_dir is EMPTY: any judge call would raise JudgeFixtureMissing.
    manifest = make_manifest(["Add a bookmark"])
    verdict = compare_scenario(
        manifest,
        completed_result("target", TREE_PLAIN),
        completed_result("clone", TREE_MUTATED),
        config_for(tmp_path),
    )
    assert verdict.verdict == "pass"
    assert verdict.judge_mode == "deterministic"
    assert verdict.judge_input == {}
    assert verdict.failure is None


def test_clone_feature_absent_fails_deterministically(tmp_path):
    manifest = make_manifest(["Add a bookmark"])
    verdict = compare_scenario(
        manifest,
        completed_result("target", TREE_PLAIN),
        broken_result("clone", "feature_absent", "element_absent"),
        config_for(tmp_path),
    )
    assert verdict.verdict == "fail"
    assert verdict.judge_mode == "deterministic"
    assert verdict.failure == "feature_absent"


def test_clone_failed_execution_fails_deterministically(tmp_path):
    manifest = make_manifest(["Add a bookmark"])
    verdict = compare_scenario(
        manifest,
        completed_result("target", TREE_PLAIN),
        broken_result("clone", "failed", "post_assertion_failed"),
        config_for(tmp_path),
    )
    assert verdict.verdict == "fail"
    assert verdict.judge_mode == "deterministic"
    assert verdict.failure == "post_assertion_failed"


def test_target_invalid_verdict_excluded_from_denominator(tmp_path):
    manifest = make_manifest(["Add a bookmark"])
    invalid = compare_scenario(
        manifest,
        broken_result("target", "invalid", "heal_failed: gone"),
        completed_result("clone", TREE_PLAIN),
        config_for(tmp_path),
    )
    assert invalid.verdict == "invalid"
    assert invalid.judge_mode == "deterministic"
    assert "target_invalid" in invalid.failure

    passing = compare_scenario(
        make_manifest(["s"], scenario_id="FEAT-2/s", feat_id="FEAT-2"),
        completed_result("target", TREE_PLAIN, scenario_id="FEAT-2/s", feat_id="FEAT-2"),
        completed_result("clone", TREE_PLAIN, scenario_id="FEAT-2/s", feat_id="FEAT-2"),
        config_for(tmp_path),
    )
    report = assemble_report(
        episode_id=1,
        snapshot_id=0,
        target="linkding",
        verdicts=[invalid, passing],
        scen_ids={
            invalid.scenario_id: scen_id(1, invalid.scenario_id),
            passing.scenario_id: scen_id(1, passing.scenario_id),
        },
    )
    assert report["score"]["scoreable"] == 1
    assert report["score"]["invalid_excluded"] == 1
    assert report["score"]["overall"] == 1.0
    assert report["failed_scenarios"] == [], (
        "invalid is apparatus defect, never a clone failure"
    )


# --- judge protocol: default-fail, single, panel (R20) ------------------------------


def test_comparison_prompt_default_fail_framing_binary_and_cot():
    diff = a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)
    prompt = comparison_prompt("Add a bookmark", "bookmark listed", "must", diff)
    assert "Default to fail" in prompt
    assert "pass or fail" in prompt, "binary verdict framing"
    assert "Reason step by step" in prompt, "CoT framing"
    assert "unsure" in prompt, "uncertainty falls to fail"
    # Deterministic prompt: same inputs, same text (fixture-key discipline).
    assert prompt == comparison_prompt(
        "Add a bookmark", "bookmark listed", "must", diff
    )
    # Panelist prompts are pairwise distinct and embed the base prompt.
    p1, p2 = panelist_prompt(1, 3, prompt), panelist_prompt(2, 3, prompt)
    assert p1 != p2 and prompt in p1 and prompt in p2


def _diffing_pair(scenario_id="FEAT-1/add-bookmark", feat_id="FEAT-1"):
    return (
        completed_result(
            "target", TREE_EXTRA_BUTTON, scenario_id=scenario_id, feat_id=feat_id
        ),
        completed_result(
            "clone", TREE_PLAIN, scenario_id=scenario_id, feat_id=feat_id
        ),
    )


def test_high_confidence_single_judgment_never_panels(tmp_path):
    manifest = make_manifest(["Add a bookmark"])
    target_result, clone_result = _diffing_pair()
    diff = a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)
    base = comparison_prompt(
        manifest.title, manifest.expected_outcome, manifest.tier, diff
    )
    # ONLY the single-judgment fixture exists: a panel call would raise.
    write_fixture(
        tmp_path, base, COMPARISON_SCHEMA, "sonnet",
        envelope(judgment("fail", "high", "bulk edit missing on the clone")),
    )
    verdict = compare_scenario(
        manifest, target_result, clone_result, config_for(tmp_path)
    )
    assert verdict.verdict == "fail"
    assert verdict.judge_mode == "single"
    assert verdict.judge_metadata["confidence"] == "high"
    assert verdict.judge_metadata["reasoning"]
    assert verdict.judge_input["diff"] == diff, (
        "the judge-input payload must persist for replay re-judging (R22)"
    )
    assert verdict.failure == "judged_different"


def test_low_confidence_must_tier_escalates_to_panel_and_records_metadata(tmp_path):
    manifest = make_manifest(["Add a bookmark"], tier="must")
    target_result, clone_result = _diffing_pair()
    diff = a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)
    base = comparison_prompt(
        manifest.title, manifest.expected_outcome, manifest.tier, diff
    )
    write_fixture(
        tmp_path, base, COMPARISON_SCHEMA, "sonnet",
        envelope(judgment("pass", "low", "hard to tell")),
    )
    panel_votes = [("fail", "high"), ("pass", "low"), ("fail", "high")]
    for k, (v, c) in enumerate(panel_votes, start=1):
        write_fixture(
            tmp_path, panelist_prompt(k, 3, base), COMPARISON_SCHEMA, "sonnet",
            envelope(judgment(v, c, f"panelist {k}")),
        )
    verdict = compare_scenario(
        manifest, target_result, clone_result, config_for(tmp_path, panel_size=3)
    )
    assert verdict.tier == "must"
    assert verdict.verdict == "fail", "2/3 fail votes: majority fails"
    assert verdict.judge_mode == "panel"
    meta = verdict.judge_metadata
    assert meta["initial"]["confidence"] == "low"
    assert meta["tally"] == {"fail": 2, "pass": 1}
    assert meta["disagreement"] is True
    assert [v["verdict"] for v in meta["votes"]] == ["fail", "pass", "fail"]


def test_panel_tie_defaults_to_fail(tmp_path):
    manifest = make_manifest(["Add a bookmark"])
    target_result, clone_result = _diffing_pair()
    diff = a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)
    base = comparison_prompt(
        manifest.title, manifest.expected_outcome, manifest.tier, diff
    )
    write_fixture(
        tmp_path, base, COMPARISON_SCHEMA, "sonnet",
        envelope(judgment("pass", "low")),
    )
    for k, v in ((1, "pass"), (2, "fail")):
        write_fixture(
            tmp_path, panelist_prompt(k, 2, base), COMPARISON_SCHEMA, "sonnet",
            envelope(judgment(v, "high")),
        )
    verdict = compare_scenario(
        manifest, target_result, clone_result, config_for(tmp_path, panel_size=2)
    )
    assert verdict.verdict == "fail", "a tied panel falls to fail (default-fail)"
    assert verdict.judge_metadata["disagreement"] is True


def test_settle_config_rejects_panel_of_one(tmp_path):
    with pytest.raises(SettleError, match="panel_size"):
        SettleConfig(model="sonnet", max_retries=0, panel_size=1)


# --- baseline property checklist (R21) ----------------------------------------------


def test_baseline_probes_check_clone_endpoints():
    http = FakeHttp(
        {
            ("POST", "/api/bookmarks/"): (200, '{"ok": true}'),
            ("GET", "/settings"): (200, "<h1>Settings</h1>"),
            ("GET", "/missing"): (
                500,
                "Traceback (most recent call last)\n  File app.py",
            ),
        }
    )
    results = run_baseline_probes(
        "http://127.0.0.1:4173",
        [
            BaselineProbe(
                kind="server_side_validation",
                path="/api/bookmarks/",
                method="POST",
                payload={"url": ""},
            ),
            BaselineProbe(kind="authz_direct_access", path="/settings"),
            BaselineProbe(kind="no_stack_trace", path="/missing"),
        ],
        http,
    )
    by_kind = {r["probe"]: r for r in results}
    assert not by_kind["server_side_validation"]["passed"], (
        "a 200 for an invalid payload means validation is client-side only"
    )
    assert not by_kind["authz_direct_access"]["passed"], (
        "a protected path served without auth fails the checklist"
    )
    assert not by_kind["no_stack_trace"]["passed"]
    assert "Traceback" in by_kind["no_stack_trace"]["detail"]
    assert all(c[1].startswith("http://127.0.0.1:4173/") for c in http.calls), (
        "baseline probes hit the clone's own endpoints (R21)"
    )

    healthy = FakeHttp(
        {
            ("POST", "/api/bookmarks/"): (400, '{"url": ["required"]}'),
            ("GET", "/settings"): (302, ""),
            ("GET", "/missing"): (404, "Not found"),
        }
    )
    results = run_baseline_probes(
        "http://127.0.0.1:4173",
        [
            BaselineProbe(
                kind="server_side_validation",
                path="/api/bookmarks/",
                method="POST",
                payload={"url": ""},
            ),
            BaselineProbe(kind="authz_direct_access", path="/settings"),
            BaselineProbe(kind="no_stack_trace", path="/missing"),
        ],
        healthy,
    )
    assert all(r["passed"] for r in results)


def test_baseline_probe_validation():
    with pytest.raises(SettleError, match="kind"):
        BaselineProbe(kind="sql_injection", path="/x")
    with pytest.raises(SettleError, match="path"):
        BaselineProbe(kind="no_stack_trace", path="x")
    with pytest.raises(SettleError, match="payload"):
        BaselineProbe(kind="server_side_validation", path="/api/", method="POST")


# --- metamorphic tier (R21) -----------------------------------------------------------


def test_metamorphic_identity_and_presence_checks():
    # Refresh idempotence: post-step trees differ only in content -> identical
    # fingerprints -> pass. Edit-then-revert against a STRUCTURAL divergence
    # -> fail. Create-then-list passes via its deterministic post-assertion.
    refresh = MetamorphicCheck(
        name="refresh-idempotence",
        kind="refresh_idempotence",
        manifest=make_manifest(
            ["Open the bookmarks page", "Reload the bookmarks page"],
            scenario_id="META/refresh",
        ),
        identity_steps=(0, 1),
    )
    driver = FakeDriver(
        [
            (TREE_PLAIN, "http://clone/bookmarks"),
            (TREE_PLAIN, "http://clone/bookmarks"),
            (TREE_MUTATED, "http://clone/bookmarks"),
        ]
    )
    cache = ResolutionCache()
    results = run_metamorphic_checks([refresh], driver, cache, ScriptedResolver())
    assert results[0]["passed"], "content-only drift must not fail idempotence"

    diverging = FakeDriver(
        [
            (TREE_PLAIN, "http://clone/bookmarks"),
            (TREE_PLAIN, "http://clone/bookmarks"),
            (TREE_EXTRA_BUTTON, "http://clone/bookmarks"),
        ]
    )
    revert = MetamorphicCheck(
        name="edit-then-revert",
        kind="edit_then_revert_identity",
        manifest=make_manifest(
            ["Edit the bookmark title", "Revert the bookmark title"],
            scenario_id="META/revert",
        ),
        identity_steps=(0, 1),
    )
    results = run_metamorphic_checks(
        [revert], diverging, ResolutionCache(), ScriptedResolver()
    )
    assert not results[0]["passed"]
    assert "diverge" in results[0]["detail"]

    create = MetamorphicCheck(
        name="create-then-list",
        kind="create_then_list",
        manifest=make_manifest(
            [
                "Add a bookmark for go.dev",
                {
                    "step": "Open the bookmarks page",
                    "post_assertion": {
                        "kind": "node_present",
                        "role": "link",
                        "name": "Go tour",
                    },
                },
            ],
            scenario_id="META/create-list",
        ),
    )
    lister = FakeDriver(
        [
            (TREE_PLAIN, "http://clone/bookmarks/new"),
            (TREE_PLAIN, "http://clone/bookmarks"),
            (TREE_MUTATED, "http://clone/bookmarks"),  # contains "Go tour"
        ]
    )
    results = run_metamorphic_checks(
        [create], lister, ResolutionCache(), ScriptedResolver()
    )
    assert results[0]["passed"]
    assert results[0]["kind"] == "create_then_list"


def test_metamorphic_check_validation():
    manifest = make_manifest(["one step"], scenario_id="META/x")
    with pytest.raises(SettleError, match="identity_steps"):
        MetamorphicCheck(name="x", kind="refresh_idempotence", manifest=manifest)
    with pytest.raises(SettleError, match="identity_steps"):
        MetamorphicCheck(
            name="x",
            kind="refresh_idempotence",
            manifest=manifest,
            identity_steps=(0, 5),
        )
    with pytest.raises(SettleError, match="post_assertion"):
        MetamorphicCheck(name="x", kind="create_then_list", manifest=manifest)
    with pytest.raises(SettleError, match="kind"):
        MetamorphicCheck(name="x", kind="monotonic", manifest=manifest)


# --- SCEN persistence (R22) -------------------------------------------------------------


def _verdict(
    scenario_id: str,
    feat_id: str,
    verdict: str,
    *,
    tier="must",
    judge_mode="deterministic",
    judge_metadata=None,
    judge_input=None,
    failure=None,
    stale=False,
) -> ScenarioVerdict:
    return ScenarioVerdict(
        scenario_id=scenario_id,
        feat_id=feat_id,
        tier=tier,
        verdict=verdict,
        judge_mode=judge_mode,
        judge_metadata=judge_metadata or {"reason": "test"},
        judge_input=judge_input or {},
        failure=failure,
        evidence_stale=stale,
    )


def test_scen_rows_keyed_and_carry_judge_inputs(store):
    add_feat(store, "FEAT-1")
    add_feat(store, "FEAT-2")
    episode = store.create_episode("linkding", "sha256:abc", 0)
    diff_payload = {"diff": a11y_diff(TREE_EXTRA_BUTTON, TREE_PLAIN)}
    verdicts = [
        _verdict("FEAT-1/add", "FEAT-1", "pass"),
        _verdict(
            "FEAT-2/search",
            "FEAT-2",
            "fail",
            judge_mode="single",
            judge_input=diff_payload,
            failure="judged_different",
        ),
    ]
    ids = write_scen_rows(store, episode, 0, verdicts)
    assert ids == {
        "FEAT-1/add": f"SCEN-e{episode}-FEAT-1-add",
        "FEAT-2/search": f"SCEN-e{episode}-FEAT-2-search",
    }
    rows = store.conn.execute(
        "SELECT * FROM trace_scen ORDER BY id"
    ).fetchall()
    assert len(rows) == 2
    for row in rows:
        assert row["episode_id"] == episode and row["snapshot_id"] == 0, (
            "SCEN rows are keyed (target, episode, snapshot) — R22"
        )
        assert row["tier"] == "must"
        assert row["judge_mode"] in ("deterministic", "single")
    judged = next(r for r in rows if r["id"].endswith("search"))
    assert json.loads(judged["judge_input_json"]) == diff_payload, (
        "replay re-judging needs the original judge inputs (R22)"
    )
    assert judged["result"] == "fail"
    assert judged["evidence"] == "judged_different"


def test_scen_insert_without_keys_rejected(store):
    add_feat(store, "FEAT-1")
    with pytest.raises(sqlite3.IntegrityError, match="episode_id and snapshot_id"):
        store.conn.execute(
            "INSERT INTO trace_scen (id, feat_id, result) VALUES (?, ?, ?)",
            ("SCEN-bare", "FEAT-1", "pass"),
        )


def test_unreached_frontier_query(store):
    add_feat(store, "FEAT-1")
    add_feat(store, "FEAT-2")
    add_feat(store, "FEAT-3")
    store.conn.execute(
        "UPDATE trace_feat SET status = 'deprecated' WHERE id = 'FEAT-3'"
    )
    mention_feat(store, "FEAT-1")
    assert unreached_feat_ids(store, "linkding") == {"FEAT-2"}, (
        "mentioned and deprecated FEATs are not unreached"
    )


# --- report assembly (R23) -----------------------------------------------------------


def _report_inputs(episode_id=1):
    verdicts = [
        _verdict("FEAT-1/add", "FEAT-1", "pass"),
        _verdict(
            "FEAT-2/search",
            "FEAT-2",
            "fail",
            tier="should",
            judge_mode="panel",
            failure="judged_different",
            stale=True,
        ),
        _verdict("FEAT-3/tags", "FEAT-3", "fail", failure="feature_absent"),
        _verdict("FEAT-4/import", "FEAT-4", "invalid", failure="target_invalid: x"),
    ]
    scen_ids = {v.scenario_id: scen_id(episode_id, v.scenario_id) for v in verdicts}
    return verdicts, scen_ids


def test_uat_accepted_but_scenario_failed_lands_in_report_section():
    verdicts, scen_ids = _report_inputs()
    report = assemble_report(
        episode_id=1,
        snapshot_id=0,
        target="linkding",
        verdicts=verdicts,
        scen_ids=scen_ids,
        uat_accepted_feats={"FEAT-2", "FEAT-1"},
    )
    section = report["uat_divergence"]
    assert [r["scenario_id"] for r in section] == ["FEAT-2/search"], (
        "only FAILED scenarios whose FEAT was UAT-accepted diverge"
    )
    assert UAT_DIVERGENCE_TAG in section[0]["tags"], "typed tag (R23)"
    passing = next(r for r in report["scenarios"] if r["feat_id"] == "FEAT-1")
    assert UAT_DIVERGENCE_TAG not in passing["tags"]


def test_unreached_frontier_bucket_full_denominator():
    verdicts, scen_ids = _report_inputs()
    report = assemble_report(
        episode_id=1,
        snapshot_id=0,
        target="linkding",
        verdicts=verdicts,
        scen_ids=scen_ids,
        unreached_feats={"FEAT-3"},
    )
    bucket = report["unreached_frontier"]
    assert bucket["feat_ids"] == ["FEAT-3"]
    assert bucket["scen_ids"] == [scen_ids["FEAT-3/tags"]]
    # Full-denominator fairness: the unreached scenario still counts.
    assert report["score"]["scoreable"] == 3
    assert report["score"]["passed"] == 1
    assert report["score"]["overall"] == pytest.approx(1 / 3)
    assert report["score"]["by_tier"] == {
        "must": {"passed": 1, "total": 2},
        "should": {"passed": 0, "total": 1},
    }


def test_report_rows_carry_runnable_trace_cli_strings():
    verdicts, scen_ids = _report_inputs(episode_id=7)
    report = assemble_report(
        episode_id=7,
        snapshot_id=2,
        target="linkding",
        verdicts=verdicts,
        scen_ids=scen_ids,
    )
    for row in report["scenarios"]:
        assert row["trace_command"] == f"af trace chain {row['scen_id']}"
    assert len(report["failed_scenarios"]) == 2
    for row in report["failed_scenarios"]:
        assert row["trace_command"].startswith("af trace chain SCEN-e7-")
    assert report["commands"]["report"] == "af episode report 7"
    # Instrument health is a real section even before U8 populates it (R23).
    assert "mutation_audits" in report["instrument_health"]
    # Target-side heal staleness surfaces (R19 -> settlement report).
    assert report["evidence_stale_feats"] == ["FEAT-2"]


def test_report_renders_deterministically():
    # Two assemblies from independently-built inputs (sets in scrambled
    # order) must render byte-identically — the U7 verification.
    renders = []
    for unreached in ({"FEAT-3", "FEAT-2"}, {"FEAT-2", "FEAT-3"}):
        verdicts, scen_ids = _report_inputs()
        report = assemble_report(
            episode_id=1,
            snapshot_id=0,
            target="linkding",
            verdicts=verdicts,
            scen_ids=scen_ids,
            unreached_feats=set(unreached),
            uat_accepted_feats={"FEAT-2"},
        )
        renders.append(render_report(report))
    assert renders[0] == renders[1]
    assert "\r" not in renders[0], "byte-stable newlines on Windows too"
    assert "created_at" not in renders[0], "timestamps live in DB columns only"


def test_store_report_persists_and_stamps_episode(store):
    episode = store.create_episode("linkding", "sha256:abc", 0)
    verdicts, scen_ids = _report_inputs(episode_id=episode)
    report = assemble_report(
        episode_id=episode,
        snapshot_id=0,
        target="linkding",
        verdicts=verdicts,
        scen_ids=scen_ids,
    )
    store_report(store, episode, report)
    row = store.conn.execute(
        "SELECT * FROM settlement_reports WHERE episode_id = ?", (episode,)
    ).fetchone()
    assert row is not None
    assert row["score"] == pytest.approx(1 / 3)
    assert json.loads(row["report_json"])["target"] == "linkding"
    assert store.get_episode(episode)["settled_at"] is not None
    with pytest.raises(StoreError, match="does not exist"):
        store_report(store, 999, report)


# --- dev server hold-open (R7) ---------------------------------------------------------

SERVER_PY = """\
import socket, sys
port = int(sys.argv[1])
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
server.bind(("127.0.0.1", port))
server.listen(5)
while True:
    conn, _ = server.accept()
    conn.close()
"""


@pytest.fixture
def server_script(tmp_path):
    script = tmp_path / "fake-clone-server.py"
    script.write_text(SERVER_PY, encoding="utf-8", newline="\n")
    return script


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connectable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.25):
            return True
    except OSError:
        return False


def server_config(script, port) -> DevServerConfig:
    return DevServerConfig(
        command=(sys.executable, str(script), "{port}"),
        port=port,
        port_attempts=3,
        readiness_timeout_s=10.0,
    )


def test_hold_open_defers_stop_until_release(server_script):
    server = DevServer(server_config(server_script, free_port()))
    try:
        with server:
            port = server.port
            server.hold_open()
            assert server.held
            server.stop()  # a nested user (UAT context) stopping on its way out
            assert connectable(port), "stop() while held must be deferred (R7)"
            assert server.port == port and server.url.endswith(str(port))
        # The context manager exit is also a deferred stop while held.
        assert connectable(port), "held server survives the with-block (R7)"
        server.release()
        assert not server.held
        assert not connectable(port), "release() executes the deferred stop"
        assert server.port is None
    finally:
        server.release()
        server.stop()


def test_hold_open_requires_running_server(server_script):
    server = DevServer(server_config(server_script, free_port()))
    with pytest.raises(DevServerError, match="not running"):
        server.hold_open()
    server.release()  # idempotent no-op without a hold
    assert not server.held


def test_release_without_deferred_stop_keeps_server_running(server_script):
    server = DevServer(server_config(server_script, free_port()))
    try:
        server.start()
        server.hold_open()
        server.release()  # no stop() arrived during the hold
        assert connectable(server.port), (
            "release() without a deferred stop must not tear down"
        )
    finally:
        server.stop()


# --- run_settlement: offline end-to-end ---------------------------------------------


def _settlement_fixture(store):
    """A two-scenario dual-app settlement: FEAT-1 passes deterministically,
    FEAT-2 is feature_absent on the clone. FEAT-2 is never mentioned."""
    add_feat(store, "FEAT-1")
    add_feat(store, "FEAT-2")
    mention_feat(store, "FEAT-1")
    episode = store.create_episode("linkding", "sha256:abc", 0)

    manifests = (
        make_manifest(
            ["Open the bookmarks page"],
            scenario_id="FEAT-1/list",
            feat_id="FEAT-1",
        ),
        make_manifest(
            ["Open the tags page"],
            scenario_id="FEAT-2/tags",
            feat_id="FEAT-2",
        ),
    )
    target_driver = FakeDriver(
        [
            (TREE_PLAIN, "http://127.0.0.1:9090/bookmarks"),
            (TREE_PLAIN, "http://127.0.0.1:9090/bookmarks"),
        ]
    )
    clone_driver = FakeDriver(
        [
            (TREE_MUTATED, "http://127.0.0.1:4173/bookmarks"),
            (TREE_MUTATED, "http://127.0.0.1:4173/bookmarks"),
        ]
    )
    resolver = ScriptedResolver(
        {
            # Target resolves first (it executes first), the clone lacks it.
            "Open the tags page": [
                Resolution("resolved", click("Tags"), "target has tags"),
                Resolution("element_absent", None, "clone has no tags link"),
            ],
        }
    )
    plan = SettlementPlan(
        episode_id=episode,
        snapshot_id=0,
        target="linkding",
        manifests=manifests,
        baseline_probes=(
            BaselineProbe(kind="no_stack_trace", path="/nope"),
        ),
        metamorphic_checks=(
            MetamorphicCheck(
                name="refresh-idempotence",
                kind="refresh_idempotence",
                manifest=make_manifest(
                    ["Open the bookmarks page", "Reload the bookmarks page"],
                    scenario_id="META/refresh",
                ),
                identity_steps=(0, 1),
            ),
        ),
        uat_accepted_feats=frozenset({"FEAT-2"}),
    )
    return episode, plan, {"target": target_driver, "clone": clone_driver}, resolver


def test_run_settlement_offline_end_to_end(store, tmp_path):
    episode, plan, drivers, resolver = _settlement_fixture(store)
    events: list[str] = []
    http = FakeHttp({("GET", "/nope"): (404, "Not found")})

    outcome = run_settlement(
        plan,
        store=store,
        config=config_for(tmp_path / "judge-fixtures"),
        reset_target=lambda: events.append("reset"),
        drivers=drivers,
        cache=ResolutionCache(),
        resolve=resolver,
        clone_url="http://127.0.0.1:4173",
        http=http,
    )

    assert events == ["reset"], "reset-to-seed runs at settlement start (R6)"
    report = outcome.report
    assert report["score"] == {
        "by_tier": {"must": {"passed": 1, "total": 2}},
        "invalid_excluded": 0,
        "overall": 0.5,
        "passed": 1,
        "scoreable": 2,
    }
    # The FEAT-2 failure is both unreached-frontier and UAT-divergent.
    failed = report["failed_scenarios"]
    assert [r["scenario_id"] for r in failed] == ["FEAT-2/tags"]
    assert failed[0]["failure"] == "feature_absent"
    assert set(failed[0]["tags"]) == {"unreached_frontier", UAT_DIVERGENCE_TAG}
    assert report["unreached_frontier"]["feat_ids"] == ["FEAT-2"]
    assert [r["scenario_id"] for r in report["uat_divergence"]] == ["FEAT-2/tags"]
    assert report["baseline"][0]["passed"]
    assert report["metamorphic"][0]["passed"]

    # SCEN rows + report persisted with complete keys (R22/R23).
    rows = store.conn.execute("SELECT * FROM trace_scen ORDER BY id").fetchall()
    assert {r["id"] for r in rows} == set(outcome.scen_ids.values())
    assert all(
        r["episode_id"] == episode and r["snapshot_id"] == 0 for r in rows
    )
    stored = store.conn.execute(
        "SELECT report_json FROM settlement_reports WHERE episode_id = ?",
        (episode,),
    ).fetchone()
    assert json.loads(stored["report_json"]) == report
    assert store.get_episode(episode)["settled_at"] is not None
    # Dual-app discipline: both drivers actually executed the rubric.
    assert drivers["target"].executed and drivers["clone"].executed


def test_run_settlement_requires_held_clone_server(store, tmp_path, server_script):
    episode, plan, drivers, resolver = _settlement_fixture(store)
    config = config_for(tmp_path / "judge-fixtures")
    cache = ResolutionCache()

    not_started = DevServer(server_config(server_script, free_port()))
    with pytest.raises(SettleError, match="not running"):
        run_settlement(
            plan, store=store, config=config, reset_target=lambda: None,
            drivers=drivers, cache=cache, resolve=resolver,
            clone_server=not_started,
        )

    running = DevServer(server_config(server_script, free_port()))
    try:
        running.start()
        with pytest.raises(SettleError, match="held open"):
            run_settlement(
                plan, store=store, config=config, reset_target=lambda: None,
                drivers=drivers, cache=cache, resolve=resolver,
                clone_server=running,
            )
    finally:
        running.stop()

    with pytest.raises(SettleError, match="missing 'clone'"):
        run_settlement(
            plan, store=store, config=config, reset_target=lambda: None,
            drivers={"target": drivers["target"]}, cache=cache,
            resolve=resolver, clone_url="http://127.0.0.1:4173",
        )

    with pytest.raises(SettleError, match="clone's URL"):
        run_settlement(
            plan, store=store, config=config, reset_target=lambda: None,
            drivers=drivers, cache=cache, resolve=resolver,
        )


# --- live mini-settlement (docker-required; needs playwright too) ----------------------


@pytest.mark.docker
@pytest.mark.skipif(
    shutil.which("docker") is None,
    reason="requires Docker Desktop (docker not on PATH)",
)
def test_live_mini_settlement_three_scenarios(store, tmp_path):
    """One live mini-settlement over a 3-scenario manifest set (the U7
    docker-required scenario): the real seeded linkding is the target, and a
    second authenticated browser session on the same app stands in as a
    perfect clone — exercising the full dual-app rubric, deterministic
    comparisons end-to-end, with a scripted resolver (zero LLM calls)."""
    sync_api = pytest.importorskip(
        "playwright.sync_api", reason="requires playwright (pip + browsers)"
    )
    from agent_families.grading.scenarios import PlaywrightDriver
    from agent_families.grading.target_env import (
        LinkdingTarget,
        SUPERUSER_NAME,
        SUPERUSER_PASSWORD,
        TargetEnvConfig,
    )
    from pathlib import Path

    target_dir = Path(__file__).resolve().parents[1] / "targets" / "linkding"
    target = LinkdingTarget(
        TargetEnvConfig(
            compose_file=target_dir / "docker-compose.yml",
            seed_manifest=target_dir / "seed_manifest.json",
            readiness_timeout_s=300.0,
            poll_interval_s=2.0,
        )
    )
    base = target.config.base_url

    for fid in ("FEAT-L1", "FEAT-L2", "FEAT-L3"):
        add_feat(store, fid)
    episode = store.create_episode("linkding", target.config.expected_digest, 0)

    manifests = (
        make_manifest(
            [
                {
                    "step": "Open the bookmarks page",
                    "post_assertion": {"kind": "url_contains", "value": "/bookmarks"},
                }
            ],
            scenario_id="FEAT-L1/list",
            feat_id="FEAT-L1",
            title="View the bookmark list",
            expected="The seeded bookmarks are listed",
        ),
        make_manifest(
            [
                "Search for python",
                {
                    "step": "Submit the search",
                    "post_assertion": {"kind": "url_contains", "value": "q=python"},
                },
            ],
            scenario_id="FEAT-L2/search",
            feat_id="FEAT-L2",
            title="Search bookmarks",
            expected="Only matching bookmarks are listed",
        ),
        make_manifest(
            [
                {
                    "step": "Open the new bookmark form",
                    "post_assertion": {"kind": "url_contains", "value": "/bookmarks/new"},
                }
            ],
            scenario_id="FEAT-L3/new-form",
            feat_id="FEAT-L3",
            title="Open the add-bookmark form",
            expected="The new bookmark form is shown",
        ),
    )
    goto = lambda path: {"action": "goto", "selector": "", "args": [base + path]}
    resolver = ScriptedResolver(
        {
            "Open the bookmarks page": Resolution(
                "resolved", goto("/bookmarks"), "scripted"
            ),
            "Search for python": Resolution(
                "resolved",
                {"action": "fill", "selector": "input[name='q']", "args": ["python"]},
                "scripted",
            ),
            "Submit the search": Resolution(
                "resolved",
                {"action": "press", "selector": "input[name='q']", "args": ["Enter"]},
                "scripted",
            ),
            "Open the new bookmark form": Resolution(
                "resolved", goto("/bookmarks/new"), "scripted"
            ),
        }
    )

    def login(page) -> None:
        page.goto(base + "/login")
        page.fill("input[name='username']", SUPERUSER_NAME)
        page.fill("input[name='password']", SUPERUSER_PASSWORD)
        page.click("button[type='submit']")
        page.wait_for_url("**/bookmarks*")

    with sync_api.sync_playwright() as pw:
        browser = pw.chromium.launch()
        page_target = browser.new_context().new_page()
        page_clone = browser.new_context().new_page()

        def reset_target() -> None:
            # Reset wipes sessions with the volume: log both apps back in.
            target.reset_to_seed()
            login(page_target)
            login(page_clone)

        try:
            target.up()
            outcome = run_settlement(
                SettlementPlan(
                    episode_id=episode,
                    snapshot_id=0,
                    target="linkding",
                    manifests=manifests,
                    baseline_probes=(
                        BaselineProbe(kind="authz_direct_access", path="/settings/general"),
                        BaselineProbe(kind="no_stack_trace", path="/definitely-missing"),
                    ),
                ),
                store=store,
                config=config_for(tmp_path / "judge-fixtures"),
                reset_target=reset_target,
                drivers={
                    "target": PlaywrightDriver(page_target),
                    "clone": PlaywrightDriver(page_clone),
                },
                cache=ResolutionCache(tmp_path / "resolution-cache.json"),
                resolve=resolver,
                clone_url=base,
                evidence_dir=tmp_path / "evidence",
            )
        finally:
            browser.close()
            target.down(drop_volume=True)

    report = outcome.report
    assert report["score"]["scoreable"] == 3
    assert report["score"]["overall"] == 1.0, (
        "a perfect clone settles at full score; comparisons were"
        f" {[(v.scenario_id, v.verdict, v.failure) for v in outcome.verdicts]}"
    )
    assert all(
        v.judge_mode == "deterministic" for v in outcome.verdicts
    ), "identical apps must settle without a single judge call"
    assert all(r["passed"] for r in report["baseline"])
    rows = store.conn.execute("SELECT * FROM trace_scen").fetchall()
    assert len(rows) == 3
