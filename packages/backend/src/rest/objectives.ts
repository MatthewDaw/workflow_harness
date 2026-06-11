import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { objectiveNodeSchema, type ObjectiveNode } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { buildTree } from '../projections/rollup.js';
import {
  badRequest,
  created,
  defaultDb,
  defaultRepo,
  forbidden,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';
import { isAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import type { PgDb } from '../db/pg/migrate.js';
import {
  deleteObjective as pgDeleteObjective,
  getObjective as pgGetObjective,
  listObjectives as pgListObjectives,
  putObjective as pgPutObjective,
} from '../db/pg/objectivesRepo.js';

/**
 * REST: objectives (U10) — the org-global RCDO tree with cached roll-ups.
 *
 *   GET    /objectives        — the full tree (nested) with cached %
 *   GET    /objectives/:id    — one node (with its cached roll-up %)
 *   POST   /objectives        — create/update a node (admin only; org-global)
 *   DELETE /objectives/:id    — delete a node (admin only)
 *
 * Objectives are a single company-wide tree (KTD8): not per-user. Reads are
 * available to any authenticated member of the org; writes require the admin
 * claim. The cached `%` on each node is maintained by the roll-up projection
 * (see projections/rollup); this endpoint serves it.
 */

export interface ObjectivesDeps {
  /** Dynamo repo — still used for membership/effective-org resolution. */
  repo: Repo;
  /** Postgres client — objectives now live here (KTD7/U16). */
  db: PgDb;
}

export async function listObjectives(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  // Data follows membership: scope by the effective org (profile.org, claim
  // fallback). An org-less caller has no objectives to show.
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return ok({ tree: buildTree([]), nodes: [] });
  const nodes = await pgListObjectives(deps.db, org);
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

  const org = await effectiveOrg(event, deps.repo);
  if (!org) return notFound();
  const node = await pgGetObjective(deps.db, org, id);
  if (!node) return notFound();

  // The roll-up projection computes the node's cached % org-wide; this view just
  // serves the node. Completion now derives from project progress, not tickets.
  return ok({ node });
}

export async function createObjective(
  event: APIGatewayProxyEventV2,
  deps: ObjectivesDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!isAdmin(event)) return forbidden();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  // The org is always the caller's EFFECTIVE org — never a client-supplied one.
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  const parsed = objectiveNodeSchema.safeParse({
    ...(body as Record<string, unknown>),
    org,
  });
  if (!parsed.success) return badRequest(parsed.error.message);
  const node: ObjectiveNode = parsed.data;
  await pgPutObjective(deps.db, node);
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
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  await pgDeleteObjective(deps.db, org, id);
  return ok({ deleted: true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: ObjectivesDeps = { repo: defaultRepo(), db: defaultDb() };
  const method = event.requestContext.http.method;
  const id = pathParam(event, 'id');
  if (method === 'POST' || method === 'PUT') return createObjective(event, deps);
  if (method === 'DELETE') return deleteObjective(event, deps);
  if (method === 'GET' && id) return getObjective(event, deps);
  return listObjectives(event, deps);
}
