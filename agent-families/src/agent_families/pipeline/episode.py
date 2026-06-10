"""Episode orchestration: the delivery loop over Phase 1 runs (plan-003 U6).

Implements the behavioral halves of 003 R1-R4 — the episode state machine that
every later phase inherits (the plan's hardest-to-change artifact):

- **R1** — an increment is exactly one Phase 1 run: the loop hands each
  increment to the run machinery and *enforces* (not trusts) that the returned
  run row carries ``episode_id`` + ``increment_index``. Workspace ownership
  belongs to the target engagement: created at the target's first episode
  (:func:`~agent_families.pipeline.workspace.ensure_engagement_workspace`),
  persisting across increments AND episodes, retained after settlement.
- **R2** — the acceptance stage: after a run settles, explorer UAT judges the
  *delivered subset only*. The UAT briefing is rendered HERE
  (:func:`render_uat_briefing`) as the explorer's own request MSGs joined to
  FEAT mentions, filtered to ``done`` tickets — escalated/blocked tickets are
  excluded (they carry into the next increment's plan automatically and are
  never re-discovered via UAT; no double-counting). Rejection feedback MSG ids
  become the next increment's planner carry-in (003 R11 →
  ``planning.run_planning(carry_in_msg_ids=...)``). A ``plan_failed``
  increment halts the episode (terminal ``aborted_error``) and escalates to
  the human via a ``review_queue`` row.
- **R3** — terminals ``frontier_exhausted | budget_spent | aborted_error``
  plus the resumable non-terminal ``suspended``: mid-increment quota
  exhaustion rides Phase 1's checkpoint (run terminal ``aborted_quota``) and
  suspends the episode — it NEVER counts as ``budget_spent``;
  :func:`resume_episode` re-enters mid-episode at the recorded phase.
- **R4** — budget = max-increments cap AND a cost ceiling (fed by Phase 1
  R16's per-run aggregation, :func:`episode_cost`), checked at increment
  boundaries. Both values come from the thresholds config and are stamped on
  the episode row at creation (``Store.create_episode``) so the governing
  values are queryable forever; per the established seam precedent they are
  caller-supplied here via :class:`EpisodeConfig` — nothing in this module
  hardcodes a tunable.

Stage seams: the explorer request, the increment (a Phase 1 run), increment
resume, UAT, target reset, and settlement are injected callables
(:class:`EpisodeStages`) — U9's run assembly binds the real ones
(``explorer.author_opening_prompt`` / ``Orchestrator.run`` with episode FKs /
``Orchestrator.resume`` / ``explorer.run_uat`` /
``LinkdingTarget.reset_to_seed`` / ``settle.run_settlement``); U6 tests drive
scripted fakes, exactly per the plan's approach. The settlement seam's
contract: it MUST reset the target to seed before executing the rubric (R6's
settlement-start reset) — ``settle.run_settlement`` does this through its
``reset_target`` hook; pass it ``ctx.reset_target``. The episode-start reset
(R6's other half) is this module's own call. The R10 mention-coverage audit
runs at every settlement so never-mentioned FEATs are force-scheduled into
the next episode's opening slice.

Episode setup order (state diagram, HTD): digest check (R9 — the apparatus is
keyed by image digest; a mismatch is a hard setup error), target reset to
seed, workspace create-or-load, then the loop. Durable state is the episodes
row (status authoritative) plus one checkpoint document per episode in
``meta`` under :func:`episode_checkpoint_key` — the same discipline as the
Phase 1 run checkpoint. Resume granularity is the phase boundary
(request | increment | uat): a suspension inside the increment records the
suspended run id and resumes it via Phase 1's own checkpoint; a suspension in
an explorer session (request/UAT) re-enters that phase fresh.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from agent_families.grading.frontier import (
    frontier_row,
    mention_coverage_audit,
    record_investigation,
    select_slice,
)
from agent_families.pipeline.orchestrator import RunResult
from agent_families.pipeline.planning import plan_report
from agent_families.pipeline.sessions import SessionQuotaExhausted
from agent_families.pipeline.workspace import (
    Workspace,
    ensure_engagement_workspace,
    load_workspace,
)
from agent_families.store import (
    EPISODE_TERMINAL_STATUSES,
    RUN_ACCEPTANCE,
    Store,
)

logger = logging.getLogger(__name__)

# The per-increment phases a checkpointed episode can re-enter at.
EPISODE_PHASES = ("request", "increment", "uat")

# review_queue kind for the R2 human-escalation record a plan_failed
# increment writes before the episode halts.
PLAN_FAILED_REVIEW_KIND = "plan_failed"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class EpisodeError(Exception):
    """Episode-loop misuse or invariant violation, with an actionable message."""


def episode_checkpoint_key(episode_id: int) -> str:
    """The ``meta`` key of an episode's checkpoint document (R3 substrate)."""
    return f"pipeline:episode:{int(episode_id)}:checkpoint"


# --- configuration (thresholds-routed by the caller, R4) --------------------------


@dataclass(frozen=True)
class EpisodeConfig:
    """One episode's governing values.

    ``max_increments`` / ``cost_ceiling_usd`` / ``slice_size`` are behavior
    tunables routed from ``thresholds.toml`` by the caller (R4/R10 — the U9
    run assembly); they are stamped onto the episode row at creation so the
    values that governed the episode stay queryable. ``workspace_dir`` is the
    target engagement's persistent workspace path (R1): created from the
    pinned template at the first episode, loaded untouched ever after.
    """

    target: str
    digest: str
    workspace_dir: Path | str
    max_increments: int
    cost_ceiling_usd: float
    slice_size: int
    template_dir: Path | str | None = None
    lock_path: Path | str | None = None

    def __post_init__(self) -> None:
        if not self.target.strip():
            raise EpisodeError("EpisodeConfig.target must be non-empty")
        if not self.digest.strip():
            raise EpisodeError(
                "EpisodeConfig.digest must be the pinned target image digest"
                " (R9: the apparatus is keyed by it)"
            )
        if self.max_increments < 1:
            raise EpisodeError(
                f"max_increments must be a positive increment cap (R4), got"
                f" {self.max_increments}"
            )
        if self.cost_ceiling_usd <= 0:
            raise EpisodeError(
                f"cost_ceiling_usd must be a positive budget (R4), got"
                f" {self.cost_ceiling_usd}"
            )
        if self.slice_size < 1:
            raise EpisodeError(
                f"slice_size must be a positive slice size (R10), got"
                f" {self.slice_size}"
            )


# --- stage contracts ----------------------------------------------------------------


@dataclass(frozen=True)
class EpisodeContext:
    """What every injected stage sees."""

    store: Store
    episode_id: int
    target: str
    digest: str
    workspace: Workspace
    reset_target: Callable[[], None]


@dataclass(frozen=True)
class IncrementContext:
    """One increment's inputs: the explorer's request plus both carry-ins.

    ``carry_in_msg_ids`` are the previous increment's UAT feedback MSG rows —
    the next planner extracts bug REQs from them (003 R11; feed them to
    ``planning.run_planning(carry_in_msg_ids=...)``). ``carry_in_ticket_ids``
    are the previous increment's escalated tickets, carried into this plan
    automatically and never re-discovered via UAT (R2).
    """

    store: Store
    episode_id: int
    increment_index: int
    workspace: Workspace
    request_msg_id: str
    request_text: str
    slice_feat_ids: tuple[str, ...]
    carry_in_msg_ids: tuple[str, ...] = ()
    carry_in_ticket_ids: tuple[str, ...] = ()


class OpeningRequestLike(Protocol):
    """What the request stage returns (``explorer.OpeningPrompt`` satisfies it)."""

    msg_id: str
    text: str


class UatResultLike(Protocol):
    """What the UAT stage returns (``explorer.UATResult`` satisfies it)."""

    verdict: str
    feedback_msg_ids: tuple[str, ...]


RequestFn = Callable[[EpisodeContext, int, tuple[str, ...]], OpeningRequestLike]
IncrementFn = Callable[[IncrementContext], RunResult]
ResumeIncrementFn = Callable[[IncrementContext, int], RunResult]
UatFn = Callable[[EpisodeContext, int, int, str], UatResultLike]
SettleFn = Callable[[EpisodeContext], object]


@dataclass(frozen=True)
class EpisodeStages:
    """The injected stage set (real bindings land in U9's run assembly).

    ``settle_fn`` contract: reset the target to seed BEFORE the rubric (R6's
    settlement-start reset — ``settle.run_settlement`` does it through its
    ``reset_target`` hook; hand it ``ctx.reset_target``). ``uat_fn`` receives
    ``(ctx, increment_index, run_id, briefing)`` and judges ONLY the briefing
    (the delivered subset, R2). ``resume_increment_fn`` re-enters a
    quota-suspended run (Phase 1 checkpoint) and may be omitted when no
    suspension is expected.
    """

    request_fn: RequestFn
    increment_fn: IncrementFn
    uat_fn: UatFn
    reset_target_fn: Callable[[], None]
    settle_fn: SettleFn
    resume_increment_fn: ResumeIncrementFn | None = None


@dataclass(frozen=True)
class EpisodeResult:
    """An episode's outcome: a terminal status or the resumable ``suspended``."""

    episode_id: int
    status: str
    increments_completed: int
    run_ids: tuple[int, ...]
    detail: str = ""


# --- the durable checkpoint document (R3) ---------------------------------------------


@dataclass
class _EpisodeState:
    """The episode checkpoint: everything resume needs beyond the row tables.

    Episode STATUS is authoritative in ``episodes``; this document carries the
    loop position (phase, active slice/request/run) and the two carry-ins the
    next increment consumes. ``slice_size`` travels here so resume needs no
    re-supplied config.
    """

    workspace_root: str
    slice_size: int
    increments_completed: int = 0
    run_ids: list[int] = field(default_factory=list)
    phase: str = "request"
    active_slice: tuple[str, ...] = ()
    active_msg_id: str | None = None
    active_msg_text: str = ""
    active_run_id: int | None = None
    carry_in_msg_ids: tuple[str, ...] = ()
    carry_in_ticket_ids: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "workspace_root": self.workspace_root,
                "slice_size": self.slice_size,
                "increments_completed": self.increments_completed,
                "run_ids": self.run_ids,
                "phase": self.phase,
                "active_slice": list(self.active_slice),
                "active_msg_id": self.active_msg_id,
                "active_msg_text": self.active_msg_text,
                "active_run_id": self.active_run_id,
                "carry_in_msg_ids": list(self.carry_in_msg_ids),
                "carry_in_ticket_ids": list(self.carry_in_ticket_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def from_json(cls, raw: str) -> _EpisodeState:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise EpisodeError(
                f"corrupt episode checkpoint document: {exc}"
            ) from exc
        state = cls(
            workspace_root=data["workspace_root"],
            slice_size=int(data["slice_size"]),
            increments_completed=int(data["increments_completed"]),
            run_ids=[int(r) for r in data["run_ids"]],
            phase=data["phase"],
            active_slice=tuple(data["active_slice"]),
            active_msg_id=data["active_msg_id"],
            active_msg_text=data["active_msg_text"],
            active_run_id=(
                int(data["active_run_id"])
                if data["active_run_id"] is not None
                else None
            ),
            carry_in_msg_ids=tuple(data["carry_in_msg_ids"]),
            carry_in_ticket_ids=tuple(data["carry_in_ticket_ids"]),
        )
        if state.phase not in EPISODE_PHASES:
            raise EpisodeError(
                f"episode checkpoint names unknown phase {state.phase!r}"
                f" (expected one of {EPISODE_PHASES})"
            )
        return state


# --- aggregation and joins (R4 / R2) ----------------------------------------------------


def episode_cost(store: Store, episode_id: int) -> float:
    """Aggregated cost across the episode's runs (R4's ceiling input, fed by
    Phase 1 R16's per-run aggregation at run settlement)."""
    row = store.conn.execute(
        "SELECT COALESCE(SUM(total_cost_usd), 0) AS cost FROM runs"
        " WHERE episode_id = ?",
        (episode_id,),
    ).fetchone()
    return float(row["cost"])


def _ticket_statuses_for_run(store: Store, run_id: int) -> dict[str, str]:
    """The run's plan tickets joined to their live trace_tkt statuses."""
    document = plan_report(store, run_id)
    out: dict[str, str] = {}
    for ticket in document["tickets"]:
        row = store.conn.execute(
            "SELECT status FROM trace_tkt WHERE id = ?", (ticket["id"],)
        ).fetchone()
        if row is None:
            raise EpisodeError(
                f"plan document for run {run_id} names ticket {ticket['id']}"
                " but trace_tkt has no such row (plan/store divergence)"
            )
        out[ticket["id"]] = row["status"]
    return out


def render_uat_briefing(store: Store, run_id: int) -> str:
    """The increment's delivered scope, rendered for UAT (R2/U6 approach).

    The explorer's own request MSGs (opening prompt + carried-in feedback)
    joined to their FEAT mentions, filtered to ``done`` tickets — escalated
    and blocked tickets are excluded (they carry into the next increment's
    plan and are never UAT-discovered; no double-counting).
    """
    document = plan_report(store, run_id)
    statuses = _ticket_statuses_for_run(store, run_id)
    requirements = {r["id"]: r for r in document["requirements"]}

    done = [t for t in document["tickets"] if statuses[t["id"]] == "done"]
    if not done:
        return (
            "(nothing delivered this increment: no ticket reached done —"
            " escalated/blocked work carries forward and is not UAT-judged)"
        )

    lines = ["## Delivered tickets (done only)"]
    delivered_msg_ids: list[str] = []
    for ticket in done:
        lines.append(
            f"- {ticket['id']}: {ticket['title']}"
            f" [{ticket.get('kind', 'feature')}]"
        )
        for req_id in ticket["covers"]:
            req = requirements.get(req_id)
            if req is None:
                raise EpisodeError(
                    f"ticket {ticket['id']} covers unknown requirement"
                    f" {req_id} in run {run_id}'s plan document"
                )
            if req["source_msg"] not in delivered_msg_ids:
                delivered_msg_ids.append(req["source_msg"])

    lines.append("")
    lines.append("## Your requests this delivery covers")
    for msg_id in delivered_msg_ids:
        row = store.conn.execute(
            "SELECT content FROM trace_msg WHERE id = ?", (msg_id,)
        ).fetchone()
        if row is None:
            raise EpisodeError(
                f"plan document for run {run_id} sources MSG {msg_id} but"
                " trace_msg has no such row"
            )
        mentions = [
            r["feat_id"]
            for r in store.conn.execute(
                "SELECT feat_id FROM trace_msg_mentions WHERE msg_id = ?"
                " ORDER BY feat_id",
                (msg_id,),
            ).fetchall()
        ]
        mention_block = f" (mentions: {', '.join(mentions)})" if mentions else ""
        lines.append(f"- [{msg_id}] {row['content']}{mention_block}")
    return "\n".join(lines)


def _check_apparatus_digest(store: Store, target: str, digest: str) -> None:
    """R9's hard setup error: every confirmed registry row for the target must
    carry the episode's image digest — a mismatch means the apparatus was
    researched against a different target build."""
    rows = store.conn.execute(
        "SELECT DISTINCT digest FROM trace_feat"
        " WHERE target = ? AND status = 'confirmed' AND digest IS NOT NULL",
        (target,),
    ).fetchall()
    stale = sorted(r["digest"] for r in rows if r["digest"] != digest)
    if stale:
        raise EpisodeError(
            f"digest mismatch at episode setup (hard error, R9): the episode"
            f" pins {digest} but the {target} registry carries"
            f" {', '.join(stale)} — re-research the registry against the"
            " pinned image before running episodes"
        )


# --- entry points --------------------------------------------------------------------------


def run_episode(
    store: Store, cfg: EpisodeConfig, stages: EpisodeStages
) -> EpisodeResult:
    """Start a new episode: setup (digest check, target reset, workspace
    create-or-load), then the delivery loop (R1-R4)."""
    _check_apparatus_digest(store, cfg.target, cfg.digest)
    if cfg.template_dir is not None:
        workspace = ensure_engagement_workspace(
            cfg.workspace_dir, cfg.template_dir, lock_path=cfg.lock_path
        )
    else:
        workspace = ensure_engagement_workspace(
            cfg.workspace_dir, lock_path=cfg.lock_path
        )
    episode_id = store.create_episode(
        cfg.target,
        cfg.digest,
        store.current_snapshot_id(),
        max_increments=cfg.max_increments,
        cost_ceiling_usd=cfg.cost_ceiling_usd,
    )
    state = _EpisodeState(
        workspace_root=str(workspace.root), slice_size=cfg.slice_size
    )
    _save_state(store, episode_id, state)
    stages.reset_target_fn()  # episode-start reset-to-seed (R6)
    store.set_episode_status(episode_id, "running")
    logger.info(
        "episode %d created: target=%s digest=%s max_increments=%d"
        " cost_ceiling=%.2f",
        episode_id,
        cfg.target,
        cfg.digest,
        cfg.max_increments,
        cfg.cost_ceiling_usd,
    )
    return _drive(store, episode_id, workspace, state, stages)


def resume_episode(
    store: Store, episode_id: int, stages: EpisodeStages
) -> EpisodeResult:
    """Re-enter a suspended (or crash-interrupted ``running``) episode at its
    recorded phase (R3). Quota suspension resumes mid-episode: a suspended
    increment re-enters through Phase 1's run checkpoint
    (``stages.resume_increment_fn``)."""
    episode = store.get_episode(episode_id)
    if episode is None:
        raise EpisodeError(f"episode {episode_id} does not exist")
    if episode["status"] in EPISODE_TERMINAL_STATUSES:
        raise EpisodeError(
            f"episode {episode_id} already settled ({episode['status']});"
            " nothing to resume"
        )
    raw = store.get_meta(episode_checkpoint_key(episode_id))
    if raw is None:
        raise EpisodeError(
            f"episode {episode_id} has no checkpoint document; it cannot be"
            " resumed"
        )
    state = _EpisodeState.from_json(raw)
    workspace = load_workspace(state.workspace_root)
    _check_apparatus_digest(store, episode["target"], episode["digest"])
    store.set_episode_status(episode_id, "running")
    logger.info(
        "episode %d resuming at phase %s (increment %d)",
        episode_id,
        state.phase,
        state.increments_completed + 1,
    )
    return _drive(store, episode_id, workspace, state, stages)


# --- the drive loop ----------------------------------------------------------------------


def _drive(
    store: Store,
    episode_id: int,
    workspace: Workspace,
    state: _EpisodeState,
    stages: EpisodeStages,
) -> EpisodeResult:
    episode = store.get_episode(episode_id)
    target = episode["target"]
    max_increments = episode["max_increments"]
    cost_ceiling = episode["cost_ceiling_usd"]
    ctx = EpisodeContext(
        store=store,
        episode_id=episode_id,
        target=target,
        digest=episode["digest"],
        workspace=workspace,
        reset_target=stages.reset_target_fn,
    )
    try:
        while True:
            increment_index = state.increments_completed + 1

            if state.phase == "request":
                slice_ = select_slice(store, target, state.slice_size)
                if not slice_:
                    return _settle(
                        store, ctx, state, stages, "frontier_exhausted"
                    )
                request = stages.request_fn(ctx, increment_index, tuple(slice_))
                state.active_slice = tuple(slice_)
                state.active_msg_id = request.msg_id
                state.active_msg_text = request.text
                state.phase = "increment"
                _save_state(store, episode_id, state)

            if state.phase == "increment":
                inc_ctx = IncrementContext(
                    store=store,
                    episode_id=episode_id,
                    increment_index=increment_index,
                    workspace=workspace,
                    request_msg_id=state.active_msg_id,
                    request_text=state.active_msg_text,
                    slice_feat_ids=state.active_slice,
                    carry_in_msg_ids=state.carry_in_msg_ids,
                    carry_in_ticket_ids=state.carry_in_ticket_ids,
                )
                if state.active_run_id is not None:
                    if stages.resume_increment_fn is None:
                        raise EpisodeError(
                            f"episode {episode_id} has a suspended run"
                            f" ({state.active_run_id}) but no"
                            " resume_increment_fn was injected"
                        )
                    result = stages.resume_increment_fn(
                        inc_ctx, state.active_run_id
                    )
                else:
                    result = stages.increment_fn(inc_ctx)
                _validate_increment_run(
                    store, result, episode_id, increment_index
                )

                if result.status == "plan_failed":
                    return _halt_plan_failed(
                        store, episode_id, state, result, increment_index
                    )
                if result.status == "aborted_quota":
                    # R3: quota suspends, never spends — Phase 1 checkpointed
                    # the run; the episode records it and parks resumable.
                    state.active_run_id = result.run_id
                    return _suspend(
                        store,
                        episode_id,
                        state,
                        f"run {result.run_id} aborted_quota mid-increment"
                        f" {increment_index}: {result.detail}",
                    )
                if result.status == "aborted_error":
                    _save_state(store, episode_id, state)
                    store.set_episode_status(episode_id, "aborted_error")
                    return EpisodeResult(
                        episode_id=episode_id,
                        status="aborted_error",
                        increments_completed=state.increments_completed,
                        run_ids=tuple(state.run_ids),
                        detail=result.detail,
                    )
                # success | partial: the slice was investigated (R10).
                record_investigation(
                    store,
                    [
                        fid
                        for fid in state.active_slice
                        if frontier_row(store, fid) is not None
                    ],
                    status=(
                        "explored"
                        if result.status == "success"
                        else "partially-explored"
                    ),
                )
                state.active_run_id = result.run_id
                state.phase = "uat"
                _save_state(store, episode_id, state)

            # phase == "uat": acceptance on the delivered subset (R2).
            run_id = state.active_run_id
            briefing = render_uat_briefing(store, run_id)
            uat = stages.uat_fn(ctx, increment_index, run_id, briefing)
            if uat.verdict not in RUN_ACCEPTANCE:
                raise EpisodeError(
                    f"UAT returned unknown verdict {uat.verdict!r}"
                    f" (expected one of {RUN_ACCEPTANCE})"
                )
            store.set_run_acceptance(run_id, uat.verdict)
            statuses = _ticket_statuses_for_run(store, run_id)
            escalated = tuple(
                sorted(t for t, s in statuses.items() if s == "escalated")
            )
            state.carry_in_msg_ids = (
                tuple(uat.feedback_msg_ids)
                if uat.verdict == "rejected"
                else ()
            )
            state.carry_in_ticket_ids = escalated
            state.run_ids.append(run_id)
            state.increments_completed += 1
            state.phase = "request"
            state.active_run_id = None
            state.active_slice = ()
            state.active_msg_id = None
            state.active_msg_text = ""
            _save_state(store, episode_id, state)
            logger.info(
                "episode %d increment %d %s (escalated carry-in: %s)",
                episode_id,
                increment_index,
                uat.verdict,
                ", ".join(escalated) or "(none)",
            )

            # Budget checks at the increment boundary (R4).
            if state.increments_completed >= max_increments:
                return _settle(store, ctx, state, stages, "budget_spent")
            cost = episode_cost(store, episode_id)
            if cost >= cost_ceiling:
                logger.info(
                    "episode %d cost ceiling tripped: %.4f >= %.4f",
                    episode_id,
                    cost,
                    cost_ceiling,
                )
                return _settle(store, ctx, state, stages, "budget_spent")
    except SessionQuotaExhausted as exc:
        # Quota inside an explorer session (request/UAT): suspend at the
        # recorded phase — it never counts as budget_spent (R3).
        return _suspend(store, episode_id, state, str(exc))


def _validate_increment_run(
    store: Store, result: RunResult, episode_id: int, increment_index: int
) -> None:
    """Enforce R1's FK invariant: every increment IS a run carrying its
    episode FKs — checked by mechanism, never trusted from the stage."""
    run = store.get_run(result.run_id)
    if run is None:
        raise EpisodeError(
            f"increment stage returned run {result.run_id} but the runs table"
            " has no such row"
        )
    if (
        run["episode_id"] != episode_id
        or run["increment_index"] != increment_index
    ):
        raise EpisodeError(
            f"an increment is exactly one Phase 1 run carrying episode FKs"
            f" (003 R1): run {result.run_id} carries"
            f" (episode_id={run['episode_id']},"
            f" increment_index={run['increment_index']}), expected"
            f" ({episode_id}, {increment_index}) — create the run via"
            " Orchestrator.run(episode_id=..., increment_index=...)"
        )


def _halt_plan_failed(
    store: Store,
    episode_id: int,
    state: _EpisodeState,
    result: RunResult,
    increment_index: int,
) -> EpisodeResult:
    """R2: a plan_failed increment halts the episode and escalates to the
    human — a review_queue record, then terminal ``aborted_error``."""
    payload = json.dumps(
        {
            "run_id": result.run_id,
            "increment_index": increment_index,
            "detail": result.detail,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    with store.transaction():
        store.conn.execute(
            "INSERT INTO review_queue (episode_id, kind, payload_json,"
            " status, created_at) VALUES (?, ?, ?, 'open', ?)",
            (episode_id, PLAN_FAILED_REVIEW_KIND, payload, _utcnow()),
        )
    _save_state(store, episode_id, state)
    store.set_episode_status(episode_id, "aborted_error")
    logger.warning(
        "episode %d halted: increment %d plan_failed — escalated to human"
        " (review_queue)",
        episode_id,
        increment_index,
    )
    return EpisodeResult(
        episode_id=episode_id,
        status="aborted_error",
        increments_completed=state.increments_completed,
        run_ids=tuple(state.run_ids),
        detail=f"plan_failed at increment {increment_index}: {result.detail}",
    )


def _suspend(
    store: Store, episode_id: int, state: _EpisodeState, detail: str
) -> EpisodeResult:
    """R3: park the episode resumable — never a budget terminal."""
    _save_state(store, episode_id, state)
    store.set_episode_status(episode_id, "suspended")
    logger.warning("episode %d suspended (resumable): %s", episode_id, detail)
    return EpisodeResult(
        episode_id=episode_id,
        status="suspended",
        increments_completed=state.increments_completed,
        run_ids=tuple(state.run_ids),
        detail=detail,
    )


def _settle(
    store: Store,
    ctx: EpisodeContext,
    state: _EpisodeState,
    stages: EpisodeStages,
    terminal: str,
) -> EpisodeResult:
    """The settlement handoff: the settle stage resets the target to seed and
    runs the rubric (its contract — pass ``ctx.reset_target`` to
    ``run_settlement``); then the R10 mention-coverage audit force-schedules
    never-mentioned FEATs for the next episode's opening slice; then the
    terminal lands."""
    _save_state(store, ctx.episode_id, state)
    stages.settle_fn(ctx)
    flagged = mention_coverage_audit(store, ctx.target)
    store.set_episode_status(ctx.episode_id, terminal)
    logger.info(
        "episode %d settled %s after %d increment(s); %d FEAT(s)"
        " force-scheduled for the next episode",
        ctx.episode_id,
        terminal,
        state.increments_completed,
        len(flagged),
    )
    return EpisodeResult(
        episode_id=ctx.episode_id,
        status=terminal,
        increments_completed=state.increments_completed,
        run_ids=tuple(state.run_ids),
    )


def _save_state(store: Store, episode_id: int, state: _EpisodeState) -> None:
    store.set_meta(episode_checkpoint_key(episode_id), state.to_json())
