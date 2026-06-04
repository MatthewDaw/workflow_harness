import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { controlActionSchema } from '@harness/shared';
import { z } from 'zod';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  defaultRepo,
  json,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  queryParam,
  unauthorized,
} from './runtime.js';
import { ApiGwPoster, type ConnectionPoster } from '../ws/runtime.js';

/**
 * REST: sessions (U8).
 *
 *   GET /sessions?live=true   — the caller's sessions (firehose), live-first
 *   GET /sessions/:id         — one session's projection + a page of its events
 *
 * Scoping: every session row is filtered to the caller's `uid` (the projection
 * carries `ownerUserId`). `?live=true` returns only active/needs_input sessions.
 * A session owned by another user is 404 (no enumeration).
 */

export interface SessionsDeps {
  repo: Repo;
  /** Posts the routed control frame to the owning daemon's connection. */
  poster?: ConnectionPoster;
}

const DEFAULT_EVENT_PAGE = 100;

/**
 * Read-time freshness window. A session whose status is active/needs_input but
 * whose last event (including periodic `session.heartbeat` pings) is older than
 * this is treated as NOT live — its daemon has gone silent (e.g. the laptop lost
 * power and died without sending a "done"). A genuinely-alive idle session keeps
 * heartbeating (~every 20s), so it stays well inside this window. This makes
 * powered-off laptops disappear from the live view within ~60s without a reaper.
 */
export const STALE_WINDOW_MS = 60_000;

/**
 * Whether a session is live RIGHT NOW: it must be in a live status
 * (active/needs_input) AND have produced an event within the stale window.
 */
export function isSessionLive(
  session: { status: string; lastEventAt: number },
  now: number,
): boolean {
  const liveStatus = session.status === 'active' || session.status === 'needs_input';
  return liveStatus && now - session.lastEventAt <= STALE_WINDOW_MS;
}

/**
 * Body of POST /sessions/{id}/control. The sessionId is the path param; the body
 * carries the ControlAction + payload (matching the web's `sendControl`, which
 * posts `{ action, payload }`).
 */
const controlBodySchema = z.object({
  action: controlActionSchema,
  payload: z.object({ text: z.string() }).partial().default({}),
});

export async function listSessions(
  event: APIGatewayProxyEventV2,
  deps: SessionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const liveOnly = queryParam(event, 'live') === 'true';
  const now = Date.now();

  // Live index is cross-project; scope it to the caller. For the full firehose we
  // gather the caller's projects then their sessions.
  let sessions = await deps.repo.listLiveSessions();
  sessions = sessions.filter((s) => s.ownerUserId === principal.userId);

  if (liveOnly) {
    // Read-time freshness: a session whose status is live but whose last event is
    // older than the stale window has a dead/silent daemon (e.g. powered-off
    // laptop). Drop it so it disappears from the live view within ~60s — no
    // reaper, no extra write path.
    sessions = sessions.filter((s) => isSessionLive(s, now));
  }

  if (!liveOnly) {
    const projects = await deps.repo.listProjectsForUser(principal.userId);
    const perProject = await Promise.all(
      projects.map((p) => deps.repo.listSessionsForProject(p.id)),
    );
    const byId = new Map(sessions.map((s) => [s.sessionId, s]));
    for (const s of perProject.flat()) {
      if (s.ownerUserId && s.ownerUserId !== principal.userId) continue;
      if (!byId.has(s.sessionId)) byId.set(s.sessionId, s);
    }
    sessions = [...byId.values()];
  }

  // Live-first, then most-recent activity. A stale active session (silent daemon)
  // is treated as not-live here too, so it sorts below genuinely-live sessions.
  const liveRank = (s: { status: string; lastEventAt: number }): number =>
    isSessionLive(s, now) ? 1 : 0;
  sessions.sort((a, b) => liveRank(b) - liveRank(a) || b.lastEventAt - a.lastEventAt);

  return ok({ sessions });
}

export async function getSession(
  event: APIGatewayProxyEventV2,
  deps: SessionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing session id');

  const session = await deps.repo.getSessionById(id);
  if (!session || session.ownerUserId !== principal.userId) return notFound();

  const limitParam = queryParam(event, 'limit');
  const limit = limitParam ? Number(limitParam) : DEFAULT_EVENT_PAGE;
  const events = await deps.repo.listEvents(id, {
    ascending: true,
    limit: Number.isFinite(limit) && limit > 0 ? limit : DEFAULT_EVENT_PAGE,
  });

  return ok({ session, events });
}

/**
 * POST /sessions/{id}/control (H1) — steer a live session from HQ web over REST.
 *
 * Security mirrors the WS control gateway exactly: authorize that the caller
 * OWNS the target session (repo.getSession by id) BEFORE any routing, then
 * resolve the owning daemon's connectionId via the `instanceId` reverse index
 * and post the ControlAction frame to it. A non-owner / missing session is 404
 * (no enumeration). Returns 202 once the frame is accepted for delivery.
 */
export async function control(
  event: APIGatewayProxyEventV2,
  deps: SessionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing session id');

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = controlBodySchema.safeParse(body);
  if (!parsed.success) return badRequest(parsed.error.message);

  // Authorize ownership BEFORE any routing. Not-owner and missing are both 404.
  const session = await deps.repo.getSessionById(id);
  if (!session || session.ownerUserId !== principal.userId) return notFound();

  // Resolve the owning daemon connection via the instance reverse index.
  const instanceId = session.instanceId;
  const daemonConnId = instanceId
    ? await deps.repo.getInstanceConnectionId(instanceId)
    : undefined;
  if (!daemonConnId) {
    // Owner authorized, but the daemon is not currently connected.
    return json(502, { error: 'daemon offline' });
  }

  const poster = deps.poster ?? new ApiGwPoster(controlEndpoint());
  const delivered = await poster.post(daemonConnId, {
    type: 'control',
    sessionId: id,
    action: parsed.data.action,
    payload: parsed.data.payload,
  });
  if (!delivered) return json(502, { error: 'daemon offline' });

  return json(202, { delivered: true });
}

/** The WS management API endpoint for posting control frames (infra sets it). */
function controlEndpoint(): string {
  const url = process.env.WS_CALLBACK_URL;
  if (!url) throw new Error('WS_CALLBACK_URL is not set');
  return url;
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SessionsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  if (method === 'POST' && path.endsWith('/control')) return control(event, deps);
  if (pathParam(event, 'id')) return getSession(event, deps);
  return listSessions(event, deps);
}
