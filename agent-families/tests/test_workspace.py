"""plan-002 U2: template pin + workspace lifecycle + containment assert.

Fast tests run against a tiny fake template (instantiation, git discipline,
hashing, containment are template-content-agnostic). The real pinned template
is exercised two ways:

- ``test_committed_template_matches_lock`` — pure hashing, runs everywhere
  offline and catches template edits that forgot to regenerate the pin;
- the ``primed``-marked gate test — instantiates a workspace from the real
  template (node_modules copy included) and runs the actual gate commands
  (typecheck / lint / vitest). It skips when the template is unprimed (fresh
  clone before the one documented online ``npm ci``) or npm is missing, per
  the plan's offline contract.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_families.pipeline.workspace import (
    DEFAULT_LOCK_PATH,
    DEFAULT_TEMPLATE_DIR,
    ContainmentViolation,
    WorkspaceError,
    assert_containment,
    commit_iteration,
    instantiate_workspace,
    is_template_primed,
    read_template_lock,
    reset_to_ticket_start,
    tag_ticket_start,
    template_content_hash,
    transcript_tool_paths,
    verify_template,
    write_template_lock,
)

GATE_TIMEOUT_S = 600


# --- helpers -----------------------------------------------------------------


def make_fake_template(root: Path, *, primed: bool = True) -> Path:
    """A minimal template: package.json + .gitignore + source + node_modules."""
    template = root / "fake-template"
    template.mkdir(parents=True)
    (template / "package.json").write_text(
        json.dumps({"name": "fake-app", "private": True}), encoding="utf-8"
    )
    (template / ".gitignore").write_text(
        "node_modules/\ndist/\ndata/\n", encoding="utf-8", newline="\n"
    )
    src = template / "src"
    src.mkdir()
    (src / "index.ts").write_text("export const answer = 42\n", encoding="utf-8")
    if primed:
        nm = template / "node_modules"
        nm.mkdir()
        (nm / ".af-placeholder").write_text("installed\n", encoding="utf-8")
    return template


def make_workspace(tmp_path: Path):
    template = make_fake_template(tmp_path)
    return instantiate_workspace(template, tmp_path / "ws")


def git_out(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        check=True,
    ).stdout


def write_transcript(path: Path, events: list[dict]) -> Path:
    path.write_text(
        "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8", newline="\n"
    )
    return path


def tool_use_event(name: str, tool_input: dict) -> dict:
    """Shape of a stream-json assistant message carrying one tool_use block."""
    return {
        "type": "assistant",
        "message": {
            "content": [{"type": "tool_use", "name": name, "input": tool_input}]
        },
    }


# --- instantiation -----------------------------------------------------------


def test_instantiate_copies_template_and_inits_git(tmp_path):
    ws = make_workspace(tmp_path)
    assert (ws.root / "package.json").exists()
    assert (ws.root / "src" / "index.ts").exists()
    # node_modules is copied (the whole point of the priming model)...
    assert (ws.root / "node_modules" / ".af-placeholder").exists()
    # ...but never tracked: the template .gitignore rode along.
    tracked = git_out(ws.root, "ls-files").splitlines()
    assert "package.json" in tracked
    assert not any(t.startswith("node_modules") for t in tracked)
    # exactly one orchestrator-owned initial commit, clean tree
    assert git_out(ws.root, "rev-list", "--count", "HEAD").strip() == "1"
    assert git_out(ws.root, "status", "--porcelain").strip() == ""
    # byte-stability discipline
    assert git_out(ws.root, "config", "core.autocrlf").strip() == "false"


def test_instantiate_refuses_existing_dest(tmp_path):
    template = make_fake_template(tmp_path)
    dest = tmp_path / "ws"
    dest.mkdir()
    with pytest.raises(WorkspaceError, match="already exists"):
        instantiate_workspace(template, dest)


def test_instantiate_unprimed_template_is_actionable(tmp_path):
    """Simulated install failure: node_modules absent -> actionable message."""
    template = make_fake_template(tmp_path, primed=False)
    with pytest.raises(WorkspaceError, match=r"npm ci") as exc_info:
        instantiate_workspace(template, tmp_path / "ws")
    assert "not primed" in str(exc_info.value)
    assert str(template) in str(exc_info.value)


def test_instantiate_missing_template_is_actionable(tmp_path):
    with pytest.raises(WorkspaceError, match="package.json"):
        instantiate_workspace(tmp_path / "nope", tmp_path / "ws")


def test_instantiate_verifies_lock_when_given(tmp_path):
    template = make_fake_template(tmp_path)
    lock = tmp_path / "template.lock"
    write_template_lock(template, lock)
    ws = instantiate_workspace(template, tmp_path / "ws-ok", lock_path=lock)
    assert ws.root.exists()
    (template / "src" / "index.ts").write_text("drifted\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="hash mismatch"):
        instantiate_workspace(template, tmp_path / "ws-drift", lock_path=lock)


# --- iteration commits (R11 plumbing) ----------------------------------------


def test_commit_iteration_one_commit_per_call_with_metadata(tmp_path):
    ws = make_workspace(tmp_path)
    (ws.root / "src" / "new.ts").write_text("export {}\n", encoding="utf-8")
    sha1 = commit_iteration(ws, "TKT-1", 1)
    assert git_out(ws.root, "rev-list", "--count", "HEAD").strip() == "2"
    # a no-change iteration still produces exactly one commit (--allow-empty)
    sha2 = commit_iteration(ws, "TKT-1", 2)
    assert sha2 != sha1
    assert git_out(ws.root, "rev-list", "--count", "HEAD").strip() == "3"
    log = git_out(ws.root, "log", "--format=%s", "-2")
    assert "ticket=TKT-1 iteration=2" in log
    assert "ticket=TKT-1 iteration=1" in log
    assert git_out(ws.root, "rev-parse", "HEAD").strip() == sha2


# --- ticket-start tag + reset (R2 escalation arm) -----------------------------


def test_ticket_start_tag_and_reset_restore_pre_ticket_state(tmp_path):
    ws = make_workspace(tmp_path)
    original = (ws.root / "src" / "index.ts").read_text(encoding="utf-8")
    tag_ticket_start(ws, "TKT-9")

    # the ticket's iterations: tracked edit + new file, committed; then an
    # uncommitted untracked file on top.
    (ws.root / "src" / "index.ts").write_text("export const broken = 1\n", encoding="utf-8")
    (ws.root / "src" / "extra.ts").write_text("export {}\n", encoding="utf-8")
    commit_iteration(ws, "TKT-9", 1)
    (ws.root / "src" / "untracked.ts").write_text("junk\n", encoding="utf-8")

    reset_to_ticket_start(ws, "TKT-9")

    assert (ws.root / "src" / "index.ts").read_text(encoding="utf-8") == original
    assert not (ws.root / "src" / "extra.ts").exists()
    assert not (ws.root / "src" / "untracked.ts").exists()
    assert git_out(ws.root, "status", "--porcelain").strip() == ""
    # clean ran WITHOUT -x: ignored node_modules survived (load-bearing for R3)
    assert (ws.root / "node_modules" / ".af-placeholder").exists()


def test_reset_to_unknown_ticket_tag_is_actionable(tmp_path):
    ws = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="no start tag"):
        reset_to_ticket_start(ws, "TKT-404")


def test_tag_rejects_unsafe_ticket_id(tmp_path):
    ws = make_workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="tag-safe"):
        tag_ticket_start(ws, "bad ticket/id")


# --- template content hash + lock (R18 pin) -----------------------------------


def test_template_hash_stable_under_crlf_and_sensitive_to_content(tmp_path):
    a = make_fake_template(tmp_path / "a")
    b = make_fake_template(tmp_path / "b")
    # same content, CRLF flavor on one side (autocrlf checkout simulation)
    lf = (a / "src" / "index.ts").read_text(encoding="utf-8")
    (b / "src" / "index.ts").write_bytes(lf.replace("\n", "\r\n").encode("utf-8"))
    assert template_content_hash(a) == template_content_hash(b)
    (b / "src" / "index.ts").write_text("changed\n", encoding="utf-8")
    assert template_content_hash(a) != template_content_hash(b)


def test_template_hash_ignores_installed_and_generated_trees(tmp_path):
    template = make_fake_template(tmp_path)
    before = template_content_hash(template)
    (template / "node_modules" / "left-pad").mkdir(parents=True)
    (template / "node_modules" / "left-pad" / "index.js").write_text(
        "module.exports = 1\n", encoding="utf-8"
    )
    (template / "dist").mkdir()
    (template / "dist" / "bundle.js").write_text("built\n", encoding="utf-8")
    (template / "data").mkdir()
    (template / "data" / "app.db").write_bytes(b"\x00sqlite")
    (template / "debug.log").write_text("noise\n", encoding="utf-8")
    assert template_content_hash(template) == before


def test_write_and_verify_template_lock_roundtrip(tmp_path):
    template = make_fake_template(tmp_path)
    lock = tmp_path / "template.lock"
    pinned = write_template_lock(template, lock)
    assert read_template_lock(lock) == pinned
    assert verify_template(template, lock) == pinned


def test_verify_template_hash_mismatch_is_actionable(tmp_path):
    template = make_fake_template(tmp_path)
    lock = tmp_path / "template.lock"
    write_template_lock(template, lock)
    (template / "src" / "index.ts").write_text("edited\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="hash mismatch") as exc_info:
        verify_template(template, lock)
    # names both hashes and the remediation path
    assert "write_template_lock" in str(exc_info.value)
    assert "sha256:" in str(exc_info.value)


def test_missing_lock_is_actionable(tmp_path):
    template = make_fake_template(tmp_path)
    with pytest.raises(WorkspaceError, match="write_template_lock"):
        verify_template(template, tmp_path / "absent.lock")


def test_malformed_lock_is_actionable(tmp_path):
    lock = tmp_path / "template.lock"
    lock.write_text("# only comments\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="malformed template lock"):
        read_template_lock(lock)


def test_committed_template_matches_lock():
    """Drift guard: editing template/ without regenerating template.lock fails."""
    assert verify_template(DEFAULT_TEMPLATE_DIR, DEFAULT_LOCK_PATH).startswith(
        "sha256:"
    )


# --- containment assert (R8) ---------------------------------------------------


def test_containment_passes_on_inside_paths(tmp_path):
    ws = make_workspace(tmp_path)
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [
            tool_use_event("Write", {"file_path": "src/feature.ts", "content": "x"}),
            tool_use_event(
                "Edit", {"file_path": str(ws.root / "src" / "index.ts")}
            ),
            tool_use_event("Bash", {"command": "npm test", "cwd": str(ws.root)}),
            # non-write tools carry no checked paths
            tool_use_event("Read", {"file_path": "C:/Windows/system.ini"}),
        ],
    )
    touched = assert_containment(transcript, ws.root)
    assert len(touched) == 3


def test_containment_fails_on_outside_write(tmp_path):
    ws = make_workspace(tmp_path)
    outside = tmp_path / "elsewhere" / "evil.ts"
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [tool_use_event("Write", {"file_path": str(outside), "content": "x"})],
    )
    with pytest.raises(ContainmentViolation, match="evil.ts"):
        assert_containment(transcript, ws.root)


def test_containment_fails_on_relative_escape(tmp_path):
    ws = make_workspace(tmp_path)
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [tool_use_event("Edit", {"file_path": "../escape.ts"})],
    )
    with pytest.raises(ContainmentViolation, match="escape.ts"):
        assert_containment(transcript, ws.root)


def test_containment_fails_on_outside_bash_cwd(tmp_path):
    ws = make_workspace(tmp_path)
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [tool_use_event("Bash", {"command": "rm -rf .", "cwd": str(tmp_path)})],
    )
    with pytest.raises(ContainmentViolation, match="Bash"):
        assert_containment(transcript, ws.root)


def test_containment_checks_harness_repo_clean(tmp_path):
    ws = make_workspace(tmp_path)
    harness = tmp_path / "harness"
    harness.mkdir()
    subprocess.run(["git", "-C", str(harness), "init"], check=True, capture_output=True)
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [tool_use_event("Write", {"file_path": "src/ok.ts", "content": "x"})],
    )
    assert assert_containment(transcript, ws.root, harness_repo=harness)
    (harness / "stray.txt").write_text("outside write\n", encoding="utf-8")
    with pytest.raises(ContainmentViolation, match="dirty"):
        assert_containment(transcript, ws.root, harness_repo=harness)


def test_containment_rejects_malformed_transcript(tmp_path):
    ws = make_workspace(tmp_path)
    bad = tmp_path / "t.jsonl"
    bad.write_text('{"type": "assistant"\nnot json at all\n', encoding="utf-8")
    with pytest.raises(WorkspaceError, match="malformed transcript line"):
        assert_containment(bad, ws.root)


def test_transcript_tool_paths_extracts_only_write_targets(tmp_path):
    transcript = write_transcript(
        tmp_path / "t.jsonl",
        [
            tool_use_event("Write", {"file_path": "a.ts"}),
            tool_use_event("NotebookEdit", {"notebook_path": "n.ipynb"}),
            tool_use_event("Bash", {"command": "ls"}),  # no cwd -> nothing
            tool_use_event("Grep", {"pattern": "x", "path": "src"}),
        ],
    )
    assert transcript_tool_paths(transcript) == [
        ("Write", "a.ts"),
        ("NotebookEdit", "n.ipynb"),
    ]


# --- the real pinned template (primed-only gate proof) -------------------------

primed = pytest.mark.skipif(
    not is_template_primed(DEFAULT_TEMPLATE_DIR) or shutil.which("npm") is None,
    reason=(
        "pinned template not primed (run `npm ci` in agent-families/template "
        "once, online) or npm unavailable"
    ),
)


@primed
def test_gate_commands_pass_on_fresh_workspace(tmp_path):
    """R18 acceptance: instantiate from the real template, gate green offline."""
    ws = instantiate_workspace(
        DEFAULT_TEMPLATE_DIR, tmp_path / "ws", lock_path=DEFAULT_LOCK_PATH
    )
    npm = shutil.which("npm")
    assert npm is not None
    for script in ("typecheck", "lint", "test"):
        proc = subprocess.run(
            [npm, "run", script],
            cwd=ws.root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GATE_TIMEOUT_S,
        )
        assert proc.returncode == 0, (
            f"gate command `npm run {script}` failed in fresh workspace:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
