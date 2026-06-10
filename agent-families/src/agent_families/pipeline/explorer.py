"""Explorer subsystem: prompts, verified-oracle Q&A, UAT, containment
(plan-003 U5, R11–R15).

Structural containment (R15): explorer sessions have **no tools at all**
(``--tools ""``, the planner-profile precedent). A session emits structured
browse requests ``{action, selector, args}`` as output; the ORCHESTRATOR
executes them (live: Playwright; offline: a scripted fake) and feeds the
accessibility-tree observations back as text in the next invocation's prompt
(:func:`run_explorer_task`). Containment therefore needs no enforcement —
there is nothing to misuse — but a transcript-scan assert (the Phase 1 R8
pattern) remains as defense-in-depth: ANY tool_use in an explorer transcript
is a violation (:func:`assert_toolless_transcript`).

The explorer is an untrusted producer: its only channel is validated
structured output, and the orchestrator (this module's store-writing
functions, running in THIS process) is the sole writer of MSG/qa_log/review
rows — the 002 R6 discipline carried forward.

R11 — the opening prompt is authored by a fresh explorer exploration session
on the target UI, briefed with the frontier slice (force-scheduled items
first — the slice arrives pre-ordered from ``frontier.select_slice``) and
its own accepted-delivery history, and instructed to describe features
beyond the already-built set. It and every subsequent explorer utterance
land as MSG rows tagged ``mentions: [FEAT-*]`` — the mentions FK makes
unminted features unmentionable mechanically (R10), and a non-confirmed
(deprecated) mention is rejected belt-and-braces.

R12 — the question round-trip (:func:`ask_question`): budget gate → fresh-
context explorer answer grounded in a fresh UI observation → grader-side
checker (:mod:`.oracle_check`) → pass: answer MSG + answered qa_log row |
fail: fresh-context re-answer embedding the contradiction (N retries,
caller-routed config; retries are grader-side and consume NO question
budget) → final failure: typed ``answer_unavailable``, the budget slot
refunded, the tuple queued for human review.

R13 — hard per-increment question cap: question N+1 receives a typed
``budget_exhausted`` bounce, logged as a qa_log row for the elicitation-
efficiency metric. UAT feedback is budget-free — acceptance is not
elicitation (:func:`run_uat` never touches qa_log). The plan-checker's
``assumptions[]`` → question conversion lives in
:func:`agent_families.pipeline.planning.check_plan_assumptions` (this unit
closes that Phase 1 seam); conversions ride this module's budget gate.

R14 — fresh-context answering: each answer attempt is a brand-new explorer
session reading the persistent registry-facing Q&A log
(:func:`qa_history_text`) — no long-lived simulator session exists.

Tunables (question cap, answer retries, browse-step cap, session budgets)
are caller-supplied per the established seam precedent — routing them from
``thresholds.toml`` is the episode orchestrator's job (003 U6).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agent_families.judge import validate_against_schema
from agent_families.pipeline.oracle_check import (
    check_answer,
    retrieve_registry_evidence,
)
from agent_families.pipeline.sessions import RoleProfile, run_session
from agent_families.pipeline.workspace import _iter_tool_uses
from agent_families.store import RUN_ACCEPTANCE

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# The orchestrator-mediated browse channel (R15): one structured request
# ``{action, selector, args}`` in, one observation out — ``{"status": "ok" |
# "element_absent" | "error", "a11y": <snapshot>, ...}``. Live this is
# Playwright driven by the orchestrator; offline it is a scripted fake.
Browse = Callable[[dict], dict]

_BROWSE_REQUEST_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "selector": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["action", "selector", "args"],
    "additionalProperties": False,
}

# Every explorer session turn is one of two moves: request another mediated
# observation, or finish with the task's result payload (validated against
# the task-specific result schema via extra_validate).
_STEP_ACTIONS = ("browse", "finish")


def _step_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_STEP_ACTIONS)},
            "request": {
                "type": ["object", "null"],
                "properties": dict(_BROWSE_REQUEST_SCHEMA["properties"]),
                "required": list(_BROWSE_REQUEST_SCHEMA["required"]),
                "additionalProperties": False,
            },
            "result": {"type": ["object", "null"]},
        },
        "required": ["action", "request", "result"],
        "additionalProperties": False,
    }


OPENING_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt": {"type": "string"},
        "mentions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["prompt", "mentions"],
    "additionalProperties": False,
}

ANSWER_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

_UAT_FEEDBACK_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string"},
        "mentions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["content", "mentions"],
    "additionalProperties": False,
}

UAT_RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(RUN_ACCEPTANCE)},
        "feedback": {"type": "array", "items": _UAT_FEEDBACK_ITEM_SCHEMA},
    },
    "required": ["verdict", "feedback"],
    "additionalProperties": False,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ExplorerError(Exception):
    """Explorer-subsystem misuse or a broken invariant, actionable message."""


class ExplorerContainmentViolation(ExplorerError):
    """An explorer transcript contains tool use — the session was supposed to
    be structurally tool-less (R15)."""


# --- the explorer profile (R15: no tools at all) --------------------------------


def explorer_profile(*, model: str, max_turns: int, timeout_s: float) -> RoleProfile:
    """R15: the explorer session has NO tools — structured browse requests
    are its only channel; the orchestrator executes them."""
    return RoleProfile(
        role="explorer",
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tools="",
        allowed_tools=None,
    )


# --- containment assert (R15 defense-in-depth, the Phase 1 R8 pattern) ----------


def transcript_tool_use_names(transcript_path: str | Path) -> list[str]:
    """Every tool name used anywhere in a JSONL transcript, in order.

    Non-JSON lines are skipped (live stream capture writes noise lines into
    transcripts by design) — a scanner that crashes on noise protects
    nothing.
    """
    names: list[str] = []
    with Path(transcript_path).open(encoding="utf-8") as fh:
        for line in fh:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                event = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            for tool_use in _iter_tool_uses(event):
                name = tool_use.get("name")
                names.append(name if isinstance(name, str) else "<unnamed>")
    return names


def assert_toolless_transcript(transcript_path: str | Path) -> None:
    """Raise :class:`ExplorerContainmentViolation` on ANY tool use.

    The explorer's containment is structural (no tools granted), so this can
    only fire on a harness regression or a forged transcript — exactly what
    defense-in-depth is for.
    """
    names = transcript_tool_use_names(transcript_path)
    if names:
        raise ExplorerContainmentViolation(
            f"explorer transcript {transcript_path} contains tool use"
            f" ({', '.join(sorted(set(names)))}) — explorer sessions are"
            " structurally tool-less (R15); this is a harness regression,"
            " not explorer misbehavior"
        )


# --- the orchestrator-mediated browse loop (R15) ---------------------------------


@dataclass(frozen=True)
class ExplorerConfig:
    """One explorer task's session envelope, caller-supplied (no hidden
    tunables): the tool-less profile, the mediated browse channel, and the
    per-session budgets the orchestrator routes from config."""

    profile: RoleProfile
    browse: Browse
    transcript_dir: Path
    max_steps: int
    max_retries: int
    family: str | None = None
    mode: str | None = None
    script_path: str | Path | None = None

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ExplorerError(
                f"ExplorerConfig.max_steps must be a positive browse-loop"
                f" cap, got {self.max_steps}"
            )


@dataclass(frozen=True)
class ExplorerTaskResult:
    """A finished explorer task: the result payload plus its observation log."""

    result: dict
    observations: tuple[dict, ...]  # [{"request": ..., "observation": ...}]
    steps: int
    transcript_path: Path

    def fresh_a11y(self):
        """The LAST successful observation's a11y snapshot — the fresh
        observation an answer must be grounded in (R12); None if the task
        never observed anything."""
        for entry in reversed(self.observations):
            observation = entry["observation"]
            if observation.get("status") == "ok" and observation.get("a11y"):
                return observation["a11y"]
        return None


def _render_observations(observations: Sequence[dict]) -> str:
    blocks = []
    for i, entry in enumerate(observations, start=1):
        request = json.dumps(entry["request"], sort_keys=True, ensure_ascii=False)
        observation = json.dumps(
            entry["observation"], sort_keys=True, ensure_ascii=False
        )
        blocks.append(f"### Observation {i} — request {request}\n{observation}")
    return "\n".join(blocks)


def _validate_step(output: dict, result_schema: dict) -> str | None:
    """Step-shape validation beyond the schema: the action decides which of
    request/result must be present, and a finish payload must satisfy the
    task's result schema."""
    if output["action"] == "browse":
        if not isinstance(output.get("request"), dict):
            return (
                "a browse step requires a request object"
                " {action, selector, args} (R15)"
            )
        if output.get("result") is not None:
            return "a browse step must leave result null"
        return None
    if output.get("request") is not None:
        return "a finish step must leave request null"
    if not isinstance(output.get("result"), dict):
        return "a finish step requires the task's result object"
    return validate_against_schema(output["result"], result_schema, "$.result")


def run_explorer_task(
    prompt: str,
    result_schema: dict,
    cfg: ExplorerConfig,
    *,
    transcript_path: str | Path,
    store: Store | None = None,
    run_id: int | None = None,
) -> ExplorerTaskResult:
    """Drive one tool-less explorer task over the mediated browse loop (R15).

    Each step is a fresh ``run_session`` invocation whose prompt carries the
    base brief plus every observation so far; the session either requests
    one more observation or finishes with the task's result. The transcript
    is scanned for tool use after every step (defense-in-depth). Exceeding
    ``cfg.max_steps`` without finishing is a hard error — the orchestrator
    decides what an unfinished task costs, not this loop.
    """
    transcript_path = Path(transcript_path)
    observations: list[dict] = []
    current_prompt = prompt
    for step in range(1, cfg.max_steps + 1):
        session = run_session(
            current_prompt,
            _step_schema(),
            cfg.profile,
            transcript_path=transcript_path,
            max_retries=cfg.max_retries,
            store=store,
            run_id=run_id,
            family=cfg.family,
            agent="explorer",
            ralph_iteration=step,
            extra_validate=lambda output: _validate_step(output, result_schema),
            mode=cfg.mode,
            script_path=cfg.script_path,
        )
        assert_toolless_transcript(session.transcript_path)
        output = session.output
        if output["action"] == "finish":
            return ExplorerTaskResult(
                result=output["result"],
                observations=tuple(observations),
                steps=step,
                transcript_path=transcript_path,
            )
        observation = cfg.browse(dict(output["request"]))
        if not isinstance(observation, dict):
            observation = {"status": "malformed-observation"}
        observations.append(
            {"request": dict(output["request"]), "observation": observation}
        )
        current_prompt = (
            f"{prompt}\n\n## Observations so far\n"
            f"{_render_observations(observations)}\n\n"
            "Continue: emit one more browse request, or finish with the"
            " task's result."
        )
    raise ExplorerError(
        f"explorer task did not finish within {cfg.max_steps} step(s);"
        " transcript: " + str(transcript_path)
    )


# --- MSG rows with mentions (orchestrator-written, FK-checked) --------------------


def write_explorer_msg(
    store: Store, msg_id: str, content: str, mentions: Sequence[str]
) -> str:
    """Persist one explorer-authored MSG row with its FEAT mentions, atomically.

    Mentions are mandatory (R11/R12) and FK-checked: an unminted FEAT id is
    rejected by the ``trace_msg_mentions`` FK (R10's mentionability gate) and
    the whole write rolls back; a minted-but-deprecated FEAT is rejected
    belt-and-braces (new utterances mention live features only — history
    keeps its old mentions).
    """
    unique = tuple(dict.fromkeys(mentions))
    if not unique:
        raise ExplorerError(
            f"explorer message {msg_id} carries no FEAT mentions — every"
            " explorer utterance is tagged mentions: [FEAT-*] (R11/R12)"
        )
    with store.transaction():
        try:
            store.conn.execute(
                "INSERT INTO trace_msg (id, content) VALUES (?, ?)",
                (msg_id, content),
            )
        except sqlite3.IntegrityError as exc:
            raise ExplorerError(
                f"MSG id {msg_id} already exists — explorer MSG ids are"
                " minted once per utterance and never reused"
            ) from exc
        for fid in unique:
            try:
                store.conn.execute(
                    "INSERT INTO trace_msg_mentions (msg_id, feat_id)"
                    " VALUES (?, ?)",
                    (msg_id, fid),
                )
            except sqlite3.IntegrityError as exc:
                raise ExplorerError(
                    f"{fid} is not mentionable: no FEAT row exists. Features"
                    " become mentionable only once runtime-confirmed and"
                    " minted (R10) — the mentions FK rejected it and the"
                    " message was not written"
                ) from exc
        placeholders = ", ".join("?" for _ in unique)
        stale = store.conn.execute(
            f"SELECT id FROM trace_feat WHERE id IN ({placeholders})"
            " AND status != 'confirmed' ORDER BY id",
            unique,
        ).fetchall()
        if stale:
            raise ExplorerError(
                f"message {msg_id} mentions non-confirmed FEAT(s):"
                f" {', '.join(r['id'] for r in stale)} — new explorer"
                " utterances mention confirmed features only (R9/R10);"
                " the message was not written"
            )
    return msg_id


# --- the opening prompt (R11) ------------------------------------------------------


@dataclass(frozen=True)
class OpeningPrompt:
    """The increment's explorer-authored opening request, persisted as MSG."""

    msg_id: str
    text: str
    mentions: tuple[str, ...]
    task: ExplorerTaskResult


def build_exploration_brief(
    *, target: str, slice_feat_ids: Sequence[str], built_history: str = ""
) -> str:
    """The fresh exploration session's brief (R11): the frontier slice in
    its scheduled order (force-scheduled items arrive first from
    ``frontier.select_slice``), the accepted-delivery history (the
    already-built set), and the describe-beyond-the-built-set instruction."""
    if not slice_feat_ids:
        raise ExplorerError(
            "an opening prompt needs a non-empty frontier slice — an empty"
            " slice means the frontier is exhausted and the episode should"
            " settle, not explore (R3/R10)"
        )
    slice_block = "\n".join(
        f"{i}. {fid}" for i, fid in enumerate(slice_feat_ids, start=1)
    )
    built_block = (
        "## Already built (your accepted-delivery history)\n\n"
        f"{built_history.strip()}\n\n"
        if built_history.strip()
        else "## Already built (your accepted-delivery history)\n\n(nothing yet)\n\n"
    )
    return (
        f"You are a customer exploring the live app '{target}' through the"
        " mediated browse channel: emit one browse request at a time and"
        " read the observations that come back. You may browse both the"
        " target and your clone-in-progress — both are UI.\n\n"
        f"{built_block}"
        "## Feature areas to investigate this increment, in priority order"
        " (force-scheduled first)\n\n"
        f"{slice_block}\n\n"
        "Explore the app, then finish with the OPENING PROMPT for the next"
        " delivery increment: describe, as a customer would, the features"
        " you want built BEYOND the already-built set above — grounded in"
        " what you actually observed. Set result.prompt to that request"
        " text and result.mentions to the FEAT ids it concerns (at minimum"
        " the slice items you covered)."
    )


def author_opening_prompt(
    store: Store,
    episode_id: int,
    increment_index: int,
    cfg: ExplorerConfig,
    *,
    target: str,
    slice_feat_ids: Sequence[str],
    built_history: str = "",
    run_id: int | None = None,
) -> OpeningPrompt:
    """R11: a fresh explorer session writes the increment's opening prompt,
    persisted as an MSG row whose mentions are FK-checked."""
    brief = build_exploration_brief(
        target=target,
        slice_feat_ids=slice_feat_ids,
        built_history=built_history,
    )
    task = run_explorer_task(
        brief,
        OPENING_RESULT_SCHEMA,
        cfg,
        transcript_path=Path(cfg.transcript_dir)
        / f"e{episode_id}-inc{increment_index:03d}-opening.jsonl",
        store=store,
        run_id=run_id,
    )
    text = task.result["prompt"]
    mentions = tuple(dict.fromkeys(task.result["mentions"]))
    if not text.strip():
        raise ExplorerError("the explorer returned an empty opening prompt")
    msg_id = f"MSG-e{episode_id}-inc{increment_index:03d}-open"
    write_explorer_msg(store, msg_id, text, mentions)
    return OpeningPrompt(msg_id=msg_id, text=text, mentions=mentions, task=task)


# --- the verified-oracle question round-trip (R12/R13/R14) --------------------------


@dataclass(frozen=True)
class CheckerConfig:
    """The grader-side checker's caller-routed envelope: the judge model and
    its schema-retry budget, plus R12's N — fresh-context re-answers per
    question before ``answer_unavailable``."""

    model: str
    judge_retries: int
    answer_retries: int
    mode: str | None = None
    fixtures_dir: str | Path | None = None

    def __post_init__(self) -> None:
        if self.answer_retries < 0:
            raise ExplorerError(
                f"CheckerConfig.answer_retries must be >= 0, got"
                f" {self.answer_retries}"
            )


@dataclass(frozen=True)
class QAOutcome:
    """One question slot's typed outcome (R12/R13)."""

    qa_id: int
    outcome: str  # "answered" | "answer_unavailable" | "budget_exhausted"
    answer: str | None
    retries: int
    contradiction: dict | None
    question_msg_id: str | None
    answer_msg_id: str | None


def questions_spent(
    store: Store, episode_id: int, run_id: int | None = None
) -> int:
    """Budget-counted question slots so far — bounced and refunded slots
    (``budget_counted = 0``) never count (R13)."""
    if run_id is None:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM qa_log"
            " WHERE episode_id = ? AND budget_counted = 1",
            (episode_id,),
        ).fetchone()
    else:
        row = store.conn.execute(
            "SELECT COUNT(*) AS n FROM qa_log"
            " WHERE episode_id = ? AND run_id = ? AND budget_counted = 1",
            (episode_id, run_id),
        ).fetchone()
    return row["n"]


def qa_history_text(store: Store, episode_id: int) -> str:
    """The persistent registry-facing Q&A log a fresh answering instance
    reads (R14) — answered slots only, oldest first."""
    rows = store.conn.execute(
        "SELECT question, answer FROM qa_log"
        " WHERE episode_id = ? AND outcome = 'answered' ORDER BY id",
        (episode_id,),
    ).fetchall()
    return "\n".join(f"Q: {r['question']}\nA: {r['answer']}" for r in rows)


def build_answer_prompt(
    question: str, qa_history: str, *, contradiction: dict | None = None
) -> str:
    """The fresh-context answering brief (R14); a retry embeds the checker's
    contradiction object verbatim (R12)."""
    history_block = (
        f"## Q&A log so far\n\n{qa_history.strip()}\n\n"
        if qa_history.strip()
        else ""
    )
    retry_block = (
        "## Your previous answer was rejected by the grader-side checker\n\n"
        + json.dumps(contradiction, sort_keys=True, ensure_ascii=False)
        + "\n\nRe-observe the app and answer again, resolving the"
        " contradiction above.\n\n"
        if contradiction is not None
        else ""
    )
    return (
        "You are a fresh explorer instance answering one pipeline question"
        " about the target app. Ground your answer in a FRESH observation:"
        " emit browse requests first, read the observations, then finish"
        " with result.answer. An answer without a fresh observation will be"
        " rejected.\n\n"
        f"{history_block}"
        f"{retry_block}"
        f"## Question\n{question}"
    )


def _insert_qa_row(
    store: Store,
    *,
    episode_id: int,
    run_id: int | None,
    question: str,
    question_msg_id: str | None = None,
    answer: str | None = None,
    answer_msg_id: str | None = None,
    checker_verdict: str | None = None,
    contradiction_json: str | None = None,
    retries: int = 0,
    outcome: str | None = None,
    budget_counted: int = 1,
) -> int:
    cur = store.conn.execute(
        "INSERT INTO qa_log (episode_id, run_id, question, question_msg_id,"
        " answer, answer_msg_id, checker_verdict, contradiction_json,"
        " retries, outcome, budget_counted, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (episode_id, run_id, question, question_msg_id, answer, answer_msg_id,
         checker_verdict, contradiction_json, retries, outcome, budget_counted,
         _utcnow()),
    )
    return cur.lastrowid


def ask_question(
    store: Store,
    episode_id: int,
    question: str,
    mentions: Sequence[str],
    cfg: ExplorerConfig,
    checker: CheckerConfig,
    *,
    cap: int,
    run_id: int | None = None,
) -> QAOutcome:
    """The full verified-oracle round-trip for one question (R12/R13/R14).

    budget gate (typed ``budget_exhausted`` bounce at the cap) → question MSG
    (mandatory mentions, FK-checked) → fresh-context explorer answer over the
    mediated browse loop → grader-side checker → pass: answer MSG + counted
    ``answered`` slot | fail: fresh-context re-answer with the contradiction
    embedded (``checker.answer_retries`` times, budget-free) → final failure:
    ``answer_unavailable``, slot refunded, review-queue row. ``cap`` is the
    per-increment hard cap, routed from config by the orchestrator (U6).
    """
    if cap < 0:
        raise ExplorerError(f"question cap must be >= 0, got {cap}")
    if store.get_episode(episode_id) is None:
        raise ExplorerError(f"episode {episode_id} does not exist")
    unique_mentions = tuple(dict.fromkeys(mentions))
    if not unique_mentions:
        raise ExplorerError(
            "questions carry mandatory FEAT mentions (R12) — got none"
        )

    spent = questions_spent(store, episode_id, run_id)
    if spent >= cap:
        with store.transaction():
            qa_id = _insert_qa_row(
                store,
                episode_id=episode_id,
                run_id=run_id,
                question=question,
                outcome="budget_exhausted",
                budget_counted=0,
            )
        logger.info(
            "question budget exhausted (episode=%d, run=%s, cap=%d): typed"
            " bounce logged for the elicitation-efficiency metric (R13)",
            episode_id,
            run_id,
            cap,
        )
        return QAOutcome(
            qa_id=qa_id,
            outcome="budget_exhausted",
            answer=None,
            retries=0,
            contradiction=None,
            question_msg_id=None,
            answer_msg_id=None,
        )

    q_index = store.conn.execute(
        "SELECT COUNT(*) AS n FROM qa_log WHERE episode_id = ?", (episode_id,)
    ).fetchone()["n"] + 1
    question_msg_id = f"MSG-e{episode_id}-q{q_index:04d}"
    write_explorer_msg(store, question_msg_id, question, unique_mentions)

    history = qa_history_text(store, episode_id)
    evidence = retrieve_registry_evidence(store, unique_mentions)
    contradiction: dict | None = None
    answer: str | None = None
    for attempt in range(checker.answer_retries + 1):
        task = run_explorer_task(
            build_answer_prompt(question, history, contradiction=contradiction),
            ANSWER_RESULT_SCHEMA,
            cfg,
            transcript_path=Path(cfg.transcript_dir)
            / f"e{episode_id}-q{q_index:04d}-attempt{attempt:02d}.jsonl",
            store=store,
            run_id=run_id,
        )
        answer = task.result["answer"]
        fresh = task.fresh_a11y()
        if fresh is None:
            # an ungrounded answer never reaches the checker: it fails by
            # construction and rides the same fresh-context retry arm
            contradiction = {
                "claim": answer,
                "evidence_ref": "fresh-observation",
                "observed": (
                    "the explorer made no fresh UI observation before"
                    " answering — every answer must be grounded in one (R12)"
                ),
            }
            logger.warning(
                "answer attempt %d ungrounded (no fresh observation);"
                " retrying fresh-context",
                attempt,
            )
            continue
        verdict = check_answer(
            question,
            answer,
            fresh,
            evidence,
            model=checker.model,
            max_retries=checker.judge_retries,
            mode=checker.mode,
            fixtures_dir=checker.fixtures_dir,
        )
        if verdict.passed:
            answer_msg_id = f"{question_msg_id}-a"
            with store.transaction():
                write_explorer_msg(store, answer_msg_id, answer, unique_mentions)
                qa_id = _insert_qa_row(
                    store,
                    episode_id=episode_id,
                    run_id=run_id,
                    question=question,
                    question_msg_id=question_msg_id,
                    answer=answer,
                    answer_msg_id=answer_msg_id,
                    checker_verdict="pass",
                    retries=attempt,
                    outcome="answered",
                    budget_counted=1,
                )
            return QAOutcome(
                qa_id=qa_id,
                outcome="answered",
                answer=answer,
                retries=attempt,
                contradiction=None,
                question_msg_id=question_msg_id,
                answer_msg_id=answer_msg_id,
            )
        contradiction = verdict.contradiction
        logger.info(
            "checker rejected answer attempt %d (grader-side retry,"
            " no budget consumed): %s",
            attempt,
            contradiction,
        )

    # Final failure (R12): typed answer_unavailable, slot refunded, tuple
    # queued for human review.
    contradiction_json = json.dumps(
        contradiction, sort_keys=True, ensure_ascii=False
    )
    with store.transaction():
        qa_id = _insert_qa_row(
            store,
            episode_id=episode_id,
            run_id=run_id,
            question=question,
            question_msg_id=question_msg_id,
            checker_verdict="fail",
            contradiction_json=contradiction_json,
            retries=checker.answer_retries,
            outcome="answer_unavailable",
            budget_counted=0,
        )
        store.conn.execute(
            "INSERT INTO review_queue (episode_id, qa_log_id, kind,"
            " payload_json, status, created_at)"
            " VALUES (?, ?, 'answer_unavailable', ?, 'open', ?)",
            (
                episode_id,
                qa_id,
                json.dumps(
                    {
                        "question": question,
                        "last_answer": answer,
                        "contradiction": contradiction,
                        "mentions": list(unique_mentions),
                    },
                    sort_keys=True,
                    ensure_ascii=False,
                ),
                _utcnow(),
            ),
        )
    logger.warning(
        "question unanswerable after %d retry(ies): answer_unavailable, slot"
        " refunded, queued for human review (qa_log id %d)",
        checker.answer_retries,
        qa_id,
    )
    return QAOutcome(
        qa_id=qa_id,
        outcome="answer_unavailable",
        answer=None,
        retries=checker.answer_retries,
        contradiction=contradiction,
        question_msg_id=question_msg_id,
        answer_msg_id=None,
    )


# --- UAT (R11; budget-free per R13) ---------------------------------------------


@dataclass(frozen=True)
class UATResult:
    """One acceptance pass: the verdict plus the persisted feedback MSG ids."""

    verdict: str  # "accepted" | "rejected"
    feedback_msg_ids: tuple[str, ...]
    task: ExplorerTaskResult


def build_uat_prompt(briefing: str) -> str:
    """The acceptance brief over the orchestrator-rendered delivered scope."""
    return (
        "You are the explorer running user acceptance on the increment just"
        " delivered to your clone app. The delivered scope is below — judge"
        " ONLY that subset. Use the mediated browse channel to exercise it"
        " on the running clone, then finish with result.verdict"
        " ('accepted' or 'rejected') and result.feedback: when rejecting,"
        " one item per problem ({content, mentions: [FEAT ids]}), written"
        " as a customer describing what is wrong.\n\n"
        f"## Delivered scope\n\n{briefing}"
    )


def run_uat(
    store: Store,
    episode_id: int,
    increment_index: int,
    cfg: ExplorerConfig,
    *,
    briefing: str,
    run_id: int | None = None,
) -> UATResult:
    """Explorer UAT on the delivered subset (R11/003 R2).

    Feedback lands as explorer-authored MSG rows with mentions — the next
    increment's planner extracts bug REQs from them
    (``planning.run_planning`` carry-in). UAT is budget-free (R13): this
    function never touches qa_log. With ``run_id`` the verdict is recorded
    on the run row (the acceptance stage).
    """
    task = run_explorer_task(
        build_uat_prompt(briefing),
        UAT_RESULT_SCHEMA,
        cfg,
        transcript_path=Path(cfg.transcript_dir)
        / f"e{episode_id}-inc{increment_index:03d}-uat.jsonl",
        store=store,
        run_id=run_id,
    )
    verdict = task.result["verdict"]
    feedback = task.result["feedback"]
    if verdict == "rejected" and not feedback:
        raise ExplorerError(
            "a rejected UAT must carry at least one feedback item — the"
            " bug REQs of the next increment are extracted from it (R11)"
        )
    msg_ids: list[str] = []
    for n, item in enumerate(feedback, start=1):
        msg_id = f"MSG-e{episode_id}-inc{increment_index:03d}-uat{n:02d}"
        write_explorer_msg(store, msg_id, item["content"], item["mentions"])
        msg_ids.append(msg_id)
    if run_id is not None:
        store.set_run_acceptance(run_id, verdict)
    logger.info(
        "UAT %s for episode %d increment %d (%d feedback message(s);"
        " budget-free per R13)",
        verdict,
        episode_id,
        increment_index,
        len(msg_ids),
    )
    return UATResult(
        verdict=verdict, feedback_msg_ids=tuple(msg_ids), task=task
    )
