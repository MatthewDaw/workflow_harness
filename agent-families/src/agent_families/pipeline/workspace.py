"""Workspace lifecycle: pinned-template instantiation, git discipline, containment.

Plan-002 U2 (R18, R8, R11 plumbing). The pinned output-stack template lives at
``agent-families/template/``; a workspace is created by **copying the
once-installed template directory, node_modules included** — ``npm ci`` is
template maintenance (one documented online step, like Phase 0's model-cache
priming), never per-workspace work. That copy model is what keeps the offline
test contract honest (R7).

Template pin (R18 deviation, recorded): the plan says "referenced by commit
hash in config", but the template is a directory inside the harness monorepo —
it has no commit hash of its own, and any repo commit would move it without
the template changing. The pin is therefore a **content hash** over the
template's source files (sha256, newline-normalized so ``core.autocrlf``
checkouts hash identically, excluding node_modules/build outputs), stored with
provenance in ``agent-families/template.lock`` — the same revert-independence
rationale R16 applies to prompt sets. ``thresholds.toml`` is untouched: the
hash is a pin, not a tunable, and its loader (Phase 0 U1) is frozen this wave.

Git discipline (R11/R2/R3 plumbing):

- the orchestrator owns every commit — :func:`commit_iteration` makes exactly
  one commit per call (``--allow-empty``) stamped with ticket/iteration;
- :func:`tag_ticket_start` tags each ticket's starting commit, the escalation
  reset target (R2); :func:`reset_to_ticket_start` / :func:`reset_hard` use
  ``git reset --hard`` + ``git clean -fd`` (**no** ``-x`` — the template's
  ``.gitignore`` covering node_modules and build outputs is what makes the
  clean safe, R3);
- workspace repos are created with ``core.autocrlf=false`` so iteration diffs
  are byte-stable on Windows.

Containment (R8): :func:`assert_containment` scans a session's JSONL
transcript for tool-use file paths (Write/Edit targets, Bash cwd) and asserts
every touched path resolves under the workspace root, plus asserts the harness
repo's own ``git status`` stays clean. A workspace-internal git diff is
structurally incapable of seeing outside writes, so it is never used for
containment (only for ``files_touched`` derivation elsewhere). No sandboxing —
this is an assert, per the security non-goal.

Retention: workspaces are kept after the run (repro commands re-execute
against them); nothing in this module deletes a workspace.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePath

# agent-families/ (src layout, editable install: __file__ is under
# agent-families/src/agent_families/pipeline/).
_AGENT_FAMILIES_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TEMPLATE_DIR = _AGENT_FAMILIES_ROOT / "template"
DEFAULT_LOCK_PATH = _AGENT_FAMILIES_ROOT / "template.lock"

# Mirrors template/.gitignore: everything machine-generated stays out of the
# content hash so priming/building/running never changes the pin.
HASH_EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        "dist",
        ".vite",
        "data",
        "test-results",
        "playwright-report",
        "coverage",
    }
)
HASH_EXCLUDED_SUFFIXES = (".tsbuildinfo", ".log")
HASH_EXCLUDED_NAMES = frozenset({".env"})

HASH_PREFIX = "sha256:"

# Tool-use input keys that name write targets (R8). Bash is checked via its
# optional cwd; its command string is not parsed (the assert is transcript
# path discipline, not a sandbox).
_WRITE_TOOL_PATH_KEYS: dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}

_PRIMING_HINT = (
    "Prime it once (online template maintenance): run `npm ci` in {template}. "
    "Workspace instantiation only copies the installed template; it never "
    "installs (plan-002 R18)."
)


class WorkspaceError(Exception):
    """Base for every workspace-lifecycle failure."""


class ContainmentViolation(WorkspaceError):
    """A session transcript touched paths outside its workspace (R8)."""


@dataclass(frozen=True)
class Workspace:
    """A live workspace: a git repo instantiated from the pinned template."""

    root: Path


# --- template pin (content hash + lock file) --------------------------------


def _hash_excluded(rel: PurePath) -> bool:
    if any(part in HASH_EXCLUDED_DIRS for part in rel.parts):
        return True
    name = rel.name
    if name in HASH_EXCLUDED_NAMES or name.startswith(".env."):
        return True
    return name.endswith(HASH_EXCLUDED_SUFFIXES)


def template_content_hash(template_dir: Path | str = DEFAULT_TEMPLATE_DIR) -> str:
    """Deterministic content hash of the template's source files.

    sha256 over (sorted posix relpath, newline-normalized bytes) pairs.
    CRLF is normalized to LF so the hash is identical across
    ``core.autocrlf`` checkout flavors.
    """
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        raise WorkspaceError(f"template directory not found: {template_dir}")
    digest = hashlib.sha256()
    files = sorted(
        (
            p.relative_to(template_dir)
            for p in template_dir.rglob("*")
            if p.is_file() and not _hash_excluded(p.relative_to(template_dir))
        ),
        key=lambda rel: rel.as_posix(),
    )
    if not files:
        raise WorkspaceError(f"template directory has no source files: {template_dir}")
    for rel in files:
        content = (template_dir / rel).read_bytes().replace(b"\r\n", b"\n")
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return HASH_PREFIX + digest.hexdigest()


def read_template_lock(lock_path: Path | str = DEFAULT_LOCK_PATH) -> str:
    """Read the pinned hash from ``template.lock`` (comments/# lines allowed)."""
    lock_path = Path(lock_path)
    if not lock_path.exists():
        raise WorkspaceError(
            f"template lock not found: {lock_path}\n"
            "Generate it after template maintenance with "
            "agent_families.pipeline.workspace.write_template_lock()."
        )
    pins = [
        line.strip()
        for line in lock_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if len(pins) != 1 or not pins[0].startswith(HASH_PREFIX):
        raise WorkspaceError(
            f"malformed template lock {lock_path}: expected exactly one "
            f"'{HASH_PREFIX}<hex>' line, got {pins!r}"
        )
    return pins[0]


def write_template_lock(
    template_dir: Path | str = DEFAULT_TEMPLATE_DIR,
    lock_path: Path | str = DEFAULT_LOCK_PATH,
) -> str:
    """Pin the current template content hash (template maintenance only)."""
    pinned = template_content_hash(template_dir)
    Path(lock_path).write_text(
        "# Pinned content hash of agent-families/template/ (plan-002 R18).\n"
        "# sha256 over the template's source files (sorted posix relpaths,\n"
        "# newline-normalized; node_modules/build outputs excluded). Regenerate\n"
        "# ONLY after intentional template maintenance, via\n"
        "# agent_families.pipeline.workspace.write_template_lock(), then re-verify\n"
        "# template boot: npm ci && npm run typecheck && npm run lint && npm test.\n"
        f"{pinned}\n",
        encoding="utf-8",
        newline="\n",
    )
    return pinned


def verify_template(
    template_dir: Path | str = DEFAULT_TEMPLATE_DIR,
    lock_path: Path | str = DEFAULT_LOCK_PATH,
) -> str:
    """Assert the template matches its pinned hash; return the hash (R18).

    Called at orchestrator startup so an unpinned template edit fails loudly
    before any run builds on it.
    """
    pinned = read_template_lock(lock_path)
    actual = template_content_hash(template_dir)
    if actual != pinned:
        raise WorkspaceError(
            f"template hash mismatch: {Path(template_dir)} hashes to {actual} "
            f"but {Path(lock_path)} pins {pinned}. If the template change is "
            "intentional, regenerate the pin with write_template_lock() and "
            "re-verify template boot; otherwise revert the template edit."
        )
    return pinned


def is_template_primed(template_dir: Path | str = DEFAULT_TEMPLATE_DIR) -> bool:
    """True if the template carries an installed node_modules tree."""
    return (Path(template_dir) / "node_modules").is_dir()


# --- git plumbing ------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    """Run git in ``root``; raise :class:`WorkspaceError` with stderr on failure."""
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise WorkspaceError(
            f"git {' '.join(args)} failed in {root} "
            f"(exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc.stdout


# --- instantiation -----------------------------------------------------------


def instantiate_workspace(
    template_dir: Path | str = DEFAULT_TEMPLATE_DIR,
    dest: Path | str = None,  # type: ignore[assignment]
    *,
    lock_path: Path | str | None = None,
) -> Workspace:
    """Copy the once-installed template to ``dest`` and git-init it.

    - refuses a missing/implausible template or an already-existing ``dest``;
    - an unprimed template (no node_modules — i.e. the install never happened
      or failed) is an actionable error, because instantiation never installs;
    - with ``lock_path``, the template is verified against its pin first;
    - the new repo gets ``core.autocrlf=false`` (byte-stable diffs) and one
      initial commit (orchestrator-owned; node_modules stays untracked via the
      template's .gitignore).
    """
    template_dir = Path(template_dir)
    if dest is None:
        raise WorkspaceError("instantiate_workspace requires a destination path")
    dest = Path(dest)

    if not template_dir.is_dir() or not (template_dir / "package.json").exists():
        raise WorkspaceError(
            f"template directory missing or not a template (no package.json): "
            f"{template_dir}"
        )
    if lock_path is not None:
        verify_template(template_dir, lock_path)
    if not is_template_primed(template_dir):
        raise WorkspaceError(
            f"template is not primed (no node_modules in {template_dir}). "
            + _PRIMING_HINT.format(template=template_dir)
        )
    if dest.exists():
        raise WorkspaceError(
            f"workspace destination already exists: {dest}. Workspaces are "
            "retained after their run; pick a fresh path per run."
        )

    try:
        shutil.copytree(template_dir, dest)
    except OSError as exc:
        raise WorkspaceError(
            f"copying template {template_dir} -> {dest} failed: {exc}"
        ) from exc

    _git(dest, "init", "--initial-branch=main")
    _git(dest, "config", "user.name", "af-orchestrator")
    _git(dest, "config", "user.email", "af-orchestrator@localhost")
    _git(dest, "config", "core.autocrlf", "false")
    _git(dest, "config", "commit.gpgsign", "false")
    _git(dest, "add", "-A")
    _git(dest, "commit", "-m", "af workspace: instantiated from pinned template")
    return Workspace(root=dest)


# --- iteration commits, ticket tags, resets (R11/R2/R3 plumbing) -------------


def commit_iteration(
    ws: Workspace, ticket_id: str, iteration: int, *, detail: str = ""
) -> str:
    """Commit the workspace state for one Ralph iteration; return the sha.

    Exactly one commit per call (``--allow-empty``), stamped with ticket and
    iteration so resume/reset targets are mechanically findable.
    """
    message = f"af iteration: ticket={ticket_id} iteration={iteration}"
    if detail:
        message += f"\n\n{detail}"
    _git(ws.root, "add", "-A")
    _git(ws.root, "commit", "--allow-empty", "-m", message)
    return _git(ws.root, "rev-parse", "HEAD").strip()


def _ticket_tag(ticket_id: str) -> str:
    if not ticket_id or not all(
        ch.isalnum() or ch in "._-" for ch in ticket_id
    ):
        raise WorkspaceError(
            f"ticket id {ticket_id!r} is not tag-safe (alnum/._- only)"
        )
    return f"af-ticket-{ticket_id}-start"


def tag_ticket_start(ws: Workspace, ticket_id: str) -> str:
    """Tag the current commit as ``ticket_id``'s start (escalation reset target, R2)."""
    tag = _ticket_tag(ticket_id)
    _git(ws.root, "tag", "-f", tag)
    return tag


def reset_hard(ws: Workspace, ref: str) -> None:
    """``git reset --hard <ref>`` + ``git clean -fd`` (NO ``-x``: the template's
    .gitignore keeps node_modules and build outputs out of the clean, R3)."""
    _git(ws.root, "reset", "--hard", ref)
    _git(ws.root, "clean", "-fd")


def reset_to_ticket_start(ws: Workspace, ticket_id: str) -> None:
    """Restore the workspace to ``ticket_id``'s start tag (R2 escalation arm)."""
    tag = _ticket_tag(ticket_id)
    try:
        _git(ws.root, "rev-parse", "--verify", f"refs/tags/{tag}")
    except WorkspaceError as exc:
        raise WorkspaceError(
            f"no start tag for ticket {ticket_id!r} ({tag}) in {ws.root}; "
            "tag_ticket_start must run when the ticket starts"
        ) from exc
    reset_hard(ws, tag)


# --- containment assert (R8) -------------------------------------------------


def _iter_tool_uses(node: object):
    """Yield every ``{"type": "tool_use", ...}`` dict anywhere in a JSON value."""
    if isinstance(node, dict):
        if node.get("type") == "tool_use":
            yield node
        for value in node.values():
            yield from _iter_tool_uses(value)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_tool_uses(value)


def transcript_tool_paths(transcript_path: Path | str) -> list[tuple[str, str]]:
    """Extract (tool_name, raw_path) write-target/cwd pairs from a JSONL transcript."""
    transcript_path = Path(transcript_path)
    if not transcript_path.exists():
        raise WorkspaceError(f"transcript not found: {transcript_path}")
    pairs: list[tuple[str, str]] = []
    with transcript_path.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise WorkspaceError(
                    f"malformed transcript line {lineno} in {transcript_path}: {exc}"
                ) from exc
            for tool_use in _iter_tool_uses(event):
                name = tool_use.get("name")
                tool_input = tool_use.get("input")
                if not isinstance(name, str) or not isinstance(tool_input, dict):
                    continue
                key = _WRITE_TOOL_PATH_KEYS.get(name)
                if key is not None and isinstance(tool_input.get(key), str):
                    pairs.append((name, tool_input[key]))
                if name == "Bash" and isinstance(tool_input.get("cwd"), str):
                    pairs.append((name, tool_input["cwd"]))
    return pairs


def _resolves_inside(raw: str, root: Path) -> tuple[Path, bool]:
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    root_resolved = root.resolve()
    inside = PurePath(os.path.normcase(str(resolved))).is_relative_to(
        PurePath(os.path.normcase(str(root_resolved)))
    )
    return resolved, inside


def assert_containment(
    transcript_path: Path | str,
    workspace_root: Path | str,
    *,
    harness_repo: Path | str | None = None,
) -> list[Path]:
    """Post-iteration containment assert (R8).

    Every Write/Edit target and Bash cwd in the transcript must resolve under
    ``workspace_root`` (relative paths resolve against it; comparison is
    case-normalized for Windows). With ``harness_repo``, the harness repo's
    own ``git status --porcelain`` must be empty. Raises
    :class:`ContainmentViolation` naming every offender; returns the resolved
    in-workspace paths on success.
    """
    workspace_root = Path(workspace_root)
    violations: list[str] = []
    touched: list[Path] = []
    for tool, raw in transcript_tool_paths(transcript_path):
        resolved, inside = _resolves_inside(raw, workspace_root)
        if inside:
            touched.append(resolved)
        else:
            violations.append(f"{tool}: {raw} -> {resolved}")
    if harness_repo is not None:
        status = _git(Path(harness_repo), "status", "--porcelain")
        if status.strip():
            violations.append(
                f"harness repo {harness_repo} is dirty:\n{status.strip()}"
            )
    if violations:
        raise ContainmentViolation(
            "containment violated (paths outside workspace "
            f"{workspace_root}):\n" + "\n".join(violations)
        )
    return touched
