"""Rehearsal pass and the one-shot metric (plan-005 U2, R5-R8).

The autonomy instrument. After an increment *converges* (its Ralph loops have
settled all tickets, DESIGN §11), the rehearsal pass re-executes every ticket as
a **fan-out**: each ticket runs once, in its own worktree branched from the
orchestrator-minted increment-base ref (Plan 4's contract), **one shot, no Ralph
loop**, consuming the episode's accumulated run-memory workflows (that
consumption is what makes one-shot rates a *learning* curve rather than a
raw-model baseline — R5). The fan-out runs in DAG waves (topological order,
parallel within a wave) with file-ownership serialization: the Phase 1
file-ownership lint — WARN-only and consumer-less until now — is **promoted to
enforcement** here (R8), serializing tickets that claim a shared file into
separate waves so two parallel one-shot sessions never write the same file.

After the waves, the fan-out artifact is merged in wave order, gated, and given
ONE integration verify:

- **all green** → adopt the fan-out artifact (the engagement workspace head moves
  to it);
- **any failure** → fall back to the converged artifact (head unchanged) and,
  when every ticket passed *alone* but the merge *broke together*, record the
  ``passed_alone_broke_together`` integration-failure class — the reflector's
  first integration-lesson source (R5). Rehearsal failure is signal, never a loop
  (§11): the episode is left exactly as a no-rehearsal run would leave it.

The one-shot metric (R6) is computed per (target, epoch, snapshot) alongside
rubric scores: the **ticket one-shot rate** (rehearsal tickets passing with zero
iterations), **increment one-shot** (the whole increment's fan-out adopted in one
shot), and **episode one-shot** — the last measurable ONLY by ``rebuild_probe``
episodes (a fresh workspace + full-app opening prompt + one delivery pass);
ordinary increments structurally cannot measure it, so ``episode_one_shot`` is
``None`` outside rebuild-probe mode.

Rehearsal economics (R7): :func:`should_rehearse` rehearses every increment for
the first ``baseline_increments`` (the metric baseline) then samples so rehearsal
spend stays within ``max_budget_share`` of the episode budget; the converge-first
→ fan-out-first graduation is a computed cost-model crossover
(:func:`cost_model_crossover`) surfaced in reports and flipped by config — never
hardcoded here.

Offline by construction (the established seam discipline): the per-ticket one-shot
session, the integration verify, the workspace adopt, and the workflow injection
are injected callables, so the full decision table — waves, adopt/fallback, the
metric, sampling, the crossover — runs with zero quota and no ``claude`` on PATH.
Tunables are caller-supplied via :class:`SamplingPolicy` (the U3/U6/validate
precedent); nothing here reads config directly.

Deviation (recorded, smallest faithful adaptation): the
``passed_alone_broke_together`` integration-failure record is persisted inside the
rehearsal record document (meta), NOT as a ``failure_records`` row — the Phase 0
``FAILURE_KINDS`` enum has no such kind and this wave does not migrate the schema
(an owning-unit change). The record is typed, queryable
(:func:`rehearsal_records`), and carries the same §7 fields the reflector reads.
The one-shot metric is exposed for settlement reports via
:func:`rehearsal_metric_section` (a builder the report assembler embeds), for the
same reason: this unit does not edit the Phase 2 settlement renderer.

## Conformance

plan-005 U2 test scenario / invariant -> test (in ``tests/test_rehearsal.py``):

- diamond DAG rehearses in waves with the conflicting-files pair serialized:
  ``test_diamond_dag_rehearses_in_waves_with_conflicting_pair_serialized``
- non-conflicting same-level tickets share a wave:
  ``test_nonconflicting_tickets_share_a_wave``
- all-green rehearsal adopts the fan-out artifact (workspace head moves):
  ``test_all_green_rehearsal_adopts_fanout_artifact``
- passed-alone-broke-together falls back and records the integration failure:
  ``test_passed_alone_broke_together_falls_back_and_records``
- a ticket failing its one-shot falls back (no adopt, no integration attempt):
  ``test_failed_ticket_one_shot_falls_back``
- one-shot sessions consume the episode's run-memory workflow ledger:
  ``test_one_shot_sessions_consume_run_memory_ledger``
- one-shot rates computed correctly across fixtures:
  ``test_one_shot_rates_computed_across_fixtures``,
  ``test_rebuild_probe_measures_episode_one_shot``
- sampling honors the budget share:
  ``test_sampling_honors_budget_share``
- crossover report compares both regimes' measured costs:
  ``test_crossover_report_compares_both_regimes``
- the metric is persisted and surfaced for settlement reports:
  ``test_metric_persisted_and_surfaced_for_settlement``
- the fallback path leaves the episode equivalent to a no-rehearsal run:
  ``test_fallback_leaves_episode_equivalent_to_no_rehearsal``
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# Rehearsal modes (R6): an ordinary increment rehearsal cannot measure episode
# one-shot; only a rebuild-probe (fresh workspace, full-app opening prompt, one
# delivery pass) can. The mode gates which metric is meaningful.
MODE_INCREMENT = "increment"
MODE_REBUILD_PROBE = "rebuild_probe"
REHEARSAL_MODES = (MODE_INCREMENT, MODE_REBUILD_PROBE)

# The typed integration-failure class (R5): every ticket passed its own one-shot
# session but the merged fan-out broke the integration verify. This is the
# reflector's first integration-lesson source.
INTEGRATION_FAILURE_KIND = "passed_alone_broke_together"


class RehearsalError(Exception):
    """Rehearsal misuse or invariant breach with an actionable message."""


# --- the rehearsal unit of work ---------------------------------------------------


@dataclass(frozen=True)
class RehearsalTicket:
    """One converged ticket to re-execute one-shot: its id, deps, and owned files.

    ``files`` are the workspace-relative paths the ticket owns (the planner's
    ``files`` field, canonicalized): the file-ownership lint's data, consumed
    here for wave serialization (R8).
    """

    ticket_id: str
    depends_on: tuple[str, ...] = ()
    files: tuple[str, ...] = ()


def tickets_from_plan(document: dict) -> tuple[RehearsalTicket, ...]:
    """Build the rehearsal ticket set from a canonical plan document (planning).

    Reads each ticket's canonical id, ``depends_on``, and ``files`` — exactly the
    fields the orchestrator executes and the file-ownership lint inspects.
    """
    out: list[RehearsalTicket] = []
    for ticket in document["tickets"]:
        out.append(
            RehearsalTicket(
                ticket_id=ticket["id"],
                depends_on=tuple(ticket.get("depends_on", ())),
                files=tuple(ticket.get("files", ())),
            )
        )
    return tuple(out)


# --- wave scheduling: DAG waves + file-ownership serialization (R8) ----------------


def _topological_order(tickets: Sequence[RehearsalTicket]) -> list[str]:
    """Deterministic topological order (lexicographic tiebreak); loud on cycles."""
    ids = {t.ticket_id for t in tickets}
    if len(ids) != len(tickets):
        raise RehearsalError("duplicate ticket id in the rehearsal set")
    deps: dict[str, set[str]] = {}
    for t in tickets:
        for dep in t.depends_on:
            if dep == t.ticket_id:
                raise RehearsalError(f"ticket {t.ticket_id} depends on itself")
            if dep not in ids:
                raise RehearsalError(
                    f"ticket {t.ticket_id} has unknown dependency {dep!r}"
                )
        deps[t.ticket_id] = set(t.depends_on)
    indegree = {tid: len(deps[tid]) for tid in ids}
    dependents: dict[str, list[str]] = {tid: [] for tid in ids}
    for tid, dset in deps.items():
        for dep in dset:
            dependents[dep].append(tid)
    ready = sorted(tid for tid, d in indegree.items() if d == 0)
    order: list[str] = []
    while ready:
        current = ready.pop(0)
        order.append(current)
        for nxt in dependents[current]:
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                # Keep `ready` sorted so the order is deterministic.
                ready.append(nxt)
                ready.sort()
    if len(order) != len(ids):
        cyclic = sorted(tid for tid, d in indegree.items() if d > 0)
        raise RehearsalError(f"dependency cycle among rehearsal tickets: {cyclic}")
    return order


def compute_waves(
    tickets: Sequence[RehearsalTicket],
) -> tuple[tuple[str, ...], ...]:
    """The fan-out wave schedule (R8): topological waves, conflicts serialized.

    A ticket's earliest wave is one past the latest wave of any dependency.
    Within that wave it is bumped forward past any already-assigned ticket whose
    file set it intersects — the file-ownership lint promoted from WARN to
    **enforcement**: two tickets that claim a shared file are never in the same
    wave, so parallel one-shot sessions cannot collide on a file. Waves are
    contiguous and each is lexicographically sorted; deterministic.
    """
    if not tickets:
        return ()
    by_id = {t.ticket_id: t for t in tickets}
    files = {t.ticket_id: set(t.files) for t in tickets}
    wave_of: dict[str, int] = {}
    members: dict[int, list[str]] = {}
    for tid in _topological_order(tickets):
        earliest = 0
        for dep in by_id[tid].depends_on:
            earliest = max(earliest, wave_of[dep] + 1)
        wave = earliest
        while any(
            files[tid] & files[other] for other in members.get(wave, ())
        ):
            wave += 1
        wave_of[tid] = wave
        members.setdefault(wave, []).append(tid)
    # Remap to contiguous, gap-free wave indices in order.
    used = sorted(members)
    return tuple(tuple(sorted(members[w])) for w in used)


def file_conflict_pairs(
    tickets: Sequence[RehearsalTicket],
) -> list[tuple[str, str]]:
    """Ticket pairs sharing >= 1 owned file (the enforcement input, R8).

    The same overlap the file-ownership lint records as a WARN in Phase 1; here
    it is the data the wave scheduler enforces on. Pairs are ordered (a < b) and
    deduplicated; deterministic.
    """
    files = [(t.ticket_id, set(t.files)) for t in tickets]
    pairs: set[tuple[str, str]] = set()
    for i in range(len(files)):
        for j in range(i + 1, len(files)):
            if files[i][1] & files[j][1]:
                a, b = sorted((files[i][0], files[j][0]))
                pairs.add((a, b))
    return sorted(pairs)


# --- per-ticket and integration outcomes (injected seams) --------------------------


@dataclass(frozen=True)
class RehearsalTicketResult:
    """One ticket's one-shot fan-out outcome.

    ``passed`` is the single delivery pass's verdict; ``iterations`` is the
    number of Ralph iterations spent — for a true one-shot it is 1, and a value
    > 1 means the no-loop contract was violated (a caller/seam bug, caught by
    :func:`run_rehearsal`).
    """

    ticket_id: str
    passed: bool
    iterations: int = 1
    detail: str = ""


@dataclass(frozen=True)
class IntegrationResult:
    """The single post-merge integration verify's outcome (gate + verifier)."""

    passed: bool
    detail: str = ""


# (ticket, injected_workflow_ledger_section) -> one-shot result.
RehearseTicketFn = Callable[[RehearsalTicket, str], RehearsalTicketResult]
# (the adopted fan-out ticket results) -> integration verify outcome.
IntegrateFn = Callable[[Sequence[RehearsalTicketResult]], IntegrationResult]
# Move the engagement workspace head to the merged fan-out artifact (adopt).
AdoptFn = Callable[[], None]
# Compose the episode's run-memory workflow ledger section (the injection, R5).
ComposeInjectionFn = Callable[[], str]


# --- the one-shot metric (R6) ------------------------------------------------------


@dataclass(frozen=True)
class OneShotMetric:
    """The autonomy curve's per-rehearsal point (R6).

    ``ticket_one_shot_rate`` is the fraction of rehearsal tickets that passed
    their one-shot session; ``increment_one_shot`` is True iff the whole fan-out
    was adopted (every ticket passed alone AND the integration verify passed);
    ``episode_one_shot`` is meaningful ONLY for ``rebuild_probe`` mode (ordinary
    increments cannot measure it) and is ``None`` otherwise.
    """

    mode: str
    tickets_total: int
    tickets_passed: int
    ticket_one_shot_rate: float
    increment_one_shot: bool
    episode_one_shot: bool | None

    def as_dict(self) -> dict:
        return {
            "episode_one_shot": self.episode_one_shot,
            "increment_one_shot": self.increment_one_shot,
            "mode": self.mode,
            "ticket_one_shot_rate": self.ticket_one_shot_rate,
            "tickets_passed": self.tickets_passed,
            "tickets_total": self.tickets_total,
        }


def _compute_metric(
    *, mode: str, tickets_total: int, tickets_passed: int, adopted: bool
) -> OneShotMetric:
    rate = (tickets_passed / tickets_total) if tickets_total else 0.0
    return OneShotMetric(
        mode=mode,
        tickets_total=tickets_total,
        tickets_passed=tickets_passed,
        ticket_one_shot_rate=rate,
        increment_one_shot=adopted,
        # Episode one-shot is measurable only by a rebuild-probe (R6).
        episode_one_shot=adopted if mode == MODE_REBUILD_PROBE else None,
    )


@dataclass(frozen=True)
class IntegrationFailure:
    """The ``passed_alone_broke_together`` typed record (R5).

    Written ONLY when every ticket passed its own one-shot session but the merged
    fan-out failed the integration verify — the integration-lesson the reflector
    consumes. ``passed_alone`` names the tickets that each passed alone.
    """

    kind: str
    passed_alone: tuple[str, ...]
    location: str
    expected: str
    observed: str

    def as_record(self) -> dict:
        return {
            "expected": self.expected,
            "kind": self.kind,
            "location": self.location,
            "observed": self.observed,
            "passed_alone": list(self.passed_alone),
        }


# --- the rehearsal result + persistence -------------------------------------------


@dataclass(frozen=True)
class RehearsalResult:
    """One increment's rehearsal outcome — the metric and the adopt/fallback fact."""

    episode_id: int
    increment_index: int
    run_id: int
    mode: str
    waves: tuple[tuple[str, ...], ...]
    ticket_results: tuple[RehearsalTicketResult, ...]
    adopted: bool
    integration_attempted: bool
    integration_passed: bool
    metric: OneShotMetric
    integration_failure: IntegrationFailure | None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "adopted": self.adopted,
            "detail": self.detail,
            "episode_id": self.episode_id,
            "increment_index": self.increment_index,
            "integration_attempted": self.integration_attempted,
            "integration_failure": (
                self.integration_failure.as_record()
                if self.integration_failure is not None
                else None
            ),
            "integration_passed": self.integration_passed,
            "metric": self.metric.as_dict(),
            "mode": self.mode,
            "run_id": self.run_id,
            "ticket_results": [
                {
                    "detail": r.detail,
                    "iterations": r.iterations,
                    "passed": r.passed,
                    "ticket_id": r.ticket_id,
                }
                for r in self.ticket_results
            ],
            "waves": [list(w) for w in self.waves],
        }


def rehearsal_record_key(episode_id: int, increment_index: int) -> str:
    """The ``meta`` key of one increment's rehearsal record document."""
    return f"pipeline:rehearsal:{int(episode_id)}:{int(increment_index)}"


def _rehearsal_key_prefix(episode_id: int) -> str:
    return f"pipeline:rehearsal:{int(episode_id)}:"


def persist_rehearsal_record(store: Store, result: RehearsalResult) -> None:
    """Persist the rehearsal record (R5/R6 — the metric and the typed failure)."""
    store.set_meta(
        rehearsal_record_key(result.episode_id, result.increment_index),
        json.dumps(result.as_dict(), sort_keys=True, ensure_ascii=False),
    )


def rehearsal_records(store: Store, episode_id: int) -> list[dict]:
    """Every persisted rehearsal record for the episode, increment-ordered."""
    rows = store.conn.execute(
        "SELECT key, value FROM meta WHERE key LIKE ? ORDER BY key",
        (_rehearsal_key_prefix(episode_id) + "%",),
    ).fetchall()
    out: list[dict] = []
    for row in rows:
        try:
            out.append(json.loads(row["value"]))
        except json.JSONDecodeError as exc:  # pragma: no cover - corrupt meta
            raise RehearsalError(
                f"corrupt rehearsal record at {row['key']}: {exc}"
            ) from exc
    # `meta` keys sort lexicographically; sort by integer increment to be exact.
    out.sort(key=lambda d: int(d["increment_index"]))
    return out


# --- the rehearsal pass (R5/R6/R8) ------------------------------------------------


def run_rehearsal(
    store: Store,
    *,
    episode_id: int,
    increment_index: int,
    run_id: int,
    tickets: Sequence[RehearsalTicket],
    rehearse_fn: RehearseTicketFn,
    integrate_fn: IntegrateFn,
    adopt_fn: AdoptFn | None = None,
    compose_injection_fn: ComposeInjectionFn | None = None,
    mode: str = MODE_INCREMENT,
    persist: bool = True,
) -> RehearsalResult:
    """Run one increment's rehearsal fan-out and compute the one-shot metric.

    The fan-out runs ticket-by-ticket in wave order (:func:`compute_waves`), each
    a one-shot session via ``rehearse_fn`` receiving the episode's run-memory
    ledger (``compose_injection_fn``, R5). When EVERY ticket passes alone the
    merged artifact gets ONE integration verify (``integrate_fn``); all green
    adopts (``adopt_fn`` moves the workspace head, R5) and any failure falls back
    — recording ``passed_alone_broke_together`` when the break is at integration.
    A ticket that fails its own one-shot short-circuits to fallback with no
    integration attempt (it is not a passed-alone-broke-together case).

    Rehearsal failure is signal, never a loop (§11): on fallback nothing mutates
    the engagement workspace, so the episode is left exactly as a no-rehearsal run
    would leave it.
    """
    if mode not in REHEARSAL_MODES:
        raise RehearsalError(
            f"unknown rehearsal mode {mode!r} (expected one of {REHEARSAL_MODES})"
        )
    waves = compute_waves(tickets)
    injection = compose_injection_fn() if compose_injection_fn is not None else ""

    ticket_results: list[RehearsalTicketResult] = []
    by_id = {t.ticket_id: t for t in tickets}
    for wave in waves:
        for tid in wave:
            res = rehearse_fn(by_id[tid], injection)
            if res.ticket_id != tid:
                raise RehearsalError(
                    f"rehearse_fn returned result for {res.ticket_id!r} when"
                    f" rehearsing {tid!r}"
                )
            if res.iterations != 1:
                raise RehearsalError(
                    f"rehearsal is one-shot (no Ralph loop, R5): ticket {tid}"
                    f" reported {res.iterations} iterations"
                )
            ticket_results.append(res)

    passed_alone = tuple(r.ticket_id for r in ticket_results if r.passed)
    all_passed = len(tickets) > 0 and len(passed_alone) == len(tickets)

    integration_attempted = False
    integration_passed = False
    adopted = False
    integration_failure: IntegrationFailure | None = None
    detail = ""

    if all_passed:
        integration_attempted = True
        integ = integrate_fn(ticket_results)
        integration_passed = integ.passed
        if integ.passed:
            if adopt_fn is not None:
                adopt_fn()
            adopted = True
            detail = "fan-out adopted (all tickets passed alone; integration green)"
        else:
            # passed alone, broke together — the integration-lesson class (R5).
            integration_failure = IntegrationFailure(
                kind=INTEGRATION_FAILURE_KIND,
                passed_alone=passed_alone,
                location=f"integration verify (run {run_id})",
                expected="merged fan-out passes the single integration verify",
                observed=integ.detail
                or "integration verify failed though every ticket passed alone",
            )
            detail = (
                "fallback to converged artifact: passed_alone_broke_together"
                f" — {integ.detail or 'integration verify failed'}"
            )
            logger.warning(
                "rehearsal episode %d increment %d: %s (passed alone: %s)",
                episode_id,
                increment_index,
                INTEGRATION_FAILURE_KIND,
                ", ".join(passed_alone) or "(none)",
            )
    else:
        failed = [r.ticket_id for r in ticket_results if not r.passed]
        detail = (
            "fallback to converged artifact: ticket(s) failed their one-shot"
            f" session: {', '.join(failed) or '(no tickets)'}"
        )

    metric = _compute_metric(
        mode=mode,
        tickets_total=len(tickets),
        tickets_passed=len(passed_alone),
        adopted=adopted,
    )
    result = RehearsalResult(
        episode_id=episode_id,
        increment_index=increment_index,
        run_id=run_id,
        mode=mode,
        waves=waves,
        ticket_results=tuple(ticket_results),
        adopted=adopted,
        integration_attempted=integration_attempted,
        integration_passed=integration_passed,
        metric=metric,
        integration_failure=integration_failure,
        detail=detail,
    )
    if persist:
        persist_rehearsal_record(store, result)
    logger.info(
        "rehearsal episode %d increment %d (mode=%s): %d/%d tickets one-shot,"
        " adopted=%s",
        episode_id,
        increment_index,
        mode,
        metric.tickets_passed,
        metric.tickets_total,
        adopted,
    )
    return result


# --- run-memory injection for the one-shot sessions (R5) ---------------------------


def episode_workflow_injection(
    store: Store, episode_id: int, budget_tokens: int
) -> str:
    """The episode's run-memory ledger rendered for a one-shot session (R5).

    Composes the live workflows under the standard injection budget (the Plan 4
    R3 budget cap) — the one-shot sessions consume the SAME run memory the Ralph
    workers did, which is what makes one-shot rates a learning curve, not a
    raw-model baseline. The library half is left to the caller's retrieval thunk
    (omitted here = run memory only); the run-assembly wiring binds the query
    vector / family / mode exactly as it does for the workers.
    """
    from agent_families.pipeline.runmemory import compose_injection

    return compose_injection(
        store, episode_id=episode_id, budget_tokens=budget_tokens
    ).section


# --- settlement surfacing (R6) -----------------------------------------------------


def rehearsal_metric_section(store: Store, episode_id: int) -> dict:
    """The episode's one-shot metric, aggregated for the settlement report (R6).

    The builder the settlement-report assembler embeds (this unit does not edit
    the Phase 2 renderer): per-increment metric points plus episode aggregates —
    the increment one-shot rate, the mean ticket one-shot rate, and the
    rebuild-probe episode one-shot when one was run.
    """
    records = rehearsal_records(store, episode_id)
    increments = [r["metric"] for r in records]
    rehearsed = len(increments)
    increment_one_shots = sum(1 for m in increments if m["increment_one_shot"])
    mean_ticket_rate = (
        sum(m["ticket_one_shot_rate"] for m in increments) / rehearsed
        if rehearsed
        else 0.0
    )
    probe_points = [
        m["episode_one_shot"]
        for m in increments
        if m["mode"] == MODE_REBUILD_PROBE and m["episode_one_shot"] is not None
    ]
    return {
        "episode_id": episode_id,
        "increment_one_shot_rate": (
            increment_one_shots / rehearsed if rehearsed else 0.0
        ),
        "increments_rehearsed": rehearsed,
        "integration_failures": [
            {
                "increment_index": r["increment_index"],
                "record": r["integration_failure"],
            }
            for r in records
            if r["integration_failure"] is not None
        ],
        "mean_ticket_one_shot_rate": mean_ticket_rate,
        "per_increment": increments,
        "rebuild_probe_episode_one_shot": (
            all(probe_points) if probe_points else None
        ),
    }


# --- rehearsal economics: sampling + cost-model crossover (R7) ---------------------


@dataclass(frozen=True)
class SamplingPolicy:
    """The rehearsal sampling tunables, caller-supplied (R7; no hidden config).

    Rehearse every increment for the first ``baseline_increments`` (the metric
    baseline), then sample so cumulative rehearsal spend stays within
    ``max_budget_share`` of the episode budget. PROVENANCE (DESIGN §17 / R7):
    "rehearse every increment for the first episodes, then sample so rehearsal
    spend ≤ ~15-20% of episode budget". TUNING METRIC: one-shot-curve variance vs.
    rehearsal quota burn.
    """

    baseline_increments: int
    max_budget_share: float

    def __post_init__(self) -> None:
        if self.baseline_increments < 0:
            raise RehearsalError(
                "baseline_increments must be >= 0, got"
                f" {self.baseline_increments}"
            )
        if not (0.0 < self.max_budget_share <= 1.0):
            raise RehearsalError(
                "max_budget_share must be in (0, 1], got"
                f" {self.max_budget_share}"
            )


@dataclass(frozen=True)
class SamplingDecision:
    """Whether to rehearse this increment, and why (R7)."""

    rehearse: bool
    reason: str
    projected_share: float


def should_rehearse(
    policy: SamplingPolicy,
    *,
    increment_index: int,
    rehearsal_cost_so_far: float,
    projected_rehearsal_cost: float,
    episode_budget: float,
) -> SamplingDecision:
    """Decide whether to rehearse increment ``increment_index`` (1-based, R7).

    The first ``baseline_increments`` always rehearse (the metric baseline).
    After that, rehearse only while the projected cumulative rehearsal spend
    stays within ``max_budget_share`` of the episode budget — the sampling that
    bounds rehearsal economics.
    """
    if episode_budget <= 0:
        raise RehearsalError(
            f"episode_budget must be positive, got {episode_budget}"
        )
    if increment_index <= policy.baseline_increments:
        projected = rehearsal_cost_so_far + projected_rehearsal_cost
        return SamplingDecision(
            rehearse=True,
            reason=(
                f"baseline period (increment {increment_index} <="
                f" {policy.baseline_increments}): always rehearse for the metric"
                " baseline"
            ),
            projected_share=projected / episode_budget,
        )
    projected = rehearsal_cost_so_far + projected_rehearsal_cost
    projected_share = projected / episode_budget
    within = projected_share <= policy.max_budget_share
    return SamplingDecision(
        rehearse=within,
        reason=(
            f"sampling: projected rehearsal share {projected_share:.4f}"
            f" {'<=' if within else '>'} budget share {policy.max_budget_share:.4f}"
        ),
        projected_share=projected_share,
    )


# Regimes for the cost-model crossover (R7).
REGIME_CONVERGE_FIRST = "converge_first"
REGIME_FAN_OUT_FIRST = "fan_out_first"


@dataclass(frozen=True)
class CrossoverReport:
    """The converge-first → fan-out-first cost crossover (R7).

    Compares both regimes' MEASURED costs; ``crossed_over`` is True once
    fan-out-first is the cheaper regime (the graduation point) and ``savings`` is
    how much the cheaper regime saves. Surfaced in reports and flipped by config
    — never an implicit default.
    """

    converge_first_cost: float
    fan_out_first_cost: float
    cheaper_regime: str
    crossed_over: bool
    savings: float

    def as_dict(self) -> dict:
        return {
            "cheaper_regime": self.cheaper_regime,
            "converge_first_cost": self.converge_first_cost,
            "crossed_over": self.crossed_over,
            "fan_out_first_cost": self.fan_out_first_cost,
            "savings": self.savings,
        }


def cost_model_crossover(
    *, converge_first_cost: float, fan_out_first_cost: float
) -> CrossoverReport:
    """Compare both regimes' measured costs and report the crossover (R7)."""
    if converge_first_cost < 0 or fan_out_first_cost < 0:
        raise RehearsalError(
            "regime costs must be >= 0, got"
            f" converge_first={converge_first_cost},"
            f" fan_out_first={fan_out_first_cost}"
        )
    crossed = fan_out_first_cost < converge_first_cost
    cheaper = REGIME_FAN_OUT_FIRST if crossed else REGIME_CONVERGE_FIRST
    savings = abs(converge_first_cost - fan_out_first_cost)
    return CrossoverReport(
        converge_first_cost=converge_first_cost,
        fan_out_first_cost=fan_out_first_cost,
        cheaper_regime=cheaper,
        crossed_over=crossed,
        savings=savings,
    )
