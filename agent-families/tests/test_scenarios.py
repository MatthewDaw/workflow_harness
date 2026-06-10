"""Scenario harness tests: resolve, cache, replay, heal (plan-003 U4, R16-R19).

Everything here is offline: a11y trees are static in-file fixtures (built by
helpers, structurally linkding-shaped), resolver traffic is either scripted
fakes through the ``ResolveFn`` seam or recorded judge-seam fixtures written
with ``write_fixture`` and replayed with zero subprocess calls, and drivers
are scripted state machines. The live-tree/docker cases are the documented
smoke in the module docstring (one scenario on live linkding, cached run two
zero-LLM) — deliberately not CI.

## Conformance

Required acceptance tests (plan-003 U4 — named MUST-tests):

- ``test_fingerprint_ignores_content_mutation`` — two accessibility-tree
  states differing ONLY in row content, dates, or numeric counts produce the
  IDENTICAL fingerprint (within-settlement state mutation must not invalidate
  the target cache).
- ``test_fingerprint_detects_structural_change`` — a structural change
  (added/removed interactive element or landmark) produces a DIFFERENT
  fingerprint.
- ``test_target_cache_survives_mutation`` — after a scenario mutates target
  state, a subsequent cached resolution still hits with zero resolver calls
  (the "target cache accumulates hits forever" economics hold).

Unit test scenarios (plan-003 U4) -> tests:

- cache hit replays with zero resolver calls ->
  ``test_cache_hit_replays_with_zero_resolver_calls``
- fingerprint change forces re-resolution of only the changed step ->
  ``test_fingerprint_change_re_resolves_only_changed_step``
- ``element_absent`` on clone step 1 -> ``feature_absent``, remaining steps
  skipped, no healing, snapshot captured ->
  ``test_element_absent_on_clone_step_one_is_feature_absent``
- heal on clone logs to clone channel and pairs with post-assertion ->
  ``test_heal_on_clone_logs_clone_channel_and_pairs_post_assertion``,
  ``test_heal_requires_post_assertion``
- planted target-side heal flags FEAT evidence stale ->
  ``test_target_side_heal_flags_evidence_stale``
- target hard failure marks scenario ``invalid`` and the denominator excludes
  it -> ``test_target_hard_failure_marks_invalid_and_excluded_from_denominator``
- resolution fixture-driven (recorded resolver outputs) ->
  ``test_judge_resolver_replays_recorded_fixture``,
  ``test_judge_resolver_element_absent_fixture``,
  ``test_judge_resolver_rejects_incoherent_output``
"""

from __future__ import annotations

import json

import pytest

from agent_families.grading.scenarios import (
    ActionExecutionError,
    HealTelemetry,
    ManifestError,
    Observation,
    Resolution,
    ResolutionCache,
    ResolverConfig,
    RESOLVER_SCHEMA,
    ScenarioError,
    cache_key,
    check_post_assertion,
    execute_scenario,
    fingerprint,
    load_manifest_json,
    make_judge_resolver,
    parse_manifest,
    resolver_prompt,
    score_denominator,
    url_pattern,
)
from agent_families.judge import JudgeSchemaViolation, write_fixture

# --- a11y tree fixtures (static, linkding-shaped; captured-once discipline) -----


def n(role: str, name: str = "", *children: dict) -> dict:
    node: dict = {"role": role, "name": name}
    if children:
        node["children"] = list(children)
    return node


def bookmark_row(title: str, url_text: str, date: str) -> dict:
    return n(
        "listitem",
        "",
        n("link", title),
        n("text", f"{url_text} | {date}"),
        n("link", "Edit"),
        n("button", "Remove"),
    )


def app_tree(rows: list[dict], *, tags_count: int = 3, extra: list | None = None) -> dict:
    main_children = [
        n("searchbox", "Search"),
        n("button", "Add bookmark"),
        n("list", "", *rows),
    ] + list(extra or [])
    return n(
        "document",
        f"Bookmarks ({len(rows)})",
        n("banner", "", n("link", "linkding")),
        n(
            "navigation",
            "",
            n("link", "Bookmarks"),
            n("link", f"Tags ({tags_count})"),
            n("link", "Settings"),
        ),
        n("main", "", *main_children),
    )


def form_tree() -> dict:
    return n(
        "document",
        "Add bookmark",
        n("banner", "", n("link", "linkding")),
        n(
            "main",
            "",
            n(
                "form",
                "",
                n("textbox", "URL"),
                n("textbox", "Title"),
                n("button", "Save"),
            ),
        ),
    )


ROWS_FEW = [
    bookmark_row("Python docs", "docs.python.org", "June 1, 2026"),
    bookmark_row("Rust book", "doc.rust-lang.org", "May 12, 2026"),
]
ROWS_MANY = [
    bookmark_row(f"Item {i}", f"site{i}.example.com", f"2026-06-0{i}")
    for i in range(1, 6)
]


# --- scripted seams ---------------------------------------------------------------


class FakeDriver:
    """Scripted state-machine driver: ``observe`` returns the current state,
    ``execute`` advances it (and fails on scripted call numbers, 1-based)."""

    def __init__(self, states: list[tuple[dict, str]], fail_execute_calls=()):
        self.states = states
        self.idx = 0
        self.executed: list[dict] = []
        self.execute_calls = 0
        self.fail_execute_calls = set(fail_execute_calls)

    def observe(self) -> Observation:
        tree, url = self.states[min(self.idx, len(self.states) - 1)]
        return Observation(a11y_tree=tree, url=url)

    def execute(self, action: dict) -> None:
        self.execute_calls += 1
        if self.execute_calls in self.fail_execute_calls:
            raise ActionExecutionError("scripted execute failure")
        self.executed.append(action)
        self.idx = min(self.idx + 1, len(self.states) - 1)

    def screenshot(self) -> bytes:
        return b"\x89PNG-fake-screenshot"


def click(target: str) -> dict:
    return {"action": "click", "selector": f"text={target}", "args": []}


class ScriptedResolver:
    """ResolveFn fake: per-step scripted outcomes (a list pops per call);
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


def make_manifest(steps, *, scenario_id="FEAT-1/add-bookmark", tier="must") -> dict:
    return parse_manifest(
        {
            "scenario_id": scenario_id,
            "feat_id": "FEAT-1",
            "title": "Add a bookmark",
            "steps": steps,
            "expected_outcome": "The bookmark appears in the list",
            "tier": tier,
        }
    )


# --- manifests (R16) ----------------------------------------------------------------


def test_manifest_round_trip_parses_steps_and_assertions():
    manifest = load_manifest_json(
        json.dumps(
            {
                "scenario_id": "FEAT-2/search",
                "feat_id": "FEAT-2",
                "title": "Search bookmarks",
                "steps": [
                    "Open the bookmarks page",
                    {
                        "step": "Search for python",
                        "post_assertion": {
                            "kind": "url_contains",
                            "value": "q=python",
                        },
                    },
                ],
                "expected_outcome": "Only matching bookmarks are listed",
                "tier": "should",
            }
        )
    )
    assert manifest.scenario_id == "FEAT-2/search"
    assert manifest.tier == "should"
    assert len(manifest.steps) == 2
    assert manifest.steps[0].post_assertion is None
    assert manifest.steps[1].post_assertion == {
        "kind": "url_contains",
        "value": "q=python",
    }


def test_manifest_rejects_bad_tier():
    with pytest.raises(ManifestError, match="tier"):
        make_manifest(["step one"], tier="optional")


def test_manifest_rejects_empty_steps():
    with pytest.raises(ManifestError, match="steps"):
        make_manifest([])


def test_manifest_rejects_non_feat_id():
    with pytest.raises(ManifestError, match="FEAT-"):
        parse_manifest(
            {
                "scenario_id": "x",
                "feat_id": "TKT-1",
                "title": "t",
                "steps": ["s"],
                "expected_outcome": "o",
                "tier": "must",
            }
        )


def test_manifest_rejects_bad_post_assertion_kind():
    with pytest.raises(ManifestError, match="post_assertion kind"):
        make_manifest(
            [{"step": "s", "post_assertion": {"kind": "screenshot_matches"}}]
        )


# --- fingerprint normalization (R17) -------------------------------------------------


def test_fingerprint_ignores_content_mutation():
    # Differs ONLY in: row contents (titles, URLs), dates, row COUNT (2 vs 5),
    # and numeric counts inside accessible names ("Tags (3)" vs "Tags (9)",
    # "Bookmarks (2)" vs "Bookmarks (5)").
    state_a = app_tree(ROWS_FEW, tags_count=3)
    state_b = app_tree(ROWS_MANY, tags_count=9)
    assert fingerprint(state_a) == fingerprint(state_b)


def test_fingerprint_detects_structural_change():
    base = app_tree(ROWS_FEW)
    added_interactive = app_tree(ROWS_FEW, extra=[n("button", "Bulk edit")])
    assert fingerprint(base) != fingerprint(added_interactive)

    removed_landmark = app_tree(ROWS_FEW)
    removed_landmark["children"] = [
        c for c in removed_landmark["children"] if c["role"] != "navigation"
    ]
    assert fingerprint(base) != fingerprint(removed_landmark)


def test_url_pattern_normalizes_ids_and_drops_query():
    assert (
        url_pattern("http://127.0.0.1:9090/bookmarks/42/edit?return=/bookmarks")
        == "/bookmarks/{n}/edit"
    )
    assert url_pattern("http://127.0.0.1:9090/") == "/"
    assert url_pattern("http://host") == "/"


# --- cache (R17) ----------------------------------------------------------------------


def test_cache_key_varies_on_each_component():
    fp_a = fingerprint(app_tree(ROWS_FEW))
    fp_b = fingerprint(form_tree())
    base = cache_key("click save", "/bookmarks", fp_a)
    assert cache_key("click cancel", "/bookmarks", fp_a) != base
    assert cache_key("click save", "/bookmarks/new", fp_a) != base
    assert cache_key("click save", "/bookmarks", fp_b) != base


def test_cache_scoped_by_scenario_and_app(tmp_path):
    cache = ResolutionCache(tmp_path / "cache.json")
    key = cache_key("step", "/p", "f" * 64)
    cache.put("FEAT-1/s", "clone", key, click("Save"))
    assert cache.get("FEAT-1/s", "clone", key) == click("Save")
    assert cache.get("FEAT-1/s", "target", key) is None
    assert cache.get("FEAT-2/s", "clone", key) is None


def test_cache_file_round_trip_byte_stable(tmp_path):
    path = tmp_path / "cache.json"
    key = cache_key("step", "/p", "f" * 64)
    ResolutionCache(path).put("FEAT-1/s", "target", key, click("Save"))
    first = path.read_bytes()
    assert b"\r" not in first  # byte-stable: \n newlines on Windows too

    reloaded = ResolutionCache(path)
    assert reloaded.get("FEAT-1/s", "target", key) == click("Save")
    reloaded.put("FEAT-1/s", "target", key, click("Save"))
    assert path.read_bytes() == first


def test_cache_rejects_malformed_action(tmp_path):
    cache = ResolutionCache()
    with pytest.raises(ScenarioError, match="action kind"):
        cache.put("s", "clone", "k", {"action": "rm -rf", "selector": "", "args": []})


# --- resolver through the judge seam (recorded resolver outputs, R17/R18) -------------


def envelope(output: dict) -> dict:
    return {
        "structured_output": output,
        "is_error": False,
        "total_cost_usd": 0.0,
        "duration_ms": 1,
    }


def test_judge_resolver_replays_recorded_fixture(tmp_path):
    tree = app_tree(ROWS_FEW)
    step = "Click the Add bookmark button"
    recorded = {
        "outcome": "resolved",
        "action": {
            "action": "click",
            "selector": "role=button[name='Add bookmark']",
            "args": [],
        },
        "reason": "exactly one Add bookmark button in main",
    }
    write_fixture(
        tmp_path, resolver_prompt(step, tree), RESOLVER_SCHEMA, "sonnet",
        envelope(recorded),
    )
    resolve = make_judge_resolver(
        ResolverConfig(
            model="sonnet", max_retries=0, mode="replay", fixtures_dir=tmp_path
        )
    )
    resolution = resolve(step, tree)
    assert resolution.outcome == "resolved"
    assert resolution.action["selector"] == "role=button[name='Add bookmark']"


def test_judge_resolver_element_absent_fixture(tmp_path):
    tree = form_tree()
    step = "Click the archive button"
    recorded = {
        "outcome": "element_absent",
        "action": None,
        "reason": "no archive control in the tree",
    }
    write_fixture(
        tmp_path, resolver_prompt(step, tree), RESOLVER_SCHEMA, "sonnet",
        envelope(recorded),
    )
    resolve = make_judge_resolver(
        ResolverConfig(
            model="sonnet", max_retries=0, mode="replay", fixtures_dir=tmp_path
        )
    )
    resolution = resolve(step, tree)
    assert resolution.outcome == "element_absent"
    assert resolution.action is None


def test_judge_resolver_rejects_incoherent_output(tmp_path):
    # 'resolved' with a null action is incoherent: extra_validate routes it
    # through the schema-violation path (typed failure, no silent acceptance).
    tree = form_tree()
    step = "Click save"
    write_fixture(
        tmp_path, resolver_prompt(step, tree), RESOLVER_SCHEMA, "sonnet",
        envelope({"outcome": "resolved", "action": None, "reason": "??"}),
    )
    resolve = make_judge_resolver(
        ResolverConfig(
            model="sonnet", max_retries=0, mode="replay", fixtures_dir=tmp_path
        )
    )
    with pytest.raises(JudgeSchemaViolation, match="non-null action"):
        resolve(step, tree)


def test_resolver_prompt_deterministic_and_default_fail():
    tree = app_tree(ROWS_FEW)
    prompt_a = resolver_prompt("Click save", tree)
    prompt_b = resolver_prompt("Click save", app_tree(ROWS_FEW))
    assert prompt_a == prompt_b  # no volatile data: stable fixture keys (R23)
    assert "element_absent" in prompt_a
    assert "ambiguous" in prompt_a
    assert "Default to failure" in prompt_a


# --- execution: cache hits and replay (R17) --------------------------------------------


STATES_BASE = [
    (app_tree(ROWS_FEW), "http://127.0.0.1:4173/bookmarks"),
    (form_tree(), "http://127.0.0.1:4173/bookmarks/new"),
    (app_tree(ROWS_FEW + [bookmark_row("New", "new.example", "June 10, 2026")]),
     "http://127.0.0.1:4173/bookmarks"),
]
TWO_STEPS = ["Click the Add bookmark button", "Save the bookmark form"]


def test_cache_hit_replays_with_zero_resolver_calls():
    manifest = make_manifest(TWO_STEPS)
    cache = ResolutionCache()

    warm_resolver = ScriptedResolver()
    warm = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), cache, warm_resolver
    )
    assert warm.status == "completed"
    assert warm.resolver_calls == 2
    assert [s.status for s in warm.steps] == ["resolved", "resolved"]

    replay_resolver = ScriptedResolver()
    replay = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), cache, replay_resolver
    )
    assert replay.status == "completed"
    assert replay.resolver_calls == 0
    assert replay_resolver.calls == []
    assert [s.status for s in replay.steps] == ["cached", "cached"]


def test_target_cache_survives_mutation():
    # Run 1 resolves on the target and its steps mutate state (the post-step
    # list carries an extra row). Run 2 sees a content-mutated target — more
    # rows, different titles/dates, different counts in names — and must still
    # be all cache hits: ZERO resolver calls (the target-cache economics).
    manifest = make_manifest(TWO_STEPS)
    cache = ResolutionCache()

    warm = execute_scenario(
        manifest, "target", FakeDriver(list(STATES_BASE)), cache,
        ScriptedResolver(),
    )
    assert warm.status == "completed"
    assert warm.resolver_calls == 2

    mutated_states = [
        (app_tree(ROWS_MANY, tags_count=9), "http://127.0.0.1:4173/bookmarks"),
        (form_tree(), "http://127.0.0.1:4173/bookmarks/new"),
        (app_tree(ROWS_MANY, tags_count=9), "http://127.0.0.1:4173/bookmarks"),
    ]
    replay_resolver = ScriptedResolver()
    replay = execute_scenario(
        manifest, "target", FakeDriver(mutated_states), cache, replay_resolver
    )
    assert replay.status == "completed"
    assert replay.resolver_calls == 0
    assert replay_resolver.calls == []
    assert [s.status for s in replay.steps] == ["cached", "cached"]
    # Mutation absorbed by normalization: no heals, no stale evidence (R19).
    assert replay.heal_events == []
    assert replay.evidence_stale is False


def test_fingerprint_change_re_resolves_only_changed_step():
    steps = ["Open bookmarks", "Click the Add bookmark button", "Save the form"]
    states = [
        (app_tree(ROWS_FEW), "http://127.0.0.1:4173/"),
        (app_tree(ROWS_FEW), "http://127.0.0.1:4173/bookmarks"),
        (form_tree(), "http://127.0.0.1:4173/bookmarks/new"),
        (app_tree(ROWS_FEW), "http://127.0.0.1:4173/bookmarks"),
    ]
    manifest = make_manifest(steps)
    cache = ResolutionCache()
    warm = execute_scenario(
        manifest, "clone", FakeDriver(list(states)), cache, ScriptedResolver()
    )
    assert warm.resolver_calls == 3

    # Step 2's page gains an interactive element (structural change); steps 1
    # and 3 observe structurally-identical states.
    changed = list(states)
    changed[1] = (
        app_tree(ROWS_FEW, extra=[n("button", "Bulk edit")]),
        "http://127.0.0.1:4173/bookmarks",
    )
    resolver = ScriptedResolver()
    second = execute_scenario(
        manifest, "clone", FakeDriver(changed), cache, resolver
    )
    assert second.status == "completed"
    assert second.resolver_calls == 1
    assert resolver.calls == ["Click the Add bookmark button"]
    assert [s.status for s in second.steps] == ["cached", "resolved", "cached"]


# --- execution: feature_absent short-circuit (R18) --------------------------------------


def test_element_absent_on_clone_step_one_is_feature_absent():
    manifest = make_manifest(TWO_STEPS)
    telemetry = HealTelemetry()
    resolver = ScriptedResolver(
        {TWO_STEPS[0]: Resolution("element_absent", None, "not in the tree")}
    )
    result = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), ResolutionCache(),
        resolver, telemetry=telemetry,
    )
    assert result.status == "feature_absent"
    assert result.failure_reason == "element_absent"
    # Remaining steps skipped, no healing attempted (R18).
    assert [s.status for s in result.steps] == ["failed", "skipped"]
    assert result.heal_events == []
    assert telemetry.channels == {"target": [], "clone": []}
    assert resolver.calls == [TWO_STEPS[0]]
    # The stopping snapshot is captured: a11y tree + screenshot evidence.
    stopping = result.steps[0].evidence
    assert stopping is not None
    assert stopping.a11y_tree
    assert stopping.screenshot.startswith(b"\x89PNG")


def test_ambiguous_on_clone_is_failed_not_feature_absent():
    # R18: element_absent (not built) is distinct from ambiguous (built wrong).
    manifest = make_manifest(TWO_STEPS)
    resolver = ScriptedResolver(
        {TWO_STEPS[0]: Resolution("ambiguous", None, "two plausible buttons")}
    )
    result = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), ResolutionCache(),
        resolver,
    )
    assert result.status == "failed"
    assert result.failure_reason == "ambiguous_match"


# --- execution: self-healing (R19) -------------------------------------------------------


HEAL_STEP = {
    "step": "Click the Add bookmark button",
    "post_assertion": {"kind": "url_contains", "value": "/bookmarks/new"},
}
HEAL_STATES = [
    (app_tree(ROWS_FEW), "http://127.0.0.1:4173/bookmarks"),
    (form_tree(), "http://127.0.0.1:4173/bookmarks/new"),
]


def _warmed_cache(app: str) -> ResolutionCache:
    cache = ResolutionCache()
    warm = execute_scenario(
        make_manifest([HEAL_STEP]), app, FakeDriver(list(HEAL_STATES)), cache,
        ScriptedResolver(
            {HEAL_STEP["step"]: Resolution("resolved", click("old"), "warm")}
        ),
    )
    assert warm.status == "completed"
    return cache


def test_heal_on_clone_logs_clone_channel_and_pairs_post_assertion():
    cache = _warmed_cache("clone")
    telemetry = HealTelemetry()
    resolver = ScriptedResolver(
        {HEAL_STEP["step"]: Resolution("resolved", click("new"), "healed")}
    )
    # The cached action fails on replay (execute call #1); the heal's
    # re-execution (call #2) succeeds and the post-assertion is checked.
    driver = FakeDriver(list(HEAL_STATES), fail_execute_calls={1})
    result = execute_scenario(
        make_manifest([HEAL_STEP]), "clone", driver, cache, resolver,
        telemetry=telemetry,
    )
    assert result.status == "completed"
    assert result.steps[0].status == "healed"
    assert resolver.calls == [HEAL_STEP["step"]]
    assert len(telemetry.channels["clone"]) == 1
    assert telemetry.channels["target"] == []
    event = telemetry.channels["clone"][0]
    assert event.old_action == click("old")
    assert event.new_action == click("new")
    # Clone-side healing is expected churn: no staleness flag (R19).
    assert result.evidence_stale is False
    # The cache was updated to the healed action at the same fingerprint.
    obs = FakeDriver(list(HEAL_STATES)).observe()
    key = cache_key(
        HEAL_STEP["step"], url_pattern(obs.url), fingerprint(obs.a11y_tree)
    )
    assert cache.get("FEAT-1/add-bookmark", "clone", key) == click("new")


def test_heal_requires_post_assertion():
    # R19: a heal MUST pair with a deterministic post-assertion; a step
    # without one cannot heal and fails with a typed reason.
    bare_step = "Click the Add bookmark button"
    cache = ResolutionCache()
    warm = execute_scenario(
        make_manifest([bare_step]), "clone", FakeDriver(list(HEAL_STATES)),
        cache, ScriptedResolver(),
    )
    assert warm.status == "completed"

    resolver = ScriptedResolver()
    result = execute_scenario(
        make_manifest([bare_step]), "clone",
        FakeDriver(list(HEAL_STATES), fail_execute_calls={1}), cache, resolver,
    )
    assert result.status == "failed"
    assert result.failure_reason == "heal_requires_post_assertion"
    assert resolver.calls == []  # no re-resolution without the pairing


def test_target_side_heal_flags_evidence_stale():
    # A planted target-side heal: cache hit at a stable fingerprint whose
    # action fails -> fingerprint-stable re-resolution -> the FEAT's evidence
    # is flagged stale and surfaces on the target telemetry channel (R19).
    cache = _warmed_cache("target")
    telemetry = HealTelemetry()
    resolver = ScriptedResolver(
        {HEAL_STEP["step"]: Resolution("resolved", click("new"), "healed")}
    )
    driver = FakeDriver(list(HEAL_STATES), fail_execute_calls={1})
    result = execute_scenario(
        make_manifest([HEAL_STEP]), "target", driver, cache, resolver,
        telemetry=telemetry,
    )
    assert result.status == "completed"
    assert result.steps[0].status == "healed"
    assert result.evidence_stale is True
    assert len(telemetry.channels["target"]) == 1
    assert telemetry.channels["clone"] == []


def test_target_hard_failure_marks_invalid_and_excluded_from_denominator():
    manifest = make_manifest(TWO_STEPS)
    resolver = ScriptedResolver(
        {TWO_STEPS[0]: Resolution("element_absent", None, "registry drift")}
    )
    invalid = execute_scenario(
        manifest, "target", FakeDriver(list(STATES_BASE)), ResolutionCache(),
        resolver,
    )
    assert invalid.status == "invalid"
    assert invalid.failure_reason == "element_absent"

    completed = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), ResolutionCache(),
        ScriptedResolver(),
    )
    scoreable = score_denominator([invalid, completed])
    assert scoreable == [completed]


# --- execution: post-assertions and evidence ----------------------------------------------


def test_post_assertion_failure_fails_clone_scenario():
    step = {
        "step": "Click the Add bookmark button",
        "post_assertion": {"kind": "url_contains", "value": "/settings"},
    }
    result = execute_scenario(
        make_manifest([step]), "clone", FakeDriver(list(HEAL_STATES)),
        ResolutionCache(), ScriptedResolver(),
    )
    assert result.status == "failed"
    assert result.failure_reason == "post_assertion_failed"


def test_check_post_assertion_node_kinds():
    obs = Observation(a11y_tree=form_tree(), url="http://x/bookmarks/new")
    assert check_post_assertion(
        {"kind": "node_present", "role": "button", "name": "Save"}, obs
    )
    assert check_post_assertion(
        {"kind": "node_absent", "role": "button", "name": "Archive"}, obs
    )
    assert not check_post_assertion(
        {"kind": "node_present", "role": "button", "name": "Archive"}, obs
    )


def test_evidence_written_per_step(tmp_path):
    manifest = make_manifest(TWO_STEPS)
    result = execute_scenario(
        manifest, "clone", FakeDriver(list(STATES_BASE)), ResolutionCache(),
        ScriptedResolver(), evidence_dir=tmp_path,
    )
    assert result.status == "completed"
    snapshots = sorted(p.name for p in tmp_path.glob("*.json"))
    screenshots = sorted(p.name for p in tmp_path.glob("*.png"))
    assert len(snapshots) == 2
    assert len(screenshots) == 2
    body = (tmp_path / snapshots[0]).read_bytes()
    assert b"\r" not in body  # \n-stable evidence files (Windows KTD)
    payload = json.loads(body.decode("utf-8"))
    assert set(payload) == {"a11y_tree", "url"}


def test_invalid_app_rejected():
    with pytest.raises(ScenarioError, match="app must be one of"):
        execute_scenario(
            make_manifest(["s"]), "staging", FakeDriver(list(HEAL_STATES)),
            ResolutionCache(), ScriptedResolver(),
        )
