import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { agentSchema, orgScope, type Agent } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  conflict,
  created,
  defaultRepo,
  forbidden,
  gone,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  unauthorized,
} from './runtime.js';
import { isBuiltin, resolveOrgCatalogAuth } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';
import { withAuthorNames } from './authorNames.js';
import { flattenBundle } from './bundles.js';

/**
 * REST: agents + agent bundles — collapsed to a single ORG catalog (mirrors
 * skills.ts).
 *
 *   GET    /agents                        — the caller's org catalog
 *   GET    /agents/:name                  — one agent from the org catalog
 *   POST   /agents                        — create (server forces org scope + createdBy; admin)
 *   PUT    /agents/:name                  — update (scope/createdBy immutable; admin)
 *   DELETE /agents/:name                  — delete (admin)
 *   POST   /agents/:name/members          — add a member ref to an agent bundle
 *   DELETE /agents/:name/members/:member  — eject a member (it stays standalone)
 *   POST   /agents/:name/dissolve         — flatten a bundle: members standalone, bundle removed
 *   POST   /agents/:name/scope            — RETIRED (410 Gone)
 *
 * Agents keep `skills[]`. There is no tier elevation/demotion in the org catalog.
 * An agent bundle (`kind:'bundle'`) holds member *refs* (agent names); a member
 * may itself be a bundle (nesting); resolution is transitive.
 *
 * Agents also carry a `description` (delegation trigger) alongside `model`/`tools`.
 * POST/PUT parse the body through `agentSchema` (so `description` flows through
 * with a '' default for back-compat), and GET returns the full record. The
 * wrapper materializes `model`, `tools`, and `description` into the subagent
 * file's frontmatter; this REST layer stores/serves them unchanged.
 */

export interface AgentsDeps {
  repo: Repo;
}

export async function resolveAgents(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer route).
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ agents: [] });
  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped agent shadows an org-scoped one of the same name).
  const agents = await deps.repo.listAgents(org, principal.userId);
  const byName = new Map(agents.map((a) => [a.name, a]));
  // Annotate bundles with their transitively-resolved leaf members (mirror skills).
  const annotated = agents.map((a) =>
    a.kind === 'bundle' ? { ...a, resolvedMembers: flattenBundle(a, byName) } : a,
  );
  // Show the author's real name (their email) instead of the raw Cognito sub that
  // claude+ device-token writes stamp into createdBy.name.
  return ok({ agents: await withAuthorNames(deps.repo, annotated) });
}

export async function createAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept the gateway Cognito JWT OR a raw device token (HttpNoneAuthorizer
  // route); admin is decided server-side from the profile so the device token
  // (no role claim) can write.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  if (!auth.org) return unauthorized();
  const { principal, org } = auth;

  const name = pathParam(event, 'name');
  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = agentSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const agent: Agent = parsed.data;

  // Canonical built-ins are owned by the git seed: reject an in-place write to the
  // BASE variant (no repo/author). Forking (repoId + authorUserId) is still allowed.
  const targetName = name ?? agent.name;
  const existing = await deps.repo.getAgent(orgScope(org), targetName);
  if (isBuiltin(existing) && !agent.repoId && !agent.authorUserId) {
    return conflict(
      `"${targetName}" is a canonical built-in agent — fork it (set repoId + authorUserId) ` +
        `or change it in .claude/agents and re-seed; in-place writes are rejected.`,
    );
  }

  // Bundle-overwrite guard (mirrors skills.ts): a non-bundle agent write must NOT
  // clobber an existing `kind:'bundle'` of the same name. Sync upserts by name, so
  // a local agent sharing a bundle's name would otherwise be pushed over the bundle
  // and wipe its members. Refuse it — the author must rename one or the other.
  if (existing?.kind === 'bundle' && agent.kind !== 'bundle') {
    return conflict(
      `"${targetName}" is already an agent bundle in the org catalog — an agent push under ` +
        `the same name would clobber it and wipe its members. Rename the local agent (or the ` +
        `bundle) so their names don't collide.`,
    );
  }

  if (name) {
    // PUT /agents/:name — update; preserve the existing createdBy stamp.
    agent.createdBy = existing?.createdBy ?? agent.createdBy;
    agent.baseName = existing?.baseName ?? agent.baseName ?? name;
  } else {
    agent.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    agent.baseName = agent.baseName ?? agent.name;
  }

  // VERSIONING (KTD6): snapshot a revision + fork/advance the variant instead of
  // clobbering; `putNewVersion` also upserts the live record under `agentKey`.
  const stamped = await deps.repo.putNewVersion('AGENT', agent, {
    repoId: agent.repoId,
    authorUserId: agent.authorUserId,
  });
  return name ? ok({ agent: stamped }) : created({ agent: stamped });
}

/**
 * POST /agents/:name/promote — repoint the org-wide TRUE variant for a baseName.
 * NOT admin-gated (any authed member). Body: `{ variantId, rev? }`. Mirrors
 * skills' promote: it ONLY repoints TRUE, never editing/deleting a variant.
 */
export async function promoteAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Promote is NOT admin-gated (any authed org member may repoint TRUE), but it
  // still accepts the device token via the shared resolver.
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const org = auth.org;

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const variantId = (body as { variantId?: unknown })?.variantId;
  if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
  const revRaw = (body as { rev?: unknown })?.rev;
  const rev = typeof revRaw === 'number' ? revRaw : undefined;

  const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
  await deps.repo.setTrueVariant(orgScope(org), 'AGENT', pointer);
  return ok({ true: pointer });
}

export async function getAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return notFound();
  const agent = await deps.repo.getAgent(orgScope(auth.org), name);
  if (!agent) return notFound();
  return ok({ agent });
}

export async function deleteAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  if (!auth.org) return unauthorized();
  const existing = await deps.repo.getAgent(orgScope(auth.org), name);
  if (isBuiltin(existing)) {
    return conflict(
      `"${name}" is a canonical built-in agent — remove it from .claude/agents and ` +
        `re-seed; it cannot be deleted via REST.`,
    );
  }
  await deps.repo.deleteAgent(orgScope(auth.org), name);
  return ok({ deleted: true });
}

/** Add a member ref to an agent bundle. The member may be an agent or another bundle. */
export async function addMember(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const body = parseBodySafe(event);
  if (body === INVALID_JSON) return badRequest('invalid JSON body');
  const member = (body as { member?: unknown })?.member;
  if (typeof member !== 'string' || !member) return badRequest('missing member');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getAgent(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in agent bundle — change its members in ` +
        `catalog/agents/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  if (!bundle.members.includes(member)) {
    bundle.members = [...bundle.members, member];
    await deps.repo.putAgent(bundle);
  }
  return ok({ agent: bundle });
}

/**
 * Remove/eject a member from an agent bundle. The member agent record is left
 * intact — "eject" means it is no longer in the bundle but still exists standalone.
 */
export async function removeMember(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  const member = pathParam(event, 'member');
  if (!name || !member) return badRequest('missing name or member');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getAgent(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in agent bundle — change its members in ` +
        `catalog/agents/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  bundle.members = bundle.members.filter((m) => m !== member);
  await deps.repo.putAgent(bundle);
  return ok({ agent: bundle });
}

/**
 * Dissolve an agent bundle: its members all remain as standalone agents (they
 * already exist as their own records), and the bundle record itself is deleted.
 */
export async function dissolveAgentBundle(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const auth = await resolveOrgCatalogAuth(event, deps.repo);
  if (!auth) return unauthorized();
  if (!auth.admin) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  if (!auth.org) return unauthorized();
  const bundle = await deps.repo.getAgent(orgScope(auth.org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');
  if (isBuiltin(bundle)) {
    return conflict(
      `"${name}" is a canonical built-in agent bundle — change its members in ` +
        `catalog/agents/bundles.json and re-seed; in-place edits are rejected.`,
    );
  }

  const members = bundle.members;
  await deps.repo.deleteAgent(orgScope(auth.org), name);
  return ok({ dissolved: true, members });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: AgentsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const name = pathParam(event, 'name');
  const path = event.requestContext.http.path;
  const isScopeRoute = path.endsWith('/scope');

  // The scope-change endpoint is retired in the org-only catalog.
  if (method === 'POST' && isScopeRoute) return gone('scope changes are retired');
  if (method === 'POST' && path.endsWith('/promote')) return promoteAgent(event, deps);
  if (method === 'POST' && path.endsWith('/members')) return addMember(event, deps);
  if (method === 'DELETE' && pathParam(event, 'member')) return removeMember(event, deps);
  if (method === 'POST' && path.endsWith('/dissolve')) return dissolveAgentBundle(event, deps);
  if (method === 'POST') return createAgent(event, deps);
  if (method === 'PUT') return createAgent(event, deps); // upsert
  if (method === 'DELETE') return deleteAgent(event, deps);
  if (method === 'GET' && name) return getAgent(event, deps);
  return resolveAgents(event, deps);
}
