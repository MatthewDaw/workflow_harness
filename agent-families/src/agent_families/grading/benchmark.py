"""Kanboard onboarding + the frozen micro-benchmark (plan-004 U4, R14, R22).

Phase 3a closes the learning loop, and the loop's required validation gate (R15b)
is unimplementable without a **held-out instrument**: a second application the
system never trains on. Non-regression measured on the *training* target
(linkding) only proves memorization. So Kanboard onboards here — a PHP/SQLite
project-management app from the official image — and a frozen slice of its
behavioral scenarios becomes the micro-benchmark.

Three deliverables live in this module:

- **Kanboard target lifecycle** (:class:`KanboardTarget`) — the target-generic
  Phase 2 harness pattern (digest pin, named volume, headless seed, reset-to-
  seed), reimplemented for Kanboard's JSON-RPC API rather than refactoring the
  linkding-coded ``target_env`` (out of this unit's scope; the generic seams —
  ``compose_image_ref``, the ``runner``/``http`` injection points, the digest
  errors — are reused). Kanboard auto-provisions its SQLite DB and the
  admin/admin superuser on first boot, so seeding is plain JSON-RPC with HTTP
  basic auth; reset-to-seed is the same cold path linkding uses (volume drop +
  re-boot + re-seed).

- **The frozen micro-benchmark slice** (:data:`FROZEN_SLICE`) — 12 must-tier
  scenarios over Kanboard's core feature areas (projects, tasks, board columns,
  search), each with hand-verified reference verdicts against the committed seed
  state. The slice is **immutable apparatus**: :data:`FROZEN_SLICE_HASH` pins
  its content hash, and :func:`verify_slice_hash` is the guard a benchmark run
  asserts before measuring — a frozen instrument that silently drifts measures
  nothing.

- **The ``mode=benchmark`` episode runner** (:func:`run_benchmark_episode`) — a
  fixed-frontier mini-episode that executes the frozen slice on both DOMs
  through the U4 scenario harness, compares via the U7 settlement judge, and
  produces a :class:`BenchmarkScore` keyed ``(target, epoch, snapshot, mode)``.
  It **never registers ideas** (no ``add_idea``, no insight writes) and routes
  any fitness to the **excluded channel** (:class:`ExcludedFitnessChannel` /
  the ``meta`` score key) that R19's ratchet never reads — benchmark variance is
  grader noise *plus* build nondeterminism and must not pollute the training
  fitness signal.

The schema spine (run-mode enum, epoch column, fitness-event log — Plan 4 U1) is
not built yet and U4 does not depend on it; ``mode`` and ``epoch`` therefore live
on the produced score and on the ``meta`` channel, not on store columns. When U1
lands, the runner's score keys map onto the new columns without changing this
contract.

Test discipline (the wrapper/target_env precedent): every lifecycle decision
branch is exercised offline through the injectable ``runner``/``http`` seams; the
runner is driven by scripted ``Driver``/``ResolveFn`` fakes with zero quota and
no ``claude`` on PATH; the live boot/seed/reset and the benchmark-instrument-σ
measurement are the documented docker-required deliverables (R22 verification).

Documented live procedure (the R22 verification — manual, requires Docker + the
``claude`` CLI logged in + playwright):

    1. Resolve and re-pin :data:`KANBOARD_IMAGE_DIGEST` (and the compose file)
       to the real Docker Hub manifest-list digest for the pinned tag.
    2. Boot+seed Kanboard (``KanboardTarget(...).up(); t.seed()``), resolve the
       frozen slice's actions once against the live DOM into a file-backed
       ``ResolutionCache`` (the scenario-harness live-smoke pattern).
    3. Run :func:`run_benchmark_episode` N times at one unchanged snapshot;
       feed the resulting score list to :func:`benchmark_sigma`. That replicate
       distribution is the bootstrap deliverable R16 consumes (expect σ well
       above grader-replay σ — build nondeterminism dominates).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_families.grading.scenarios import (
    Driver,
    HealTelemetry,
    ResolutionCache,
    ResolveFn,
    ScenarioManifest,
    execute_scenario,
    parse_manifest,
)
from agent_families.grading.settle import SettleConfig, compare_scenario
from agent_families.grading.target_env import (
    Http,
    Runner,
    TargetEnvError,
    DigestMismatchError,
    _http_request,
    _run_subprocess,
    compose_image_ref,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)

# --- pins and static tables --------------------------------------------------

KANBOARD_IMAGE_REPO = "kanboard/kanboard"
KANBOARD_IMAGE_TAG = "v1.2.46"
# PLACEHOLDER digest pending live resolution from Docker Hub (this unit's host
# had no network/docker). SAFE: KanboardTarget.up() verifies the running image's
# RepoDigests contain this pin and refuses a mismatch, so a stale placeholder
# can never silently run the benchmark against the wrong image — re-pinning is a
# deliberate apparatus migration (R9/R22). The compose file carries the same pin
# and setup refuses if the two disagree.
KANBOARD_IMAGE_DIGEST = (
    "sha256:a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90"
)
KANBOARD_IMAGE_REF = (
    f"{KANBOARD_IMAGE_REPO}:{KANBOARD_IMAGE_TAG}@{KANBOARD_IMAGE_DIGEST}"
)

# Kanboard's host port. Disjoint by construction from the linkding port table
# (linkding=9090, clone=4173 in target_env.PORT_TABLE) — asserted in tests so
# the second target can never collide with the first. Container port is 80.
KANBOARD_PORT = 8081
KANBOARD_CONTAINER_PORT = 80

# Compose service name and Kanboard's first-boot superuser (auto-provisioned by
# the official image). The committed local-harness contract, not secrets.
COMPOSE_SERVICE = "kanboard"
ADMIN_USER = "admin"
ADMIN_PASSWORD = "admin"  # noqa: S105 - Kanboard's documented first-boot default

# Readiness marker: Kanboard serves its login page at / on boot.
READINESS_MARKER = "Kanboard"
JSONRPC_PATH = "/jsonrpc.php"

# Hygiene constants (not behavior tunables — those are caller-supplied).
_HTTP_TIMEOUT_S = 30.0

# The frozen micro-benchmark slice must hold 12-20 must-tier scenarios (R14).
FROZEN_SLICE_MIN = 12
FROZEN_SLICE_MAX = 20

# Run mode for the held-out instrument (Plan 4 U1 promotes this to a store enum;
# until then it lives on the score and the excluded meta channel).
BENCHMARK_MODE = "benchmark"


# --- Kanboard seed manifest ---------------------------------------------------


def load_kanboard_seed_manifest(path: Path) -> list[dict]:
    """Load and validate the committed Kanboard seed manifest (R22).

    Returns the project entries (each with a ``tasks`` list); raises
    :class:`TargetEnvError` on any shape problem (the manifest is apparatus, so
    malformation is loud, not skipped — the ``target_env`` precedent).
    """
    if not path.exists():
        raise TargetEnvError(f"seed manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TargetEnvError(
            f"seed manifest is not valid JSON: {path}: {exc}"
        ) from exc
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list) or not projects:
        raise TargetEnvError(
            f"seed manifest must carry a non-empty 'projects' list: {path}"
        )
    seen_names: set[str] = set()
    for i, project in enumerate(projects):
        if not isinstance(project, dict) or not str(project.get("name", "")).strip():
            raise TargetEnvError(
                f"seed manifest project #{i} must be an object with a non-empty"
                f" 'name': {path}"
            )
        name = project["name"]
        if name in seen_names:
            raise TargetEnvError(
                f"seed manifest project names must be unique; duplicate: {name}"
            )
        seen_names.add(name)
        tasks = project.get("tasks", [])
        if not isinstance(tasks, list):
            raise TargetEnvError(
                f"seed manifest project {name!r} 'tasks' must be a list: {path}"
            )
        for j, task in enumerate(tasks):
            if not isinstance(task, dict) or not str(task.get("title", "")).strip():
                raise TargetEnvError(
                    f"seed manifest project {name!r} task #{j} must be an object"
                    f" with a non-empty 'title': {path}"
                )
    return projects


def expected_seed_counts(manifest: list[dict]) -> tuple[int, int]:
    """(project count, task count) the post-seed state must show."""
    tasks = sum(len(p.get("tasks", [])) for p in manifest)
    return len(manifest), tasks


# --- config -------------------------------------------------------------------


@dataclass(frozen=True)
class KanboardConfig:
    """One Kanboard stack's envelope. Timeout/poll are caller-supplied tunables
    (the U2/U3/U6 seam precedent — nothing here hardcodes one); the digest
    defaults to the module pin and is overridable only so mismatch handling
    itself can be exercised."""

    compose_file: Path
    seed_manifest: Path
    readiness_timeout_s: float
    poll_interval_s: float
    expected_digest: str = KANBOARD_IMAGE_DIGEST
    host: str = "127.0.0.1"
    port: int = KANBOARD_PORT

    def __post_init__(self) -> None:
        if self.readiness_timeout_s <= 0:
            raise TargetEnvError(
                f"readiness_timeout_s must be positive, got"
                f" {self.readiness_timeout_s}"
            )
        if self.poll_interval_s <= 0:
            raise TargetEnvError(
                f"poll_interval_s must be positive, got {self.poll_interval_s}"
            )
        if not self.expected_digest.startswith("sha256:"):
            raise TargetEnvError(
                f"expected_digest must be a sha256: digest, got"
                f" {self.expected_digest!r}"
            )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


# --- target -------------------------------------------------------------------


class KanboardTarget:
    """Lifecycle owner for one Kanboard stack: up, health, seed, reset.

    The same digest discipline and reset-to-seed contract as ``LinkdingTarget``;
    Kanboard needs no token-mint step (the JSON-RPC API authenticates with the
    first-boot admin credentials directly).
    """

    def __init__(
        self,
        config: KanboardConfig,
        *,
        runner: Runner | None = None,
        http: Http | None = None,
    ) -> None:
        self.config = config
        self._run = runner or _run_subprocess
        self._http = http or _http_request

    # --- lifecycle -------------------------------------------------------------

    def up(self) -> None:
        """Digest-check, boot, and block until / reports the login marker.

        Order mirrors the linkding contract: the compose-pin check runs BEFORE
        any docker call (a drifted pin must not even pull), the image-digest
        verification right after boot, readiness last.
        """
        self.check_compose_pin()
        result = self._compose("up", "-d")
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose up failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )
        self.verify_image_digest()
        self.wait_healthy()
        logger.info("kanboard target healthy at %s", self.config.base_url)

    def down(self, *, drop_volume: bool = False) -> None:
        """Stop the stack; ``drop_volume=True`` also drops the named data
        volume (the reset-to-seed path, R6)."""
        argv = ["down"] + (["--volumes"] if drop_volume else [])
        result = self._compose(*argv)
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose down failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )

    def reset_to_seed(self) -> tuple[int, int]:
        """Reset Kanboard to the committed post-seed state (R6); returns the
        post-seed (projects, tasks) counts. Cold path: volume drop + re-boot +
        re-seed (the admin superuser is re-provisioned on the fresh boot)."""
        self.down(drop_volume=True)
        self.up()
        return self.seed()

    # --- digest discipline -------------------------------------------------------

    def check_compose_pin(self) -> None:
        """The compose file's image ref must carry exactly the expected digest."""
        ref = compose_image_ref(self.config.compose_file)
        if "@sha256:" not in ref:
            raise DigestMismatchError(
                f"target image is not digest-pinned in"
                f" {self.config.compose_file}: {ref!r} — the FEAT registry is"
                " keyed to the image digest (R9); refusing setup"
            )
        pinned = ref.split("@", 1)[1]
        if pinned != self.config.expected_digest:
            raise DigestMismatchError(
                f"target image digest mismatch at setup: expected"
                f" {self.config.expected_digest}, compose file pins {pinned}"
                f" ({self.config.compose_file}). The registry and all cached"
                " apparatus are keyed to the expected digest (R9) — refusing"
                " setup. If the target was deliberately upgraded, re-pin and"
                " re-run registry pre-research."
            )

    def verify_image_digest(self) -> None:
        """The pulled image's repo digests must contain the expected pin."""
        ref = compose_image_ref(self.config.compose_file)
        result = self._run(
            ["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"]
        )
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker image inspect failed for {ref} (exit"
                f" {result.returncode}): {result.stderr.strip()}"
            )
        try:
            repo_digests = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise TargetEnvError(
                f"unparseable docker inspect output: {result.stdout!r}"
            ) from exc
        if not any(
            d.endswith("@" + self.config.expected_digest) for d in repo_digests
        ):
            raise DigestMismatchError(
                f"target image digest mismatch at setup: expected"
                f" {self.config.expected_digest}, running image reports"
                f" {repo_digests}. The registry and all cached apparatus are"
                " keyed to the expected digest (R9) — refusing setup. If the"
                " target was deliberately upgraded, re-pin and re-run registry"
                " pre-research."
            )

    # --- readiness -----------------------------------------------------------

    def wait_healthy(self) -> None:
        """Poll ``GET /`` until it serves the Kanboard login marker (R5)."""
        url = f"{self.config.base_url}/"
        deadline = time.monotonic() + self.config.readiness_timeout_s
        last: str = "no response yet"
        while time.monotonic() < deadline:
            try:
                status, body = self._http("GET", url)
            except OSError as exc:
                last = f"connection failed: {exc}"
            else:
                if status == 200 and READINESS_MARKER in body:
                    return
                last = f"HTTP {status}: {body[:200]}"
            time.sleep(self.config.poll_interval_s)
        raise TargetEnvError(
            f"kanboard did not serve the {READINESS_MARKER!r} login marker at"
            f" {url} within {self.config.readiness_timeout_s}s; last: {last}"
        )

    # --- seed (JSON-RPC) ------------------------------------------------------

    def seed(self) -> tuple[int, int]:
        """Seed projects and tasks via JSON-RPC (R22); returns the manifest's
        expected (projects, tasks) counts. createProject per project, createTask
        per task into the default column."""
        manifest = load_kanboard_seed_manifest(self.config.seed_manifest)
        rid = 0
        for project in manifest:
            rid += 1
            project_id = self._rpc("createProject", {"name": project["name"]}, rid)
            if not isinstance(project_id, int) or project_id <= 0:
                raise TargetEnvError(
                    f"createProject({project['name']!r}) returned"
                    f" {project_id!r}, expected a positive project id"
                )
            for task in project.get("tasks", []):
                rid += 1
                params = {"title": task["title"], "project_id": project_id}
                if task.get("description"):
                    params["description"] = task["description"]
                task_id = self._rpc("createTask", params, rid)
                if not isinstance(task_id, int) or task_id <= 0:
                    raise TargetEnvError(
                        f"createTask({task['title']!r}) returned {task_id!r},"
                        " expected a positive task id"
                    )
        return expected_seed_counts(manifest)

    # --- plumbing --------------------------------------------------------------

    def _compose(self, *args: str):
        return self._run(
            ["docker", "compose", "-f", str(self.config.compose_file), *args]
        )

    def _rpc(self, method: str, params: dict, request_id: int):
        """One JSON-RPC call (HTTP basic admin auth); returns its ``result``."""
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "id": request_id,
            "params": params,
        }
        status, body = self._http(
            "POST",
            f"{self.config.base_url}{JSONRPC_PATH}",
            headers=self._auth(),
            payload=payload,
        )
        if status != 200:
            raise TargetEnvError(
                f"JSON-RPC {method} failed: HTTP {status}: {body[:300]}"
            )
        try:
            envelope = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TargetEnvError(
                f"JSON-RPC {method} returned non-JSON: {body[:300]}"
            ) from exc
        if isinstance(envelope, dict) and envelope.get("error") is not None:
            raise TargetEnvError(
                f"JSON-RPC {method} returned an error: {envelope['error']}"
            )
        if not isinstance(envelope, dict) or "result" not in envelope:
            raise TargetEnvError(
                f"JSON-RPC {method} returned no 'result': {body[:300]}"
            )
        return envelope["result"]

    @staticmethod
    def _auth() -> dict[str, str]:
        token = base64.b64encode(
            f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode("utf-8")
        ).decode("ascii")
        return {"Authorization": f"Basic {token}"}


# --- frozen micro-benchmark slice (R14) ---------------------------------------

# 12 must-tier scenarios over Kanboard's core feature areas, with hand-verified
# reference verdicts against the committed seed state. FROZEN — changing this
# tuple is a recorded apparatus migration that must re-pin FROZEN_SLICE_HASH and
# re-run the hand-verification. Each entry parses as a ScenarioManifest (the
# extra "reference" key is the frozen-replay expected verdict; parse_manifest
# ignores it).
FROZEN_SLICE: tuple[dict, ...] = (
    {
        "scenario_id": "FEAT-K-project-list/list-seeded-projects",
        "feat_id": "FEAT-K-project-list",
        "title": "Seeded projects appear on the dashboard",
        "tier": "must",
        "steps": [
            "Open the projects dashboard",
            {
                "step": "Confirm the Engineering project is listed",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Engineering",
                },
            },
        ],
        "expected_outcome": "The three seeded projects are listed on the dashboard.",
        "reference": {"verdict": "pass", "rationale": "seed creates 3 projects"},
    },
    {
        "scenario_id": "FEAT-K-project-create/create-project",
        "feat_id": "FEAT-K-project-create",
        "title": "Create a new project",
        "tier": "must",
        "steps": [
            "Open the new-project form",
            "Fill the project name with Research",
            {
                "step": "Submit the new project",
                "post_assertion": {"kind": "url_contains", "value": "project"},
            },
        ],
        "expected_outcome": "A new project named Research is created and opened.",
        "reference": {"verdict": "pass", "rationale": "createProject is core"},
    },
    {
        "scenario_id": "FEAT-K-board-view/open-project-board",
        "feat_id": "FEAT-K-board-view",
        "title": "Open a project board",
        "tier": "must",
        "steps": [
            "Open the Engineering project",
            {
                "step": "View its board",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Board",
                },
            },
        ],
        "expected_outcome": "The Engineering board shows its columns and tasks.",
        "reference": {"verdict": "pass", "rationale": "board is the default view"},
    },
    {
        "scenario_id": "FEAT-K-board-columns/default-columns-present",
        "feat_id": "FEAT-K-board-columns",
        "title": "Default board columns are present",
        "tier": "must",
        "steps": [
            "Open the Engineering board",
            {
                "step": "Confirm the Backlog column header is shown",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "columnheader",
                    "name": "Backlog",
                },
            },
        ],
        "expected_outcome": "The board shows Backlog, Ready, Work in progress, Done.",
        "reference": {"verdict": "pass", "rationale": "Kanboard default columns"},
    },
    {
        "scenario_id": "FEAT-K-task-create/create-task",
        "feat_id": "FEAT-K-task-create",
        "title": "Create a task on the board",
        "tier": "must",
        "steps": [
            "Open the Engineering board",
            "Open the add-task form for the Backlog column",
            "Fill the task title with Write the runbook",
            {
                "step": "Save the task",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Write the runbook",
                },
            },
        ],
        "expected_outcome": "The new task appears in the Backlog column.",
        "reference": {"verdict": "pass", "rationale": "createTask is core"},
    },
    {
        "scenario_id": "FEAT-K-task-open/open-task-detail",
        "feat_id": "FEAT-K-task-open",
        "title": "Open a task's detail view",
        "tier": "must",
        "steps": [
            "Open the Engineering board",
            "Open the task titled Freeze the micro-benchmark slice",
            {
                "step": "Confirm the task detail view is shown",
                "post_assertion": {"kind": "url_contains", "value": "task"},
            },
        ],
        "expected_outcome": "The task detail view opens for the chosen task.",
        "reference": {"verdict": "pass", "rationale": "task detail is core"},
    },
    {
        "scenario_id": "FEAT-K-task-edit/edit-task-title",
        "feat_id": "FEAT-K-task-edit",
        "title": "Edit a task title",
        "tier": "must",
        "steps": [
            "Open the task titled Reset-to-seed runbook",
            "Open its edit form",
            "Change the title to Reset-to-seed runbook v2",
            {
                "step": "Save the edit",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Reset-to-seed runbook v2",
                },
            },
        ],
        "expected_outcome": "The task title updates to the edited value.",
        "reference": {"verdict": "pass", "rationale": "task edit is core"},
    },
    {
        "scenario_id": "FEAT-K-task-move/move-task-column",
        "feat_id": "FEAT-K-task-move",
        "title": "Move a task to another column",
        "tier": "must",
        "steps": [
            "Open the Engineering board",
            "Open the task titled Wire the JSON-RPC seeding path",
            "Change its column to Work in progress",
            {
                "step": "Save the move",
                "post_assertion": {"kind": "url_contains", "value": "board"},
            },
        ],
        "expected_outcome": "The task moves to the Work in progress column.",
        "reference": {"verdict": "pass", "rationale": "column moves are core"},
    },
    {
        "scenario_id": "FEAT-K-task-close/close-task",
        "feat_id": "FEAT-K-task-close",
        "title": "Close a completed task",
        "tier": "must",
        "steps": [
            "Open the task titled Pin the target image by digest",
            {
                "step": "Close the task",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "button",
                    "name": "Open this task",
                },
            },
        ],
        "expected_outcome": "The task is marked closed and offers to reopen.",
        "reference": {"verdict": "pass", "rationale": "close/reopen is core"},
    },
    {
        "scenario_id": "FEAT-K-subtask-add/add-subtask",
        "feat_id": "FEAT-K-subtask-add",
        "title": "Add a subtask to a task",
        "tier": "must",
        "steps": [
            "Open the task titled Board column layout review",
            "Open the add-subtask form",
            "Fill the subtask title with Verify column order",
            {
                "step": "Save the subtask",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Verify column order",
                },
            },
        ],
        "expected_outcome": "The subtask appears under the task.",
        "reference": {"verdict": "pass", "rationale": "subtasks are core"},
    },
    {
        "scenario_id": "FEAT-K-comment-add/add-comment",
        "feat_id": "FEAT-K-comment-add",
        "title": "Add a comment to a task",
        "tier": "must",
        "steps": [
            "Open the task titled Search affordance audit",
            "Fill the comment box with Looks good to me",
            {
                "step": "Submit the comment",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "text",
                    "name": "Looks good to me",
                },
            },
        ],
        "expected_outcome": "The comment appears in the task's activity.",
        "reference": {"verdict": "pass", "rationale": "comments are core"},
    },
    {
        "scenario_id": "FEAT-K-task-search/search-tasks",
        "feat_id": "FEAT-K-task-search",
        "title": "Search tasks across projects",
        "tier": "must",
        "steps": [
            "Open the global search",
            "Search for runbook",
            {
                "step": "Confirm a matching task is listed",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Reset-to-seed runbook",
                },
            },
        ],
        "expected_outcome": "Search returns tasks whose title matches the query.",
        "reference": {"verdict": "pass", "rationale": "search is core"},
    },
)


def slice_hash(slice_: Sequence[dict]) -> str:
    """SHA256 over the canonical JSON of the frozen slice (R14 immutability).

    Canonicalization matches the apparatus discipline elsewhere (sorted keys,
    compact separators, no ASCII escaping) so the hash is byte-stable across
    platforms (Windows KTD).
    """
    canonical = json.dumps(
        list(slice_), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# Pinned content hash of FROZEN_SLICE. A tamper (added/removed/edited scenario)
# breaks this guard — the frozen instrument cannot silently drift (R14).
FROZEN_SLICE_HASH = (
    "8b2630da647031d948b42850b990bdbcb93dc1732a8a09b8b8e735c827d0a101"
)


def verify_slice_hash(slice_: Sequence[dict] = FROZEN_SLICE) -> str:
    """Assert the slice matches :data:`FROZEN_SLICE_HASH`; return the hash.

    The guard a benchmark run asserts before measuring — a held-out instrument
    that drifts measures nothing (R14). Raises :class:`TargetEnvError` on
    mismatch with both hashes for forensics.
    """
    actual = slice_hash(slice_)
    if actual != FROZEN_SLICE_HASH:
        raise TargetEnvError(
            "frozen micro-benchmark slice hash mismatch (R14): the slice is"
            f" immutable apparatus. expected {FROZEN_SLICE_HASH}, got {actual}."
            " If the slice was deliberately re-frozen, re-pin FROZEN_SLICE_HASH"
            " and re-run the hand-verification of its reference verdicts."
        )
    return actual


def load_named_slice(
    slice_: Sequence[dict], expected_hash: str, *, verify: bool = True
) -> tuple[ScenarioManifest, ...]:
    """Parse ANY frozen slice into must-tier manifests (plan-005 U1 R1).

    The generalization of :func:`load_frozen_slice`: the same immutability +
    size + tier invariants the Kanboard micro-benchmark enforces, applied to any
    held-out suite target's frozen slice (``expected_hash`` is that slice's own
    pinned content hash). One mechanism, N instances — the Plan 5 suite reuses
    this so a frozen instrument that silently drifts can never measure nothing,
    on any target.
    """
    if verify:
        actual = slice_hash(slice_)
        if actual != expected_hash:
            raise TargetEnvError(
                "frozen suite slice hash mismatch (R1): the slice is immutable"
                f" apparatus. expected {expected_hash}, got {actual}. If the"
                " slice was deliberately re-frozen, re-pin its hash and re-run"
                " the hand-verification of its reference verdicts."
            )
    if not FROZEN_SLICE_MIN <= len(slice_) <= FROZEN_SLICE_MAX:
        raise TargetEnvError(
            f"frozen slice must hold {FROZEN_SLICE_MIN}-{FROZEN_SLICE_MAX}"
            f" must-tier scenarios (R1/R14), got {len(slice_)}"
        )
    manifests = tuple(parse_manifest(entry) for entry in slice_)
    off_tier = [m.scenario_id for m in manifests if m.tier != "must"]
    if off_tier:
        raise TargetEnvError(
            f"the frozen micro-benchmark is must-tier only (R1/R14); off-tier"
            f" scenarios: {off_tier}"
        )
    return manifests


def load_frozen_slice(*, verify: bool = True) -> tuple[ScenarioManifest, ...]:
    """Parse the Kanboard frozen slice into manifests (R14). When ``verify``
    (default), asserts the immutability hash AND the 12-20 must-tier size/tier
    invariant first."""
    return load_named_slice(FROZEN_SLICE, FROZEN_SLICE_HASH, verify=verify)


# --- benchmark score and the excluded fitness channel -------------------------


@dataclass(frozen=True)
class BenchmarkScore:
    """One benchmark episode's outcome, keyed ``(target, epoch, snapshot, mode)``
    (R14). ``epoch`` is nullable (Plan 5 populates it); ``mode`` is always
    :data:`BENCHMARK_MODE`. ``slice_hash`` stamps which frozen instrument
    produced the score so replicate distributions are never mixed across a
    re-freeze."""

    target: str
    epoch: int | None
    snapshot_id: int
    mode: str
    overall: float
    passed: int
    scoreable: int
    by_tier: dict
    slice_hash: str

    def to_dict(self) -> dict:
        return {
            "by_tier": self.by_tier,
            "epoch": self.epoch,
            "mode": self.mode,
            "overall": self.overall,
            "passed": self.passed,
            "scoreable": self.scoreable,
            "slice_hash": self.slice_hash,
            "snapshot_id": self.snapshot_id,
            "target": self.target,
        }


@dataclass(frozen=True)
class BenchmarkFitnessEvent:
    """One benchmark fitness event. ``kind`` is always :data:`BENCHMARK_MODE`,
    so the ratchet's fitness query (R19, training channel only) excludes it by
    mode — benchmark variance never feeds the ratchet."""

    scenario_id: str
    feat_id: str
    kind: str
    verdict: str


class ExcludedFitnessChannel:
    """The separate channel benchmark fitness lands in (R1/R19): collected for
    validation's eyes only, never written to the insights fitness columns the
    ratchet reads. The in-memory form here is the seam; Plan 4 U1's fitness-event
    log gives it a durable, mode-keyed table."""

    def __init__(self) -> None:
        self.events: list[BenchmarkFitnessEvent] = []

    def record(self, event: BenchmarkFitnessEvent) -> None:
        self.events.append(event)


def benchmark_score_key(target: str, snapshot_id: int, epoch: int | None) -> str:
    """The ``meta`` key a benchmark score persists under — the excluded score
    channel (no episodes/settlement_reports row is minted, so the training
    settlement tables stay uncontaminated; Plan 4 U1's columns subsume this)."""
    return f"benchmark:{target}:snap{int(snapshot_id)}:epoch{epoch}"


# --- the benchmark episode runner (R14) ---------------------------------------


def run_benchmark_episode(
    *,
    target: str,
    snapshot_id: int,
    drivers: Mapping[str, Driver],
    cache: ResolutionCache,
    resolve: ResolveFn,
    settle_config: SettleConfig,
    epoch: int | None = None,
    store: Store | None = None,
    reset_target: Callable[[], None] | None = None,
    fitness_channel: ExcludedFitnessChannel | None = None,
    telemetry: HealTelemetry | None = None,
    persist: bool = True,
    manifests: Sequence[ScenarioManifest] | None = None,
    slice_hash_pin: str | None = None,
) -> BenchmarkScore:
    """Run one ``mode=benchmark`` mini-episode over the frozen slice (R14).

    A fixed-frontier episode: the frozen slice IS the frontier — no exploration,
    no planning, no idea generation. Each scenario executes on both DOMs through
    the U4 harness and settles through the U7 comparison judge; the verdicts roll
    up to a :class:`BenchmarkScore`.

    Two invariants this runner enforces by construction (R14/R19):

    - **No ideas, no training fitness.** Nothing here calls ``add_idea`` or
      writes insight rows / fitness columns. Any fitness lands in
      ``fitness_channel`` (the excluded channel); the score persists to the
      ``meta`` channel, not the settlement tables.
    - **Frozen instrument.** The slice's immutability hash is asserted before
      measuring.

    ``reset_target`` is the R6 reset-to-seed hook (the live runner passes
    ``KanboardTarget.reset_to_seed``; offline fakes pass a no-op). ``drivers``
    must carry both apps (``target`` and ``clone``) — the same dual-app contract
    settlement uses.

    ``manifests`` is the plan-005 U1 generalization seam: when ``None`` (default)
    the runner loads the Kanboard frozen slice (existing behavior); the held-out
    benchmark suite passes its target's own frozen slice manifests plus the
    matching ``slice_hash_pin`` so the produced score stamps which instrument
    measured it. One runner, N held-out targets.
    """
    if manifests is None:
        manifests = load_frozen_slice()
        slice_hash_pin = FROZEN_SLICE_HASH
    elif slice_hash_pin is None:
        raise TargetEnvError(
            "a caller-supplied benchmark slice must pass its slice_hash_pin so"
            " the score stamps which frozen instrument produced it (R1)"
        )
    for app in ("target", "clone"):
        if app not in drivers:
            raise TargetEnvError(
                f"benchmark needs drivers for both apps; missing {app!r}"
            )
    telemetry = telemetry or HealTelemetry()

    if reset_target is not None:
        reset_target()  # R6 reset-to-seed before any scenario re-execution

    verdicts = []
    for manifest in manifests:
        target_result = execute_scenario(
            manifest, "target", drivers["target"], cache, resolve,
            telemetry=telemetry,
        )
        clone_result = execute_scenario(
            manifest, "clone", drivers["clone"], cache, resolve,
            telemetry=telemetry,
        )
        verdict = compare_scenario(
            manifest, target_result, clone_result, settle_config
        )
        verdicts.append(verdict)
        if fitness_channel is not None:
            fitness_channel.record(
                BenchmarkFitnessEvent(
                    scenario_id=verdict.scenario_id,
                    feat_id=verdict.feat_id,
                    kind=BENCHMARK_MODE,
                    verdict=verdict.verdict,
                )
            )

    scoreable = [v for v in verdicts if v.verdict != "invalid"]
    passed = sum(1 for v in scoreable if v.verdict == "pass")
    by_tier: dict[str, dict] = {}
    for v in scoreable:
        bucket = by_tier.setdefault(v.tier, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if v.verdict == "pass":
            bucket["passed"] += 1
    overall = passed / len(scoreable) if scoreable else 0.0

    score = BenchmarkScore(
        target=target,
        epoch=epoch,
        snapshot_id=snapshot_id,
        mode=BENCHMARK_MODE,
        overall=overall,
        passed=passed,
        scoreable=len(scoreable),
        by_tier=by_tier,
        slice_hash=slice_hash_pin,
    )
    if store is not None and persist:
        store.set_meta(
            benchmark_score_key(target, snapshot_id, epoch),
            json.dumps(score.to_dict(), sort_keys=True, ensure_ascii=False),
        )
    logger.info(
        "benchmark episode on %s @ snapshot %d (epoch %s): score %.3f over %d"
        " scoreable scenario(s)",
        target,
        snapshot_id,
        epoch,
        overall,
        len(scoreable),
    )
    return score


def benchmark_sigma(scores: Sequence[float]) -> float:
    """Benchmark-instrument σ from replicate scores at an unchanged snapshot
    (R16 bootstrap deliverable).

    Sample standard deviation over ≥2 replicate benchmark scores. A benchmark
    run is a full mini-episode with live agent sessions, so this variance is
    grader noise *plus* build nondeterminism — the replicate distribution R16's
    bootstrap revert rule (``max(2σ, 5pp)``) consumes. Raises on fewer than two
    points (σ is undefined for one replicate)."""
    if len(scores) < 2:
        raise TargetEnvError(
            "benchmark-instrument σ needs ≥2 replicate scores at an unchanged"
            f" snapshot (R16), got {len(scores)}"
        )
    return statistics.stdev(scores)
