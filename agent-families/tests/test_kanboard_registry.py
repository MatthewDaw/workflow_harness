"""plan-007 U13a: Kanboard registry pre-research — the registry machinery,
de-pinned off linkding, runs on a second target (R12, KTD8).

Fully offline, like its FEAT/DEC siblings (test_registry.py,
test_decision_registry.py): the browse channel is a scripted fake, git is a
recorded fake runner, and the pre-research session rides the Phase 1 scripted
agent. Zero quota, no docker, no network — the LIVE Kanboard mint + hand-check
is the documented procedure in ``targets/kanboard/README.md`` (pending-docker).

## Conformance

U13a Test scenarios -> enforcing tests:

- "linkding extraction unchanged post-parameterization (regression)"
  -> ``test_linkding_pins_unchanged_after_depin`` (the back-compat constants AND
     LINKDING_REGISTRY_CONFIG still carry the historical linkding pins) +
     ``test_linkding_prompt_byte_identical_after_depin`` (the linkding brief
     resolves the same 18-areas / 40-60-behaviors numbers with no config arg) +
     ``test_ensure_source_checkout_linkding_unchanged`` (linkding source
     acquisition still clones the linkding repo at its pinned tag)
- "Kanboard extraction mints FEAT+DEC with evidence"
  -> ``test_kanboard_pre_research_mints_feat_and_dec_with_evidence`` (a scripted
     Kanboard pre-research pass -> refresh_registry + refresh_decisions mint FEAT
     and DEC rows, each carrying a digest-stamped runtime-evidence capture)
- "digest stamping keys the blur cache correctly"
  -> ``test_digest_stamping_keys_the_blur_cache`` (minted Kanboard rows are
     stamped with the Kanboard image digest, which is the ``entry_digest`` the
     founder_blur_cache keys on; a different digest is a distinct cache key, the
     same digest collides on the UNIQUE key)

Supporting scenarios -> tests:

- the de-pin is a real per-target config, unknown targets are loud
  -> ``test_registry_config_for_unknown_target_is_loud``
- Kanboard carries its own (larger) pins, sourced from the image tag
  -> ``test_kanboard_config_pins`` + ``test_kanboard_prompt_uses_kanboard_surface``
- the expected-range check is per-target (config-driven)
  -> ``test_registry_in_expected_range_is_per_target``
- the pinned Kanboard source checkout is never committed
  -> ``test_kanboard_source_checkout_gitignored``
- the per-target live procedure is a committed artifact
  -> ``test_kanboard_readme_documents_live_procedure``
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

import pytest

from agent_families.grading.benchmark import KANBOARD_IMAGE_DIGEST, KANBOARD_IMAGE_TAG
from agent_families.grading.registry import (
    EXPECTED_AREA_COUNT,
    EXPECTED_BEHAVIOR_RANGE,
    KANBOARD_REGISTRY_CONFIG,
    LINKDING_REGISTRY_CONFIG,
    SOURCE_REPO_URL,
    SOURCE_TAG,
    DecisionCandidate,
    FeatureCandidate,
    RegistryError,
    TargetRegistryConfig,
    build_pre_research_prompt,
    decision_rows,
    ensure_source_checkout,
    grader_profile,
    mint_feat,
    refresh_decisions,
    refresh_registry,
    registry_config_for,
    registry_in_expected_range,
    registry_rows,
    run_pre_research_full,
)
from agent_families.store import Store

KANBOARD = "kanboard"
DIGEST = KANBOARD_IMAGE_DIGEST


# --- helpers -------------------------------------------------------------------


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def feat(key: str, **kw) -> FeatureCandidate:
    defaults = dict(
        key=key,
        area="tasks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="app/Controller/TaskController.php",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/board"}},
            {"action": "snapshot", "selector": f"[data-test={key}]", "args": {}},
        ),
        scenario_steps=("Open the board", f"Exercise {key}"),
        expected_outcome="the behavior is observable on the page",
        tier="must",
    )
    return FeatureCandidate(**{**defaults, **kw})


def dec(key: str, category: str = "auth-gated", **kw) -> DecisionCandidate:
    defaults = dict(
        key=key,
        category=category,
        description=f"the app resolved {key.replace('-', ' ')}",
        route="app/Core/Security/Authentication.php",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/settings"}},
            {"action": "snapshot", "selector": f"[data-dec={key}]", "args": {}},
        ),
    )
    return DecisionCandidate(**{**defaults, **kw})


class FakeBrowse:
    """The orchestrator-mediated browse channel, scripted (R15 pattern):
    answers ``ok`` with an a11y snapshot, except for planted-absent selectors."""

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


def write_script(tmp_path: Path, output: dict) -> Path:
    script = tmp_path / "grader-script.json"
    script.write_text(
        json.dumps({"steps": [grader_step(output)]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return script


# --- REGRESSION: linkding unchanged post-parameterization ----------------------


def test_linkding_pins_unchanged_after_depin():
    """De-pinning kept the linkding pins byte-identical: the back-compat module
    constants AND the linkding config still carry the historical values."""
    assert SOURCE_REPO_URL == "https://github.com/sissbruecker/linkding.git"
    assert SOURCE_TAG == "v1.45.0"
    assert EXPECTED_AREA_COUNT == 18
    assert EXPECTED_BEHAVIOR_RANGE == (40, 60)

    # the constants are now exactly the linkding config's fields (aliases)
    cfg = LINKDING_REGISTRY_CONFIG
    assert cfg.target == "linkding"
    assert SOURCE_REPO_URL == cfg.source_repo_url
    assert SOURCE_TAG == cfg.source_tag
    assert EXPECTED_AREA_COUNT == cfg.expected_area_count
    assert EXPECTED_BEHAVIOR_RANGE == cfg.expected_behavior_range
    assert registry_config_for("linkding") is cfg


def test_linkding_prompt_byte_identical_after_depin(tmp_path):
    """The linkding enumeration brief is unchanged: with no config arg it
    resolves the linkding surface numbers exactly as before."""
    prompt = build_pre_research_prompt(
        target="linkding", source_root=tmp_path,
        ui_observations="nav shows Bookmarks",
    )
    # the historical R8 brief survives verbatim
    assert "roughly 18 feature areas" in prompt
    assert "between 40 and 60 testable behaviors" in prompt
    assert "FEAT-<key>" in prompt
    assert "nav shows Bookmarks" in prompt
    # passing the config explicitly yields the same brief
    assert prompt == build_pre_research_prompt(
        target="linkding", source_root=tmp_path,
        ui_observations="nav shows Bookmarks",
        config=LINKDING_REGISTRY_CONFIG,
    )


def test_ensure_source_checkout_linkding_unchanged(tmp_path):
    """linkding source acquisition is byte-identical: the defaulted repo_url/tag
    still clone the linkding repo at its pinned tag."""
    dest = tmp_path / "source"

    def git_ok(argv):
        if "describe" in argv:
            return proc(argv, stdout=SOURCE_TAG + "\n")
        return proc(argv)

    runner = FakeRunner(git_ok)
    assert ensure_source_checkout(dest, runner=runner) == dest
    assert runner.calls[0] == [
        "git", "clone", "--depth", "1", "--branch", SOURCE_TAG,
        SOURCE_REPO_URL, str(dest),
    ]


# --- the de-pin is a real per-target config ------------------------------------


def test_registry_config_for_unknown_target_is_loud():
    """Pre-research onboarding is deliberate: an unknown target is a hard error,
    never silently defaulted to another target's numbers."""
    with pytest.raises(RegistryError, match="no registry pre-research config"):
        registry_config_for("nope")
    # the prompt builder resolves through the same lookup, so it is loud too
    with pytest.raises(RegistryError, match="no registry pre-research config"):
        build_pre_research_prompt(target="nope", source_root="x")


def test_target_registry_config_validates():
    with pytest.raises(RegistryError, match="non-empty"):
        TargetRegistryConfig("kanboard", "", "v1", 10, (5, 9))
    with pytest.raises(RegistryError, match="expected_behavior_range"):
        TargetRegistryConfig("t", "url", "v1", 10, (60, 40))
    with pytest.raises(RegistryError, match="expected_area_count"):
        TargetRegistryConfig("t", "url", "v1", 0, (5, 9))


# --- Kanboard carries its own pins, sourced from the image tag -----------------


def test_kanboard_config_pins():
    cfg = KANBOARD_REGISTRY_CONFIG
    assert cfg.target == KANBOARD
    assert cfg.source_repo_url == "https://github.com/kanboard/kanboard.git"
    # the source tag is the tag half of the image pin (single source of truth)
    assert cfg.source_tag == KANBOARD_IMAGE_TAG == "v1.2.46"
    assert registry_config_for(KANBOARD) is cfg
    # KTD8: Kanboard's surface is materially larger than linkding's
    klo, khi = cfg.expected_behavior_range
    llo, lhi = LINKDING_REGISTRY_CONFIG.expected_behavior_range
    assert klo > llo and khi > lhi
    assert cfg.expected_area_count > LINKDING_REGISTRY_CONFIG.expected_area_count


def test_kanboard_prompt_uses_kanboard_surface(tmp_path):
    prompt = build_pre_research_prompt(
        target=KANBOARD, source_root=tmp_path, ui_observations="nav shows Board"
    )
    lo, hi = KANBOARD_REGISTRY_CONFIG.expected_behavior_range
    assert f"roughly {KANBOARD_REGISTRY_CONFIG.expected_area_count} feature areas" in prompt
    assert f"between {lo} and {hi} testable behaviors" in prompt
    # the DEC sibling brief rides the same pass (KTD1), target-independent
    assert "decisions" in prompt and "DEC-<key>" in prompt
    # and it is NOT the linkding brief
    assert "between 40 and 60 testable behaviors" not in prompt


# --- Kanboard extraction mints FEAT + DEC with evidence ------------------------


def test_kanboard_pre_research_mints_feat_and_dec_with_evidence(tmp_path):
    """A scripted Kanboard pre-research pass yields both arrays; confirm-on-
    runtime mints FEAT and DEC rows, each carrying a digest-stamped capture."""
    store = make_store(tmp_path)
    output = {
        "candidates": [
            {
                "key": "task-create",
                "area": "tasks",
                "behavior": "user can create a task",
                "route": "app/Controller/TaskCreationController.php",
                "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
                "scenario_steps": ["Create a task"],
                "expected_outcome": "the task appears on the board",
                "tier": "must",
            }
        ],
        "decisions": [
            {
                "key": "auth-session-cookie",
                "category": "auth-gated",
                "description": "session cookie + role enum",
                "route": "app/Core/Security/Authentication.php",
                "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
            }
        ],
    }
    script = write_script(tmp_path, output)
    pre = run_pre_research_full(
        grader_profile(model="sonnet", max_turns=20, timeout_s=300.0),
        target=KANBOARD,
        source_root=tmp_path,
        transcript_path=tmp_path / "transcripts" / "pre.jsonl",
        max_retries=0,
        ui_observations="nav shows Board, Projects, Settings",
        mode="scripted",
        script_path=script,
    )
    assert [c.key for c in pre.candidates] == ["task-create"]
    assert [d.key for d in pre.decisions] == ["auth-session-cookie"]

    browse = FakeBrowse()
    feat_refresh = refresh_registry(
        store, pre.candidates, browse,
        target=KANBOARD, digest=DIGEST, evidence_dir=tmp_path / "evidence",
    )
    dec_refresh = refresh_decisions(
        store, pre.decisions, browse,
        target=KANBOARD, digest=DIGEST, evidence_dir=tmp_path / "evidence",
    )
    assert feat_refresh.minted == ("FEAT-task-create",)
    assert dec_refresh.minted == ("DEC-auth-session-cookie",)

    (frow,) = registry_rows(store, KANBOARD)
    (drow,) = decision_rows(store, KANBOARD)
    for row in (frow, drow):
        assert row["target"] == KANBOARD
        assert row["status"] == "confirmed"
        assert row["digest"] == DIGEST, "rows stamped with the Kanboard image digest"
        assert row["evidence_ref"] and Path(row["evidence_ref"]).exists()
        evidence = json.loads(Path(row["evidence_ref"]).read_text(encoding="utf-8"))
        assert evidence["digest"] == DIGEST


# --- Kanboard source acquisition is pinned -------------------------------------


def test_ensure_source_checkout_clones_kanboard_pin(tmp_path):
    """The de-pinned source acquisition clones the Kanboard repo at the tag half
    of its image pin."""
    dest = tmp_path / "source"
    cfg = KANBOARD_REGISTRY_CONFIG

    def git_ok(argv):
        if "describe" in argv:
            return proc(argv, stdout=cfg.source_tag + "\n")
        return proc(argv)

    runner = FakeRunner(git_ok)
    ensure_source_checkout(
        dest, runner=runner, repo_url=cfg.source_repo_url, tag=cfg.source_tag
    )
    assert runner.calls[0] == [
        "git", "clone", "--depth", "1", "--branch", "v1.2.46",
        "https://github.com/kanboard/kanboard.git", str(dest),
    ]


def test_ensure_source_checkout_kanboard_tag_mismatch_is_loud(tmp_path):
    dest = tmp_path / "source"
    (dest / ".git").mkdir(parents=True)
    cfg = KANBOARD_REGISTRY_CONFIG
    runner = FakeRunner(lambda argv: proc(argv, stdout="v1.2.45\n"))
    with pytest.raises(RegistryError, match="v1.2.45") as exc_info:
        ensure_source_checkout(
            dest, runner=runner, repo_url=cfg.source_repo_url, tag=cfg.source_tag
        )
    assert cfg.source_tag in str(exc_info.value)


# --- the expected-range check is per-target ------------------------------------


def test_registry_in_expected_range_is_per_target(tmp_path):
    """The range check is config-driven: linkding keeps 40-60, and a config
    override drives the band."""
    store = make_store(tmp_path)
    # tiny override config proves the range, not the target, is what's read
    tiny = TargetRegistryConfig(
        target=KANBOARD,
        source_repo_url=KANBOARD_REGISTRY_CONFIG.source_repo_url,
        source_tag=KANBOARD_REGISTRY_CONFIG.source_tag,
        expected_area_count=1,
        expected_behavior_range=(1, 2),
    )
    assert not registry_in_expected_range(store, KANBOARD, config=tiny)  # 0 rows
    mint_feat(store, feat("a"), "ev/a.json", target=KANBOARD, digest=DIGEST)
    assert registry_in_expected_range(store, KANBOARD, config=tiny)  # 1 in [1,2]
    mint_feat(store, feat("b"), "ev/b.json", target=KANBOARD, digest=DIGEST)
    mint_feat(store, feat("c"), "ev/c.json", target=KANBOARD, digest=DIGEST)
    assert not registry_in_expected_range(store, KANBOARD, config=tiny)  # 3 > 2

    # default lookup uses Kanboard's real (large) band: 3 confirmed << 55
    assert not registry_in_expected_range(store, KANBOARD)


# --- digest stamping keys the blur cache ---------------------------------------


def test_digest_stamping_keys_the_blur_cache(tmp_path):
    """The image digest stamped on minted rows is the ``entry_digest`` the
    founder_blur_cache keys on (KTD3): same digest collides on the UNIQUE key, a
    re-extraction digest is a distinct cache key."""
    store = make_store(tmp_path)
    browse = FakeBrowse()
    refresh_registry(
        store, [feat("task-create")], browse,
        target=KANBOARD, digest=DIGEST, evidence_dir=tmp_path / "evidence",
    )
    refresh_decisions(
        store, [dec("auth-session-cookie")], browse,
        target=KANBOARD, digest=DIGEST, evidence_dir=tmp_path / "evidence",
    )
    # the registry rows are stamped with the Kanboard image digest (not linkding's)
    (frow,) = registry_rows(store, KANBOARD)
    (drow,) = decision_rows(store, KANBOARD)
    assert frow["digest"] == DIGEST
    assert drow["digest"] == DIGEST

    def insert_blur(ref_kind: str, ref_id: str, entry_digest: str) -> None:
        store.conn.execute(
            "INSERT INTO founder_blur_cache"
            " (target, ref_kind, ref_id, entry_digest, seed, params_hash)"
            " VALUES (?, ?, ?, ?, 7, 'p0')",
            (KANBOARD, ref_kind, ref_id, entry_digest),
        )

    # blur cache keyed on the registry row's stamped digest, per ref kind
    insert_blur("feat", frow["id"], frow["digest"])
    insert_blur("dec", drow["id"], drow["digest"])
    cached = store.conn.execute(
        "SELECT entry_digest FROM founder_blur_cache ORDER BY ref_kind"
    ).fetchall()
    assert [r["entry_digest"] for r in cached] == [DIGEST, DIGEST]

    # the SAME (target, ref, entry_digest, seed, params, prompt_set) collides:
    # the digest is part of the cache key
    with pytest.raises(sqlite3.IntegrityError):
        insert_blur("feat", frow["id"], frow["digest"])

    # a re-extraction at a DIFFERENT digest is a distinct cache key (the cache
    # invalidates on a registry re-extraction precisely because the digest keys it)
    other_digest = "sha256:" + "ff" * 32
    insert_blur("feat", frow["id"], other_digest)
    assert store.conn.execute(
        "SELECT COUNT(*) AS n FROM founder_blur_cache WHERE ref_id = ?",
        (frow["id"],),
    ).fetchone()["n"] == 2


# --- committed per-target artifacts --------------------------------------------


def _kanboard_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "targets" / "kanboard"


def test_kanboard_source_checkout_gitignored():
    gitignore = _kanboard_dir() / ".gitignore"
    assert "source/" in gitignore.read_text(encoding="utf-8"), (
        "the pinned Kanboard source checkout must never be committed (U13a)"
    )


def test_kanboard_readme_documents_live_procedure():
    readme = (_kanboard_dir() / "README.md").read_text(encoding="utf-8")
    assert "ensure_source_checkout" in readme
    assert "pending-docker" in readme or "pending live" in readme.lower()
    assert "v1.2.46" in readme
