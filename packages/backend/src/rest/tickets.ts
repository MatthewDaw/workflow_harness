import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  isValidTicketTransition,
  ticketSchema,
  ticketStatusSchema,
  type Ticket,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';
import {
  badRequest,
  created,
  defaultRepo,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';

/**
 * REST: tickets (U11) — CRUD + lifecycle transitions, scoped to the owning
 * project's owner.
 *
 *   GET    /projects/:pid/tickets             — the project's board
 *   POST   /projects/:pid/tickets             — create a ticket
 *   GET    /projects/:pid/tickets/:tid        — one ticket
 *   PUT    /projects/:pid/tickets/:tid        — update fields (title/desc/etc.)
 *   POST   /projects/:pid/tickets/:tid/status — transition status (+ links)
 *
 * Every ticket inherits its project's ownership: a non-owner sees 404 (no
 * enumeration). Status transitions are validated against the lifecycle
 * (backlog -> in_progress -> in_review -> done, plus icebox), and the same call
 * attaches a session/branch/PR link as the ticket advances.
 */

export interface TicketsDeps {
  repo: Repo;
}

/** Resolve the project and confirm the caller owns it. Undefined => 404. */
async function ownedProject(repo: Repo, principal: Principal, projectId: string): Promise<boolean> {
  const project = await repo.getProject(projectId);
  return Boolean(project && project.ownerUserId === principal.userId);
}

export async function listTickets(
  event: APIGatewayProxyEventV2,
  deps: TicketsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  if (!pid) return badRequest('missing project id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  const tickets = await deps.repo.listTickets(pid);
  return ok({ tickets });
}

export async function getTicket(
  event: APIGatewayProxyEventV2,
  deps: TicketsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const tid = pathParam(event, 'tid');
  if (!pid || !tid) return badRequest('missing project or ticket id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  const ticket = await deps.repo.getTicket(pid, tid);
  if (!ticket) return notFound();
  return ok({ ticket });
}

export async function createTicket(
  event: APIGatewayProxyEventV2,
  deps: TicketsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  if (!pid) return badRequest('missing project id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = ticketSchema.safeParse({
    status: 'backlog',
    priority: 'medium',
    ...(body as Record<string, unknown>),
    projectId: pid, // project comes from the path, never the body
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  const ticket: Ticket = parsed.data;
  await deps.repo.putTicket(ticket);
  return created({ ticket });
}

export async function updateTicket(
  event: APIGatewayProxyEventV2,
  deps: TicketsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const tid = pathParam(event, 'tid');
  if (!pid || !tid) return badRequest('missing project or ticket id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  const existing = await deps.repo.getTicket(pid, tid);
  if (!existing) return notFound();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  // Merge editable fields; status changes go through the transition endpoint so
  // the lifecycle stays enforced.
  const parsed = ticketSchema.safeParse({
    ...existing,
    ...(body as Record<string, unknown>),
    id: tid,
    projectId: pid,
    status: existing.status,
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  await deps.repo.putTicket(parsed.data);
  return ok({ ticket: parsed.data });
}

/**
 * Transition a ticket's status and attach links in the same call. The body
 * carries `{ to, sessionId?, branch?, pr? }`. An invalid transition (e.g.
 * backlog -> done) is rejected. Moving to `in_progress` typically attaches a
 * `sessionId` (the "start session on ticket" flow); moving to `in_review`
 * attaches a `pr`.
 */
export async function transitionTicket(
  event: APIGatewayProxyEventV2,
  deps: TicketsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const pid = pathParam(event, 'pid');
  const tid = pathParam(event, 'tid');
  if (!pid || !tid) return badRequest('missing project or ticket id');
  if (!(await ownedProject(deps.repo, principal, pid))) return notFound();

  const existing = await deps.repo.getTicket(pid, tid);
  if (!existing) return notFound();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const b = (body ?? {}) as {
    to?: unknown;
    sessionId?: string;
    branch?: string;
    pr?: string;
  };
  const toParsed = ticketStatusSchema.safeParse(b.to);
  if (!toParsed.success) return badRequest('invalid target status');
  const to = toParsed.data;

  if (!isValidTicketTransition(existing.status, to)) {
    return badRequest(`invalid transition ${existing.status} -> ${to}`);
  }

  const updated: Ticket = {
    ...existing,
    status: to,
    sessionId: b.sessionId ?? existing.sessionId,
    branch: b.branch ?? existing.branch,
    pr: b.pr ?? existing.pr,
  };
  await deps.repo.putTicket(updated);
  return ok({ ticket: updated });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: TicketsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const tid = pathParam(event, 'tid');

  if (method === 'POST' && path.endsWith('/status')) return transitionTicket(event, deps);
  if (method === 'POST') return createTicket(event, deps);
  if (method === 'PUT') return updateTicket(event, deps);
  if (method === 'GET' && tid) return getTicket(event, deps);
  return listTickets(event, deps);
}
