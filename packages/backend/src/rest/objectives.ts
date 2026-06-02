import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { objectiveNodeSchema, type ObjectiveNode } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { buildTree } from '../projections/rollup.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';
import { isAdmin } from './scopeauth.js';

/**
 * REST: objectives (U10) — the org-global RCDO tree with cached roll-ups.
 *
 *   GET    /objectives        — the full tree (nested) with cached %
 *   GET    /objectives/:id    — one node + its linked tickets/weekly items
 *   POST   /objectives        — create/update a node (admin only; org-global)
 *   DELETE /objectives/:id    — delete a node (admin only)
 *
 * Objectives are a single company-wide tree (KTD8): not per-user. Reads are
 * available to any authenticated member of the org; writes require the admin
 * claim. The cached `%` on each node is maintained by the roll-up projection
 * (see projections/rollup); this endpoint serves it.
 */

export interface ObjectivesDeps {
  repo: Repo;
}

export async function listObjectives(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const nodes = await deps.repo.listObjectives(principal.org);
  return ok({ tree: buildTree(nodes), nodes });
}

export async function getObjective(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing objective id');

  const node = await deps.repo.getObjective(principal.org, id);
  if (!node) return notFound();

  // Linked work for the node is gathered by the caller's projects (the work the
  // caller can see). The roll-up projection computes % org-wide; this view is the
  // owner's slice of the linked tickets/weekly items.
  const projects = await deps.repo.listProjectsForUser(principal.userId);
  const ticketsPerProject = await Promise.all(projects.map((p) => deps.repo.listTickets(p.id)));
  const linkedTickets = ticketsPerProject.flat().filter((t) => t.objectiveId === id);

  return ok({ node, linkedTickets });
}

export async function createObjective(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!isAdmin(event)) return forbidden();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  // The org is always the caller's org — never a client-supplied one.
  const parsed = objectiveNodeSchema.safeParse({
    ...(body as Record<string, unknown>),
    org: principal.org,
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  const node: ObjectiveNode = parsed.data;
  await deps.repo.putObjective(node);
  return created({ node });
}

export async function deleteObjective(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!isAdmin(event)) return forbidden();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing objective id');
  await deps.repo.deleteObjective(principal.org, id);
  return ok({ deleted: true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: ObjectivesDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const id = pathParam(event, 'id');
  if (method === 'POST' || method === 'PUT') return createObjective(event, deps);
  if (method === 'DELETE') return deleteObjective(event, deps);
  if (method === 'GET' && id) return getObjective(event, deps);
  return listObjectives(event, deps);
}
