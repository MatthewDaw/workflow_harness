import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import type { Repo } from '../db/repo.js';
import { defaultRepo } from './runtime.js';

/**
 * `$disconnect` for the WebSocket API.
 *
 * Removes the connection from the registry. For a daemon connection this also
 * clears the `instanceId -> connectionId` reverse index (handled inside
 * `Repo.deleteConnection`), which is exactly what "mark the instance offline"
 * means here: with no current connection, the control gateway can no longer
 * route to it and surfaces the daemon as offline. Idempotent — a disconnect for
 * an already-removed connection is a no-op.
 */

export interface DisconnectDeps {
  repo: Repo;
}

export async function disconnect(
  event: APIGatewayProxyWebsocketEventV2,
  deps: DisconnectDeps = { repo: defaultRepo() },
): Promise<APIGatewayProxyResultV2> {
  const connectionId = event.requestContext.connectionId;
  await deps.repo.deleteConnection(connectionId);
  return { statusCode: 200, body: 'disconnected' };
}

export const handler = (event: APIGatewayProxyWebsocketEventV2): Promise<APIGatewayProxyResultV2> =>
  disconnect(event);
