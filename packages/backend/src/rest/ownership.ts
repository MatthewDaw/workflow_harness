import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { Project } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { Principal } from '../auth/verify.js';
import { badRequest, notFound, pathParam, unauthorized } from './runtime.js';
import { resolvePrincipal } from './bearerAuth.js';

/**
 * Resolve the caller and the project they OWN, or the error response to return:
 * no principal → 401, no path param → 400, missing OR not-owned project → 404
 * (one status for both, so resource ids cannot be enumerated). The principal is
 * resolved via `resolvePrincipal`, so the gateway Cognito JWT and the claude+
 * device token both work. `param` names the path parameter carrying the project
 * id (`id` on /projects routes, `pid` on the nested memories/weekly routes).
 */
export async function ownedProject(
  event: APIGatewayProxyEventV2,
  repo: Repo,
  param: 'id' | 'pid' = 'id',
): Promise<{ project: Project; principal: Principal } | { error: APIGatewayProxyResultV2 }> {
  const principal = await resolvePrincipal(event);
  if (!principal) return { error: unauthorized() };
  const id = pathParam(event, param);
  if (!id) return { error: badRequest('missing project id') };
  const project = await repo.getProject(id);
  if (!project || project.ownerUserId !== principal.userId) return { error: notFound() };
  return { project, principal };
}
