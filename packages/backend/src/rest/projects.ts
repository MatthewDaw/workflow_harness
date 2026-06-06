import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { orgScope, projectSchema, type Project } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { GitHubApp, PublicGitHubReader } from '../github/app.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  queryParam,
  unauthorized,
} from './runtime.js';
import { isAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';

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
  /**
   * Build the read-only GitHub App client for a project (U7/U8). Injected so
   * tests stub the client and never touch the network; the default reads App
   * credentials from the environment. Returns undefined when the project is not
   * GitHub-connected or credentials are absent.
   */
  githubFor?: (project: Project) => GitHubApp | undefined;
}

/**
 * Translate a stored project `repo` (e.g. `gh/acme/weekly-compass` or
 * `acme/weekly-compass`) into the `owner/repo` the GitHub App client expects.
 */
export function ownerRepoOf(repo: string): string {
  return repo.replace(/^gh\//, '');
}

/** Adapt the platform `fetch` to the client's minimal `FetchLike` shape. */
const fetchLike = (
  url: string,
  init?: { method?: string; headers?: Record<string, string>; body?: string },
) =>
  fetch(url, init).then((r) => ({
    status: r.status,
    ok: r.ok,
    text: () => r.text(),
    json: () => r.json(),
  }));

/**
 * Default GitHub client factory: build a read-only client for a project's repo.
 * Prefers an authenticated GitHub App client when App credentials are configured
 * in the environment (per-project installation id falls back to a single shared
 * `GITHUB_INSTALLATION_ID`). When no App is configured, fall back to an
 * **unauthenticated public-repo reader** so a public repo's docs/requirements
 * still populate; a private repo's reads will just 404/403 and degrade to empty.
 * Returns undefined only when the project has no repo at all.
 */
export function defaultGithubFor(project: Project): GitHubApp | undefined {
  const repo = ownerRepoOf(project.repo);
  if (!repo) return undefined;

  const appId = process.env.GITHUB_APP_ID;
  const privateKeyPem = process.env.GITHUB_APP_PRIVATE_KEY;
  const installationId = process.env.GITHUB_INSTALLATION_ID;
  if (appId && privateKeyPem && installationId) {
    return new GitHubApp({ appId, privateKeyPem, installationId, repo }, fetchLike);
  }

  // No App configured → best-effort public read (works for public repos).
  return new PublicGitHubReader(repo, fetchLike);
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

  // The owner is always the caller — never trust a client-supplied owner. The
  // org is stamped from the creator's EFFECTIVE org so the project follows their
  // membership (used for future org partitioning); undefined when org-less.
  const org = await effectiveOrg(event, deps.repo);
  const parsed = projectSchema.safeParse({
    liveSessionCount: 0,
    ...(body as Record<string, unknown>),
    ownerUserId: principal.userId,
    ...(org ? { org } : {}),
  });
  if (!parsed.success) return badRequest(parsed.error.message);

  const project: Project = parsed.data;
  await deps.repo.putProject(project);
  return created({ project });
}

/**
 * DELETE /projects/:id — remove a project and everything under it (sessions,
 * instances, weekly, framing) plus its repo pointer. Gated to the project's
 * **owner or an org admin**; a missing/not-owned project is a 404 (no
 * enumeration). The GitHub repo itself is never touched — this only forgets the
 * project inside HQ, so the repo can be reconnected later.
 */
export async function deleteProjectHandler(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const id = pathParam(event, 'id');
  if (!id) return badRequest('missing project id');

  const project = await deps.repo.getProject(id);
  if (!project) return notFound();
  // Owner or org admin may delete; anyone else gets 404 (don't reveal existence).
  if (!isAdmin(event) && project.ownerUserId !== principal.userId) return notFound();

  const { deleted } = await deps.repo.deleteProject(id);
  if (!deleted) return notFound();
  return ok({ deleted: true });
}

// --- U7: GitHub framing refresh -----------------------------------------

/** Resolve the project for the caller, or a 404 result. */
async function ownedProject(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<{ project: Project } | { error: APIGatewayProxyResultV2 }> {
  const principal = principalOf(event);
  if (!principal) return { error: unauthorized() };
  const id = pathParam(event, 'id');
  if (!id) return { error: badRequest('missing project id') };
  const project = await deps.repo.getProject(id);
  if (!project || project.ownerUserId !== principal.userId) return { error: notFound() };
  return { project };
}

/**
 * POST /projects/:id/refresh — re-read the project's framing from GitHub
 * (`completion:` frontmatter, PRD goal, owned Supporting Outcomes), store it, and
 * return the updated project. GitHub being unreachable is NOT a 500: we mark the
 * last-known data stale and return it, so the UI degrades to "stale" rather than
 * erroring. The GitHub App is read-only — this endpoint never writes to GitHub.
 */
export async function refreshProject(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const make = deps.githubFor ?? defaultGithubFor;
  const app = make(project);
  if (!app) {
    // Not GitHub-connected (or no creds): serve last-known, flagged stale.
    return ok({ project: { ...project, framingStale: true }, stale: true });
  }

  try {
    const framing = await app.readFramingWithCompletion();
    const readAt = new Date().toISOString();
    await deps.repo.putProjectFraming(project.id, {
      progressPct: framing.progressPct ?? 0,
      prdGoal: framing.goal,
      supportingOutcomeIds: framing.supportingOutcomeIds,
      readAt,
      stale: false,
    });
    const updated = await deps.repo.getProject(project.id);
    return ok({
      project: {
        ...(updated ?? project),
        liveSessionCount: await liveCount(deps.repo, project.id),
      },
      stale: false,
    });
  } catch {
    // GitHub unreachable / token expired: never 500. Return last-known + staleness.
    return ok({
      project: {
        ...project,
        framingStale: true,
        liveSessionCount: await liveCount(deps.repo, project.id),
      },
      stale: true,
    });
  }
}

// --- U8: docs/plans tree + content --------------------------------------

/**
 * GET /projects/:id/docs — the `docs/plans/` tree with per-doc `completion:`.
 * Returns `{ docs: [{ path, title, completion }] }`. An empty/absent tree yields
 * `{ docs: [] }`. GitHub unreachable returns an empty tree + a `stale` flag
 * rather than a 500 (the detail screen shows a stale banner, not an error).
 */
export async function getProjectDocs(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const make = deps.githubFor ?? defaultGithubFor;
  const app = make(project);
  if (!app) return ok({ docs: [], stale: true });

  try {
    const docs = await app.listDocs();
    return ok({ docs });
  } catch {
    return ok({ docs: [], stale: true });
  }
}

/**
 * GET /projects/:id/docs/content?path=<repo-rel> — raw markdown for one
 * `docs/plans/` doc. Returns `{ path, markdown }`. A path that is not a known
 * doc is a 404. GitHub unreachable surfaces as a stale empty payload, not a 500.
 */
export async function getProjectDocContent(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const path = queryParam(event, 'path');
  if (!path) return badRequest('missing path');

  const make = deps.githubFor ?? defaultGithubFor;
  const app = make(project);
  if (!app) return ok({ path, markdown: '', stale: true });

  try {
    const markdown = await app.readDocContent(path);
    if (markdown === undefined) return notFound();
    return ok({ path, markdown });
  } catch {
    return ok({ path, markdown: '', stale: true });
  }
}

// --- Project Requirements: read-only from docs/PRD.md --------------------

/**
 * GET /projects/:id/requirements — the project's high-level requirements body,
 * read read-only from `docs/PRD.md` on GitHub (distinct from the repo-root
 * `PRD.md` that feeds the Project Overview). Returns `{ markdown }`, with an
 * empty string when the file is absent. GitHub being unreachable / not connected
 * degrades to `{ markdown: '', stale: true }` rather than a 500, mirroring the
 * doc-content endpoint's failure posture.
 */
export async function getProjectRequirements(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const make = deps.githubFor ?? defaultGithubFor;
  const app = make(project);
  if (!app) return ok({ markdown: '', stale: true });

  try {
    const markdown = await app.readPrdDoc();
    return ok({ markdown: markdown ?? '' });
  } catch {
    return ok({ markdown: '', stale: true });
  }
}

// --- Project opt-in: enable/disable org-catalog skills + agents ----------
//
// Auth gate for all four: org admin OR the project's owner. The skill/agent name
// must exist in the caller's org catalog (else 404). Responses return the
// hydrated Project (both enabledSkills + enabledAgents arrays).

/**
 * Resolve the project for a catalog opt-in mutation, enforcing the
 * admin-or-owner gate. Returns the project + principal, or an error result. A
 * missing project is a 404; a non-owner non-admin is 403.
 */
async function projectForOptIn(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<
  | { project: Project; principal: { userId: string; org: string } }
  | { error: APIGatewayProxyResultV2 }
> {
  const principal = principalOf(event);
  if (!principal) return { error: unauthorized() };
  const id = pathParam(event, 'projectId');
  if (!id) return { error: badRequest('missing project id') };
  const project = await deps.repo.getProject(id);
  if (!project) return { error: notFound() };
  if (!isAdmin(event) && project.ownerUserId !== principal.userId) return { error: forbidden() };
  return { project, principal };
}

/** POST /projects/:projectId/skills/:skillName — idempotent enable. */
export async function enableProjectSkill(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, principal } = resolved;
  const skillName = pathParam(event, 'skillName');
  if (!skillName) return badRequest('missing skill name');

  // The skill must exist in the org catalog.
  const skill = await deps.repo.getSkill(orgScope(principal.org), skillName);
  if (!skill) return notFound();

  const updated = await deps.repo.addSkillToProject(project.id, skillName);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** DELETE /projects/:projectId/skills/:skillName — disable. */
export async function disableProjectSkill(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;
  const skillName = pathParam(event, 'skillName');
  if (!skillName) return badRequest('missing skill name');

  const updated = await deps.repo.removeSkillFromProject(project.id, skillName);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** POST /projects/:projectId/agents/:agentName — enable + union the agent's skills. */
export async function enableProjectAgent(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, principal } = resolved;
  const agentName = pathParam(event, 'agentName');
  if (!agentName) return badRequest('missing agent name');

  // The agent must exist in the org catalog.
  const agent = await deps.repo.getAgent(orgScope(principal.org), agentName);
  if (!agent) return notFound();

  const updated = await deps.repo.addAgentToProject(project.id, agentName, principal.org);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** DELETE /projects/:projectId/agents/:agentName — disable (does NOT prune skills). */
export async function disableProjectAgent(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;
  const agentName = pathParam(event, 'agentName');
  if (!agentName) return badRequest('missing agent name');

  const updated = await deps.repo.removeAgentFromProject(project.id, agentName);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** Routes the verbs/sub-paths by method/path for a single Lambda integration. */
export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: ProjectsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const hasId = Boolean(pathParam(event, 'id'));
  const rawPath = event.requestContext.http.path ?? event.rawPath ?? '';

  // Project opt-in for the org catalog (uses the :projectId path param).
  if (/\/skills\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectSkill(event, deps);
    if (method === 'DELETE') return disableProjectSkill(event, deps);
  }
  if (/\/agents\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectAgent(event, deps);
    if (method === 'DELETE') return disableProjectAgent(event, deps);
  }

  if (method === 'DELETE' && hasId) return deleteProjectHandler(event, deps);
  if (method === 'POST' && hasId && rawPath.endsWith('/refresh'))
    return refreshProject(event, deps);
  if (method === 'GET' && hasId && /\/requirements$/.test(rawPath))
    return getProjectRequirements(event, deps);
  if (method === 'GET' && hasId && /\/docs\/content$/.test(rawPath))
    return getProjectDocContent(event, deps);
  if (method === 'GET' && hasId && /\/docs$/.test(rawPath)) return getProjectDocs(event, deps);
  if (method === 'POST') return createProject(event, deps);
  if (method === 'GET' && hasId) return getProject(event, deps);
  return listProjects(event, deps);
}
