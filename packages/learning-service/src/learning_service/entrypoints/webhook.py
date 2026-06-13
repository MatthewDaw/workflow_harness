"""Webhook receiver stub — deferred continuous-mode trigger (not v1).

This module is the future home of the ``pull_request`` webhook handler
(action:closed + merged:true + base.ref == default branch).  It is scaffolded
now so the service image and import graph are complete; the implementation is
a deferred follow-on that adds a public endpoint + HMAC verification without
changing the core ingest loop.

v1 trigger: :mod:`learning_service.entrypoints.ingest` (user-triggered replay).
Continuous trigger (this file): same :func:`merge_handler` behind a GitHub webhook.

When implemented, the handler flow is:
    1. Parse the raw ``pull_request`` event payload (JSON body).
    2. HMAC-SHA256 verify against the ``X-Hub-Signature-256`` header
       (constant-time compare, body-size cap before hashing — DoS guard).
    3. Filter: action == "closed" AND merged == True AND base.ref == default branch.
    4. Deduplicate via ``X-GitHub-Delivery`` header (short-TTL cache) layered on
       the ``PROCESSED#`` cursor (HTTP-layer replay vs. business idempotency).
    5. Route to the same merge handler used by the ingest job.

Security notes (for the implementer):
    - Use ``hmac.compare_digest`` (constant-time) per GitHub docs.
    - Cap body size *before* hashing (block gigantic payloads before HMAC).
    - The model Lambda must NOT sit on GitHub's 10-s delivery clock; a thin ack
      Lambda (no ML deps) HMAC-verifies, enqueues to SQS, returns 202 — the
      model Lambda consumes from SQS (see plan §Language & service boundary).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Lambda handler signature (AWS Lambda / container Lambda).
# event: dict from SQS or API Gateway; context: LambdaContext.
def handler(event: dict, context: object) -> dict:
    """AWS Lambda handler — deferred continuous-mode webhook receiver.

    Stub: logs the event type and returns a 200 OK to satisfy Lambda health
    checks.  Replace with the full HMAC-verify + merge-handler wiring when
    the continuous-mode webhook is implemented.
    """
    logger.info("webhook stub received event: source=%s", event.get("source", "unknown"))
    return {"statusCode": 200, "body": "webhook stub — not yet implemented"}
