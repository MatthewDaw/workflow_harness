"""Rehearsal pass and the one-shot metric (plan-005 U2, R5-R8).

The system's headline goal is to approach **one-shotting an entire codebase**
(DESIGN §11). Each increment is therefore executed in two passes: the
**convergence pass** (Phase 1's Ralph loop — already built) produces a working,
integrated artifact; this module adds the **rehearsal pass** that re-executes
the same tickets as a *fan-out* and measures how close the library has come to
delivering them with zero iteration.

What the rehearsal does (R5):

- Post-convergence, re-execute every ticket as a fan-out — each in its own git
  worktree cut from the **orchestrator-minted increment-base ref** (the commit
  the increment started from, Plan 4's contract; a bare worktree has no
  ``node_modules`` so the live provider checks the increment-base tree out over
  a template copy — :class:`WorktreeProvider`; the offline suite drives the
  lightweight git-worktree provider with fake one-shot sessions).
- Tickets run in **DAG waves** (:func:`compute_waves`): topological order,
  parallel within a wave, **with file-ownership serialization** — two tickets in
  the same topological layer that claim the same file are split into separate
  waves. This is the Phase 1 file-ownership lint (planning.py) finally promoted
  from *warn* to *enforcement* (R8): its data now drives the schedule
  (:func:`~agent_families.pipeline.planning.file_ownership_conflicts`).
- Each ticket gets **one shot, no iteration**, consuming the episode's
  accumulated run-memory workflows (R5 — that consumption is what makes one-shot
  rate a *learning* curve rather than a raw-model baseline;
  :func:`~agent_families.pipeline.runmemory.compose_injection` under the standard
  injection budget).
- Merge in wave order, run the harness gate, one integration verify. **All
  green → adopt** the fan-out artifact (the engagement workspace HEAD moves to
  it). **Any failure → fall back** to the converged artifact (the workspace is
  left byte-for-byte where convergence left it) and write the typed
  *passed-alone-broke-together* integration-failure record — the reflector's
  first integration-lesson source (R5).

Rehearsal failure is **signal, never a loop** (§11): it is never iterated; the
converged artifact is always the fallback, so the episode always advances.

The one-shot metric (R6): *ticket one-shot rate* (rehearsal tickets passing with
zero iterations), *increment one-shot* (the whole fan-out passes integration on
the first try), tracked per ``(target, epoch, snapshot)`` alongside rubric
scores. *Episode one-shot* is structurally measurable only by **rebuild-probe
episodes** (``mode=rebuild_probe``: fresh workspace, full-app opening prompt, one
delivery pass) — :data:`REBUILD_PROBE_MODE` and :func:`should_promote_probe`
carry the probe-promotion (clone-rot remedy) decision; the probe *episode*
orchestration itself is the scheduler's job (U5/U7).

Rehearsal economics (R7): :class:`RehearsalSamplingPolicy` rehearses every
increment for the first episodes (the metric baseline) then samples so rehearsal
spend stays under a configured budget share; :func:`crossover_report` surfaces
the converge-first → fan-out-first cost-model crossover (flipped by config, not a
rule of thumb).

Persistence: the §7 integration-failure record's ``failure_kind`` is not in the
store's frozen ``failure_records`` enum (owned by the schema units), so the whole
rehearsal outcome — metric and any integration-failure record in the §7 shape —
is persisted in the ``meta`` channel keyed per run (the no-new-DDL precedent
benchmark.py / curriculum.py established). When a future migration adds a
rehearsal-integration failure kind the record maps onto the column unchanged.
:func:`attach_one_shot_metrics` folds the episode's metric into a settlement
report dict (the report renderer is owned by plan-003 U7; this is the seam the
run-assembly wires).

Windows KTDs honored: utf-8 subprocess decoding; the metric/outcome documents
carry no timestamps or volatile data so they serialize byte-stably. Tunables
(injection budget, sampling cadence/share) are caller-supplied — nothing here
hardcodes one.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from agent_families.pipeline.planning import file_ownership_conflicts, plan_report
from agent_families.pipeline.runmemory import compose_injection
from agent_families.pipeline.workspace import Workspace
from agent_families.store import Store

logger = logging.getLogger(__name__)

# The rebuild-probe episode mode (R6): a fresh-workspace, full-app, one-delivery
# pass — the ONLY way episode-one-shot is structurally measurable. It is a
# rehearsal-domain mode tracked here, not a value of the store's episodes.mode
# enum (training | trial | benchmark), which the schema units own.
REBUILD_PROBE_MODE = "rebuild_probe"

# The typed §7 failure kind for the rehearsal integration class (R5). Persisted
# in the meta channel, not the failure_records enum (see module docstring).
REHEARSAL_INTEGRATION_FAILURE_KIND = "rehearsal_integration"

# The two failure classes a non-adopted rehearsal records. The headline class is
# `passed_alone_broke_together` (every ticket passed its own one-shot but the
# integrated artifact broke) — the integration knowledge sequential building
# masks; `ticket_one_shot_failed` is the simpler case (a ticket could not be
# delivered in one shot).
FAILURE_CLASS_INTEGRATION = "passed_alone_broke_together"
FAILURE_CLASS_TICKET = "ticket_one_shot_failed"


class RehearsalError(Exception):
    """Rehearsal misuse or invariant breach with an actionable message."""


# --- the ticket DAG and wave scheduling (R5/R8) ------------------------------


@dataclass(frozen=True)
class RehearsalTicket:
    """One rehearsal ticket: its id, the tickets it depends on, the files it
    owns. The DAG defines the waves; the files define the serialization."""

    ticket_id: str
    depends_on: tuple[str, ...]
    files: tuple[str, ...]


def tickets_from_plan(document: dict) -> tuple[RehearsalTicket, ...]:
    """Project a persisted plan document's tickets onto the rehearsal DAG."""
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


def compute_waves(
    tickets: Sequence[RehearsalTicket],
) -> tuple[tuple[str, ...], ...]:
    """The rehearsal schedule: topological DAG layers, then file-ownership
    serialization within each layer (R5/R8).

    A *layer* is the set of tickets at the same longest-path depth from the
    roots — all mutually independent (an A→B edge forces ``depth(B) > depth(A)``,
    so same-layer tickets never depend on one another). Within a layer, tickets
    that claim a common file are split into separate waves by deterministic
    greedy coloring (sorted by ticket id): the file-ownership lint promoted to
    enforcement. Two tickets sharing a file therefore never rehearse
    concurrently, so their fan-out merges can never conflict. Returns waves in
    execution order; the concatenation is a valid topological order.
    """
    by_id = {t.ticket_id: t for t in tickets}
    if len(by_id) != len(tickets):
        raise RehearsalError("rehearsal ticket set has duplicate ticket ids")
    deps: dict[str, set[str]] = {}
    for t in tickets:
        d = set(t.depends_on)
        for dep in d:
            if dep == t.ticket_id:
                raise RehearsalError(f"ticket {t.ticket_id} depends on itself")
            if dep not in by_id:
                raise RehearsalError(
                    f"ticket {t.ticket_id} depends on unknown ticket {dep!r}"
                )
        deps[t.ticket_id] = d

    dependents: dict[str, list[str]] = defaultdict(list)
    for tid, d in deps.items():
        for dep in d:
            dependents[dep].append(tid)

    # Longest-path layering (Kahn level by level, deterministic).
    indeg = {tid: len(deps[tid]) for tid in by_id}
    level = {tid: 0 for tid in by_id}
    ready = [tid for tid in by_id if indeg[tid] == 0]
    seen = 0
    while ready:
        nxt: list[str] = []
        for tid in sorted(ready):
            seen += 1
            for child in dependents.get(tid, ()):
                level[child] = max(level[child], level[tid] + 1)
                indeg[child] -= 1
                if indeg[child] == 0:
                    nxt.append(child)
        ready = nxt
    if seen != len(by_id):
        cyclic = sorted(tid for tid in by_id if indeg[tid] > 0)
        raise RehearsalError(
            f"rehearsal DAG has a dependency cycle among: {', '.join(cyclic)}"
        )

    layers: dict[int, list[str]] = defaultdict(list)
    for tid, lvl in level.items():
        layers[lvl].append(tid)

    # The promoted file-ownership lint (planning.file_ownership_conflicts, 005
    # R8): only contested files can serialize a wave — a file with a single owner
    # never forces a split. Restricting the conflict set to contested files is
    # the enforcement consumer the Phase 1 warn lint never had.
    contested = set(
        file_ownership_conflicts({t.ticket_id: t.files for t in tickets})
    )

    waves: list[tuple[str, ...]] = []
    for lvl in sorted(layers):
        members = sorted(layers[lvl])
        # Greedy graph-coloring on the "shares a contested file" conflict graph:
        # place each ticket in the first sub-wave that holds no file-conflicting
        # sibling.
        subwaves: list[tuple[set[str], list[str]]] = []
        for tid in members:
            files = set(by_id[tid].files) & contested
            placed = False
            for used_files, bucket in subwaves:
                if files.isdisjoint(used_files):
                    used_files.update(files)
                    bucket.append(tid)
                    placed = True
                    break
            if not placed:
                subwaves.append((set(files), [tid]))
        for _used, bucket in subwaves:
            waves.append(tuple(sorted(bucket)))
    return tuple(waves)


# --- stage seams (fake-driven offline; real bindings land in U5/U7) ----------


@dataclass(frozen=True)
class RehearsalTicketContext:
    """What a one-shot ticket session sees: the ticket, its isolated worktree,
    and the run-memory injection it consumes (R5)."""

    store: Store
    episode_id: int
    run_id: int
    ticket: RehearsalTicket
    workspace: Workspace
    injection_section: str
    injection_workflow_ids: tuple[int, ...]


@dataclass(frozen=True)
class IntegrationContext:
    """What the integration gate and verifier see: the merged fan-out artifact."""

    store: Store
    episode_id: int
    run_id: int
    workspace: Workspace
    ticket_ids: tuple[str, ...]


@dataclass(frozen=True)
class CheckResult:
    """A one-shot ticket session's or integration check's verdict."""

    passed: bool
    detail: str = ""


OneShotFn = Callable[[RehearsalTicketContext], CheckResult]
IntegrationCheckFn = Callable[[IntegrationContext], CheckResult]


class WorktreeProvider(Protocol):
    """Provisions a fan-out worktree at ``base_ref`` and tears it down.

    The default :class:`GitWorktreeProvider` cuts a lightweight ``git worktree``
    (enough for the offline suite's fake sessions). The live provider copies the
    primed template and checks the increment-base tree out over it — a bare
    worktree has no ``node_modules`` (the Phase 1 copy-installed-template
    pattern, per-worktree; a shared read-only junction-linked ``node_modules`` is
    the tested Windows alternative). Per-worktree setup time/disk feeds R7's
    crossover cost model.
    """

    def provision(self, label: str, base_ref: str) -> Workspace: ...

    def teardown(self, ws: Workspace) -> None: ...


@dataclass(frozen=True)
class RehearsalStages:
    """The injected rehearsal stage set."""

    oneshot_fn: OneShotFn
    gate_fn: IntegrationCheckFn
    verify_fn: IntegrationCheckFn


# --- config ------------------------------------------------------------------


@dataclass(frozen=True)
class RehearsalConfig:
    """Rehearsal plumbing. ``injection_budget_tokens`` is the standard run-memory
    injection budget (Plan 4 R3) the one-shot sessions get — caller-supplied
    (thresholds-routed), never hardcoded. The branch/worktree prefixes are
    protocol names, not tunables."""

    injection_budget_tokens: int
    branch_prefix: str = "af-rh"

    def __post_init__(self) -> None:
        if self.injection_budget_tokens < 1:
            raise RehearsalError(
                "injection_budget_tokens must be a positive token budget, got"
                f" {self.injection_budget_tokens}"
            )
        if not self.branch_prefix.strip():
            raise RehearsalError("branch_prefix must be non-empty")


# --- the one-shot metric (R6) ------------------------------------------------


@dataclass(frozen=True)
class OneShotMetric:
    """The autonomy instrument, keyed ``(target, epoch, snapshot)`` (R6).

    ``episode_one_shot`` is ``None`` outside rebuild-probe episodes — ordinary
    episodes structurally cannot measure it (the persistent clone is never
    rebuilt from scratch).
    """

    target: str
    epoch: int | None
    snapshot_id: int
    mode: str
    rehearsal_tickets: int
    tickets_passed: int
    ticket_one_shot_rate: float
    increment_one_shot: bool
    episode_one_shot: bool | None = None

    def as_dict(self) -> dict:
        return {
            "episode_one_shot": self.episode_one_shot,
            "epoch": self.epoch,
            "increment_one_shot": self.increment_one_shot,
            "mode": self.mode,
            "rehearsal_tickets": self.rehearsal_tickets,
            "snapshot_id": self.snapshot_id,
            "target": self.target,
            "ticket_one_shot_rate": self.ticket_one_shot_rate,
            "tickets_passed": self.tickets_passed,
        }


def _compute_metric(
    *,
    target: str,
    epoch: int | None,
    snapshot_id: int,
    mode: str,
    ticket_results: dict[str, bool],
    increment_one_shot: bool,
    episode_one_shot: bool | None,
) -> OneShotMetric:
    total = len(ticket_results)
    passed = sum(1 for ok in ticket_results.values() if ok)
    rate = (passed / total) if total else 0.0
    return OneShotMetric(
        target=target,
        epoch=epoch,
        snapshot_id=snapshot_id,
        mode=mode,
        rehearsal_tickets=total,
        tickets_passed=passed,
        ticket_one_shot_rate=rate,
        increment_one_shot=increment_one_shot,
        episode_one_shot=episode_one_shot,
    )


# --- the rehearsal outcome and its persistence -------------------------------


@dataclass(frozen=True)
class RehearsalOutcome:
    """One rehearsal's full yield."""

    adopted: bool
    metric: OneShotMetric
    waves: tuple[tuple[str, ...], ...]
    ticket_results: dict[str, bool]
    base_ref: str
    converged_ref: str
    adopted_ref: str | None
    integration_failure: dict | None
    detail: str = ""

    def as_dict(self) -> dict:
        return {
            "adopted": self.adopted,
            "adopted_ref": self.adopted_ref,
            "base_ref": self.base_ref,
            "converged_ref": self.converged_ref,
            "detail": self.detail,
            "integration_failure": self.integration_failure,
            "metric": self.metric.as_dict(),
            "ticket_results": dict(self.ticket_results),
            "waves": [list(w) for w in self.waves],
        }


def rehearsal_meta_key(run_id: int) -> str:
    """The ``meta`` key of a run's rehearsal outcome document."""
    return f"rehearsal:run:{int(run_id)}"


def store_rehearsal_outcome(
    store: Store, run_id: int, outcome: RehearsalOutcome
) -> None:
    """Persist the rehearsal outcome (metric + any integration-failure record)
    in the meta channel keyed per run."""
    store.set_meta(
        rehearsal_meta_key(run_id),
        json.dumps(outcome.as_dict(), sort_keys=True, ensure_ascii=False),
    )


def load_rehearsal_outcome(store: Store, run_id: int) -> dict | None:
    """Read back a run's persisted rehearsal outcome, or ``None`` if it never
    rehearsed (sampled out, or a non-converged increment)."""
    raw = store.get_meta(rehearsal_meta_key(run_id))
    return json.loads(raw) if raw is not None else None


def _integration_failure_record(
    *,
    failure_class: str,
    base_ref: str,
    ticket_results: dict[str, bool],
    location: str,
    observed: str,
    repro_command: str,
) -> dict:
    """A §7-shaped typed failure record for a non-adopted rehearsal (R5).

    ``failure_kind`` is :data:`REHEARSAL_INTEGRATION_FAILURE_KIND`;
    ``failure_class`` separates the headline *passed-alone-broke-together* case
    from a plain one-shot miss. The record is the reflector's first
    integration-lesson source.
    """
    failed = sorted(t for t, ok in ticket_results.items() if not ok)
    return {
        "base_ref": base_ref,
        "expected": "the fan-out artifact passes the integration gate and"
        " verify on the first try",
        "failed_tickets": failed,
        "failure_class": failure_class,
        "failure_kind": REHEARSAL_INTEGRATION_FAILURE_KIND,
        "location": location,
        "observed": observed,
        "repro_command": repro_command,
    }


# --- git plumbing (private; the default worktree provider) -------------------


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in ``root`` with utf-8 capture; raise on failure when ``check``."""
    proc = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if check and proc.returncode != 0:
        raise RehearsalError(
            f"git {' '.join(args)} failed in {root}"
            f" (exit {proc.returncode}): {proc.stderr.strip()}"
        )
    return proc


def current_head(workspace: Workspace) -> str:
    """The workspace's current HEAD commit sha (the increment-base / converged
    ref capture point)."""
    return _git(workspace.root, "rev-parse", "HEAD").stdout.strip()


def _repro(branch: str) -> str:
    return subprocess.list2cmdline(["git", "merge", "--no-ff", branch])


@dataclass
class GitWorktreeProvider:
    """The default worktree provider: a lightweight ``git worktree`` cut from
    the base ref onto a fresh branch under ``work_dir``.

    Enough for the offline suite (fake one-shot sessions need no ``node_modules``)
    and for the real provider's git mechanics; the live template-copy variant
    overlays the increment-base tree on a primed template (module docstring).
    Worktrees and their branches are tracked for teardown.
    """

    workspace: Workspace
    work_dir: Path
    branch_prefix: str
    _provisioned: list[tuple[str, Path]] = field(default_factory=list)

    def provision(self, label: str, base_ref: str) -> Workspace:
        branch = f"{self.branch_prefix}-{label}"
        dest = self.work_dir / label
        if dest.exists():
            raise RehearsalError(
                f"rehearsal worktree destination already exists: {dest}"
            )
        _git(
            self.workspace.root,
            "worktree",
            "add",
            "-b",
            branch,
            str(dest),
            base_ref,
        )
        self._provisioned.append((branch, dest))
        return Workspace(root=dest)

    def teardown(self, ws: Workspace) -> None:
        _git(
            self.workspace.root,
            "worktree",
            "remove",
            "--force",
            str(ws.root),
            check=False,
        )

    def teardown_all(self) -> None:
        """Best-effort removal of every provisioned worktree and its branch."""
        for branch, dest in self._provisioned:
            _git(
                self.workspace.root,
                "worktree",
                "remove",
                "--force",
                str(dest),
                check=False,
            )
            if dest.exists():
                shutil.rmtree(dest, ignore_errors=True)
            _git(self.workspace.root, "branch", "-D", branch, check=False)
        _git(self.workspace.root, "worktree", "prune", check=False)
        self._provisioned.clear()


# --- the rehearsal driver (R5/R6) --------------------------------------------


def run_rehearsal(
    store: Store,
    *,
    episode_id: int,
    run_id: int,
    workspace: Workspace,
    increment_base_ref: str,
    stages: RehearsalStages,
    config: RehearsalConfig,
    work_dir: Path | str,
    provider: GitWorktreeProvider | None = None,
    mode: str = "rehearsal",
    episode_one_shot: bool | None = None,
) -> RehearsalOutcome:
    """Run one increment's rehearsal pass: fan-out → merge → gate → verify →
    adopt-or-fallback, with the one-shot metric (R5/R6).

    ``workspace`` is the engagement workspace at the **converged** artifact
    (HEAD). ``increment_base_ref`` is the orchestrator-minted ref the increment
    started from. On adoption the workspace HEAD moves to the fan-out artifact;
    on any failure the workspace is left exactly at the converged artifact
    (fallback) and a typed integration-failure record is written. The outcome
    (metric + record) is persisted in the meta channel.
    """
    episode = store.get_episode(episode_id)
    if episode is None:
        raise RehearsalError(f"episode {episode_id} does not exist")
    run = store.get_run(run_id)
    if run is None:
        raise RehearsalError(f"run {run_id} does not exist")

    converged_ref = current_head(workspace)
    tickets = tickets_from_plan(plan_report(store, run_id))
    if not tickets:
        raise RehearsalError(
            f"run {run_id} has no tickets to rehearse — convergence produced an"
            " empty plan"
        )
    waves = compute_waves(tickets)
    by_id = {t.ticket_id: t for t in tickets}

    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    owned = provider is None
    provider = provider or GitWorktreeProvider(
        workspace=workspace,
        work_dir=work_dir,
        branch_prefix=f"{config.branch_prefix}-{run_id}",
    )

    ticket_results: dict[str, bool] = {}
    branch_of: dict[str, str] = {}
    try:
        # Fan-out: one shot per ticket, in its own worktree from the base ref,
        # consuming the episode's run-memory workflows (R5).
        for wave in waves:
            for tid in wave:
                ws = provider.provision(tid, increment_base_ref)
                injection = compose_injection(
                    store,
                    episode_id=episode_id,
                    budget_tokens=config.injection_budget_tokens,
                )
                ctx = RehearsalTicketContext(
                    store=store,
                    episode_id=episode_id,
                    run_id=run_id,
                    ticket=by_id[tid],
                    workspace=ws,
                    injection_section=injection.section,
                    injection_workflow_ids=injection.workflow_ids,
                )
                result = stages.oneshot_fn(ctx)
                _git(ws.root, "add", "-A")
                _git(
                    ws.root,
                    "commit",
                    "--allow-empty",
                    "-m",
                    f"af rehearsal: ticket={tid} (one-shot)",
                )
                branch_of[tid] = f"{provider.branch_prefix}-{tid}"
                ticket_results[tid] = bool(result.passed)

        all_tickets_passed = all(ticket_results.values())

        # Integration: merge in wave order onto a branch from the base ref, then
        # the harness gate + one integration verify (R5).
        ordered = [tid for wave in waves for tid in wave]
        int_ws = provider.provision("integration", increment_base_ref)
        merge_conflict_on: str | None = None
        for tid in ordered:
            merge = _git(
                int_ws.root,
                "merge",
                "--no-edit",
                "--no-ff",
                branch_of[tid],
                check=False,
            )
            if merge.returncode != 0:
                _git(int_ws.root, "merge", "--abort", check=False)
                merge_conflict_on = tid
                break
        integration_sha = current_head(int_ws)

        int_ctx = IntegrationContext(
            store=store,
            episode_id=episode_id,
            run_id=run_id,
            workspace=int_ws,
            ticket_ids=tuple(ordered),
        )
        gate = (
            stages.gate_fn(int_ctx)
            if merge_conflict_on is None
            else CheckResult(False, f"merge conflict on {merge_conflict_on}")
        )
        verify = (
            stages.verify_fn(int_ctx)
            if gate.passed
            else CheckResult(False, "skipped: gate did not pass")
        )

        integration_ok = (
            merge_conflict_on is None and gate.passed and verify.passed
        )
        adopt = all_tickets_passed and integration_ok
        increment_one_shot = adopt

        integration_failure: dict | None = None
        adopted_ref: str | None = None
        if adopt:
            # Move the engagement workspace HEAD to the fan-out artifact. The
            # integration sha is captured before teardown deletes the branch, so
            # the reset target survives.
            _git(workspace.root, "reset", "--hard", integration_sha)
            adopted_ref = current_head(workspace)
            detail = (
                f"adopted fan-out artifact {adopted_ref[:12]} over converged"
                f" {converged_ref[:12]}"
            )
            logger.info(
                "rehearsal run %d adopted fan-out artifact (%d/%d tickets"
                " one-shot)",
                run_id,
                len(ticket_results),
                len(ticket_results),
            )
        else:
            # Fallback: the converged artifact stands; the workspace is
            # untouched. Record the typed integration-failure class (R5).
            if all_tickets_passed:
                failure_class = FAILURE_CLASS_INTEGRATION
                if merge_conflict_on is not None:
                    location = f"merge {branch_of[merge_conflict_on]}"
                    observed = f"merge conflict integrating {merge_conflict_on}"
                    repro = _repro(branch_of[merge_conflict_on])
                elif not gate.passed:
                    location = "integration gate"
                    observed = f"gate failed: {gate.detail}"
                    repro = "af rehearsal gate"
                else:
                    location = "integration verify"
                    observed = f"verify failed: {verify.detail}"
                    repro = "af rehearsal verify"
            else:
                failure_class = FAILURE_CLASS_TICKET
                failed = sorted(
                    t for t, ok in ticket_results.items() if not ok
                )
                location = f"tickets {', '.join(failed)}"
                observed = (
                    f"{len(failed)} ticket(s) failed their one-shot session:"
                    f" {', '.join(failed)}"
                )
                repro = "af rehearsal fanout"
            integration_failure = _integration_failure_record(
                failure_class=failure_class,
                base_ref=increment_base_ref,
                ticket_results=ticket_results,
                location=location,
                observed=observed,
                repro_command=repro,
            )
            detail = (
                f"fell back to converged artifact {converged_ref[:12]}"
                f" ({failure_class})"
            )
            logger.warning(
                "rehearsal run %d fell back (%s): %s",
                run_id,
                failure_class,
                observed,
            )
    finally:
        if owned:
            provider.teardown_all()

    metric = _compute_metric(
        target=episode["target"],
        epoch=episode["epoch"],
        snapshot_id=int(run["snapshot_id"]),
        mode=mode,
        ticket_results=ticket_results,
        increment_one_shot=increment_one_shot,
        episode_one_shot=episode_one_shot,
    )
    outcome = RehearsalOutcome(
        adopted=adopt,
        metric=metric,
        waves=waves,
        ticket_results=ticket_results,
        base_ref=increment_base_ref,
        converged_ref=converged_ref,
        adopted_ref=adopted_ref,
        integration_failure=integration_failure,
        detail=detail,
    )
    store_rehearsal_outcome(store, run_id, outcome)
    return outcome


# --- settlement-report integration (R6 verification seam) --------------------


def episode_one_shot_metrics(store: Store, episode_id: int) -> list[dict]:
    """Every rehearsed increment's metric for an episode, in increment order."""
    rows = store.conn.execute(
        "SELECT id FROM runs WHERE episode_id = ?"
        " ORDER BY increment_index, id",
        (episode_id,),
    ).fetchall()
    metrics: list[dict] = []
    for row in rows:
        doc = load_rehearsal_outcome(store, row["id"])
        if doc is not None:
            metrics.append(doc["metric"])
    return metrics


def attach_one_shot_metrics(
    report: dict, store: Store, episode_id: int
) -> dict:
    """Fold the episode's one-shot metric into a settlement report dict (R6).

    The plan-003 U7 report renderer is closed to this unit; this is the seam the
    run-assembly calls so the metric *appears in settlement reports* alongside
    the rubric score. Pure: returns a new dict with a ``one_shot`` section
    aggregating every rehearsed increment (ticket one-shot rate over all
    rehearsal tickets, increment one-shot rate over rehearsed increments). When
    no increment rehearsed, the section records that explicitly.
    """
    metrics = episode_one_shot_metrics(store, episode_id)
    out = dict(report)
    if not metrics:
        out["one_shot"] = {
            "increments": [],
            "note": "no increment rehearsed this episode (sampled out or no"
            " converged increment)",
            "rehearsed_increments": 0,
        }
        return out
    total_tickets = sum(m["rehearsal_tickets"] for m in metrics)
    passed_tickets = sum(m["tickets_passed"] for m in metrics)
    increment_one_shots = sum(1 for m in metrics if m["increment_one_shot"])
    episode_probe = [
        m["episode_one_shot"]
        for m in metrics
        if m["episode_one_shot"] is not None
    ]
    out["one_shot"] = {
        "episode_one_shot": (
            all(episode_probe) if episode_probe else None
        ),
        "increment_one_shot_rate": increment_one_shots / len(metrics),
        "increments": metrics,
        "rehearsed_increments": len(metrics),
        "ticket_one_shot_rate": (
            passed_tickets / total_tickets if total_tickets else 0.0
        ),
    }
    return out


# --- rehearsal economics: sampling (R7) --------------------------------------


@dataclass(frozen=True)
class SamplingDecision:
    """Whether an increment rehearses, and why."""

    rehearse: bool
    reason: str


@dataclass(frozen=True)
class RehearsalSamplingPolicy:
    """The R7 sampling policy: rehearse every increment for the first episodes
    (the metric baseline + the reflector's only integration-lesson source), then
    sample so rehearsal spend stays under ``max_budget_share`` of the episode
    budget. All three values are caller-supplied tunables (thresholds-routed)."""

    baseline_increments: int
    sample_every: int
    max_budget_share: float

    def __post_init__(self) -> None:
        if self.baseline_increments < 0:
            raise RehearsalError(
                f"baseline_increments must be >= 0, got {self.baseline_increments}"
            )
        if self.sample_every < 1:
            raise RehearsalError(
                f"sample_every must be >= 1, got {self.sample_every}"
            )
        if not 0.0 < self.max_budget_share <= 1.0:
            raise RehearsalError(
                "max_budget_share must be in (0, 1] — the §17 rehearsal spend"
                f" cap (~0.15-0.20), got {self.max_budget_share}"
            )

    def should_rehearse(
        self,
        increment_index: int,
        *,
        rehearsal_spend_usd: float,
        episode_budget_usd: float,
    ) -> SamplingDecision:
        """Decide whether increment ``increment_index`` (1-based) rehearses.

        The budget-share cap is checked first and dominates: once rehearsal spend
        has reached ``max_budget_share`` of the episode budget, no further
        increment rehearses regardless of cadence. Within budget, the first
        ``baseline_increments`` always rehearse (the metric baseline); after the
        baseline, only every ``sample_every``-th increment does.
        """
        if increment_index < 1:
            raise RehearsalError(
                f"increment_index is 1-based, got {increment_index}"
            )
        if episode_budget_usd <= 0:
            raise RehearsalError(
                f"episode_budget_usd must be positive, got {episode_budget_usd}"
            )
        cap = self.max_budget_share * episode_budget_usd
        if rehearsal_spend_usd >= cap:
            return SamplingDecision(
                False,
                f"budget: rehearsal spend {rehearsal_spend_usd:.4f} >= share cap"
                f" {cap:.4f} ({self.max_budget_share:.0%} of"
                f" {episode_budget_usd:.4f})",
            )
        if increment_index <= self.baseline_increments:
            return SamplingDecision(
                True,
                f"baseline: increment {increment_index} <="
                f" {self.baseline_increments}",
            )
        if increment_index % self.sample_every == 0:
            return SamplingDecision(
                True, f"sample: increment {increment_index} hits every"
                f" {self.sample_every}"
            )
        return SamplingDecision(
            False,
            f"sample: increment {increment_index} not on the every-"
            f"{self.sample_every} cadence",
        )


# --- rehearsal economics: the cost-model crossover (R7) ----------------------


@dataclass(frozen=True)
class RegimeCost:
    """One execution regime's measured cost."""

    name: str
    total_cost_usd: float

    def __post_init__(self) -> None:
        if self.total_cost_usd < 0:
            raise RehearsalError(
                f"regime {self.name!r} cost must be >= 0, got"
                f" {self.total_cost_usd}"
            )


# The two regimes the crossover compares (DESIGN §11 graduation).
REGIME_CONVERGE_FIRST = "converge_first"
REGIME_FANOUT_FIRST = "fanout_first"


def crossover_report(
    converge_first: RegimeCost,
    fanout_first: RegimeCost,
    *,
    current_regime: str = REGIME_CONVERGE_FIRST,
    ticket_one_shot_rate: float | None = None,
) -> dict:
    """Compare both regimes' measured costs and surface the crossover (R7).

    Early training the converge-first regime (converge, then rehearse — additive
    one-shot executions) is cheaper; as ticket one-shot rate climbs, fan-out-
    first (one-shot everything, drop into sequential repair only for the residue)
    crosses under it. The report names the cheaper regime and whether the flip is
    recommended; the flip itself is a config promotion (not a rule of thumb), so
    this only *surfaces* the crossover for the report.
    """
    if current_regime not in (REGIME_CONVERGE_FIRST, REGIME_FANOUT_FIRST):
        raise RehearsalError(
            f"current_regime must be one of {REGIME_CONVERGE_FIRST!r} /"
            f" {REGIME_FANOUT_FIRST!r}, got {current_regime!r}"
        )
    cheaper = (
        REGIME_FANOUT_FIRST
        if fanout_first.total_cost_usd < converge_first.total_cost_usd
        else REGIME_CONVERGE_FIRST
    )
    crossover_reached = (
        fanout_first.total_cost_usd < converge_first.total_cost_usd
    )
    return {
        "converge_first_usd": converge_first.total_cost_usd,
        "crossover_reached": crossover_reached,
        "current_regime": current_regime,
        "delta_usd": fanout_first.total_cost_usd
        - converge_first.total_cost_usd,
        "fanout_first_usd": fanout_first.total_cost_usd,
        "flip_recommended": cheaper != current_regime,
        "recommended_regime": cheaper,
        "ticket_one_shot_rate": ticket_one_shot_rate,
    }


# --- rebuild-probe promotion: the clone-rot remedy (R6) ----------------------


def should_promote_probe(
    probe_must_score: float, engagement_must_revisit_score: float
) -> bool:
    """Whether a rebuild-probe artifact should be promoted to the engagement
    clone (R6): when the probe's settlement score on the **must** tier matches or
    beats the engagement clone's latest revisit score on the must tier, the
    cleaner one-shot rebuild replaces the accumulation (a workspace-pointer swap,
    recorded as an engagement event — the swap is the scheduler's job). Detection
    and remediation are the same instrument: rising one-shot capability
    continuously refreshes the codebase, and a declining code-structure score on
    the persistent clone is the corroborating rot signal.
    """
    return probe_must_score >= engagement_must_revisit_score
