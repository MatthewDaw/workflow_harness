import type { APIGatewayProxyResultV2, APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import { z } from 'zod';
import type { Repo } from '../db/repo.js';
import { ApiGwPoster, defaultRepo, managementEndpoint, type ConnectionPoster } from './runtime.js';

/**
 * `subscribe` route — a web client asks to watch a session's live feed (U7).
 *
 * Flow:
 *  1. Authorize: the requester must own the session (its projection's
 *     `ownerUserId` must match the connection's authenticated user). A
 *     non-owner is denied — no listener registered, no replay.
 *  2. Register the web `connectionId` as a listener so ingestion fans new events
 *     out to it.
 *  3. Replay a recent window of events so the client has immediate context,
 *     then it receives live events as they arrive.
 */

const subscribeBodySchema = z.object({
  action: z.literal('subscribe').optional(),
  sessionId: z.string().min(1),
  /** How many recent events to replay; clamped to a sane ceiling. */
  replay: z.number().int().positive().max(500).optional(),
});

const DEFAULT_REPLAY = 50;

export interface SubscribeDeps {
  repo: Repo;
  poster: ConnectionPoster;
}

export async function subscribe(
  event: APIGatewayProxyWebsocketEventV2,
  deps: SubscribeDeps,
): Promise<APIGatewayProxyResultV2> {
  const connectionId = event.requestContext.connectionId;

  let body: z.infer<typeof subscribeBodySchema>;
  try {
    body = subscribeBodySchema.parse(event.body ? JSON.parse(event.body) : undefined);
  } catch {
    return { statusCode: 400, body: 'invalid subscribe request' };
  }

  const conn = await deps.repo.getConnection(connectionId);
  if (!conn) {
    return { statusCode: 401, body: 'unknown connection' };
  }

  const session = await deps.repo.getSessionById(body.sessionId);
  // 404-style denial (don't distinguish missing from forbidden, to avoid
  // leaking the existence of another user's sessions).
  if (!session || session.ownerUserId !== conn.userId) {
    return { statusCode: 403, body: 'forbidden' };
  }

  // 2. Register as a listener for live fan-out.
  await deps.repo.addListener(body.sessionId, connectionId);

  // 3. Replay a recent window (oldest-first so the client renders in order).
  const window = await deps.repo.listEvents(body.sessionId, {
    limit: body.replay ?? DEFAULT_REPLAY,
    ascending: false,
  });
  const replay = window.reverse();
  await deps.poster.post(connectionId, {
    type: 'replay',
    sessionId: body.sessionId,
    events: replay,
  });

  return { statusCode: 200, body: JSON.stringify({ subscribed: true, replayed: replay.length }) };
}

export const handler = (event: APIGatewayProxyWebsocketEventV2): Promise<APIGatewayProxyResultV2> =>
  subscribe(event, {
    repo: defaultRepo(),
    poster: new ApiGwPoster(managementEndpoint(event)),
  });
