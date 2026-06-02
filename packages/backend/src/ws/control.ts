import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import { controlFrameSchema, type ControlFrame } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { ApiGwPoster, defaultRepo, managementEndpoint, type ConnectionPoster } from './runtime.js';

/**
 * `control` route — steer a live session from HQ web (U7).
 *
 * Security is load-bearing here (plan R1): every control frame re-checks that
 * the requesting connection's authenticated user OWNS the target session before
 * any routing happens. A non-owner is denied with 403 and nothing is sent to any
 * daemon.
 *
 * On success we resolve the owning daemon's connectionId (via the session's
 * `instanceId` reverse index) and post the frame to it with
 * `PostToConnection`. The daemon's control receiver (U15) applies it: `inject`
 * writes to the session PTY stdin, `pause`/`interrupt` map to signals. An
 * offline daemon (no current connection, or a 410 on post) surfaces as an error
 * to the caller rather than silently dropping.
 */

export interface ControlDeps {
  repo: Repo;
  poster: ConnectionPoster;
}

export async function control(
  event: APIGatewayProxyWebsocketEventV2,
  deps: ControlDeps,
): Promise<APIGatewayProxyResultV2> {
  const connectionId = event.requestContext.connectionId;

  let frame: ControlFrame;
  try {
    frame = controlFrameSchema.parse(event.body ? JSON.parse(event.body) : undefined);
  } catch {
    return { statusCode: 400, body: 'invalid control frame' };
  }

  const conn = await deps.repo.getConnection(connectionId);
  if (!conn) {
    return { statusCode: 401, body: 'unknown connection' };
  }

  // Authorize ownership BEFORE any routing. Treat unknown + not-owned alike.
  const session = await deps.repo.getSessionById(frame.sessionId);
  if (!session || session.ownerUserId !== conn.userId) {
    return { statusCode: 403, body: 'forbidden' };
  }

  // Resolve the owning daemon connection.
  const instanceId = session.instanceId;
  const daemonConnId = instanceId ? await deps.repo.getInstanceConnectionId(instanceId) : undefined;
  if (!daemonConnId) {
    // Owner authorized, but the daemon is not currently connected.
    return { statusCode: 502, body: JSON.stringify({ error: 'daemon offline' }) };
  }

  const delivered = await deps.poster.post(daemonConnId, {
    type: 'control',
    sessionId: frame.sessionId,
    action: frame.action,
    payload: frame.payload,
  });
  if (!delivered) {
    // The daemon connection went away between lookup and post.
    return { statusCode: 502, body: JSON.stringify({ error: 'daemon offline' }) };
  }

  return { statusCode: 200, body: JSON.stringify({ delivered: true }) };
}

export const handler = (event: APIGatewayProxyWebsocketEventV2): Promise<APIGatewayProxyResultV2> =>
  control(event, {
    repo: defaultRepo(),
    poster: new ApiGwPoster(managementEndpoint(event)),
  });
