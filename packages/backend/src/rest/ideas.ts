import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { corroborationCount, type Idea, type UnassignedEntry } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { CORROBORATION_K } from '../ideas/corroborate.js';
import { badRequest, defaultRepo, forbidden, ok, pathParam, unauthorized } from './runtime.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';
import { resolveOrgCatalogAuth } from './scopeauth.js';

/**
 * REST: skill ideas — the read paths for the skill-idea loop.
 *
 *   GET  /skills/{name}/candidate-learnings — corroborated-only, ranked + capped (U11)
 *   GET  /skills/{name}/ideas               — EVERY idea (Command HQ dropdown) (U13)
 *   GET  /ideas/unassigned                  — the org's unassigned bin (U15)
 *   POST /ideas/unassigned/{id}/promote-to-skill — admin-only action stub (U15)
 *
 * The candidate-learnings path serves the ideas a working session is allowed to
 * see when a skill loads (R16/R17): only ideas corroborated by `>= K` distinct
 * sessions, never folded, ordered strongest-and-freshest-first, capped at `N`. The
 * corroboration gate is a SERVER-SIDE SECURITY BOUNDARY — uncorroborated ideas
 * never leave the backend on this path, so a client can't coax them into a
 * session's context.
 *
 * The all-ideas path (U13/R18) is the Command HQ surface: it returns EVERY idea
 * for the skill — corroborated, uncorroborated, and folded history alike — each
 * decorated with its derived corroboration count, status, provenance, and
 * `foldedIntoRev`. It has no gate, because nothing here is injected into a working
 * session; it is a read for a human deciding what to fold.
 *
 * Org is resolved via `effectiveOrg` (the PROFILE-driven membership, NOT the raw
 * token `principal.org`) so the reads are scoped exactly like every other org read
 * and never leak across orgs. Both routes are `noAuth` at the gateway and accept
 * the claude+ wrapper's HS256 device token in-handler (the gateway JWT authorizer
 * can't validate it), mirroring the `/skills` routes.
 */

/**
 * Corroboration threshold: an idea must be backed by at least this many DISTINCT
 * sessions before it may surface in a working session. The gate
 * (`corroborationCount(idea) >= CORROBORATION_K`) is enforced server-side.
 * Shared with the corroboration pipeline so the write-side count and this
 * read-side gate can never disagree.
 */
export { CORROBORATION_K };

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

/**
 * An idea decorated with its DERIVED corroboration count for the Command HQ
 * surface. The count is not stored on the idea (it is `|distinct sessionId|` over
 * `sources`); we materialize it here so the dropdown does not have to recompute,
 * while still carrying the full idea (status, `foldedIntoRev`, `sources`
 * provenance) for the history view.
 */
export type IdeaWithCorroboration = Idea & { corroborationCount: number };

/**
 * EVERY idea for a skill, for the HQ dropdown (U13/R18): corroborated,
 * uncorroborated, and folded history — no gate, no cap. Pure (no IO): decorate
 * each idea with its corroboration count and order strongest-and-freshest-first,
 * exactly like candidate-learnings so the surfaced order is consistent.
 */
export function allIdeas(ideas: Idea[]): IdeaWithCorroboration[] {
  return ideas
    .map((i) => ({ ...i, corroborationCount: corroborationCount(i) }))
    .sort((a, b) => {
      const byCorroboration = b.corroborationCount - a.corroborationCount;
      if (byCorroboration !== 0) return byCorroboration;
      return b.updatedAt - a.updatedAt;
    });
}

export async function resolveSkillIdeas(
  event: APIGatewayProxyEventV2,
  deps: IdeasDeps,
): Promise<APIGatewayProxyResultV2> {
  // Same auth + org-scoping contract as candidate-learnings above.
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();

  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ ideas: [] });

  const ideas = await deps.repo.listIdeasForSkill(org, name);
  return ok({ ideas: allIdeas(ideas) });
}

/**
 * A bin entry decorated with its DERIVED frequency for the Command HQ backlog
 * view (U15). Frequency is the same unit as idea corroboration — `|distinct
 * sessionId|` over the entry's `sources` — so a topic that the judge rejected
 * from every skill but keeps recurring across sessions surfaces as a strong
 * new-skill candidate. We materialize it here so the bin screen never has to
 * recompute it, while still carrying the full entry (topic `text`, `sources`
 * provenance) for the read.
 */
export type UnassignedEntryWithFrequency = UnassignedEntry & { frequency: number };

/**
 * The org's unassigned bin (U15/R6/R7): topics the judge rejected from every
 * candidate skill — the new-skill backlog. Pure (no IO): decorate each entry
 * with its frequency (distinct sessions) and order most-frequent-then-freshest
 * first so recurring off-catalog topics float to the top.
 */
export function unassignedBin(entries: UnassignedEntry[]): UnassignedEntryWithFrequency[] {
  return entries
    .map((e) => ({ ...e, frequency: corroborationCount(e) }))
    .sort((a, b) => {
      const byFrequency = b.frequency - a.frequency;
      if (byFrequency !== 0) return byFrequency;
      return b.updatedAt - a.updatedAt;
    });
}

/**
 * GET /ideas/unassigned (U15). The org-readable new-skill backlog: every bin
 * entry for the caller's effective org, each decorated with its frequency
 * (distinct sessions) and ordered most-frequent-first. READ is open to any org
 * member (no admin gate) — acting on an entry is the admin-gated path below.
 * Org is the effective (PROFILE-driven) org, so an org-A read never sees org-B
 * bin entries.
 */
export async function resolveUnassignedBin(
  event: APIGatewayProxyEventV2,
  deps: IdeasDeps,
): Promise<APIGatewayProxyResultV2> {
  // Same auth + org-scoping contract as the ideas reads above.
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();

  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ entries: [] });

  const entries = await deps.repo.listUnassignedForOrg(org);
  return ok({ entries: unassignedBin(entries) });
}

/**
 * POST /ideas/unassigned/{entryId}/promote-to-skill (U15). Acting on a bin
 * entry — turning a recurring off-catalog topic into a new skill — is
 * ADMIN-GATED: the read above is open to any member, but only an org admin may
 * act. Admin is decided server-side from the profile (so the claude+ device
 * token, which carries no role claim, still works), reconciling exactly with
 * the `createSkill`/`fold`/`promote` write gate.
 *
 * v1 is a stub: it enforces the permission boundary (the whole point of U15 —
 * "non-admins read but can't action") and acknowledges the entry. The actual
 * skill-creation flow is the `/skill-idea-iterate` skill's territory (U17); this
 * is the server-side gate it relies on.
 */
export async function actOnUnassignedEntry(
  event: APIGatewayProxyEventV2,
  deps: IdeasDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  if (!auth.org) return unauthorized();

  const entryId = pathParam(event, 'entryId');
  if (!entryId) return badRequest('missing entryId');

  // v1 stub: the gate is the deliverable. Confirm the entry exists in the org's
  // bin (no cross-org act) and acknowledge; creating the skill is out of scope.
  const entries = await deps.repo.listUnassignedForOrg(auth.org);
  const entry = entries.find((e) => e.entryId === entryId);
  if (!entry) return badRequest('unknown bin entry');

  return ok({ entryId, acknowledged: true });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: IdeasDeps = { repo: defaultRepo() };
  // One Lambda serves every ideas read/action path; dispatch on the route. The
  // unassigned-bin paths are matched first (their `/ideas/...` prefix would
  // otherwise be swallowed by the `/ideas` suffix check). The all-ideas path
  // ends in `/ideas`; everything else is candidate-learnings (the default, so a
  // bare path still hits the security-gated surface).
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  if (path.endsWith('/promote-to-skill') && method === 'POST') {
    return actOnUnassignedEntry(event, deps);
  }
  if (path.endsWith('/ideas/unassigned')) return resolveUnassignedBin(event, deps);
  if (path.endsWith('/ideas')) return resolveSkillIdeas(event, deps);
  return resolveCandidateLearnings(event, deps);
}
