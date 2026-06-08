import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import type { ScopeRef, UserProfile } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';
import { resolvePrincipal } from './bearerAuth.js';

/**
 * Authorization helpers shared by the scoped Agents/Skills registries (U9).
 *
 * Read rules per tier (canReadScope):
 *  - org:     the caller must belong to the org (scope.id === principal.org).
 *  - user:    the caller may read only their own user scope.
 *  - project: the caller must own the project (resolved via the Repo).
 *
 * Write rules per tier (canWriteScope):
 *  - org:     only an admin (the `custom:admin` claim) may write at org scope,
 *             and only within their own org.
 *  - user:    a user may write only their own user scope.
 *  - project: a user may write a project scope only if they own the project
 *             (project.ownerUserId === principal.userId) — never an arbitrary
 *             project id (cross-tenant write).
 */

export function isAdmin(event: APIGatewayProxyEventV2): boolean {
  const claims = (
    event.requestContext as { authorizer?: { jwt?: { claims?: Record<string, unknown> } } }
  ).authorizer?.jwt?.claims;
  return claims?.['custom:admin'] === 'true' || claims?.['custom:admin'] === true;
}

/**
 * Is the caller an admin of `org` for org-catalog writes? Admin is decided
 * SERVER-SIDE and authoritatively from the PROFILE (`profile.admin` for the
 * active org, or `profile.adminOrgs` listing the org) — the same source `GET /me`
 * uses — OR'd with the gateway `custom:admin` claim for the Cognito-web path.
 *
 * Deriving it from the profile (not the token) is what lets the claude+ device
 * token write the catalog: that token carries org + identity but NO role claim,
 * so a token-only `isAdmin` could never authorize it. It is also revocable
 * instantly (edit the profile) with no 180-day token re-mint.
 */
export function isOrgAdmin(
  event: APIGatewayProxyEventV2,
  profile: UserProfile | undefined,
  org: string | undefined,
): boolean {
  if (isAdmin(event)) return true;
  if (profile?.admin === true) return true;
  return org ? (profile?.adminOrgs ?? []).includes(org) : false;
}

/**
 * Resolved authorization context for an ORG-CATALOG request (skills/agents/
 * mcp-servers). Accepts EITHER the gateway Cognito JWT (HQ web) OR the claude+
 * device token (raw `Authorization: Bearer`) — the device token only reaches the
 * handler on routes wired with `HttpNoneAuthorizer`, so the gateway does not
 * pre-reject its HS256 signature.
 */
export interface OrgCatalogAuth {
  principal: Principal;
  /** Effective org: the profile's org, falling back to the token's org claim. */
  org?: string;
  /** Whether the caller may WRITE the org catalog (server-side admin gate). */
  admin: boolean;
}

/**
 * Resolve the caller, their effective org, and their org-catalog write authority
 * in one place, accepting both auth paths. Returns `undefined` only when NO
 * principal verifies (the caller answers 401); a verified-but-non-admin caller
 * returns `{ admin: false }` (the caller answers 403). The org mirrors the read
 * handlers' `effectiveOrg(...) ?? principal.org` fallback so a device token whose
 * profile has no stamped org still scopes to its token org.
 */
export async function resolveOrgCatalogAuth(
  event: APIGatewayProxyEventV2,
  repo: Repo,
): Promise<OrgCatalogAuth | undefined> {
  const principal = await resolvePrincipal(event);
  if (!principal) return undefined;
  const profile = await repo.getUser(principal.userId);
  const org = profile?.org ?? principal.org;
  return { principal, org, admin: isOrgAdmin(event, profile, org) };
}

/**
 * May `principal` read at `scope`? The org/user tiers are decided from the
 * principal alone; the project tier requires a Repo lookup so ownership /
 * org-membership of the project can be enforced (no cross-tenant reads).
 */
export async function canReadScope(
  scope: ScopeRef,
  principal: Principal,
  repo: Repo,
): Promise<boolean> {
  switch (scope.tier) {
    case 'org':
      return scope.id === principal.org;
    case 'user':
      return scope.id === principal.userId;
    case 'project': {
      const project = await repo.getProject(scope.id);
      if (!project) return false;
      return project.ownerUserId === principal.userId;
    }
  }
}

/**
 * May `principal` write at `scope`? `admin` gates the org tier. The project tier
 * requires real authorization against the project record (ownership or org
 * match) — it is no longer unconditionally allowed (cross-tenant write fix).
 */
export async function canWriteScope(
  scope: ScopeRef,
  principal: Principal,
  admin: boolean,
  repo: Repo,
): Promise<boolean> {
  switch (scope.tier) {
    case 'org':
      return admin && scope.id === principal.org;
    case 'user':
      return scope.id === principal.userId;
    case 'project': {
      const project = await repo.getProject(scope.id);
      if (!project) return false;
      return project.ownerUserId === principal.userId;
    }
  }
}
