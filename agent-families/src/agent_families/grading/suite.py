"""The held-out benchmark suite: generalization instrument (plan-005 U1, R1/R2).

Plan 4 built one held-out instrument (the Kanboard micro-benchmark). This module
generalizes that one mechanism to N held-out targets — a fixed suite the system
is **never trained on** — and adds the epoch-keyed scoring + control charting the
generalization curve needs:

- **Suite targets** (:class:`SuiteTarget`) — one instance per training archetype,
  each onboarded through the generic Phase 2 harness (digest-pinned compose,
  named volume, disjoint host port) with its **own frozen must-tier slice** (the
  Plan 4 micro-benchmark pattern, generalized via
  :func:`agent_families.grading.benchmark.load_named_slice`). The slice is
  immutable apparatus: its content hash is pinned, asserted before every measure.

- **The suite runner** (:func:`run_suite`) — runs each target's frozen slice as a
  ``mode=benchmark`` mini-episode (reusing :func:`run_benchmark_episode`), keys
  every score ``(target, epoch, snapshot, mode=benchmark)``, appends it to the
  per-target and aggregate **history** channels (meta, the excluded channel — a
  benchmark score never feeds the ratchet), and **recomputes the control limits**
  — per target and aggregate — on the Plan 4 SPC machinery. Control limits
  recompute **only** on suite runs (the generation counter advances nowhere else),
  so an ordinary training episode never moves the chart.

- **The held-out constraint** (:func:`assert_held_out`) — a training-pool target
  is refused as a suite target. The boundary is enforced in both directions:
  ``curriculum.build_training_pool`` refuses to admit a held-out name, and this
  refuses to score a trained-on name. That is what makes the suite a
  *generalization* measurement rather than a memorization one.

Test discipline (the benchmark/target_env precedent): the suite runner is driven
offline by scripted ``Driver``/``ResolveFn`` fakes (zero quota, no ``claude`` on
PATH); the live boot/seed/reset of each suite stack is the documented
docker-required deliverable (pending-docker — the digests are placeholders until
live-resolved, and the generic env refuses a mismatch until then).

## Conformance

Test-scenario / invariant (plan-005 U1) -> tests (tests/test_suite.py):

- suite run produces per-target + aggregate scores keyed (target, epoch,
  snapshot, mode=benchmark): ``test_suite_run_produces_keyed_scores``,
  ``test_suite_scores_persist_to_excluded_channel``
- revisit curve query returns the generalization series:
  ``test_revisit_curve_is_the_generalization_series``,
  ``test_aggregate_curve_across_epochs``
- a training-pool target is rejected as a suite target:
  ``test_training_pool_target_rejected_as_suite_target``,
  ``test_assert_held_out_accepts_suite_targets``
- control-chart limits recompute only on suite runs:
  ``test_control_limits_recompute_only_on_suite_runs``
- two fixture epochs produce a coherent generalization curve (Verification):
  ``test_two_epochs_produce_a_coherent_generalization_curve``
- frozen-slice immutability / size / tier per suite target:
  ``test_suite_frozen_slices_are_pinned_and_must_tier``,
  ``test_suite_slice_tamper_breaks_the_guard``
- generic harness static contract (digest pin, named volume, disjoint ports):
  ``test_suite_compose_pins_image_by_digest``,
  ``test_suite_compose_uses_named_volume``, ``test_suite_ports_disjoint``
- generic env boot/health branches (offline seams):
  ``test_suite_env_up_digest_then_boot_then_ready``,
  ``test_suite_env_digest_mismatch_fails_before_boot``
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from agent_families.grading.benchmark import (
    BENCHMARK_MODE,
    BenchmarkScore,
    ExcludedFitnessChannel,
    benchmark_sigma,
    load_named_slice,
    run_benchmark_episode,
    slice_hash,
)
from agent_families.grading.scenarios import (
    Driver,
    HealTelemetry,
    ResolutionCache,
    ResolveFn,
    ScenarioManifest,
)
from agent_families.grading.settle import SettleConfig
from agent_families.grading.target_env import (
    DigestMismatchError,
    Http,
    Runner,
    TargetEnvError,
    _http_request,
    _run_subprocess,
    compose_image_ref,
)
from agent_families.pipeline import curriculum
from agent_families.reflector.validate import (
    SpcLimits,
    ValidateParams,
    spc_limits,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)


class SuiteError(Exception):
    """A held-out suite invariant was violated."""


TARGETS_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "targets"

# Host ports for the held-out suite stacks. Disjoint by construction from the
# linkding/clone table (9090/4173) and Kanboard (8081) — asserted in tests.
SUITE_PORTS: Mapping[str, int] = {
    "shaarli": 8082,
    "privatebin": 8083,
    "dokuwiki": 8084,
}

# The aggregate channel's pseudo-target key in the limits/history meta tables.
AGGREGATE_KEY = "__aggregate__"


# --- frozen suite slices (R1 — generalized Plan 4 micro-benchmark pattern) ----


def _scn(key: str, area: str, title: str, steps: list, outcome: str) -> dict:
    """Build one must-tier suite scenario dict (the FROZEN_SLICE shape)."""
    return {
        "scenario_id": f"FEAT-{key}-{area}/{area}",
        "feat_id": f"FEAT-{key}-{area}",
        "title": title,
        "tier": "must",
        "steps": steps,
        "expected_outcome": outcome,
        "reference": {"verdict": "pass", "rationale": f"{area} is a core feature"},
    }


def _present(role: str, name: str) -> dict:
    return {"kind": "node_present", "role": role, "name": name}


def _url(value: str) -> dict:
    return {"kind": "url_contains", "value": value}


# shaarli — bookmarks archetype (held-out instance of linkding's archetype).
SHAARLI_SLICE: tuple[dict, ...] = (
    _scn("SH-link-list", "link-list", "Seeded links appear on the front page",
         ["Open the front page",
          {"step": "Confirm a seeded link is listed",
           "post_assertion": _present("link", "Agent Families design notes")}],
         "The seeded shaares are listed newest-first."),
    _scn("SH-link-add", "link-add", "Add a new shaare",
         ["Open the add-link form", "Fill the URL field",
          {"step": "Save the shaare", "post_assertion": _url("?")}],
         "A new shaare is created and shown on the front page."),
    _scn("SH-link-edit", "link-edit", "Edit a shaare's title",
         ["Open a shaare's edit form", "Change its title",
          {"step": "Save the edit",
           "post_assertion": _present("link", "Control charts for held-out benchmarks")}],
         "The shaare's title updates to the edited value."),
    _scn("SH-link-delete", "link-delete", "Delete a shaare",
         ["Open a shaare", {"step": "Delete it", "post_assertion": _url("?")}],
         "The shaare is removed from the list."),
    _scn("SH-tag-filter", "tag-filter", "Filter links by tag",
         [{"step": "Open the design tag",
           "post_assertion": _present("link", "Agent Families design notes")}],
         "Only shaares carrying the chosen tag are shown."),
    _scn("SH-tag-cloud", "tag-cloud", "The tag cloud lists seeded tags",
         [{"step": "Open the tag cloud",
           "post_assertion": _present("link", "benchmark")}],
         "The tag cloud shows every seeded tag."),
    _scn("SH-search", "search", "Full-text search across shaares",
         ["Open the search box", "Search for curriculum",
          {"step": "Confirm a match is listed",
           "post_assertion": _present("link", "Rotation curriculum reference")}],
         "Search returns shaares whose text matches the query."),
    _scn("SH-permalink", "permalink", "Open a shaare permalink",
         [{"step": "Open a shaare's permalink", "post_assertion": _url("shaare")}],
         "The permalink page opens for the chosen shaare."),
    _scn("SH-private", "private", "Mark a shaare private",
         ["Open a shaare's edit form", "Toggle the private flag",
          {"step": "Save", "post_assertion": _url("?")}],
         "The shaare is flagged private."),
    _scn("SH-daily", "daily", "The daily view groups shaares by day",
         [{"step": "Open the daily view", "post_assertion": _url("daily")}],
         "Shaares are grouped under their publication day."),
    _scn("SH-rss", "rss", "The RSS feed lists shaares",
         [{"step": "Open the feed", "post_assertion": _url("feed")}],
         "The feed serves the latest shaares."),
    _scn("SH-login", "login", "The login page is reachable",
         [{"step": "Open the login page",
           "post_assertion": _present("button", "Login")}],
         "The login form is shown."),
)

# privatebin — encrypted-paste archetype.
PRIVATEBIN_SLICE: tuple[dict, ...] = (
    _scn("PB-paste-new", "paste-new", "The new-paste editor loads",
         [{"step": "Open the front page",
           "post_assertion": _present("button", "Send")}],
         "The new-paste editor is shown with a Send action."),
    _scn("PB-paste-send", "paste-send", "Create an encrypted paste",
         ["Type a message", {"step": "Send the paste", "post_assertion": _url("?")}],
         "A paste is created and its share URL is shown."),
    _scn("PB-paste-view", "paste-view", "Open a paste by its URL",
         [{"step": "Open a paste link", "post_assertion": _url("?")}],
         "The decrypted paste body is shown."),
    _scn("PB-expire", "expire", "Choose an expiration",
         [{"step": "Open the expiration menu",
           "post_assertion": _present("text", "1 week")}],
         "The expiration selector offers standard durations."),
    _scn("PB-burn", "burn", "Burn-after-reading toggle is present",
         [{"step": "Open the paste options",
           "post_assertion": _present("text", "Burn after reading")}],
         "The burn-after-reading option is offered."),
    _scn("PB-format", "format", "Choose a paste format",
         [{"step": "Open the format menu",
           "post_assertion": _present("text", "Plain Text")}],
         "The format selector offers plain/markdown/source."),
    _scn("PB-password", "password", "Password-protect a paste",
         ["Open the password field", "Set a password",
          {"step": "Send", "post_assertion": _url("?")}],
         "The paste is protected by the chosen password."),
    _scn("PB-clone", "clone", "Clone an existing paste",
         [{"step": "Open the clone action",
           "post_assertion": _present("button", "Clone")}],
         "The clone action seeds a new paste from this one."),
    _scn("PB-discussion", "discussion", "Open discussion toggle is present",
         [{"step": "Open the paste options",
           "post_assertion": _present("text", "Open discussion")}],
         "The discussion option is offered."),
    _scn("PB-rawview", "rawview", "View a paste raw",
         [{"step": "Open the raw view", "post_assertion": _url("?")}],
         "The raw paste text is served."),
    _scn("PB-qrcode", "qrcode", "A paste exposes a QR code action",
         [{"step": "Open the QR action",
           "post_assertion": _present("button", "QR code")}],
         "A QR code for the paste URL is offered."),
    _scn("PB-newfromview", "newfromview", "Start a new paste from a view",
         [{"step": "Open the new-paste action",
           "post_assertion": _present("button", "New")}],
         "A fresh editor opens from the current paste."),
)

# dokuwiki — flat-file wiki archetype (derived namespaces, revisions, search).
DOKUWIKI_SLICE: tuple[dict, ...] = (
    _scn("DW-page-view", "page-view", "The start page renders",
         [{"step": "Open the start page",
           "post_assertion": _present("heading", "start")}],
         "The seeded start page is shown."),
    _scn("DW-page-edit", "page-edit", "Open the page editor",
         [{"step": "Open the edit view", "post_assertion": _url("do=edit")}],
         "The wiki-text editor opens for the page."),
    _scn("DW-page-save", "page-save", "Save an edited page",
         ["Edit the page text",
          {"step": "Save the page", "post_assertion": _present("heading", "start")}],
         "The page saves and re-renders."),
    _scn("DW-page-create", "page-create", "Create a new page",
         ["Open a non-existent page", "Create it",
          {"step": "Save", "post_assertion": _url("do=edit")}],
         "A new page is created at the requested id."),
    _scn("DW-search", "search", "Full-text search across pages",
         ["Open the search box", "Search for benchmark",
          {"step": "Confirm a match is listed",
           "post_assertion": _present("link", "benchmark")}],
         "Search returns pages whose text matches the query."),
    _scn("DW-revisions", "revisions", "View a page's revision history",
         [{"step": "Open the revisions view", "post_assertion": _url("do=revisions")}],
         "The old-revisions list is shown."),
    _scn("DW-diff", "diff", "Diff two revisions",
         [{"step": "Open a revision diff", "post_assertion": _url("do=diff")}],
         "A side-by-side diff is shown."),
    _scn("DW-index", "index", "The sitemap index lists pages",
         [{"step": "Open the index", "post_assertion": _url("do=index")}],
         "The page index lists the namespace's pages."),
    _scn("DW-namespace", "namespace", "Open a namespaced page",
         [{"step": "Open a namespace page",
           "post_assertion": _present("link", "rotation")}],
         "The namespaced page renders."),
    _scn("DW-backlinks", "backlinks", "Show backlinks to a page",
         [{"step": "Open the backlinks view", "post_assertion": _url("do=backlink")}],
         "Pages linking here are listed."),
    _scn("DW-recent", "recent", "The recent-changes view loads",
         [{"step": "Open recent changes", "post_assertion": _url("do=recent")}],
         "Recently edited pages are listed."),
    _scn("DW-login", "login", "The login page is reachable",
         [{"step": "Open the login page", "post_assertion": _url("do=login")}],
         "The login form is shown."),
)


# --- suite target onboarding (generic harness) --------------------------------


@dataclass(frozen=True)
class SuiteTarget:
    """One held-out suite target: its stack, frozen slice, and pinned hash.

    ``frozen_slice_hash`` is the slice's own immutability pin (R1); :meth:`verify`
    asserts it (and the must-tier/size invariant) exactly as the Kanboard
    micro-benchmark does, generalized through ``benchmark.load_named_slice``.
    """

    name: str
    archetype: str
    stack: str
    compose_file: Path
    seed_manifest: Path
    readiness_marker: str
    port: int
    frozen_slice: tuple[dict, ...]
    frozen_slice_hash: str

    def verify(self) -> tuple[ScenarioManifest, ...]:
        """Assert the slice's immutability + must-tier/size invariant; return
        the parsed manifests (R1)."""
        return load_named_slice(self.frozen_slice, self.frozen_slice_hash)

    def manifests(self) -> tuple[ScenarioManifest, ...]:
        return self.verify()


# Pinned content hashes of each suite slice (R1 immutability). Computed from the
# canonical JSON of the slice; a tamper breaks the guard. Re-freezing is a
# recorded apparatus migration that re-pins the hash and re-verifies the slice.
SHAARLI_SLICE_HASH = (
    "8b922c52e28eaacd68dcd8a8bc5b2fce84f4265a4f35c6f0137b8b85656340c2"
)
PRIVATEBIN_SLICE_HASH = (
    "67f8769f9590ff340841c3bd3d809f3384d4a6f837e2ad2fa7b933f363f918a4"
)
DOKUWIKI_SLICE_HASH = (
    "5516cfd8f445dc144787e78522940d12bc56f46d4174377c7ec9fb1d2833e335"
)


def _target(name, archetype, stack, marker, slice_, slice_hash_pin) -> SuiteTarget:
    return SuiteTarget(
        name=name,
        archetype=archetype,
        stack=stack,
        compose_file=TARGETS_ROOT / name / "docker-compose.yml",
        seed_manifest=TARGETS_ROOT / name / "seed_manifest.json",
        readiness_marker=marker,
        port=SUITE_PORTS[name],
        frozen_slice=slice_,
        frozen_slice_hash=slice_hash_pin,
    )


SUITE_REGISTRY: tuple[SuiteTarget, ...] = (
    _target("shaarli", "bookmarks", "php", "Shaarli", SHAARLI_SLICE,
            SHAARLI_SLICE_HASH),
    _target("privatebin", "paste", "php", "PrivateBin", PRIVATEBIN_SLICE,
            PRIVATEBIN_SLICE_HASH),
    _target("dokuwiki", "wiki", "php", "DokuWiki", DOKUWIKI_SLICE,
            DOKUWIKI_SLICE_HASH),
)


def suite_target(name: str) -> SuiteTarget:
    for t in SUITE_REGISTRY:
        if t.name == name:
            return t
    raise SuiteError(f"no held-out suite target named {name!r}")


# --- the held-out constraint (R2) ---------------------------------------------


def assert_held_out(name: str, *, training_pool: Sequence[str] = ()) -> None:
    """Refuse ``name`` as a suite target if it is trained on (R2).

    A suite target must be in the held-out set and must NOT be in the active
    training pool — a trained-on target scoring itself would measure memorization,
    not generalization.
    """
    if name in training_pool:
        raise SuiteError(
            f"{name!r} is in the training pool — refusing to use a trained-on"
            " target as a held-out suite target (R2 generalization constraint)."
        )
    if name not in curriculum.HELD_OUT:
        raise SuiteError(
            f"{name!r} is not a held-out target (held-out set:"
            f" {sorted(curriculum.HELD_OUT)}); refusing it as a suite target (R2)."
        )


# --- generic suite target environment (boot/health/reset seams) ---------------


@dataclass(frozen=True)
class SuiteEnvConfig:
    target: SuiteTarget
    readiness_timeout_s: float
    poll_interval_s: float
    host: str = "127.0.0.1"

    def __post_init__(self) -> None:
        if self.readiness_timeout_s <= 0:
            raise TargetEnvError("readiness_timeout_s must be positive")
        if self.poll_interval_s <= 0:
            raise TargetEnvError("poll_interval_s must be positive")

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.target.port}"


class SuiteTargetEnv:
    """Generic lifecycle owner for a held-out suite stack: digest-pinned boot +
    readiness. Reuses the Phase 2 target_env seams (``runner``/``http``) so every
    branch is offline-testable; seeding is target-specific and live
    (docker-required, pending-docker) — the generic env owns boot/health/down
    only. The same digest discipline the linkding/Kanboard targets enforce."""

    def __init__(
        self,
        config: SuiteEnvConfig,
        *,
        runner: Runner | None = None,
        http: Http | None = None,
    ) -> None:
        self.config = config
        self._run = runner or _run_subprocess
        self._http = http or _http_request

    def up(self) -> None:
        self.check_compose_pin()
        result = self._compose("up", "-d")
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose up failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )
        self.verify_image_digest()
        self.wait_healthy()
        logger.info(
            "suite target %s healthy at %s",
            self.config.target.name,
            self.config.base_url,
        )

    def down(self, *, drop_volume: bool = False) -> None:
        argv = ["down"] + (["--volumes"] if drop_volume else [])
        result = self._compose(*argv)
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose down failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )

    def check_compose_pin(self) -> None:
        ref = compose_image_ref(self.config.target.compose_file)
        if "@sha256:" not in ref:
            raise DigestMismatchError(
                f"suite target image is not digest-pinned in"
                f" {self.config.target.compose_file}: {ref!r} (R9); refusing setup"
            )

    def verify_image_digest(self) -> None:
        ref = compose_image_ref(self.config.target.compose_file)
        pinned = ref.split("@", 1)[1]
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
        if not any(d.endswith("@" + pinned) for d in repo_digests):
            raise DigestMismatchError(
                f"suite target image digest mismatch: expected {pinned}, running"
                f" image reports {repo_digests} (R9); refusing setup."
            )

    def wait_healthy(self) -> None:
        import time

        url = f"{self.config.base_url}/"
        marker = self.config.target.readiness_marker
        deadline = time.monotonic() + self.config.readiness_timeout_s
        last = "no response yet"
        while time.monotonic() < deadline:
            try:
                status, body = self._http("GET", url)
            except OSError as exc:
                last = f"connection failed: {exc}"
            else:
                if status == 200 and marker in body:
                    return
                last = f"HTTP {status}: {body[:200]}"
            time.sleep(self.config.poll_interval_s)
        raise TargetEnvError(
            f"suite target {self.config.target.name} did not serve the"
            f" {marker!r} marker at {url} within"
            f" {self.config.readiness_timeout_s}s; last: {last}"
        )

    def _compose(self, *args: str):
        return self._run(
            ["docker", "compose", "-f", str(self.config.target.compose_file), *args]
        )


# --- history channels (excluded — a benchmark score never feeds the ratchet) --


def _history_key(target: str) -> str:
    return f"suite:history:{target}"


def _read_history(store: Store, target: str) -> list[dict]:
    raw = store.get_meta(_history_key(target))
    return json.loads(raw) if raw else []


def _append_history(store: Store, target: str, entry: dict) -> None:
    history = _read_history(store, target)
    history.append(entry)
    store.set_meta(
        _history_key(target),
        json.dumps(history, sort_keys=True, ensure_ascii=False),
    )


def revisit_curve(store: Store, target: str) -> list[tuple[int | None, float]]:
    """The generalization series for one target: ``[(epoch, overall), ...]`` in
    suite-run order (R2). Improvement on a revisit — library shaped by *other*
    targets in between — is generalization, not memorization."""
    return [(e["epoch"], e["overall"]) for e in _read_history(store, target)]


def aggregate_curve(store: Store) -> list[tuple[int | None, float]]:
    """The suite's aggregate score per suite run: ``[(epoch, aggregate), ...]``."""
    return [(e["epoch"], e["overall"]) for e in _read_history(store, AGGREGATE_KEY)]


# --- control charts (recompute only on suite runs) ----------------------------

SUITE_GEN_KEY = "suite:generation"


def suite_run_count(store: Store) -> int:
    """How many suite runs have completed. Advances ONLY in :func:`run_suite`."""
    raw = store.get_meta(SUITE_GEN_KEY)
    return int(raw) if raw else 0


def _bump_generation(store: Store) -> int:
    nxt = suite_run_count(store) + 1
    store.set_meta(SUITE_GEN_KEY, str(nxt))
    return nxt


def _limits_key(target: str) -> str:
    return f"suite:limits:{target}"


def _history_overalls(store: Store, target: str) -> list[float]:
    return [float(e["overall"]) for e in _read_history(store, target)]


def recompute_control_limits(
    store: Store,
    targets: Sequence[str],
    *,
    generation: int,
    params: ValidateParams = ValidateParams(),
    default_sigma: float = 0.0,
) -> dict[str, SpcLimits]:
    """Recompute per-target + aggregate individuals-chart limits (R1/R2).

    σ per target is the replicate σ from that target's own benchmark history
    (``benchmark_sigma`` over ≥2 points; ``default_sigma`` for a single point).
    The Plan 4 ``spc_limits`` machinery computes center ± k·σ over the clean
    history. Called **only** from :func:`run_suite`, stamped with the suite-run
    ``generation`` — so the chart never moves on an ordinary episode.
    """
    out: dict[str, SpcLimits] = {}
    for target in [*targets, AGGREGATE_KEY]:
        history = _history_overalls(store, target)
        if not history:
            continue
        sigma = benchmark_sigma(history) if len(history) >= 2 else default_sigma
        limits = spc_limits(history, sigma, params)
        out[target] = limits
        store.set_meta(
            _limits_key(target),
            json.dumps(
                {
                    "center": limits.center,
                    "lcl": limits.lcl,
                    "ucl": limits.ucl,
                    "sigma": sigma,
                    "n": len(history),
                    "generation": generation,
                },
                sort_keys=True,
            ),
        )
    return out


def control_limits(store: Store, target: str) -> dict | None:
    """The stored control limits for a target (or aggregate), with the suite-run
    ``generation`` they were computed at; ``None`` before the first suite run."""
    raw = store.get_meta(_limits_key(target))
    return json.loads(raw) if raw else None


# --- the suite runner (R1) ----------------------------------------------------


@dataclass(frozen=True)
class SuiteRunResult:
    """One full suite run's outcome (R1)."""

    epoch: int | None
    snapshot_id: int
    generation: int
    per_target: dict[str, BenchmarkScore]
    aggregate: float
    limits: dict[str, SpcLimits]


def run_suite(
    targets: Sequence[SuiteTarget],
    *,
    epoch: int | None,
    snapshot_id: int,
    drivers_for: Callable[[SuiteTarget], Mapping[str, Driver]],
    resolve: ResolveFn,
    settle_config: SettleConfig,
    store: Store,
    params: ValidateParams = ValidateParams(),
    reset_target_for: Callable[[SuiteTarget], Callable[[], None] | None] | None = None,
    fitness_channel: ExcludedFitnessChannel | None = None,
    training_pool: Sequence[str] = (),
    persist: bool = True,
) -> SuiteRunResult:
    """Run the held-out suite: each target's frozen slice as a benchmark episode.

    Every target is held-out-checked (R2), runs in **canonical name order** (so a
    suite run is deterministic), scores keyed ``(target, epoch, snapshot,
    mode=benchmark)``, and is appended to the per-target + aggregate history. The
    control limits recompute once, at the end, stamped with the bumped suite-run
    generation (R1). Benchmark fitness lands in the excluded channel only.
    """
    if not targets:
        raise SuiteError("a suite run needs at least one held-out target")
    ordered = sorted(targets, key=lambda t: t.name)
    per_target: dict[str, BenchmarkScore] = {}
    for t in ordered:
        assert_held_out(t.name, training_pool=training_pool)
        reset = reset_target_for(t) if reset_target_for is not None else None
        score = run_benchmark_episode(
            target=t.name,
            snapshot_id=snapshot_id,
            epoch=epoch,
            drivers=drivers_for(t),
            cache=ResolutionCache(),
            resolve=resolve,
            settle_config=settle_config,
            store=store,
            reset_target=reset,
            fitness_channel=fitness_channel,
            telemetry=HealTelemetry(),
            persist=persist,
            manifests=t.manifests(),
            slice_hash_pin=t.frozen_slice_hash,
        )
        per_target[t.name] = score
        if persist:
            _append_history(
                store,
                t.name,
                {
                    "epoch": epoch,
                    "snapshot_id": snapshot_id,
                    "overall": score.overall,
                    "mode": BENCHMARK_MODE,
                    "slice_hash": score.slice_hash,
                },
            )

    aggregate = statistics.fmean(s.overall for s in per_target.values())
    generation = suite_run_count(store)
    limits: dict[str, SpcLimits] = {}
    if persist:
        _append_history(
            store,
            AGGREGATE_KEY,
            {
                "epoch": epoch,
                "snapshot_id": snapshot_id,
                "overall": aggregate,
                "mode": BENCHMARK_MODE,
            },
        )
        generation = _bump_generation(store)
        limits = recompute_control_limits(
            store,
            [t.name for t in ordered],
            generation=generation,
            params=params,
        )
    logger.info(
        "suite run (epoch %s, snapshot %d): aggregate %.3f over %d held-out"
        " target(s); control limits recomputed at generation %d",
        epoch,
        snapshot_id,
        aggregate,
        len(per_target),
        generation,
    )
    return SuiteRunResult(
        epoch=epoch,
        snapshot_id=snapshot_id,
        generation=generation,
        per_target=per_target,
        aggregate=aggregate,
        limits=limits,
    )
