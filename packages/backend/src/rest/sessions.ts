import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  defaultRepo,
  notFound,
  ok,
  pathParam,
  principalOf,
  queryParam,
  unauthorized,
} from './runtime.js';

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
}

const DEFAULT_EVENT_PAGE = 100;

export async function listSessions(
  event: APIGatewayProxyEventV2,
  deps: SessionsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const liveOnly = queryParam(event, 'live') === 'true';

  // Live index is cross-project; scope it to the caller. For the full firehose we
  // gather the caller's projects then their sessions.
  let sessions = await deps.repo.listLiveSessions();
  sessions = sessions.filter((s) => s.ownerUserId === principal.userId);

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

  // Live-first, then most-recent activity.
  const isLive = (status: string): number =>
    status === 'active' || status === 'needs_input' ? 1 : 0;
  sessions.sort((a, b) => isLive(b.status) - isLive(a.status) || b.lastEventAt - a.lastEventAt);

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

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SessionsDeps = { repo: defaultRepo() };
  if (pathParam(event, 'id')) return getSession(event, deps);
  return listSessions(event, deps);
}
