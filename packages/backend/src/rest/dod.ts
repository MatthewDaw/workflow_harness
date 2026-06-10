import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { definitionOfDoneSchema, type DefinitionOfDone } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  defaultRepo,
  forbidden,
  ok,
  parseBodySafe,
  INVALID_JSON,
  principalOf,
  unauthorized,
} from './runtime.js';
import { isAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';

/**
 * REST: org Definition of Done (plan-mapping feature 1).
 *
 *   GET /dod   — the org's Definition of Done (the default floor when unset)
 *   PUT /dod   — set the org's Definition of Done (admin only; org-scoped)
 *
 * The DoD declares what `/update-progress` must verify before marking work
 * complete (unit tests as the org-wide floor; optionally prod-E2E). It is
 * ADVISORY: surfaced in HQ and reported on by the compliance report, but it
 * never hard-blocks a progress push ("conformity never blocks").
 *
 * Reads are available to any authenticated member of the org; writes require the
 * admin claim. The org is always the caller's own org (`principalOf`) — never a
 * client-supplied one — so there is no cross-tenant write.
 */

export interface DodDeps {
  repo: Repo;
}

export async function getDod(
  event: APIGatewayProxyEventV2,
  deps: DodDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const org = await effectiveOrg(event, deps.repo);
  // An org-less caller gets the default floor (getOrgDod defaults when unset).
  if (!org) return ok({ dod: await deps.repo.getOrgDod('') });
  const dod = await deps.repo.getOrgDod(org);
  return ok({ dod });
}

export async function putDod(
  event: APIGatewayProxyEventV2,
  deps: DodDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!isAdmin(event)) return forbidden();

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const parsed = definitionOfDoneSchema.safeParse(body ?? {});
  if (!parsed.success) return badRequest(parsed.error.message);
  const dod: DefinitionOfDone = parsed.data;
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  await deps.repo.putOrgDod(org, dod);
  return ok({ dod });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: DodDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  if (method === 'PUT' || method === 'POST') return putDod(event, deps);
  return getDod(event, deps);
}
