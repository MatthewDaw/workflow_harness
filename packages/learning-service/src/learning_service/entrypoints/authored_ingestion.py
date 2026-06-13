"""authored_ingestion.py — Lambda handler for U8: authored ingestion endpoint.

Route: ``POST /internal/authored``

Dispatches on ``kind``:
  - ``"directive"`` → :func:`ingest_directive`
  - ``"paste"``     → :func:`ingest_pasted_text`
  - ``"delete"``    → :func:`delete_authored_source`
  - ``"remember"``  → :func:`remember_tool` (agent-native parity)

Request body (JSON):
    {
      "kind": "directive" | "paste" | "delete" | "remember",
      "org": "acme",
      "projectId": "proj-123",
      "userId": "user-456",
      "name": "no-dashes",        // MEM# entry name
      "content": "...",           // text (directive/paste/remember)
      "skillBaseName": "my-skill",
      "scopeTag": "project"       // optional
    }

Response body (JSON):
    {
      "sourceName": "no-dashes",
      "kind": "directive",
      "nodesWritten": 1,
      "nodesDeduped": 0,
      "supersessions": []
    }

Errors:
    400  Bad Request — missing required fields or unknown kind.
    500  Internal   — unexpected error.

Production wiring
-----------------
In production (HARNESS_TABLE set):
  - ``store``     = ``DynamoLearningStore``  (real DynamoDB)
  - ``mem_store`` = ``DynamoMemKvStore``     (real DynamoDB, same table, MEM# keys)
  - ``supersede_fn`` = ``make_supersede_fn(store)`` → routes through the real
    ``supersession.supersede(req: SupersedeRequest, store, ...)`` (Gap 1+2).
  - ``nli_classify_fn`` = ``nli.classify`` from the in-process NLI model (Gap 1).

Test injection
--------------
The store is injected from event['_test_store'] / event['_test_mem_store'] for
offline tests; production wires from HARNESS_TABLE env.  Tests may also inject
``_test_supersede_fn`` and ``_test_nli_classify_fn`` to override the NLI/supersede
callables without loading the real model.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from learning_service.authored import (
    DynamoMemKvStore,
    MemKvStore,
    delete_authored_source,
    ingest_directive,
    ingest_pasted_text,
    make_supersede_fn,
    remember_tool,
)
from learning_service.telemetry import TelemetryAccumulator, to_json_dict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: object) -> dict[str, Any]:
    """AWS Lambda handler for the authored-ingestion endpoint."""
    try:
        body = _parse_body(event)
        if "error" in body:
            return _error(400, body["error"])

        kind = body.get("kind", "").strip()
        if kind not in ("directive", "paste", "delete", "remember"):
            return _error(400, f"Unknown kind: {kind!r}. Expected directive|paste|delete|remember.")

        org = body.get("org", "").strip()
        project_id = body.get("projectId", "").strip()
        user_id = body.get("userId", "").strip()
        name = body.get("name", "").strip()
        content = body.get("content", "")
        skill_base_name = body.get("skillBaseName", "").strip()
        scope_tag = body.get("scopeTag", "project")

        # Validate required fields.
        for field_name, val in [
            ("org", org),
            ("projectId", project_id),
            ("userId", user_id),
            ("name", name),
            ("skillBaseName", skill_base_name),
        ]:
            if not val:
                return _error(400, f"Missing required field: {field_name}")

        # --- Dependency injection -------------------------------------------
        # In tests: inject via event keys to avoid loading real AWS/NLI deps.
        # In production: wire from environment / real services.

        store = event.get("_test_store") or _get_production_learning_store()

        # MEM# KV store: DynamoMemKvStore in production (real DynamoDB, same
        # table as the TS memories.ts path), MemKvStore in tests.
        raw_mem = event.get("_test_mem_store")
        if raw_mem is not None:
            mem_store = raw_mem
        else:
            mem_store = _get_production_mem_store()

        # NLI classify callable: real NLI model in production, injectable in tests.
        nli_classify_fn = event.get("_test_nli_classify_fn") or _get_production_nli_fn()

        # Supersede callable: routes through the REAL supersession.supersede()
        # in production (Gap 1 + 2), injectable in tests.
        supersede_fn = event.get("_test_supersede_fn") or make_supersede_fn(store)

        # R4: one accumulator per invocation — records all telemetry for this request.
        telemetry: TelemetryAccumulator = event.get("_test_telemetry") or TelemetryAccumulator()

        if kind == "delete":
            un_bridged = delete_authored_source(
                org=org,
                project_id=project_id,
                user_id=user_id,
                source_name=name,
                skill_base_name=skill_base_name,
                store=store,
                mem_store=mem_store,
            )
            return {
                "statusCode": 200,
                "body": json.dumps({
                    "sourceName": name,
                    "kind": "delete",
                    "unBridged": un_bridged,
                }),
            }

        # Dispatch: paste uses different param names (source_name / text vs name / content).
        if kind == "paste":
            result = ingest_pasted_text(
                org=org,
                project_id=project_id,
                user_id=user_id,
                source_name=name,
                text=content,
                skill_base_name=skill_base_name,
                store=store,
                mem_store=mem_store,
                scope_tag=scope_tag,
                nli_classify_fn=nli_classify_fn,
                supersede_fn=supersede_fn,
                telemetry=telemetry,
            )
        else:
            # directive / remember share the same signature.
            dispatch = {
                "directive": ingest_directive,
                "remember": remember_tool,
            }
            fn = dispatch[kind]
            result = fn(
                org=org,
                project_id=project_id,
                user_id=user_id,
                name=name,
                content=content,
                skill_base_name=skill_base_name,
                store=store,
                mem_store=mem_store,
                scope_tag=scope_tag,
                nli_classify_fn=nli_classify_fn,
                supersede_fn=supersede_fn,
                telemetry=telemetry,
            )

        # R4: emit accumulated telemetry as a structured log line.
        snap = telemetry.snapshot()
        payload = to_json_dict(snap)
        payload["org"] = org
        payload["skill_base_name"] = skill_base_name
        payload["kind"] = kind
        logger.info("TELEMETRY %s", json.dumps(payload))

        return {
            "statusCode": 200,
            "body": json.dumps({
                "sourceName": result.source_name,
                "kind": result.kind,
                "nodesWritten": len(result.nodes_written),
                "nodesDeduped": result.nodes_deduped,
                "supersessions": result.supersessions,
            }),
        }

    except Exception as exc:
        logger.exception("Unexpected error in authored_ingestion handler")
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


def _error(status: int, message: str) -> dict[str, Any]:
    return {"statusCode": status, "body": json.dumps({"error": message})}


def _get_production_learning_store():
    """Return the production DynamoDB-backed LearningStore singleton."""
    table_name = os.environ.get("HARNESS_TABLE", "").strip()
    if table_name:
        from learning_service.db.store import DynamoLearningStore
        return DynamoLearningStore(table_name=table_name)
    logger.warning(
        "_get_production_learning_store: HARNESS_TABLE not set — using InMemoryLearningStore. "
        "This is only safe for local development."
    )
    from learning_service.db.store import InMemoryLearningStore
    return InMemoryLearningStore()


def _get_production_mem_store():
    """Return the production DynamoMemKvStore (real MEM# DynamoDB rows).

    Uses the same HARNESS_TABLE as the learning store.  When the env var is
    not set (local dev), falls back to the in-memory MemKvStore so the handler
    is still runnable without DynamoDB.
    """
    table_name = os.environ.get("HARNESS_TABLE", "").strip()
    if table_name:
        return DynamoMemKvStore(table_name=table_name)
    logger.warning(
        "_get_production_mem_store: HARNESS_TABLE not set — using in-memory MemKvStore. "
        "This is only safe for local development."
    )
    return MemKvStore()


def _get_production_nli_fn():
    """Return the NLI classify callable.

    In production the NLI model is loaded in-process (reuses nli.py / the
    store-free agent-families core).  We attempt to load it lazily; if the
    model isn't bundled (e.g. local dev without the model artifact) we log a
    warning and return None so the semantic scan is skipped — that is the
    correct degradation (the scan is best-effort; the directive is still
    persisted and bridged).
    """
    try:
        from learning_service.nli import classify
        return classify
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "_get_production_nli_fn: could not load NLI classify (%s) — "
            "authored semantic scan disabled (directives still persisted).",
            exc,
        )
        return None
