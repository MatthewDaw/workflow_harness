import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { corroborationCount, type Idea } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { badRequest, defaultRepo, ok, pathParam, unauthorized } from './runtime.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';

/**
 * REST: skill ideas — the surfacing read path for the skill-idea loop.
 *
 *   GET /skills/{name}/candidate-learnings — corroborated-only, ranked + capped
 *
 * The endpoint serves the ideas a working session is allowed to see when a skill
 * loads (R16/R17): only ideas corroborated by `>= K` distinct sessions, never
 * folded, ordered strongest-and-freshest-first, capped at `N`. The corroboration
 * gate is a SERVER-SIDE SECURITY BOUNDARY — uncorroborated ideas never leave the
 * backend on this path, so a client can't coax them into a session's context.
 *
 * Org is resolved via `effectiveOrg` (the PROFILE-driven membership, NOT the raw
 * token `principal.org`) so the read is scoped exactly like every other org read
 * and never leaks across orgs. The route is `noAuth` at the gateway and accepts
 * the claude+ wrapper's HS256 device token in-handler (the gateway JWT authorizer
 * can't validate it), mirroring the `/skills` routes.
 */

/**
 * Corroboration threshold: an idea must be backed by at least this many DISTINCT
 * sessions before it may surface in a working session. Documented default; the
 * gate (`corroborationCount(idea) >= CORROBORATION_K`) is enforced server-side.
 */
export const CORROBORATION_K = 2;

/**
 * Cap on how many candidate learnings surface per skill. Honors the
 * context-degradation rationale (native skill selection degrades past ~30–50
 * descriptions in-prompt): we surface only the strongest few so the injected
 * block stays focused. Tunable; default kept small on purpose.
 */
export const CANDIDATE_CAP = 5;

export interface IdeasDeps {
  repo: Repo;
}

/**
 * The corroborated, ranked, capped candidate learnings for one skill.
 * Pure (no IO) so it is trivially unit-testable: filter by gate + status, sort
 * by corroboration desc then recency (updatedAt) desc, then cap.
 */
export function candidateLearnings(
  ideas: Idea[],
  k: number = CORROBORATION_K,
  cap: number = CANDIDATE_CAP,
): Idea[] {
  return ideas
    .filter((i) => i.status === 'open' && corroborationCount(i) >= k)
    .sort((a, b) => {
      const byCorroboration = corroborationCount(b) - corroborationCount(a);
      if (byCorroboration !== 0) return byCorroboration;
      return b.updatedAt - a.updatedAt;
    })
    .slice(0, cap);
}

export async function resolveCandidateLearnings(
  event: APIGatewayProxyEventV2,
  deps: IdeasDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept EITHER the gateway Cognito JWT (HQ web) OR a raw bearer device token
  // (the claude+ wrapper) — this route is HttpNoneAuthorizer, so the gateway does
  // not pre-reject the device token; we verify it here.
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();

  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  // Scope by the PROFILE-driven effective org, NOT the raw token org, so the read
  // is isolated exactly like every other org read. A missing org returns empty
  // (never defaults), so a malformed principal can't reach another org's ideas.
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ learnings: [] });

  const ideas = await deps.repo.listIdeasForSkill(org, name);
  return ok({ learnings: candidateLearnings(ideas) });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: IdeasDeps = { repo: defaultRepo() };
  return resolveCandidateLearnings(event, deps);
}
