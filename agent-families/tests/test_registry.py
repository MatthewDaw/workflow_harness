"""plan-003 U3: the FEAT registry — pre-research, runtime confirmation, mint
(R8, R9, R10 mint path).

Fully offline: the browse channel is a scripted fake (the R15
orchestrator-mediated pattern makes that trivial), the pre-research session
rides the Phase 1 scripted-agent fake, and git is a recorded fake runner.
Zero quota, no docker, no network.

## Conformance

Required acceptance tests (plan-003 U3) — invariant -> enforcing test:

- "a feature present in source/routes but NOT reachable/confirmable on the
  running app (dead code, disabled flag) is NOT minted as a FEAT"
  -> ``test_source_only_feature_not_minted`` (the planted dead-code candidate
  enumerates fine but the browse channel reports ``element_absent`` on the
  running app; asserts no trace_feat row, no scenario manifest, no frontier
  entry, and the mentions FK still rejects the id)
- "a feature confirmed on the running app IS minted, carrying an evidence
  ref (a11y snapshot/screenshot) and the current image digest"
  -> ``test_runtime_confirmed_feature_minted_with_evidence`` (asserts the
  evidence ref points at a written a11y-snapshot capture stamped with the
  digest, and the row carries the current digest)
- "a registry refresh never renumbers or reuses a FEAT id; a vanished
  feature flips to `deprecated`, it is not deleted"
  -> ``test_feat_ids_append_only`` (two refreshes with a vanishing +
  reappearing feature: ids stable by construction, the vanished feature
  deprecates in place, and the store triggers reject UPDATE-of-id and DELETE
  outright)

Unit test scenarios (plan-003 U3) -> tests:

- registry rows all carry evidence refs and the current digest
  -> ``test_registry_rows_carry_evidence_and_digest``
- planted source-only feature (dead code analog) not minted without runtime
  confirmation -> ``test_source_only_feature_not_minted``
- deprecation flow archives scenarios and drops frontier entries while MSG
  history stays queryable
  -> ``test_deprecation_archives_scenarios_drops_frontier_keeps_msg_history``
- mint path makes a newly-discovered feature mentionable and authorable
  -> ``test_newly_discovered_mint_path_mentionable_and_authorable``
- frontier ordering deterministic -> test_frontier.py (the U3 sibling file)

Verification note: "linkding registry lands in the expected 40–60 behavior
range with evidence per row" is the documented LIVE procedure (registry.py
module docstring) — it needs Docker, a logged-in claude CLI, and a
Playwright-backed browse executor, per the plan's test-tier decision ("live
explorer/judge smoke is documented procedure"). The range itself is pinned
offline by ``test_expected_behavior_range_pinned``.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_families.grading.registry import (
    EXPECTED_BEHAVIOR_RANGE,
    PRE_RESEARCH_SCHEMA,
    SOURCE_REPO_URL,
    SOURCE_TAG,
    DiscoveryQueue,
    FeatureCandidate,
    RegistryError,
    build_pre_research_prompt,
    candidates_from_output,
    confirm_candidate,
    deprecate_feat,
    ensure_source_checkout,
    feat_id,
    grader_profile,
    mint_feat,
    refresh_registry,
    registry_in_expected_range,
    registry_rows,
    run_pre_research,
)
from agent_families.pipeline.sessions import SessionSchemaViolation
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "ab" * 32


# --- helpers -------------------------------------------------------------------


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def cand(key: str, **kw) -> FeatureCandidate:
    defaults = dict(
        key=key,
        area="bookmarks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="bookmarks/urls.py",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/bookmarks"}},
            {"action": "snapshot", "selector": f"[data-test={key}]", "args": {}},
        ),
        scenario_steps=("Open the bookmarks page", f"Exercise {key}"),
        expected_outcome="the behavior is observable on the page",
        tier="must",
    )
    return FeatureCandidate(**{**defaults, **kw})


class FakeBrowse:
    """The orchestrator-mediated browse channel, scripted (R15 pattern):
    answers ``ok`` with an a11y snapshot, except for selectors naming a
    planted-absent key (the running app does not have the element)."""

    def __init__(self, absent: tuple[str, ...] = ()) -> None:
        self.absent = absent
        self.calls: list[dict] = []

    def __call__(self, step: dict) -> dict:
        self.calls.append(step)
        if any(key in step.get("selector", "") for key in self.absent):
            return {"status": "element_absent", "a11y": ""}
        return {
            "status": "ok",
            "a11y": f"snapshot:{step['action']}:{step['selector']}",
            "screenshot_ref": "shots/step.png",
        }


def refresh(store, tmp_path, candidates, absent: tuple[str, ...] = ()):
    browse = FakeBrowse(absent)
    result = refresh_registry(
        store,
        candidates,
        browse,
        target=TARGET,
        digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )
    return result, browse


def add_mention(store: Store, fid: str, msg_id: str = "MSG-1") -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO trace_msg (id, content) VALUES (?, '')", (msg_id,)
    )
    store.conn.execute(
        "INSERT INTO trace_msg_mentions (msg_id, feat_id) VALUES (?, ?)",
        (msg_id, fid),
    )


def manifests_for(store: Store, fid: str) -> list[sqlite3.Row]:
    return store.conn.execute(
        "SELECT * FROM scenario_manifests WHERE feat_id = ? ORDER BY id", (fid,)
    ).fetchall()


def frontier_for(store: Store, fid: str) -> sqlite3.Row | None:
    return store.conn.execute(
        "SELECT * FROM frontier WHERE feat_id = ?", (fid,)
    ).fetchone()


# --- REQUIRED acceptance test: source proposes, runtime confirms (R8) ----------


def test_source_only_feature_not_minted(tmp_path):
    """A feature present in source/routes but not confirmable on the running
    app (dead code analog) is NOT minted."""
    store = make_store(tmp_path)
    dead = cand("export-dead-code", route="bookmarks/urls.py: path('export-v0')")
    result, browse = refresh(
        store, tmp_path, [cand("bookmark-create"), dead],
        absent=("export-dead-code",),
    )

    assert result.unconfirmed == ("export-dead-code",)
    assert "FEAT-export-dead-code" not in result.minted
    # no registry row, no manifest, no frontier entry — it does not exist
    assert (
        store.conn.execute(
            "SELECT * FROM trace_feat WHERE id = 'FEAT-export-dead-code'"
        ).fetchone()
        is None
    )
    assert manifests_for(store, "FEAT-export-dead-code") == []
    assert frontier_for(store, "FEAT-export-dead-code") is None
    # and it is not mentionable: the FK rejects the unminted id
    with pytest.raises(sqlite3.IntegrityError):
        add_mention(store, "FEAT-export-dead-code")
    # the dead candidate WAS probed on the running app before rejection
    assert any(
        "export-dead-code" in c.get("selector", "") for c in browse.calls
    )


def test_ok_observation_without_a11y_snapshot_is_not_confirmation(tmp_path):
    """Evidence IS the confirmation: an 'ok' with no captured a11y payload
    does not mint."""

    def evidence_less(step):
        return {"status": "ok", "a11y": ""}

    ref = confirm_candidate(
        cand("bookmark-create"),
        evidence_less,
        evidence_dir=tmp_path / "evidence",
        digest=DIGEST,
    )
    assert ref is None


# --- REQUIRED acceptance test: confirmed -> minted with evidence (R8/R9) -------


def test_runtime_confirmed_feature_minted_with_evidence(tmp_path):
    store = make_store(tmp_path)
    result, browse = refresh(store, tmp_path, [cand("bookmark-create")])

    assert result.minted == ("FEAT-bookmark-create",)
    row = store.conn.execute(
        "SELECT * FROM trace_feat WHERE id = 'FEAT-bookmark-create'"
    ).fetchone()
    assert row["status"] == "confirmed"
    assert row["target"] == TARGET
    assert row["digest"] == DIGEST, "rows are stamped with the current digest"
    # the evidence ref points at a real captured a11y snapshot
    assert row["evidence_ref"]
    evidence = json.loads(Path(row["evidence_ref"]).read_text(encoding="utf-8"))
    assert evidence["digest"] == DIGEST
    assert evidence["feat_key"] == "bookmark-create"
    assert len(evidence["captured"]) == 2, "one capture per confirm step"
    assert all(c["a11y"].startswith("snapshot:") for c in evidence["captured"])
    assert all(c["screenshot_ref"] for c in evidence["captured"])
    # confirmation actually exercised the running app via the browse channel
    assert len(browse.calls) == 2
    # mint authored the scenario manifest (U4 format) and seeded the frontier
    (manifest,) = manifests_for(store, "FEAT-bookmark-create")
    payload = json.loads(manifest["manifest_json"])
    assert payload["steps"] == ["Open the bookmarks page", "Exercise bookmark-create"]
    assert payload["expected_outcome"]
    assert payload["tier"] == "must" == manifest["tier"]
    assert frontier_for(store, "FEAT-bookmark-create")["status"] == "unexplored"


def test_mint_refuses_empty_evidence_and_duplicate_ids(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RegistryError, match="evidence"):
        mint_feat(store, cand("tag-filter"), "  ", target=TARGET, digest=DIGEST)
    mint_feat(store, cand("tag-filter"), "ev/tag-filter.json", target=TARGET,
              digest=DIGEST)
    with pytest.raises(RegistryError, match="never[- ]reused|never\\s+reused"):
        mint_feat(store, cand("tag-filter"), "ev/tag-filter.json", target=TARGET,
                  digest=DIGEST)


# --- REQUIRED acceptance test: FEAT ids append-only (R9) ------------------------


def test_feat_ids_append_only(tmp_path):
    """A refresh never renumbers or reuses an id; a vanished feature flips to
    `deprecated`, it is not deleted."""
    store = make_store(tmp_path)
    r1, _ = refresh(store, tmp_path, [cand("bookmark-create"), cand("tag-filter")])
    assert set(r1.minted) == {"FEAT-bookmark-create", "FEAT-tag-filter"}

    # refresh #2: bookmark-create vanished, search-basic is new
    r2, _ = refresh(store, tmp_path, [cand("tag-filter"), cand("search-basic")])
    assert r2.minted == ("FEAT-search-basic",)
    assert r2.reconfirmed == ("FEAT-tag-filter",)
    assert r2.deprecated == ("FEAT-bookmark-create",)

    rows = {row["id"]: row["status"] for row in registry_rows(store, TARGET)}
    assert rows == {
        "FEAT-bookmark-create": "deprecated",  # flipped, never deleted
        "FEAT-tag-filter": "confirmed",        # same id, never renumbered
        "FEAT-search-basic": "confirmed",
    }

    # refresh #3: the vanished feature reappears — SAME id, no new number
    r3, _ = refresh(
        store, tmp_path,
        [cand("bookmark-create"), cand("tag-filter"), cand("search-basic")],
    )
    assert r3.minted == ()
    assert set(r3.reconfirmed) == set(rows)
    assert {row["id"] for row in registry_rows(store, TARGET)} == set(rows)

    # the store's triggers enforce the discipline below the API too
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(
            "UPDATE trace_feat SET id = 'FEAT-renumbered'"
            " WHERE id = 'FEAT-tag-filter'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.conn.execute(
            "DELETE FROM trace_feat WHERE id = 'FEAT-bookmark-create'"
        )


def test_feat_id_is_pure_derivation():
    assert feat_id("bulk-edit") == "FEAT-bulk-edit"
    with pytest.raises(RegistryError, match="invalid"):
        feat_id("Not A Key")


# --- scenario: rows all carry evidence refs and the current digest --------------


def test_registry_rows_carry_evidence_and_digest(tmp_path):
    store = make_store(tmp_path)
    keys = ("bookmark-create", "tag-filter", "search-basic")
    result, _ = refresh(store, tmp_path, [cand(k) for k in keys])
    assert len(result.minted) == 3

    rows = registry_rows(store, TARGET)
    assert len(rows) == 3
    for row in rows:
        assert row["digest"] == DIGEST
        assert row["evidence_ref"]
        assert Path(row["evidence_ref"]).exists(), (
            "every row's evidence ref must point at a captured snapshot"
        )


# --- scenario: deprecation flow --------------------------------------------------


def test_deprecation_archives_scenarios_drops_frontier_keeps_msg_history(tmp_path):
    store = make_store(tmp_path)
    refresh(store, tmp_path, [cand("bookmark-create"), cand("tag-filter")])
    add_mention(store, "FEAT-bookmark-create")

    # the feature vanishes from the next refresh -> deprecation flow
    result, _ = refresh(store, tmp_path, [cand("tag-filter")])
    assert result.deprecated == ("FEAT-bookmark-create",)

    assert all(
        m["status"] == "archived"
        for m in manifests_for(store, "FEAT-bookmark-create")
    ), "scenarios archive, never delete"
    assert frontier_for(store, "FEAT-bookmark-create") is None, (
        "the frontier drops deprecated features"
    )
    # MSG history stays queryable: the mention join still resolves
    row = store.conn.execute(
        "SELECT m.msg_id, t.status FROM trace_msg_mentions m"
        " JOIN trace_feat t ON t.id = m.feat_id"
        " WHERE m.feat_id = 'FEAT-bookmark-create'"
    ).fetchone()
    assert row["msg_id"] == "MSG-1" and row["status"] == "deprecated"

    # reappearance reactivates in place (same id): manifests back to active
    result, _ = refresh(store, tmp_path, [cand("bookmark-create"), cand("tag-filter")])
    assert "FEAT-bookmark-create" in result.reconfirmed
    assert all(
        m["status"] == "active"
        for m in manifests_for(store, "FEAT-bookmark-create")
    )
    assert frontier_for(store, "FEAT-bookmark-create") is not None

    deprecate_feat(store, "FEAT-tag-filter")
    assert frontier_for(store, "FEAT-tag-filter") is None
    with pytest.raises(RegistryError, match="does not exist"):
        deprecate_feat(store, "FEAT-never-was")


# --- scenario: newly-discovered mint path (R10) ----------------------------------


def test_newly_discovered_mint_path_mentionable_and_authorable(tmp_path):
    store = make_store(tmp_path)
    queue = DiscoveryQueue()
    queue.enqueue(
        cand("bulk-edit"),
        ui_evidence="explorer saw a bulk-edit toolbar on /bookmarks",
    )
    assert len(queue.pending) == 1

    # before the grader-side confirmation mints it, the feature is NOT
    # mentionable: the FK from mentions to FEAT enforces the gate
    with pytest.raises(sqlite3.IntegrityError):
        add_mention(store, "FEAT-bulk-edit")

    result = queue.drain(
        store,
        FakeBrowse(),
        target=TARGET,
        digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )
    assert result.minted == ("FEAT-bulk-edit",)
    assert queue.pending == ()
    assert frontier_for(store, "FEAT-bulk-edit")["status"] == "newly-discovered"

    # now mentionable...
    add_mention(store, "FEAT-bulk-edit")
    # ...and authorable: the mint authored its manifest, and further scenario
    # manifests can reference it
    assert len(manifests_for(store, "FEAT-bulk-edit")) == 1
    store.conn.execute(
        "INSERT INTO scenario_manifests"
        " (feat_id, tier, manifest_json, created_at)"
        " VALUES ('FEAT-bulk-edit', 'should', '{}', '2026-06-10T00:00:00+00:00')"
    )
    assert len(manifests_for(store, "FEAT-bulk-edit")) == 2


def test_discovery_queue_requires_ui_evidence_and_skips_known(tmp_path):
    store = make_store(tmp_path)
    queue = DiscoveryQueue()
    with pytest.raises(RegistryError, match="UI evidence"):
        queue.enqueue(cand("bulk-edit"), ui_evidence="   ")

    mint_feat(store, cand("tag-filter"), "ev/t.json", target=TARGET, digest=DIGEST)
    queue.enqueue(cand("tag-filter"), ui_evidence="saw tag filtering")
    queue.enqueue(cand("ghost-panel"), ui_evidence="thought I saw a panel")
    result = queue.drain(
        store,
        FakeBrowse(absent=("ghost-panel",)),
        target=TARGET,
        digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )
    assert result.already_known == ("FEAT-tag-filter",)
    assert result.unconfirmed == ("ghost-panel",)
    assert result.minted == ()


# --- candidate validation ---------------------------------------------------------


def test_candidate_validation_rejects_malformed():
    with pytest.raises(RegistryError, match="kebab-case"):
        cand("Bad Key")
    with pytest.raises(RegistryError, match="tier"):
        cand("a-key", tier="critical")
    with pytest.raises(RegistryError, match="confirm_steps"):
        cand("a-key", confirm_steps=())
    with pytest.raises(RegistryError, match="browse request"):
        cand("a-key", confirm_steps=({"action": "goto"},))
    with pytest.raises(RegistryError, match="scenario_steps"):
        cand("a-key", scenario_steps=("",))
    with pytest.raises(RegistryError, match="behavior"):
        cand("a-key", behavior="  ")


def test_candidates_from_output_rejects_empty_and_duplicates():
    with pytest.raises(RegistryError, match="zero candidates"):
        candidates_from_output({"candidates": []})
    payload = {
        "key": "tag-filter",
        "area": "tags",
        "behavior": "filter by tag",
        "route": "urls.py",
        "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
        "scenario_steps": ["Filter by a tag"],
        "expected_outcome": "filtered list",
        "tier": "must",
    }
    with pytest.raises(RegistryError, match="duplicate"):
        candidates_from_output({"candidates": [payload, dict(payload)]})


# --- grader profile (R8 surface; R15 containment by construction) -----------------


def test_grader_profile_surface():
    profile = grader_profile(
        model="sonnet", max_turns=20, timeout_s=300.0,
        source_commands=("git -C source log",),
    )
    assert profile.role == "grader"
    assert profile.tools is None
    allowed = profile.allowed_tools
    assert {"Read", "Glob", "Grep", "LS"} <= set(allowed)
    assert "Bash(git -C source log:*)" in allowed
    # no write tools and no browse tool: UI evidence is orchestrator-mediated
    assert not any(
        t.startswith(("Write", "Edit", "MultiEdit")) for t in allowed
    )
    assert not any("browser" in t.lower() or "playwright" in t.lower()
                   for t in allowed)

    bare = grader_profile(model="sonnet", max_turns=5, timeout_s=60.0)
    assert not any(t.startswith("Bash") for t in bare.allowed_tools)
    with pytest.raises(RegistryError, match="source_commands"):
        grader_profile(model="sonnet", max_turns=5, timeout_s=60.0,
                       source_commands=("",))


# --- pinned source acquisition ------------------------------------------------------


class FakeRunner:
    def __init__(self, handler):
        self.calls: list[list[str]] = []
        self._handler = handler

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        return self._handler(argv)


def proc(argv, returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def git_ok(argv):
    if "describe" in argv:
        return proc(argv, stdout=SOURCE_TAG + "\n")
    return proc(argv)


def test_ensure_source_checkout_clones_pinned_tag(tmp_path):
    dest = tmp_path / "source"
    runner = FakeRunner(git_ok)
    assert ensure_source_checkout(dest, runner=runner) == dest
    clone = runner.calls[0]
    assert clone == [
        "git", "clone", "--depth", "1", "--branch", SOURCE_TAG,
        SOURCE_REPO_URL, str(dest),
    ], "source acquisition is pinned: depth-1 clone of the image's tag"
    assert "describe" in runner.calls[1], "the checkout is verified after clone"


def test_ensure_source_checkout_verifies_existing(tmp_path):
    dest = tmp_path / "source"
    (dest / ".git").mkdir(parents=True)
    runner = FakeRunner(git_ok)
    ensure_source_checkout(dest, runner=runner)
    assert len(runner.calls) == 1 and "describe" in runner.calls[0], (
        "an existing checkout is verified, never re-cloned"
    )


def test_ensure_source_checkout_tag_mismatch_is_loud(tmp_path):
    dest = tmp_path / "source"
    (dest / ".git").mkdir(parents=True)
    runner = FakeRunner(lambda argv: proc(argv, stdout="v1.44.2\n"))
    with pytest.raises(RegistryError, match="v1.44.2") as exc_info:
        ensure_source_checkout(dest, runner=runner)
    assert SOURCE_TAG in str(exc_info.value)
    assert "digest" in str(exc_info.value), (
        "the error must tie the tag to the image digest pin (R9)"
    )


def test_ensure_source_checkout_clone_failure_is_loud(tmp_path):
    runner = FakeRunner(lambda argv: proc(argv, returncode=128,
                                          stderr="fatal: repo not found"))
    with pytest.raises(RegistryError, match="repo not found"):
        ensure_source_checkout(tmp_path / "source", runner=runner)


def test_source_checkout_is_gitignored():
    gitignore = (
        Path(__file__).resolve().parent.parent
        / "targets" / "linkding" / ".gitignore"
    )
    assert "source/" in gitignore.read_text(encoding="utf-8"), (
        "the pinned checkout must never be committed (plan-003 U3)"
    )


# --- pre-research over the run_session seam (scripted fake) -------------------------


def candidate_payload(key: str, tier: str = "must") -> dict:
    return {
        "key": key,
        "area": "bookmarks",
        "behavior": f"user can {key.replace('-', ' ')}",
        "route": "bookmarks/urls.py",
        "confirm_steps": [
            {"action": "goto", "selector": "", "args": {"url": "/bookmarks"}}
        ],
        "scenario_steps": [f"Exercise {key}"],
        "expected_outcome": "visible",
        "tier": tier,
    }


def grader_step(output: dict) -> dict:
    return {
        "role": "grader",
        "envelope": {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 900,
            "num_turns": 3,
            "result": "enumerated",
            "total_cost_usd": 0.04,
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "structured_output": output,
        },
    }


def pre_research(tmp_path, outputs: list[dict], max_retries: int = 0):
    script = tmp_path / "grader-script.json"
    script.write_text(
        json.dumps({"steps": [grader_step(o) for o in outputs]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return run_pre_research(
        grader_profile(model="sonnet", max_turns=20, timeout_s=300.0),
        target=TARGET,
        source_root=tmp_path,
        transcript_path=tmp_path / "transcripts" / "pre-research.jsonl",
        max_retries=max_retries,
        ui_observations="nav shows Bookmarks, Tags, Settings",
        mode="scripted",
        script_path=script,
    )


def test_run_pre_research_scripted_round_trip(tmp_path):
    output = {
        "candidates": [
            candidate_payload("bookmark-create"),
            candidate_payload("tag-filter", tier="should"),
        ]
    }
    candidates = pre_research(tmp_path, [output])
    assert [c.key for c in candidates] == ["bookmark-create", "tag-filter"]
    assert all(isinstance(c, FeatureCandidate) for c in candidates)
    assert candidates[1].tier == "should"
    assert isinstance(candidates[0].confirm_steps, tuple)


def test_run_pre_research_rejects_duplicate_keys_via_contract(tmp_path):
    output = {
        "candidates": [
            candidate_payload("tag-filter"),
            candidate_payload("tag-filter"),
        ]
    }
    with pytest.raises(SessionSchemaViolation, match="duplicate"):
        pre_research(tmp_path, [output])


def test_run_pre_research_rejects_bad_tier_via_schema(tmp_path):
    output = {"candidates": [candidate_payload("tag-filter", tier="critical")]}
    with pytest.raises(SessionSchemaViolation):
        pre_research(tmp_path, [output])


def test_pre_research_prompt_names_the_three_enumeration_sources(tmp_path):
    prompt = build_pre_research_prompt(
        target=TARGET, source_root=tmp_path,
        ui_observations="nav shows Bookmarks",
    )
    assert "urls.py" in prompt
    assert "UI traversal" in prompt
    assert "source" in prompt
    assert "nav shows Bookmarks" in prompt
    assert "40 and 60" in prompt, "the R8 expectation rides the brief"
    assert "FEAT-<key>" in prompt, "key stability is the identity contract"
    # the schema the session is held to matches the parser
    assert PRE_RESEARCH_SCHEMA["required"] == ["candidates"]


# --- R8 expected-surface pins (live procedure asserts against these) ----------------


def test_expected_behavior_range_pinned(tmp_path):
    assert EXPECTED_BEHAVIOR_RANGE == (40, 60), (
        "R8: expected linkding surface is 40-60 testable behaviors; changing"
        " this is a deliberate re-research decision, not a tweak"
    )
    store = make_store(tmp_path)
    mint_feat(store, cand("tag-filter"), "ev/t.json", target=TARGET, digest=DIGEST)
    assert not registry_in_expected_range(store, TARGET)
