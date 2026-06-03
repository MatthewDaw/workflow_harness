import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import { parseEnvelope, type Envelope } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { applyEvent } from './projection.js';
import { ApiGwPoster, defaultRepo, managementEndpoint, type ConnectionPoster } from './runtime.js';

/**
 * `event` route — event ingestion from a daemon (U6).
 *
 * Steps, all driven by the validated envelope:
 *  1. Validate the envelope (`parseEnvelope` from @harness/shared). An invalid
 *     envelope is rejected with a logged reason; the connection is NOT dropped
 *     (a single bad frame must not kill a daemon's stream).
 *  2. Append the event idempotently (`Repo.appendEvent` dedupes on (session, seq)).
 *  3. Fold the event into the session current-state projection and upsert it.
 *  4. Fan the event out to any web clients subscribed to this session (U7).
 *
 * The connection's stored principal/instance (from `$connect`) is attached to a
 * newly-created projection so the control gateway can later authorize ownership
 * and route to the owning daemon.
 */

export interface EventDeps {
  repo: Repo;
  poster: ConnectionPoster;
}

export async function ingest(
  event: APIGatewayProxyWebsocketEventV2,
  deps: EventDeps,
): Promise<APIGatewayProxyResultV2> {
  const connectionId = event.requestContext.connectionId;

  let envelope: Envelope;
  try {
    const raw = event.body ? JSON.parse(event.body) : undefined;
    envelope = parseEnvelope(raw);
  } catch (err) {
    // Reject but keep the connection: log the reason for observability.
    console.warn('ingest: invalid envelope rejected', {
      connectionId,
      reason: (err as Error).message,
    });
    return { statusCode: 400, body: 'invalid envelope' };
  }

  const conn = await deps.repo.getConnection(connectionId);

  // 2. Append (idempotent on duplicate seq).
  const { stored } = await deps.repo.appendEvent(envelope);

  // 3. Update the projection atomically (U6 lost-update fix). A naive
  //    read-modify-write loses concurrent updates when two events for the same
  //    session interleave; instead we read, fold, and conditionally write,
  //    retrying on a conflict by re-reading the latest state and re-folding.
  //    applyEvent guards the latest-activity fields against regression by seq,
  //    so a replayed/duplicate event remains a no-op.
  const sessionId = envelope.event.sessionId;
  const MAX_ATTEMPTS = 5;
  for (let attempt = 0; attempt < MAX_ATTEMPTS; attempt++) {
    const existing = await deps.repo.getSessionById(sessionId);
    const projection = applyEvent(existing, envelope, {
      userId: conn?.userId ?? existing?.ownerUserId ?? 'unknown',
      instanceId: conn?.instanceId ?? envelope.instanceId,
    });
    const { written } = await deps.repo.putSessionProjectionConditional(
      projection,
      existing?.maxSeq,
    );
    if (written) break;
    // A concurrent writer advanced the projection; re-read and re-fold.
  }

  // 4. Fan-out to subscribers (best-effort; prune dead listeners).
  const listeners = await deps.repo.listListeners(sessionId);
  await Promise.all(
    listeners.map(async (listenerId) => {
      const alive = await deps.poster.post(listenerId, { type: 'event', envelope });
      if (!alive) await deps.repo.removeListener(sessionId, listenerId);
    }),
  );

  return { statusCode: 200, body: JSON.stringify({ stored }) };
}

export const handler = (event: APIGatewayProxyWebsocketEventV2): Promise<APIGatewayProxyResultV2> =>
  ingest(event, {
    repo: defaultRepo(),
    poster: new ApiGwPoster(managementEndpoint(event)),
  });
