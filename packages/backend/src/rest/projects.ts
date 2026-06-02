import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { projectSchema, type Project } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  created,
  defaultRepo,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';

/**
 * REST: projects (U8).
 *
 *   GET  /projects        — the caller's projects, each with a live-session count
 *   GET  /projects/:id    — a single project (404 if missing or not owned)
 *   POST /projects        — connect a repo as a new project owned by the caller
 *
 * Every query is scoped to the authenticated `uid`. A project owned by another
 * user is reported as 404 (not 403) so resource ids cannot be enumerated.
 */

export interface ProjectsDeps {
  repo: Repo;
}

/** Count the live sessions (active | needs_input) currently in a project. */
async function liveCount(repo: Repo, projectId: string): Promise<number> {
  const sessions = await repo.listSessionsForProject(projectId);
  return sessions.filter((s) => s.status === 'active' || s.status === 'needs_input').length;
}

export async function listProjects(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  const projects = await deps.repo.listProjectsForUser(principal.userId);
  const withCounts = await Promise.all(
    projects.map(async (p) => ({ ...p, liveSessionCount: await liveCount(deps.repo, p.id) })),
  );
  return ok({ projects: withCounts });
}

export async function getProject(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing project id');

  const project = await deps.repo.getProject(id);
  // Not found OR not owned -> 404 (no enumeration).
  if (!project || project.ownerUserId !== principal.userId) return notFound();

  const instances = await deps.repo.listInstances(id);
  const sessions = await deps.repo.listSessionsForProject(id);
  return ok({
    project: { ...project, liveSessionCount: await liveCount(deps.repo, id) },
    instances,
    sessions,
  });
}

export async function createProject(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }

  // The owner is always the caller — never trust a client-supplied owner.
  const parsed = projectSchema.safeParse({
    liveSessionCount: 0,
    ...(body as Record<string, unknown>),
    ownerUserId: principal.userId,
  });
  if (!parsed.success) return badRequest(parsed.error.message);

  const project: Project = parsed.data;
  await deps.repo.putProject(project);
  return created({ project });
}

/** Routes the three verbs by method/path for a single Lambda integration. */
export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: ProjectsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const hasId = Boolean(pathParam(event, 'id'));
  if (method === 'POST') return createProject(event, deps);
  if (method === 'GET' && hasId) return getProject(event, deps);
  return listProjects(event, deps);
}
