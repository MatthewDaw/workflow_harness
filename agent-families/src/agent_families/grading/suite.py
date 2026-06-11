"""The held-out benchmark suite and its control charts (plan-005 U1, R1/R2).

Plan 4 built ONE held-out instrument — Kanboard's frozen micro-benchmark — to
prove the learning loop generalizes off the training target. This module is the
generalization of that one instrument into the **suite**: N held-out targets
(one per archetype, never trained on), each with its own frozen slice
(:class:`~agent_families.grading.benchmark.FrozenSlice`), run together every N
episodes, scored per-target and in aggregate, and **control-charted** on the
Plan 4 SPC machinery so only special-cause regressions trigger rollback (DESIGN
§5/§11).

Three deliverables live here:

- **The suite registry** (:data:`HELD_OUT_SUITE`, :func:`build_suite`) — the
  held-out targets. Kanboard (kanban) reuses Plan 4's
  :data:`~agent_families.grading.benchmark.KANBOARD_SLICE`; LinkAce (a *second*
  bookmark manager — generalization within the bookmarks archetype that linkding
  trains, "same archetype, different instance" per DESIGN §11) ships its own
  frozen slice here. The registry **refuses any target that is in the training
  pool** (R1 held-out constraint) and refuses two targets of the same archetype
  (one per archetype).

- **The suite runner** (:func:`run_suite`) — runs a ``mode=benchmark`` episode
  per held-out target (the per-target run is injectable; the default wires
  :func:`~agent_families.grading.benchmark.run_benchmark_episode` with each
  target's slice), rolls the per-target scores into an aggregate, persists every
  score keyed ``(target, epoch, snapshot, mode=benchmark)`` to the **excluded
  meta channel** (never the training-fitness columns the ratchet reads — the
  Plan 4 ``ExcludedFitnessChannel`` discipline), and **recomputes the per-target
  and aggregate control charts**. Charts recompute *only* here (R1) — a standalone
  benchmark episode never touches the suite's SPC limits.

- **The generalization curves** (:func:`revisit_curve`, :func:`aggregate_curve`)
  — the same held-out target's score across epochs is the generalization series
  (DESIGN §11: improvement on a revisit, with a library shaped by *other* targets
  in between, is generalization, not memorization).

Test discipline (the benchmark/target_env precedent): the suite runner is
exercised offline with scripted per-target benchmark runs and scripted
``Driver``/``ResolveFn`` fakes — zero quota, no ``claude`` on PATH, no Docker.
The live boot/seed/hand-verification of LinkAce's slice (digest re-pin included)
is the documented docker-required deliverable, mirroring Kanboard's.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from agent_families.grading.benchmark import (
    BENCHMARK_MODE,
    KANBOARD_SLICE,
    BenchmarkScore,
    ExcludedFitnessChannel,
    FrozenSlice,
    run_benchmark_episode,
)
from agent_families.grading.scenarios import Driver, ResolutionCache, ResolveFn
from agent_families.grading.settle import SettleConfig
from agent_families.reflector.validate import (
    SpcLimits,
    ValidateParams,
    spc_limits,
)
from agent_families.store import Store

logger = logging.getLogger(__name__)

# Suite runs are benchmark mode — they never register ideas or feed training
# fitness (R1, inherited from Plan 4's held-out discipline).
SUITE_MODE = BENCHMARK_MODE

# The targets/ subdirectory each held-out target's compose+seed apparatus lives
# in (the target_env/benchmark precedent: one dir per onboarded target).
_TARGETS_DIR = Path(__file__).resolve().parent.parent.parent.parent / "targets"
KANBOARD_DIR = _TARGETS_DIR / "kanboard"
LINKACE_DIR = _TARGETS_DIR / "linkace"


class SuiteError(Exception):
    """A broken suite invariant with an actionable message (R1)."""


# --- the LinkAce held-out slice (R1) -----------------------------------------

# 12 must-tier scenarios over LinkAce's bookmark-manager core (links, lists,
# tags, search, archive, notes), each with a hand-verified reference verdict
# against the committed seed state. LinkAce is the held-out bookmarks instance
# (linkding is the *training* bookmarks instance) so the suite measures
# generalization within the archetype, not novelty shock (DESIGN §11).
#
# FROZEN — changing this tuple is a recorded apparatus migration that must re-pin
# LINKACE_SLICE_HASH and re-run the live hand-verification (the docker-required
# deliverable, mirroring Kanboard's). Each entry parses as a ScenarioManifest;
# the extra "reference" key is the frozen-replay expected verdict.
LINKACE_SLICE_SCENARIOS: tuple[dict, ...] = (
    {
        "scenario_id": "FEAT-LA-link-list/list-seeded-links",
        "feat_id": "FEAT-LA-link-list",
        "title": "Seeded links appear in the dashboard list",
        "tier": "must",
        "steps": [
            "Open the links dashboard",
            {
                "step": "Confirm the SQLite WAL article link is listed",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "SQLite WAL mode explained",
                },
            },
        ],
        "expected_outcome": "The seeded links are listed on the dashboard.",
        "reference": {"verdict": "pass", "rationale": "seed creates link rows"},
    },
    {
        "scenario_id": "FEAT-LA-link-create/create-link",
        "feat_id": "FEAT-LA-link-create",
        "title": "Create a new link",
        "tier": "must",
        "steps": [
            "Open the add-link form",
            "Fill the URL with https://example.org/articles/raft-consensus",
            {
                "step": "Submit the new link",
                "post_assertion": {"kind": "url_contains", "value": "links"},
            },
        ],
        "expected_outcome": "A new link is created and shown in the list.",
        "reference": {"verdict": "pass", "rationale": "create is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-open/open-link-detail",
        "feat_id": "FEAT-LA-link-open",
        "title": "Open a link's detail view",
        "tier": "must",
        "steps": [
            "Open the link titled Django ORM query patterns",
            {
                "step": "Confirm the link detail view is shown",
                "post_assertion": {"kind": "url_contains", "value": "links"},
            },
        ],
        "expected_outcome": "The link detail view opens for the chosen link.",
        "reference": {"verdict": "pass", "rationale": "link detail is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-edit/edit-link-title",
        "feat_id": "FEAT-LA-link-edit",
        "title": "Edit a link title",
        "tier": "must",
        "steps": [
            "Open the link titled Playwright trace viewer",
            "Open its edit form",
            "Change the title to Playwright trace viewer v2",
            {
                "step": "Save the edit",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Playwright trace viewer v2",
                },
            },
        ],
        "expected_outcome": "The link title updates to the edited value.",
        "reference": {"verdict": "pass", "rationale": "edit is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-delete/delete-link",
        "feat_id": "FEAT-LA-link-delete",
        "title": "Delete a link",
        "tier": "must",
        "steps": [
            "Open the link titled Pagination strategies for REST APIs",
            {
                "step": "Delete the link",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "button",
                    "name": "Restore link",
                },
            },
        ],
        "expected_outcome": "The link is removed and offers a restore action.",
        "reference": {"verdict": "pass", "rationale": "delete is core"},
    },
    {
        "scenario_id": "FEAT-LA-tag-filter/filter-by-tag",
        "feat_id": "FEAT-LA-tag-filter",
        "title": "Filter links by tag",
        "tier": "must",
        "steps": [
            "Open the tags overview",
            "Open the python tag",
            {
                "step": "Confirm only python-tagged links are shown",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Django ORM query patterns",
                },
            },
        ],
        "expected_outcome": "The list narrows to links carrying the python tag.",
        "reference": {"verdict": "pass", "rationale": "tag filter is core"},
    },
    {
        "scenario_id": "FEAT-LA-list-create/create-list",
        "feat_id": "FEAT-LA-list-create",
        "title": "Create a link list (collection)",
        "tier": "must",
        "steps": [
            "Open the new-list form",
            "Fill the list name with Reading queue",
            {
                "step": "Save the list",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Reading queue",
                },
            },
        ],
        "expected_outcome": "A new list named Reading queue is created.",
        "reference": {"verdict": "pass", "rationale": "lists are core"},
    },
    {
        "scenario_id": "FEAT-LA-list-assign/assign-link-to-list",
        "feat_id": "FEAT-LA-list-assign",
        "title": "Assign a link to a list",
        "tier": "must",
        "steps": [
            "Open the link titled uv: fast Python packaging",
            "Open its edit form",
            "Add it to the Reading queue list",
            {
                "step": "Save the assignment",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "Reading queue",
                },
            },
        ],
        "expected_outcome": "The link now belongs to the Reading queue list.",
        "reference": {"verdict": "pass", "rationale": "list membership is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-search/search-links",
        "feat_id": "FEAT-LA-link-search",
        "title": "Search links by text",
        "tier": "must",
        "steps": [
            "Open the global search",
            "Search for accessibility",
            {
                "step": "Confirm a matching link is listed",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "The accessibility tree",
                },
            },
        ],
        "expected_outcome": "Search returns links whose text matches the query.",
        "reference": {"verdict": "pass", "rationale": "search is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-archive/toggle-archive",
        "feat_id": "FEAT-LA-link-archive",
        "title": "Archive a link",
        "tier": "must",
        "steps": [
            "Open the link titled Docker named volumes vs bind mounts",
            {
                "step": "Archive the link",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "button",
                    "name": "Unarchive link",
                },
            },
        ],
        "expected_outcome": "The link is archived and offers to unarchive.",
        "reference": {"verdict": "pass", "rationale": "archive is core"},
    },
    {
        "scenario_id": "FEAT-LA-link-note/add-note",
        "feat_id": "FEAT-LA-link-note",
        "title": "Add a note to a link",
        "tier": "must",
        "steps": [
            "Open the link titled Structured output from language models",
            "Fill the note box with Re-read before the grader refactor",
            {
                "step": "Save the note",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "text",
                    "name": "Re-read before the grader refactor",
                },
            },
        ],
        "expected_outcome": "The note appears on the link's detail view.",
        "reference": {"verdict": "pass", "rationale": "notes are core"},
    },
    {
        "scenario_id": "FEAT-LA-tag-create/create-tag-on-link",
        "feat_id": "FEAT-LA-tag-create",
        "title": "Create a new tag while editing a link",
        "tier": "must",
        "steps": [
            "Open the link titled CSRF protection in Django forms",
            "Open its edit form",
            "Add a new tag named web-security",
            {
                "step": "Save the tag",
                "post_assertion": {
                    "kind": "node_present",
                    "role": "link",
                    "name": "web-security",
                },
            },
        ],
        "expected_outcome": "The new tag is created and attached to the link.",
        "reference": {"verdict": "pass", "rationale": "tag creation is core"},
    },
)

# Pinned content hash of LINKACE_SLICE_SCENARIOS — the immutability guard (R1).
# Recomputed and re-pinned only on a deliberate, hand-verified re-freeze.
LINKACE_SLICE_HASH = (
    "a903f27731d192167e2bb5ff319862b5080059d121a2005f65bdfa406a41edb3"
)

LINKACE_SLICE = FrozenSlice("linkace", LINKACE_SLICE_SCENARIOS, LINKACE_SLICE_HASH)


# --- the suite registry (R1) --------------------------------------------------


@dataclass(frozen=True)
class SuiteTarget:
    """One held-out suite target: its archetype, its apparatus dir, its slice."""

    name: str
    archetype: str
    target_dir: Path
    frozen_slice: FrozenSlice

    def __post_init__(self) -> None:
        if self.name != self.frozen_slice.name:
            raise SuiteError(
                f"suite target {self.name!r} must own a slice of the same name,"
                f" got slice {self.frozen_slice.name!r}"
            )


# The held-out suite. Kanboard (kanban) reuses Plan 4's frozen slice; LinkAce
# (bookmarks — a different instance than the training linkding) ships its own.
# A suite of 2 is explicitly valid early (the plan's Risks); the registry grows
# by appending qualified archetype instances without touching frozen slices.
HELD_OUT_SUITE: tuple[SuiteTarget, ...] = (
    SuiteTarget("kanboard", "kanban", KANBOARD_DIR, KANBOARD_SLICE),
    SuiteTarget("linkace", "bookmarks", LINKACE_DIR, LINKACE_SLICE),
)


def held_out_names(suite: Sequence[SuiteTarget] = HELD_OUT_SUITE) -> frozenset[str]:
    """The set of held-out target names — the curriculum's exclusion set (R2)."""
    return frozenset(t.name for t in suite)


def build_suite(
    targets: Sequence[SuiteTarget],
    *,
    training_pool_names: Sequence[str] = (),
) -> tuple[SuiteTarget, ...]:
    """Validate and freeze a suite (R1 held-out constraint, one-per-archetype).

    Refuses (a) a target whose name is in the training pool — a held-out target
    that was ever trained on measures memorization, not generalization (R1/R2);
    (b) two targets of the same archetype (the suite is one instance per
    archetype); (c) duplicate target names; (d) an empty suite.
    """
    if not targets:
        raise SuiteError("the benchmark suite must hold at least one target (R1)")
    pool = set(training_pool_names)
    seen_names: set[str] = set()
    seen_archetypes: set[str] = set()
    for t in targets:
        if t.name in pool:
            raise SuiteError(
                f"target {t.name!r} is in the training pool and cannot also be a"
                " held-out suite target — a benchmark trained on measures"
                " memorization, not generalization (R1/R2 held-out constraint)"
            )
        if t.name in seen_names:
            raise SuiteError(f"duplicate suite target name {t.name!r}")
        if t.archetype in seen_archetypes:
            raise SuiteError(
                f"the suite holds one instance per archetype (R1); archetype"
                f" {t.archetype!r} appears twice"
            )
        seen_names.add(t.name)
        seen_archetypes.add(t.archetype)
    return tuple(targets)


# --- per-run results ----------------------------------------------------------


@dataclass(frozen=True)
class SuiteTargetScore:
    """One held-out target's score within a suite run."""

    target: str
    archetype: str
    score: BenchmarkScore


@dataclass(frozen=True)
class SuiteRun:
    """One full-suite run's outcome keyed ``(epoch, snapshot)`` (R1).

    ``charts`` maps each target name (plus :data:`AGGREGATE_KEY`) to the SPC
    limits recomputed over its clean cross-epoch history, or ``None`` when the
    history is too short to chart.
    """

    epoch: int
    snapshot_id: int
    per_target: tuple[SuiteTargetScore, ...]
    aggregate: float
    charts: Mapping[str, SpcLimits | None]


# The aggregate series' name in chart/curve lookups (a per-target name can never
# collide — target names are validated unique and this carries a reserved form).
AGGREGATE_KEY = "__aggregate__"


@dataclass(frozen=True)
class SuiteParams:
    """Suite tunables, caller-supplied (the ValidateParams/SettleConfig precedent
    — no hidden config reads). PROVENANCE per DESIGN §17 is on each field."""

    # PROVENANCE: validate.spc_limits needs a non-empty history; ≥2 points makes a
    # control chart meaningful. TUNING METRIC: false-alarm rate vs chart latency.
    min_chart_points: int = 2
    validate_params: ValidateParams = field(default_factory=ValidateParams)

    def __post_init__(self) -> None:
        if self.min_chart_points < 1:
            raise SuiteError(
                f"min_chart_points must be >= 1, got {self.min_chart_points}"
            )


# (suite_target, snapshot_id, epoch) -> that target's benchmark score.
RunTargetFn = Callable[[SuiteTarget, int, int], BenchmarkScore]


# --- persistence keys (the excluded meta channel) -----------------------------


def suite_score_key(target: str, epoch: int) -> str:
    """The meta key a per-target suite score persists under (excluded channel)."""
    return f"suite:score:{target}:epoch{int(epoch)}"


def suite_aggregate_key(epoch: int) -> str:
    """The meta key the suite aggregate score persists under."""
    return f"suite:score:{AGGREGATE_KEY}:epoch{int(epoch)}"


def suite_chart_key(name: str) -> str:
    """The meta key a target's (or the aggregate's) control chart persists under."""
    return f"suite:chart:{name}"


_SCORE_PREFIX = "suite:score:"


# --- the suite runner (R1) ----------------------------------------------------


def make_benchmark_run_target(
    *,
    drivers_by_target: Mapping[str, Mapping[str, Driver]],
    cache: ResolutionCache,
    resolve: ResolveFn,
    settle_config: SettleConfig,
    reset_by_target: Mapping[str, Callable[[], None]] | None = None,
    fitness_channel: ExcludedFitnessChannel | None = None,
) -> RunTargetFn:
    """The default per-target runner: a ``mode=benchmark`` episode over the
    target's frozen slice (R1). Persistence is handled by :func:`run_suite`, so
    the inner episode runs with ``persist=False`` and routes any fitness to the
    excluded channel — the suite never writes training fitness."""

    reset_by_target = reset_by_target or {}

    def _run(target: SuiteTarget, snapshot_id: int, epoch: int) -> BenchmarkScore:
        drivers = drivers_by_target.get(target.name)
        if drivers is None:
            raise SuiteError(
                f"no drivers provided for suite target {target.name!r}"
            )
        return run_benchmark_episode(
            target=target.name,
            snapshot_id=snapshot_id,
            epoch=epoch,
            drivers=drivers,
            cache=cache,
            resolve=resolve,
            settle_config=settle_config,
            frozen_slice=target.frozen_slice,
            reset_target=reset_by_target.get(target.name),
            fitness_channel=fitness_channel,
            persist=False,
        )

    return _run


def run_suite(
    *,
    suite: Sequence[SuiteTarget],
    snapshot_id: int,
    epoch: int,
    run_target: RunTargetFn,
    store: Store | None = None,
    params: SuiteParams | None = None,
    sigma_by_target: Mapping[str, float] | None = None,
) -> SuiteRun:
    """Run the full held-out suite once, keyed ``(epoch, snapshot, mode)`` (R1).

    For each held-out target (in canonical name order, so a run is deterministic
    and parallel-safe per R17), measure a benchmark score; roll the per-target
    overalls into an aggregate; persist every score to the excluded meta channel;
    then **recompute the per-target and aggregate control charts** over the clean
    cross-epoch history (this is the *only* place the suite's SPC limits move —
    R1: limits recompute only on suite runs).

    ``sigma_by_target`` supplies each target's benchmark-instrument σ (from
    U4 ``benchmark_sigma`` replicate measurement, the σ ``validate.spc_limits``
    expects); the aggregate chart uses the mean of the per-target σ. Targets
    without a supplied σ fall back to ``0.0`` (degenerate zero-width limits) —
    surfaced, never silently skipped.
    """
    params = params or SuiteParams()
    if not suite:
        raise SuiteError("run_suite needs a non-empty suite (R1)")
    sigma_by_target = dict(sigma_by_target or {})

    ordered = sorted(suite, key=lambda t: t.name)
    per_target: list[SuiteTargetScore] = []
    for target in ordered:
        score = run_target(target, snapshot_id, epoch)
        if score.target != target.name:
            raise SuiteError(
                f"run_target for {target.name!r} returned a score keyed to"
                f" {score.target!r}"
            )
        if score.mode != SUITE_MODE:
            raise SuiteError(
                f"suite scores must be {SUITE_MODE!r} mode (R1), got"
                f" {score.mode!r} for {target.name!r}"
            )
        per_target.append(
            SuiteTargetScore(target.name, target.archetype, score)
        )

    aggregate = statistics.fmean([p.score.overall for p in per_target])

    if store is not None:
        for p in per_target:
            store.set_meta(
                suite_score_key(p.target, epoch),
                json.dumps(
                    {
                        "target": p.target,
                        "archetype": p.archetype,
                        "epoch": epoch,
                        "snapshot_id": snapshot_id,
                        "mode": SUITE_MODE,
                        "overall": p.score.overall,
                        "suspect": False,
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ),
            )
        store.set_meta(
            suite_aggregate_key(epoch),
            json.dumps(
                {
                    "target": AGGREGATE_KEY,
                    "epoch": epoch,
                    "snapshot_id": snapshot_id,
                    "mode": SUITE_MODE,
                    "overall": aggregate,
                    "n_targets": len(per_target),
                    "suspect": False,
                },
                sort_keys=True,
                ensure_ascii=False,
            ),
        )

    # Recompute control charts — ONLY here (R1). Per-target charts read each
    # target's clean cross-epoch history; the aggregate chart reads the aggregate
    # series. σ comes from the replicate measurement, never the moving range.
    charts: dict[str, SpcLimits | None] = {}
    if store is not None:
        agg_sigmas = [sigma_by_target.get(p.target, 0.0) for p in per_target]
        agg_sigma = statistics.fmean(agg_sigmas) if agg_sigmas else 0.0
        for p in per_target:
            charts[p.target] = _recompute_chart(
                store,
                p.target,
                suite_chart_key(p.target),
                sigma_by_target.get(p.target, 0.0),
                params,
            )
        charts[AGGREGATE_KEY] = _recompute_chart(
            store,
            AGGREGATE_KEY,
            suite_chart_key(AGGREGATE_KEY),
            agg_sigma,
            params,
        )
    else:
        for p in per_target:
            charts[p.target] = None
        charts[AGGREGATE_KEY] = None

    logger.info(
        "suite run epoch %d @ snapshot %d: aggregate %.3f over %d held-out"
        " target(s)",
        epoch,
        snapshot_id,
        aggregate,
        len(per_target),
    )
    return SuiteRun(
        epoch=epoch,
        snapshot_id=snapshot_id,
        per_target=tuple(per_target),
        aggregate=aggregate,
        charts=charts,
    )


def _recompute_chart(
    store: Store,
    name: str,
    chart_key: str,
    sigma: float,
    params: SuiteParams,
) -> SpcLimits | None:
    """Recompute and persist one control chart over its clean history (R1).

    Returns ``None`` (and persists nothing) while the clean history is shorter
    than ``min_chart_points`` — too few points to chart. The clean history
    excludes instrument-suspect scores (R18 discipline; suite fixtures are all
    clean, but the filter is honored so the seam is real)."""
    series = _clean_series(store, name)
    history = [overall for _epoch, overall in series]
    if len(history) < params.min_chart_points:
        return None
    limits = spc_limits(history, sigma, params.validate_params)
    store.set_meta(
        chart_key,
        json.dumps(
            {
                "name": name,
                "center": limits.center,
                "lcl": limits.lcl,
                "ucl": limits.ucl,
                "sigma": sigma,
                "n_points": len(history),
            },
            sort_keys=True,
            ensure_ascii=False,
        ),
    )
    return limits


# --- the generalization curves (R2) -------------------------------------------


def _clean_series(store: Store, name: str) -> tuple[tuple[int, float], ...]:
    """The (epoch, overall) series for a target (or the aggregate), excluding
    instrument-suspect scores, sorted by epoch (R18/R2)."""
    rows = store.conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
        (f"{_SCORE_PREFIX}{name}:epoch%",),
    ).fetchall()
    out: list[tuple[int, float]] = []
    for row in rows:
        parsed = json.loads(row["value"])
        if parsed.get("suspect"):
            continue
        out.append((int(parsed["epoch"]), float(parsed["overall"])))
    out.sort(key=lambda pt: pt[0])
    return tuple(out)


def revisit_curve(store: Store, target: str) -> tuple[tuple[int, float], ...]:
    """The held-out target's score across epochs — the generalization series (R2).

    Improvement on a later epoch's revisit, with a library shaped by *other*
    targets in between, is generalization (DESIGN §11)."""
    return _clean_series(store, target)


def aggregate_curve(store: Store) -> tuple[tuple[int, float], ...]:
    """The suite-aggregate score across epochs."""
    return _clean_series(store, AGGREGATE_KEY)


def get_chart(store: Store, name: str) -> SpcLimits | None:
    """Read a persisted control chart's limits (read-only — never recomputes)."""
    raw = store.get_meta(suite_chart_key(name))
    if raw is None:
        return None
    parsed = json.loads(raw)
    return SpcLimits(
        center=parsed["center"], lcl=parsed["lcl"], ucl=parsed["ucl"]
    )
