import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import type { ScopeRef } from '@harness/shared';
import type { Principal } from '../auth/verify.js';

/**
 * Authorization helpers shared by the scoped Agents/Skills registries (U9).
 *
 * Write rules per tier:
 *  - org:     only an admin (the `custom:admin` claim) may write at org scope.
 *  - user:    a user may write only their own user scope.
 *  - project: a user may write a project scope (project-membership is the
 *             project's own concern; ownership is enforced at the project layer).
 */

export function isAdmin(event: APIGatewayProxyEventV2): boolean {
  const claims = (
    event.requestContext as { authorizer?: { jwt?: { claims?: Record<string, unknown> } } }
  ).authorizer?.jwt?.claims;
  return claims?.['custom:admin'] === 'true' || claims?.['custom:admin'] === true;
}

/** May `principal` write at `scope`? `admin` gates the org tier. */
export function canWriteScope(scope: ScopeRef, principal: Principal, admin: boolean): boolean {
  switch (scope.tier) {
    case 'org':
      return admin && scope.id === principal.org;
    case 'user':
      return scope.id === principal.userId;
    case 'project':
      return true;
  }
}
