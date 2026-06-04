import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import type { ScopeRef } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';

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

/**
 * May `principal` write the org catalog (skills/agents)? In the collapsed
 * org-only model every catalog item is org-scoped, so a write requires the
 * caller to be an admin of their own org. The principal is implicitly of their
 * own org, so this reduces to the admin claim.
 */
export function canWriteOrgCatalog(_principal: Principal, admin: boolean): boolean {
  return admin;
}

export function isAdmin(event: APIGatewayProxyEventV2): boolean {
  const claims = (
    event.requestContext as { authorizer?: { jwt?: { claims?: Record<string, unknown> } } }
  ).authorizer?.jwt?.claims;
  return claims?.['custom:admin'] === 'true' || claims?.['custom:admin'] === true;
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
