"""golden_judge — the ONE golden-drift-detector judge endpoint (U10 Gap 3).

MAT-141 Gap 3: there must be exactly ONE golden-judge *implementation*.  The
standalone TS advisory judge (``rerank/golden.ts``) is **superseded by this
one** — the TS module is now a thin proxy that forwards each verdict call here.

This is the single authoritative golden-case drift detector: given a ``lesson``
(the guidance a skill revision was edited to encode) and a ``candidateBody`` (a
later candidate revision), it answers one question — does the candidate STILL
satisfy the lesson, or did it drift (a regression)?

The verdict logic lives ONCE, here, on top of the shared ``run_judge`` runner
(``learning_service.judge`` → ``agent_families.judge``), so there is no second
copy of the prompt / parsing logic.  The TS side never re-implements it.

Lambda handler shape: ``handler(event, context) -> {"statusCode", "body"}``.
Route: ``POST /judge/golden``  Body: ``{ "lesson": "...", "candidateBody": "..." }``
Response: ``{ "satisfied": bool, "reason": "..." }``.

Offline-testable: a ``_test_judge`` callable can be injected via the event so the
endpoint runs without loading the model or hitting the network.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The single golden-drift-detector verdict
# ---------------------------------------------------------------------------

# The JSON schema the judge must emit (drift-detector verdict).
GOLDEN_VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "satisfied": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["satisfied", "reason"],
    "additionalProperties": False,
}

_SYSTEM_PROMPT = (
    "You are a regression judge for a skill library. A skill was previously "
    "edited to encode a specific lesson. You are given that lesson and a "
    "CANDIDATE new version of the skill body. Decide whether the candidate "
    "STILL satisfies the lesson (the guidance is still present and not "
    "contradicted). Set satisfied=false ONLY when the candidate clearly drops "
    "or contradicts the lesson (a regression)."
)


def _render_prompt(lesson: str, candidate_body: str) -> str:
    return (
        f"{_SYSTEM_PROMPT}\n\n"
        f"Lesson the skill was edited to encode:\n{lesson}\n\n"
        f"Candidate new skill body:\n{candidate_body}"
    )


def judge_golden(
    lesson: str,
    candidate_body: str,
    *,
    run_judge_fn: Callable[..., Any] | None = None,
    model: str = "claude-haiku-4-5",
) -> dict[str, Any]:
    """Return the single golden-drift verdict ``{satisfied, reason}``.

    This is the ONE implementation of the golden-judge verdict.  It delegates to
    the shared ``run_judge`` runner (schema-constrained) — there is no second
    copy of the verdict logic anywhere in the system.

    ``run_judge_fn`` is injectable for offline tests (defaults to the real
    ``learning_service.judge.run_judge``).  A garbled/empty verdict is surfaced
    as a regression (``satisfied: false``) — advisory v1 errs toward flagging,
    never a silent pass (mirrors the superseded TS judge's contract).
    """
    if run_judge_fn is None:
        from learning_service.judge import run_judge as run_judge_fn  # type: ignore[no-redef]

    try:
        result = run_judge_fn(
            prompt=_render_prompt(lesson, candidate_body),
            schema=GOLDEN_VERDICT_SCHEMA,
            model=model,
            max_retries=2,
        )
        out = getattr(result, "output", None) or {}
        satisfied = bool(out.get("satisfied", False))
        reason = str(out.get("reason", ""))
        return {"satisfied": satisfied, "reason": reason}
    except Exception as exc:  # noqa: BLE001 — advisory: a failure flags, never silently passes.
        logger.warning("golden judge verdict failed; surfacing as regression: %s", exc)
        return {"satisfied": False, "reason": f"golden judge unavailable: {exc}"}


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """POST /judge/golden — the single golden-judge endpoint."""
    try:
        raw = event.get("body", "{}")
        body = raw if isinstance(raw, dict) else json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {"statusCode": 400, "body": json.dumps({"error": f"Invalid JSON: {exc}"})}

    lesson = body.get("lesson")
    candidate_body = body.get("candidateBody")
    if not isinstance(lesson, str) or not isinstance(candidate_body, str):
        return {
            "statusCode": 400,
            "body": json.dumps({"error": "lesson and candidateBody (strings) are required"}),
        }

    # Offline test seam: inject a judge so the endpoint runs without the model.
    test_judge = event.get("_test_judge")
    verdict = judge_golden(lesson, candidate_body, run_judge_fn=test_judge)
    return {"statusCode": 200, "body": json.dumps(verdict)}
