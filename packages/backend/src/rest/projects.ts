import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  LEARNING_STREAMS,
  orgScope,
  projectSchema,
  type LearningStream,
  type Project,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import { GitHubApp, PublicGitHubReader } from '../github/app.js';
import {
  badRequest,
  conflict,
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
import { isAdmin, isOrgAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { flattenBundle } from './skills.js';
import { flattenAgentBundle } from './agents.js';
import { STARTER_BUNDLE_NAME } from '../seed/skills.js';
import { resolvePrincipal } from './bearerAuth.js';

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

  // Scope to the caller's EFFECTIVE org. A project belongs to exactly one org
  // (stamped from the creator's effective org at connect time), but the owner
  // index (GSI1 `USER#<id>`) gathers every project the user owns regardless of
  // org. Without this filter a repo connected in one org would surface under
  // every org the owner belongs to — i.e. appear "registered in both orgs".
  // Strict match: a project with no `org` (legacy, pre-org-stamping) is hidden
  // until the backfill (infra/scripts/backfill-project-org.mjs) tags it.
  const org = await effectiveOrg(event, deps.repo);
  const owned = await deps.repo.listProjectsForUser(principal.userId);
  const projects = owned.filter((p) => p.org === org);
  const withCounts = await Promise.all(
    projects.map(async (p) => ({ ...p, liveSessionCount: await liveCount(deps.repo, p.id) })),
  );
  return ok({ projects: withCounts });
}

export async function getProject(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (the claude+ wrapper reads
  // its project's enabled set here over HttpNoneAuthorizer).
  const principal = await resolvePrincipal(event);
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

  // Dedupe guard: refuse a SECOND project record for the same GitHub repo under a
  // DIFFERENT id. The web mints `id = projectIdFor(owner/repo)` and `repo =
  // gh/owner/repo`, so a normal re-connect lands the SAME id (an idempotent
  // overwrite below — allowed). The hazard is a divergent-slug record: a phantom
  // created under an old derivation (e.g. the pre-fix folder-name slug
  // `fractions-tutorial` vs the canonical `matthewdaw-fractions-tutorial`) collides
  // on the repo while differing on id. Enabling skills then lands on one record and
  // does nothing on the one the daemon/UI actually use. Reject it, naming the
  // canonical id so the caller reuses (or deletes) that record instead of forking a
  // duplicate. Compare on the normalized `owner/repo` (gh/ prefix stripped, lowercased).
  const incomingRepo = ownerRepoOf(project.repo).toLowerCase();
  if (incomingRepo) {
    const owned = await deps.repo.listProjectsForUser(principal.userId);
    const clash = owned.find(
      (p) =>
        p.id !== project.id &&
        (org ? p.org === org : true) &&
        ownerRepoOf(p.repo ?? '').toLowerCase() === incomingRepo,
    );
    if (clash) {
      return conflict(
        `repo "${incomingRepo}" is already connected as project "${clash.id}" — reuse that ` +
          `project (or delete it first) instead of creating a duplicate under "${project.id}".`,
      );
    }
  }

  // Auto-enable the command-hq-starter bundle on every new project so HQ's hq-*
  // management skills are usable from claude+ immediately, without a manual opt-in
  // (the exact gap that left existing projects with empty enabled sets and no
  // /hq-* autocomplete). Best-effort: only when the creator has an org whose
  // catalog carries the bundle; an org-less project, a catalog without the bundle,
  // or a transient read error leaves the project as-is rather than blocking create.
  if (org) {
    try {
      const catalog = await deps.repo.listSkills(org);
      const byName = new Map(catalog.map((s) => [s.name, s]));
      const bundle = byName.get(STARTER_BUNDLE_NAME);
      if (bundle && bundle.kind === 'bundle') {
        const leaves = flattenBundle(bundle, byName);
        project.enabledBundles = [
          ...new Set([...(project.enabledBundles ?? []), STARTER_BUNDLE_NAME]),
        ];
        project.enabledSkills = [...new Set([...(project.enabledSkills ?? []), ...leaves])];
      }
    } catch {
      // never block project creation on starter enablement
    }
  }

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

/**
 * GET /projects/:id/wireframe — the project's example UI wireframe, read
 * read-only from `docs/wireframe.html` on GitHub. Returns `{ html }`, with an
 * empty string when the file is absent (the Project Requirements tab then shows
 * no wireframe preview). GitHub unreachable / not connected degrades to
 * `{ html: '', stale: true }` rather than a 500, mirroring the requirements
 * endpoint's failure posture.
 */
export async function getProjectWireframe(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const make = deps.githubFor ?? defaultGithubFor;
  const app = make(project);
  if (!app) return ok({ html: '', stale: true });

  try {
    const html = await app.readWireframe();
    return ok({ html: html ?? '' });
  } catch {
    return ok({ html: '', stale: true });
  }
}

// --- Learnings: read the corrections corpus mined for a project ----------

/**
 * GET /projects/:id/learnings — the project's mined learnings (topic-focus
 * logging), read from the project's `LEARN#` partition. An optional
 * `?stream=impl|doc` filters to one stream; omitted returns both. Records come
 * back in the repo's deterministic SK order (grouped by session, then turnId).
 * Authorized exactly like the sibling `/projects/:id` reads: a missing or
 * not-owned project is a 404 (no enumeration). An empty project returns `[]`.
 */
export async function getProjectLearnings(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await ownedProject(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;

  const stream = queryParam(event, 'stream');
  if (stream !== undefined && !LEARNING_STREAMS.includes(stream as LearningStream))
    return badRequest('invalid stream');

  const learnings = await deps.repo.listLearnings(project.id);
  const filtered = stream ? learnings.filter((l) => l.stream === stream) : learnings;
  return ok({ learnings: filtered });
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
  | { project: Project; principal: { userId: string; org: string }; org: string }
  | { error: APIGatewayProxyResultV2 }
> {
  // Accept the gateway Cognito JWT OR the claude+ device token (this opt-in route
  // is HttpNoneAuthorizer, so the gateway does not pre-reject the HS256 token).
  const principal = await resolvePrincipal(event);
  if (!principal) return { error: unauthorized() };
  const id = pathParam(event, 'projectId');
  if (!id) return { error: badRequest('missing project id') };
  const project = await deps.repo.getProject(id);
  if (!project) return { error: notFound() };

  // The org to resolve catalog items against. The project belongs to exactly one
  // org (stamped at connect time), and its materialized config draws from THAT
  // org's catalog — so the item must be looked up there, NOT in the caller's raw
  // token claim (`principal.org`), which can differ from the user's effective org
  // (the DB profile org). Fall back to the caller's profile org, then the token,
  // for legacy projects with no stamped org.
  const profile = await deps.repo.getUser(principal.userId);
  const org = project.org ?? profile?.org ?? principal.org;

  // Admin-or-owner gate. Admin is derived SERVER-SIDE (profile.adminOrgs/admin OR
  // the gateway custom:admin claim) against the project's org, so a device-token
  // admin works; the owner check uses the resolved principal so a device-token
  // project owner works too.
  if (!isOrgAdmin(event, profile, project.org ?? org) && project.ownerUserId !== principal.userId)
    return { error: forbidden() };

  return { project, principal, org };
}

/** POST /projects/:projectId/skills/:skillName — idempotent enable. */
export async function enableProjectSkill(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const skillName = pathParam(event, 'skillName');
  if (!skillName) return badRequest('missing skill name');

  // The skill must exist in the org catalog.
  const skill = await deps.repo.getSkill(orgScope(org), skillName);
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
  const { project, org } = resolved;
  const agentName = pathParam(event, 'agentName');
  if (!agentName) return badRequest('missing agent name');

  // The agent must exist in the org catalog.
  const agent = await deps.repo.getAgent(orgScope(org), agentName);
  if (!agent) return notFound();

  const updated = await deps.repo.addAgentToProject(project.id, agentName, org);
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

/**
 * POST /projects/:projectId/workflows/:workflowName — idempotent enable.
 *
 * Records the workflow in `enabledWorkflows` AND, via the repo's `bringWorkflowInto`,
 * unions every referenced agent (and transitively their skills + MCP servers) into
 * the project's enabled sets — the same machinery that makes enabling an agent pull
 * its skills.
 */
export async function enableProjectWorkflow(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const workflowName = pathParam(event, 'workflowName');
  if (!workflowName) return badRequest('missing workflow name');

  // The workflow must exist in the org catalog.
  const workflow = await deps.repo.getWorkflow(orgScope(org), workflowName);
  if (!workflow) return notFound();

  const updated = await deps.repo.addWorkflowToProject(project.id, workflowName, org);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** DELETE /projects/:projectId/workflows/:workflowName — disable (does NOT prune agents). */
export async function disableProjectWorkflow(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;
  const workflowName = pathParam(event, 'workflowName');
  if (!workflowName) return badRequest('missing workflow name');

  const updated = await deps.repo.removeWorkflowFromProject(project.id, workflowName);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** POST /projects/:projectId/mcp-servers/:name — idempotent enable. */
export async function enableProjectMcpServer(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing mcp server name');

  // The server must exist in the org catalog.
  const server = await deps.repo.getMcpServer(orgScope(org), name);
  if (!server) return notFound();

  const updated = await deps.repo.addMcpServerToProject(project.id, name);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/** DELETE /projects/:projectId/mcp-servers/:name — disable. */
export async function disableProjectMcpServer(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project } = resolved;
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing mcp server name');

  const updated = await deps.repo.removeMcpServerFromProject(project.id, name);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/**
 * POST /projects/:projectId/bundles/:bundleName — enable a whole bundle as a unit.
 *
 * Recording the bundle (intent) AND unioning its flattened leaf members into
 * `enabledSkills` happens in one read-modify-write. The bundle is resolved against
 * the org catalog and must be a catalog entry of kind `bundle` (a non-bundle skill
 * name or an unknown name is a 404 — you cannot "bundle-enable" a leaf skill). The
 * REST layer flattens here (where the catalog is loaded) so the repo stays
 * catalog-agnostic.
 */
export async function enableProjectBundle(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const bundleName = pathParam(event, 'bundleName');
  if (!bundleName) return badRequest('missing bundle name');

  // Resolve the bundle against the org catalog; it must exist AND be a bundle.
  const catalog = await deps.repo.listSkills(org);
  const byName = new Map(catalog.map((s) => [s.name, s]));
  const bundle = byName.get(bundleName);
  if (!bundle || bundle.kind !== 'bundle') return notFound();

  const leaves = flattenBundle(bundle, byName);
  const updated = await deps.repo.addBundleToProject(project.id, bundleName, leaves);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/**
 * DELETE /projects/:projectId/bundles/:bundleName — disable a whole bundle.
 *
 * Clears the bundle intent AND strips the leaf skills it contributed, EXCEPT any
 * leaf still covered by another still-enabled bundle (a member shared between two
 * enabled bundles survives). We compute the "keep" set by flattening every OTHER
 * enabled bundle, then only remove the leaves not in it. A bundle that has since
 * vanished from the catalog still removes its intent successfully (with no leaves
 * to strip) so the project can never be stuck holding a dead bundle reference.
 */
export async function disableProjectBundle(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const bundleName = pathParam(event, 'bundleName');
  if (!bundleName) return badRequest('missing bundle name');

  const catalog = await deps.repo.listSkills(org);
  const byName = new Map(catalog.map((s) => [s.name, s]));

  // The leaves this bundle would contribute (empty if it vanished from the catalog).
  const bundle = byName.get(bundleName);
  const leaves = bundle && bundle.kind === 'bundle' ? flattenBundle(bundle, byName) : [];

  // Leaves still covered by some OTHER enabled bundle must be kept.
  const keep = new Set<string>();
  for (const otherName of project.enabledBundles ?? []) {
    if (otherName === bundleName) continue;
    const other = byName.get(otherName);
    if (other && other.kind === 'bundle') {
      for (const leaf of flattenBundle(other, byName)) keep.add(leaf);
    }
  }
  const removable = leaves.filter((l) => !keep.has(l));

  const updated = await deps.repo.removeBundleFromProject(project.id, bundleName, removable);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/**
 * POST /projects/:projectId/agent-bundles/:bundleName — enable a whole agent
 * bundle as a unit. Mirrors `enableProjectBundle` but against the AGENTS catalog:
 * the name must resolve to an agent of kind `bundle`. Flattening to member agents
 * happens here (where the catalog is loaded) so the repo stays catalog-agnostic;
 * each member agent is then brought in with its skills + MCP servers.
 */
export async function enableProjectAgentBundle(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const bundleName = pathParam(event, 'bundleName');
  if (!bundleName) return badRequest('missing bundle name');

  // Resolve against the org AGENT catalog; it must exist AND be a bundle.
  const catalog = await deps.repo.listAgents(org);
  const byName = new Map(catalog.map((a) => [a.name, a]));
  const bundle = byName.get(bundleName);
  if (!bundle || bundle.kind !== 'bundle') return notFound();

  const members = flattenAgentBundle(bundle, byName);
  const updated = await deps.repo.addAgentBundleToProject(project.id, bundleName, members, org);
  if (!updated) return notFound();
  return ok({ project: updated });
}

/**
 * DELETE /projects/:projectId/agent-bundles/:bundleName — disable a whole agent
 * bundle. Mirrors `disableProjectBundle`: clears the bundle intent AND drops the
 * member agents it contributed, EXCEPT any member still covered by another
 * still-enabled agent bundle. A bundle that has since vanished from the catalog
 * still removes its intent (with no members to strip) so the project can never be
 * stuck holding a dead reference.
 */
export async function disableProjectAgentBundle(
  event: APIGatewayProxyEventV2,
  deps: ProjectsDeps,
): Promise<APIGatewayProxyResultV2> {
  const resolved = await projectForOptIn(event, deps);
  if ('error' in resolved) return resolved.error;
  const { project, org } = resolved;
  const bundleName = pathParam(event, 'bundleName');
  if (!bundleName) return badRequest('missing bundle name');

  const catalog = await deps.repo.listAgents(org);
  const byName = new Map(catalog.map((a) => [a.name, a]));

  // The members this bundle would contribute (empty if it vanished from the catalog).
  const bundle = byName.get(bundleName);
  const members = bundle && bundle.kind === 'bundle' ? flattenAgentBundle(bundle, byName) : [];

  // Members still covered by some OTHER enabled agent bundle must be kept.
  const keep = new Set<string>();
  for (const otherName of project.enabledAgentBundles ?? []) {
    if (otherName === bundleName) continue;
    const other = byName.get(otherName);
    if (other && other.kind === 'bundle') {
      for (const member of flattenAgentBundle(other, byName)) keep.add(member);
    }
  }
  const removable = members.filter((m) => !keep.has(m));

  const updated = await deps.repo.removeAgentBundleFromProject(project.id, bundleName, removable);
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
  if (/\/workflows\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectWorkflow(event, deps);
    if (method === 'DELETE') return disableProjectWorkflow(event, deps);
  }
  if (/\/mcp-servers\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectMcpServer(event, deps);
    if (method === 'DELETE') return disableProjectMcpServer(event, deps);
  }
  // Agent bundles MUST be matched before /bundles/ (and /agents/) — the path
  // `/agent-bundles/<name>` is distinct (no literal `/bundles/` or `/agents/`
  // boundary), but order it first for clarity.
  if (/\/agent-bundles\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectAgentBundle(event, deps);
    if (method === 'DELETE') return disableProjectAgentBundle(event, deps);
  }
  if (/\/bundles\/[^/]+$/.test(rawPath)) {
    if (method === 'POST') return enableProjectBundle(event, deps);
    if (method === 'DELETE') return disableProjectBundle(event, deps);
  }

  if (method === 'DELETE' && hasId) return deleteProjectHandler(event, deps);
  if (method === 'POST' && hasId && rawPath.endsWith('/refresh'))
    return refreshProject(event, deps);
  if (method === 'GET' && hasId && /\/requirements$/.test(rawPath))
    return getProjectRequirements(event, deps);
  if (method === 'GET' && hasId && /\/wireframe$/.test(rawPath))
    return getProjectWireframe(event, deps);
  if (method === 'GET' && hasId && /\/learnings$/.test(rawPath))
    return getProjectLearnings(event, deps);
  if (method === 'GET' && hasId && /\/docs\/content$/.test(rawPath))
    return getProjectDocContent(event, deps);
  if (method === 'GET' && hasId && /\/docs$/.test(rawPath)) return getProjectDocs(event, deps);
  if (method === 'POST') return createProject(event, deps);
  if (method === 'GET' && hasId) return getProject(event, deps);
  return listProjects(event, deps);
}
