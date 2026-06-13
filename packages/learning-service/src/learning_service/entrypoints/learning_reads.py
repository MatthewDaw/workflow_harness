"""learning_reads — Learning read API endpoints (A1 / MAT-154) + telemetry dashboard (R4 / MAT-153).

Two read endpoints over the learning store:

  GET /skills/:name/candidate-learnings
      Returns the current (non-retired) ideas for a skill that meet the
      corroboration threshold — the set a working session is allowed to see.
      Excludes any idea where ``invalidAt`` is set (per the boundary table:
      "current set only — exclude invalidAt; history queryable").

  GET /skills/:name/all-ideas
      Returns EVERY idea for a skill (open + folded, current + historical).
      Excludes ``invalidAt`` ideas from the *current* set view; history is
      included when the ``?history=true`` query param is passed.

Both endpoints enforce org-scoping via the ``X-Org`` request header (or the
``org`` query param for simple callers / contract tests).

Lambda handler shape (same as the other entrypoints in this package):
    handler(event: dict, context: object) -> {"statusCode": int, "body": str}

The TS web app points at these endpoints via ``VITE_PYTHON_LEARNING_READ_URL``
(see ``packages/web/src/api/learningApi.ts`` for the TS side of the contract).

JSON-DTO contract (the "both sides" contract test in ``test_a1_learning_read_api.py``):

  candidate-learnings response:
    {
      "learnings": [
        {
          "ideaId": str,
          "skillBaseName": str,
          "body": str,
          "status": "open" | "folded",
          "corroborationCount": int,
          "foldedIntoRev": int | null,
          "authored": bool | null,
          "authorityKind": str | null
        },
        ...
      ]
    }

  all-ideas response:
    {
      "ideas": [
        {  // same fields as above, plus:
          "invalidAt": int | null,
          "supersededBy": str | null,
          "supersedes": [str],
          "refines": str | null
        },
        ...
      ]
    }

Telemetry dashboard endpoint (R4 / MAT-153 — Gap 4):

  GET /skills/:name/telemetry-metrics
      Returns the per-org/skill calibration metrics as a JSON dict (the R4
      metric set).  Suitable for wiring into a CloudWatch Dashboard widget or
      a Logs Insights query.

  telemetry-metrics response:
    {
      "org": str,
      "skill_base_name": str,
      "idea_count": int,
      "corroboration_weight_mean": float,
      "corroboration_weight_max": float,
      "corroboration_weight_distribution": [float, ...],
      "distinct_pr_count_mean": float,
      "distinct_pr_distribution": [int, ...],
      "per_author_credibility": { "authorId": float, ... },
      "supersede_event_count": int,
      "unfold_count": int,
      "revive_count": int,
      "necessity_demotion_count": int,
      "authored_count": int,
      "inferred_count": int,
      "authored_inferred_ratio": float
    }

  Gate status endpoint (R4 — Gap 3 surface):

  GET /telemetry/gate-status
      Returns the current gate-status snapshot from an empty accumulator
      (zero counts — useful for seeing the threshold config and gate logic
      without running an ingest job).  For a live gate reading, the ingest
      job emits gate status in its structured log output.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from learning_service.db.store import InMemoryLearningStore, LearningStore
from learning_service.schema.generated.py_types import IdeaRecord
from learning_service.telemetry import (
    GateConfig,
    TelemetryAccumulator,
    compute_org_metrics,
    gate_status,
    to_json_dict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Corroboration threshold (mirrors CORROBORATION_K in corroborate.ts / ideas.ts)
# ---------------------------------------------------------------------------

CORROBORATION_K = 2


# ---------------------------------------------------------------------------
# DTO serialisation helpers
# ---------------------------------------------------------------------------

def _idea_to_candidate_dto(record: IdeaRecord) -> dict[str, Any]:
    """Serialise an IdeaRecord to the candidate-learnings DTO shape.

    Only includes the fields that a working session is allowed to see —
    body, corroboration count, fold provenance, and authority kind.
    Deliberately excludes ``invalidAt``, ``supersededBy``, and raw sources
    (those are HQ/history surfaces).
    """
    return {
        "ideaId": record.ideaId,
        "skillBaseName": record.skillBaseName,
        "body": record.body,
        "status": record.status,
        "corroborationCount": _corroboration_count(record),
        "foldedIntoRev": record.foldedIntoRev,
        "authored": record.authored,
        "authorityKind": record.authorityKind,
    }


def _idea_to_all_ideas_dto(record: IdeaRecord) -> dict[str, Any]:
    """Serialise an IdeaRecord to the all-ideas DTO shape.

    Includes every field the HQ dropdown / history view needs, including
    ``invalidAt`` (so the UI can distinguish current vs retired).
    """
    return {
        "ideaId": record.ideaId,
        "skillBaseName": record.skillBaseName,
        "body": record.body,
        "status": record.status,
        "corroborationCount": _corroboration_count(record),
        "foldedIntoRev": record.foldedIntoRev,
        "authored": record.authored,
        "authorityKind": record.authorityKind,
        # History/supersession fields (all-ideas only):
        "invalidAt": record.invalidAt,
        "supersededBy": record.supersededBy,
        "supersedes": record.supersedes,
        "refines": record.refines,
    }


def _corroboration_count(record: IdeaRecord) -> int:
    """Return a best-effort corroboration count for an idea.

    In the PR-gated model the count is the number of distinct merged-PR votes;
    in the legacy recurrence model it was distinct session ids. Both are stored
    as ``IdeaSourceRecord`` rows under the idea.  For the read API we return
    the ``corroborationVersion`` as a proxy (it increments monotonically with
    each corroborating vote and is always present on the IdeaRecord itself —
    no extra store read needed here).

    This matches what the TS ``corroborationCount(idea)`` helper returns for
    ideas migrated through the PR-gated path: corroborationVersion tracks the
    vote sequence.
    """
    return record.corroborationVersion


# ---------------------------------------------------------------------------
# Pure business logic (easily unit-testable, no IO)
# ---------------------------------------------------------------------------

def candidate_learnings(
    ideas: list[IdeaRecord],
    k: int = CORROBORATION_K,
    cap: int = 5,
) -> list[dict[str, Any]]:
    """Return current, corroborated ideas as candidate-learning DTOs.

    Mirrors the TS ``candidateLearnings()`` in ``rest/ideas.ts``:
    - Exclude invalidAt (retired) ideas — current set only.
    - Include only ``open`` ideas (not folded; folded lessons are already in
      the skill body).
    - Gate on corroboration count >= k.
    - Sort strongest-and-freshest-first.
    - Cap at ``cap`` results.
    """
    current_open = [
        r for r in ideas
        if r.invalidAt is None and r.status == "open"
        and _corroboration_count(r) >= k
    ]
    current_open.sort(key=lambda r: _corroboration_count(r), reverse=True)
    return [_idea_to_candidate_dto(r) for r in current_open[:cap]]


def all_ideas(
    ideas: list[IdeaRecord],
    include_history: bool = False,
) -> list[dict[str, Any]]:
    """Return all ideas as all-ideas DTOs.

    Mirrors the TS ``allIdeas()`` in ``rest/ideas.ts``:
    - When ``include_history=False`` (default): exclude retired ideas
      (``invalidAt`` set) — current set only, matching the boundary table
      ("current set only — exclude invalidAt; history queryable").
    - When ``include_history=True``: return ALL ideas (current + retired).
    - Sort strongest-and-freshest-first.
    - No cap (the HQ dropdown shows all).
    """
    if include_history:
        filtered = ideas
    else:
        filtered = [r for r in ideas if r.invalidAt is None]

    filtered = sorted(filtered, key=lambda r: _corroboration_count(r), reverse=True)
    return [_idea_to_all_ideas_dto(r) for r in filtered]


# ---------------------------------------------------------------------------
# Handler helpers
# ---------------------------------------------------------------------------

def _resolve_org_and_skill(event: dict[str, Any]) -> tuple[str, str] | None:
    """Extract (org, skill_name) from the event, or return None on bad request."""
    # Support both Lambda proxy events and the lightweight dict the tests use.
    # Priority: pathParameters > queryStringParameters > headers.
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    headers = event.get("headers") or {}

    skill_name = (
        params.get("name")
        or params.get("skillName")
        or query.get("skillName")
    )
    org = (
        query.get("org")
        or headers.get("x-org")
        or headers.get("X-Org")
    )

    if not skill_name or not org:
        return None
    return org.strip(), skill_name.strip()


def _resolve_store(event: dict[str, Any]) -> LearningStore:
    """Return an injected test store or the production store."""
    test_store = event.get("_test_store")
    if test_store is not None:
        return test_store  # type: ignore[return-value]
    return _get_production_store()


def _get_production_store() -> LearningStore:
    """Return the production DynamoDB-backed LearningStore, or in-memory for local dev."""
    import os
    table_name = os.environ.get("HARNESS_TABLE", "").strip()
    if table_name:
        from learning_service.db.store import DynamoLearningStore
        return DynamoLearningStore(table_name=table_name)
    logger.warning(
        "_get_production_store: HARNESS_TABLE not set — using InMemoryLearningStore. "
        "Safe for local dev only."
    )
    return InMemoryLearningStore()


def _ok(body: dict[str, Any]) -> dict[str, Any]:
    return {"statusCode": 200, "body": json.dumps(body)}


def _error(status: int, message: str) -> dict[str, Any]:
    return {"statusCode": status, "body": json.dumps({"error": message})}


# ---------------------------------------------------------------------------
# Lambda handlers
# ---------------------------------------------------------------------------

def handle_candidate_learnings(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET /skills/:name/candidate-learnings — corroborated current ideas only.

    Returns the current set (excludes invalidAt) of ideas that meet the
    corroboration threshold, ranked strongest-and-freshest-first, capped at 5.
    This is the security-gated surface: only corroborated ideas may surface in
    a working session's context.
    """
    resolved = _resolve_org_and_skill(event)
    if resolved is None:
        return _error(400, "org (query param or X-Org header) and skill name (path param) are required")

    org, skill_name = resolved
    store = _resolve_store(event)

    ideas = store.list_current_ideas(org, skill_name)
    learnings = candidate_learnings(ideas)
    return _ok({"learnings": learnings})


def handle_all_ideas(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET /skills/:name/all-ideas — every idea for a skill (current + optional history).

    Current set by default (excludes invalidAt). Pass ``?history=true`` to
    include retired ideas in the response (HQ / audit surface).
    """
    resolved = _resolve_org_and_skill(event)
    if resolved is None:
        return _error(400, "org (query param or X-Org header) and skill name (path param) are required")

    org, skill_name = resolved
    store = _resolve_store(event)
    query = event.get("queryStringParameters") or {}
    include_history = query.get("history", "").lower() in ("true", "1", "yes")

    # For all-ideas we include both current + retired when history=true;
    # otherwise only current. Use list_current_ideas for the common case
    # (no invalidAt filter needed in memory) and list_all_ideas_for_org +
    # filter for the history path.
    if include_history:
        all_org_ideas = store.list_all_ideas_for_org(org)
        ideas = [r for r in all_org_ideas if r.skillBaseName == skill_name]
    else:
        ideas = store.list_current_ideas(org, skill_name)

    result = all_ideas(ideas, include_history=include_history)
    return _ok({"ideas": result})


def handle_telemetry_metrics(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET /skills/:name/telemetry-metrics — per-org/skill R4 calibration metrics.

    Computes and returns the full R4 telemetry metric set for a skill family.
    This is the dashboard/query surface required by MAT-153 Gap 4.

    The response is the ``compute_org_metrics`` output dict, which includes:
    - Corroboration weight distribution + per-author credibility
    - Distinct-PR distribution
    - Supersede event count + unfold count + revive count
    - Necessity demotion count
    - Authored-vs-inferred ratio

    These metrics are computed live from the store (read-only).  For continuous
    monitoring, point a CloudWatch Dashboard widget at this endpoint or emit
    the same payload via structured logs from the ingest job (which the ``TELEMETRY``
    log line already does).
    """
    resolved = _resolve_org_and_skill(event)
    if resolved is None:
        return _error(400, "org (query param or X-Org header) and skill name (path param) are required")

    org, skill_name = resolved
    store = _resolve_store(event)

    try:
        metrics = compute_org_metrics(org, skill_name, store)
    except Exception as exc:
        logger.exception("compute_org_metrics failed for org=%s skill=%s", org, skill_name)
        return _error(500, f"Failed to compute metrics: {exc}")

    # Emit as a structured log line for CloudWatch Logs Insights / MetricFilter.
    logger.info("TELEMETRY_DASHBOARD %s", json.dumps(metrics))

    return _ok(metrics)


def handle_gate_status(event: dict[str, Any], context: object) -> dict[str, Any]:
    """GET /telemetry/gate-status — current enforce-gate configuration and status.

    Returns the gate status from an empty accumulator by default (shows threshold
    config without needing a live ingest run).  A pre-seeded accumulator can be
    injected via event['_test_accumulator'] for testing.

    This endpoint provides the REST surface for querying whether each mode-flip
    gate is open — callable from a dashboard or monitoring system.
    """
    acc = event.get("_test_accumulator") or TelemetryAccumulator()

    # Allow config overrides from the event (for testing different thresholds).
    _defaults = GateConfig()
    query = event.get("queryStringParameters") or {}
    try:
        fp_ceiling = float(query.get("fp_ceiling", _defaults.supersede_fp_ceiling))
    except (TypeError, ValueError):
        fp_ceiling = _defaults.supersede_fp_ceiling
    try:
        fp_min_samples = int(query.get("fp_min_samples", _defaults.supersede_fp_min_samples))
    except (TypeError, ValueError):
        fp_min_samples = _defaults.supersede_fp_min_samples
    try:
        min_anchor_samples = int(query.get("min_anchor_samples", _defaults.min_anchor_samples))
    except (TypeError, ValueError):
        min_anchor_samples = _defaults.min_anchor_samples

    cfg = GateConfig(
        supersede_fp_ceiling=fp_ceiling,
        supersede_fp_min_samples=fp_min_samples,
        min_anchor_samples=min_anchor_samples,
    )

    snap = acc.snapshot()
    gs = gate_status(snap, config=cfg)

    # Also include the current accumulator metrics so the caller can see what data
    # informed the gate decision.
    payload = {
        "verified_learning_gate_open": gs.verified_learning_gate_open,
        "verified_learning_reason": gs.verified_learning_reason,
        "supersede_gate_open": gs.supersede_gate_open,
        "supersede_reason": gs.supersede_reason,
        "unfold_gate_open": gs.unfold_gate_open,
        "unfold_reason": gs.unfold_reason,
        "config": {
            "supersede_fp_ceiling": cfg.supersede_fp_ceiling,
            "supersede_fp_min_samples": cfg.supersede_fp_min_samples,
            "min_anchor_samples": cfg.min_anchor_samples,
        },
        "accumulator_snapshot": to_json_dict(snap),
    }

    logger.info("GATE_STATUS %s", json.dumps({
        "vl_open": gs.verified_learning_gate_open,
        "sup_open": gs.supersede_gate_open,
        "unfold_open": gs.unfold_gate_open,
    }))

    return _ok(payload)


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """Unified Lambda handler: dispatch on path suffix.

    Routes:
      GET /skills/:name/candidate-learnings  → handle_candidate_learnings
      GET /skills/:name/all-ideas            → handle_all_ideas
      GET /skills/:name/telemetry-metrics    → handle_telemetry_metrics (R4)
      GET /telemetry/gate-status             → handle_gate_status (R4)
    """
    try:
        path = (
            event.get("rawPath")
            or (event.get("requestContext") or {}).get("http", {}).get("path", "")
            or ""
        )
        if path.endswith("/candidate-learnings"):
            return handle_candidate_learnings(event, context)
        if path.endswith("/all-ideas"):
            return handle_all_ideas(event, context)
        if path.endswith("/telemetry-metrics"):
            return handle_telemetry_metrics(event, context)
        if path.endswith("/gate-status") or path.endswith("/telemetry/gate-status"):
            return handle_gate_status(event, context)
        return _error(404, f"no learning read route matched: {path}")
    except Exception as exc:
        logger.exception("Unexpected error in learning_reads handler")
        return _error(500, f"Internal error: {exc}")
