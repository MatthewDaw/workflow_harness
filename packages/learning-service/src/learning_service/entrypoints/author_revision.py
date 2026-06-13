"""author_revision — the Python author-revision API endpoint (U10 DRY guardrail).

The TS catalog-authoring UIs (hq-add-skill, web editor, seeding) call this
endpoint instead of writing skill revisions themselves.  This ensures there is
exactly one revision writer (Python), one CAS implementation, and one
IDEAGOLD# capture path.

Lambda handler shape: ``handler(event, context) -> {"statusCode": ..., "body": ...}``

Route: ``POST /internal/skills/:name/revisions``

Request body (JSON):
    {
      "org": "acme",
      "baseName": "my-skill",
      "variantId": "",           // empty = org base
      "body": "...",
      "authorUserId": "...",     // optional
      "description": "...",      // optional
      "ideaId": "...",           // optional (fold path)
      "goldenCase": {            // optional (fold path)
        "caseId": "...",
        "before": "...",
        "after": "...",
        "ideaBody": "..."
      }
    }

Response body (JSON):
    {
      "org": "acme",
      "baseName": "my-skill",
      "variantId": "",
      "rev": 3,
      "truePointerUpdated": true,
      "goldenCaseWritten": false
    }

Errors:
    400  Bad Request — missing required fields.
    409  Conflict   — CAS failed after max retries (caller should retry).
    500  Internal   — unexpected error.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import os

from learning_service.skills_write import (
    ConflictError,
    DynamoSkillStore,
    GoldenCasePayload,
    InMemorySkillStore,
    RevisionRequest,
    write_revision,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """AWS Lambda handler for the author-revision endpoint.

    Thin dispatcher: parse → validate → delegate to :func:`write_revision`.
    The DynamoDB store is injected from the module-level singleton (or the
    test-injected store from ``event['_test_store']`` for offline tests).
    """
    try:
        body = _parse_body(event)
        if isinstance(body, dict) and "error" in body:
            return _error(400, body["error"])

        req = _build_request(body)
        if isinstance(req, dict) and "error" in req:
            return _error(400, req["error"])

        # Dependency injection for tests.
        store = event.get("_test_store") or _get_production_store()

        result = write_revision(req, store)

        return {
            "statusCode": 200,
            "body": json.dumps({
                "org": result.org,
                "baseName": result.base_name,
                "variantId": result.variant_id,
                "rev": result.rev,
                "truePointerUpdated": result.true_pointer_updated,
                "goldenCaseWritten": result.golden_case_written,
            }),
        }

    except ConflictError as exc:
        logger.warning("CAS conflict after max retries: %s", exc)
        return _error(409, f"CAS conflict: {exc}")
    except Exception as exc:
        logger.exception("Unexpected error in author_revision handler")
        return _error(500, f"Internal error: {exc}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body", "{}")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return {"error": f"Invalid JSON body: {exc}"}


def _build_request(body: dict[str, Any]) -> RevisionRequest | dict[str, str]:
    org = body.get("org", "").strip()
    base_name = body.get("baseName", "").strip()
    variant_id = body.get("variantId", "")
    revision_body = body.get("body", "")

    if not org:
        return {"error": "Missing required field: org"}
    if not base_name:
        return {"error": "Missing required field: baseName"}
    if not isinstance(revision_body, str):
        return {"error": "Field 'body' must be a string"}

    golden: GoldenCasePayload | None = None
    gc_raw = body.get("goldenCase")
    if gc_raw and isinstance(gc_raw, dict):
        golden = GoldenCasePayload(
            case_id=gc_raw.get("caseId", ""),
            before=gc_raw.get("before", ""),
            after=gc_raw.get("after", ""),
            idea_body=gc_raw.get("ideaBody", ""),
        )

    return RevisionRequest(
        org=org,
        base_name=base_name,
        variant_id=variant_id,
        body=revision_body,
        author_user_id=body.get("authorUserId"),
        description=body.get("description"),
        golden_case=golden,
        idea_id=body.get("ideaId"),
    )


def _error(status: int, message: str) -> dict[str, Any]:
    return {"statusCode": status, "body": json.dumps({"error": message})}


def _get_production_store():
    """Return the production DynamoDB-backed SkillStore singleton.

    Uses ``DynamoSkillStore`` when ``HARNESS_TABLE`` is set in the environment
    (the Lambda runtime).  Falls back to ``InMemorySkillStore`` ONLY for local
    development runs where no table is configured — never in the deployed Lambda
    (the env var is always set there).

    This is Gap 5 of MAT-141: the author-revision endpoint must talk to the real
    F2 DynamoDB store, not the in-memory stub, in production.
    """
    table_name = os.environ.get("HARNESS_TABLE", "").strip()
    if table_name:
        return DynamoSkillStore(table_name=table_name)
    logger.warning(
        "_get_production_store: HARNESS_TABLE not set — using InMemorySkillStore. "
        "This is only safe for local development; the deployed Lambda always has "
        "HARNESS_TABLE set."
    )
    return InMemorySkillStore()
