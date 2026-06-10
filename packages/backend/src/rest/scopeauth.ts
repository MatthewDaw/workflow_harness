import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { UserProfile } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';
import { resolvePrincipal } from './bearerAuth.js';
import { forbidden, unauthorized } from './runtime.js';

/** Authorization helpers shared by the org-catalog registries (skills/agents/mcp/workflows). */

/**
 * Is a catalog record CANONICAL — owned by the git seed (`catalog/skills` /
 * `.claude/agents` → `seed-*.mjs`), not by REST? The org catalog is the single
 * runtime source of truth,
 * but a canonical record's BASE variant is updated ONLY by the seed; REST callers
 * fork it instead (the variant model) or change the repo and re-seed. This keeps the
 * default bundle reviewable/rollback-able in git while every read and every
 * user-authored write still goes through the DB.
 *
 * The marker is uniform across skills/agents/mcp-servers: the seed stamps every
 * built-in with `createdBy.userId === 'system'` (a real user write never is). Skills
 * additionally carry `source:'built-in'`; agents/mcp-servers have no `source` field,
 * so the `createdBy` marker is the one that spans all three.
 */
export function isBuiltin(
  record: { source?: string; createdBy?: { userId?: string } } | undefined,
): boolean {
  return record?.source === 'built-in' || record?.createdBy?.userId === 'system';
}

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

/** A passed gate (the resolved auth) or the HTTP error response to return. */
export type OrgCatalogGate<A = OrgCatalogAuth> = { auth: A } | { error: APIGatewayProxyResultV2 };

/**
 * Gate a READ-side org-catalog handler: resolve the caller or produce the 401.
 *
 * Accepts EITHER the gateway Cognito JWT (HQ web) OR the claude+ device token —
 * these routes are wired with `HttpNoneAuthorizer`, so the raw HS256 bearer
 * token reaches the handler instead of being pre-rejected by the gateway. What a
 * missing org means differs per read handler (404, empty list, …), so org
 * handling stays with the caller.
 */
export async function requireOrgCatalogAuth(
  event: APIGatewayProxyEventV2,
  repo: Repo,
): Promise<OrgCatalogGate> {
  const auth = await resolveOrgCatalogAuth(event, repo);
  if (!auth) return { error: unauthorized() };
  return { auth };
}

/**
 * Gate a WRITE-side (admin) org-catalog handler: verified caller (else 401),
 * server-side admin (else 403), resolvable org (else 401). Admin is decided
 * from the PROFILE, not the token, so the claude+ device token — which carries
 * org + identity but NO role claim — can write the catalog (see `isOrgAdmin`).
 */
export async function requireOrgCatalogAdmin(
  event: APIGatewayProxyEventV2,
  repo: Repo,
): Promise<OrgCatalogGate<OrgCatalogAuth & { org: string }>> {
  const auth = await resolveOrgCatalogAuth(event, repo);
  if (!auth) return { error: unauthorized() };
  if (!auth.admin) return { error: forbidden() };
  if (!auth.org) return { error: unauthorized() };
  return { auth: { ...auth, org: auth.org } };
}
