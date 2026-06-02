import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import { verifyDeviceToken, type Principal } from '../auth/verify.js';
import type { Repo } from '../db/repo.js';
import { defaultRepo } from './runtime.js';

/**
 * `$connect` for the WebSocket API.
 *
 * Both daemons (event ingestion) and web clients (live watch/steer) open a
 * connection here. The handshake carries the credential and connection metadata
 * on the query string (browsers and the daemon both lack the ability to set
 * arbitrary headers on a WS upgrade):
 *
 *   ?token=<device-token>&instanceId=<id>&role=daemon|web
 *
 * We verify the device token (U4 `auth/verify`) to resolve the principal, then
 * store `connectionId -> {uid, org, instanceId, role}` in the registry so later
 * frames (event/subscribe/control) and `$disconnect` can resolve identity and
 * routing without re-authenticating. A daemon connection also writes the reverse
 * `instanceId -> connectionId` index used by the control gateway.
 */

export interface ConnectDeps {
  repo: Repo;
  verify: (token: string) => Promise<Principal>;
  now: () => number;
}

function defaultDeps(): ConnectDeps {
  return { repo: defaultRepo(), verify: (t) => verifyDeviceToken(t), now: Date.now };
}

export async function connect(
  event: APIGatewayProxyWebsocketEventV2,
  deps: ConnectDeps = defaultDeps(),
): Promise<APIGatewayProxyResultV2> {
  const connectionId = event.requestContext.connectionId;
  const qs = (event as { queryStringParameters?: Record<string, string | undefined> })
    .queryStringParameters;
  const token = qs?.token;
  if (!token) {
    return { statusCode: 401, body: 'missing token' };
  }

  let principal: Principal;
  try {
    principal = await deps.verify(token);
  } catch {
    // Deny the upgrade on a bad/expired/forged token.
    return { statusCode: 401, body: 'unauthorized' };
  }

  const role = qs?.role === 'web' ? 'web' : 'daemon';
  const instanceId = role === 'daemon' ? qs?.instanceId : undefined;

  await deps.repo.putConnection({
    connectionId,
    userId: principal.userId,
    org: principal.org,
    instanceId,
    role,
    connectedAt: deps.now(),
  });

  return { statusCode: 200, body: 'connected' };
}

export const handler = (event: APIGatewayProxyWebsocketEventV2): Promise<APIGatewayProxyResultV2> =>
  connect(event);
