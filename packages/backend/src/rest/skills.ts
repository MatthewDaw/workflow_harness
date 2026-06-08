import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { orgScope, skillSchema, type Skill } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  created,
  defaultRepo,
  forbidden,
  gone,
  notFound,
  ok,
  parseBody,
  pathParam,
  principalOf,
  unauthorized,
} from './runtime.js';
import { canWriteOrgCatalog, isAdmin } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';

/**
 * REST: skills + bundles — collapsed to a single ORG catalog.
 *
 *   GET    /skills                        — the caller's org catalog
 *   GET    /skills/:name                  — one skill from the org catalog
 *   POST   /skills                        — create (server forces org scope + createdBy; admin)
 *   PUT    /skills/:name                  — update (scope/createdBy immutable; admin)
 *   DELETE /skills/:name                  — delete (admin), 204
 *   POST   /skills/:name/members          — add a member ref to a bundle
 *   DELETE /skills/:name/members/:member  — eject a member (it stays standalone)
 *   POST   /skills/:name/dissolve         — flatten a bundle: members standalone, bundle removed
 *   GET    /skills/:name/usage            — count of agents depending on the skill
 *   POST   /skills/:name/scope            — RETIRED (410 Gone): no tiers in the org catalog
 *
 * Bundles hold member *refs* (names). A member may itself be a bundle (nesting);
 * resolution is transitive. Ejecting a member only edits the bundle.
 */

export interface SkillsDeps {
  repo: Repo;
}

/**
 * Flatten a bundle's members transitively into the set of leaf-skill names.
 * Nested bundles are expanded; a cycle is guarded by a visited set.
 */
export function flattenBundle(
  bundle: Skill,
  byName: Map<string, Skill>,
  seen = new Set<string>(),
): string[] {
  const leaves: string[] = [];
  for (const memberName of bundle.members) {
    if (seen.has(memberName)) continue;
    seen.add(memberName);
    const member = byName.get(memberName);
    if (member?.kind === 'bundle') {
      leaves.push(...flattenBundle(member, byName, seen));
    } else {
      leaves.push(memberName);
    }
  }
  return [...new Set(leaves)];
}

export async function resolveSkills(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  // Accept EITHER the gateway Cognito JWT (HQ web) OR a raw bearer device token
  // (the claude+ wrapper) — this route is HttpNoneAuthorizer so the gateway does
  // not pre-reject the device token. Fall back to the device token's own org claim
  // when there are no gateway claims to drive effectiveOrg.
  const principal = await resolvePrincipal(event);
  if (!principal) return unauthorized();
  const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
  if (!org) return ok({ skills: [] });

  // Pass the caller's userId so the merged org+user catalog is returned (a
  // user-scoped skill shadows an org-scoped one of the same name).
  const all = await deps.repo.listSkills(org, principal.userId);
  const byName = new Map(all.map((s) => [s.name, s]));
  // Annotate bundles with their transitively-resolved leaf members.
  const annotated = all.map((s) =>
    s.kind === 'bundle' ? { ...s, resolvedMembers: flattenBundle(s, byName) } : s,
  );
  return ok({ skills: annotated });
}

export async function createSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();

  const name = pathParam(event, 'name');
  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }

  // Force org scope (ignore any client-supplied scope) and parse the rest.
  const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
  const parsed = skillSchema.safeParse(candidate);
  if (!parsed.success) return badRequest(parsed.error.message);
  const skill: Skill = parsed.data;

  if (name) {
    // PUT /skills/:name — update; preserve the existing createdBy stamp.
    const existing = await deps.repo.getSkill(orgScope(org), name);
    skill.createdBy = existing?.createdBy ?? skill.createdBy;
    // Carry the variant identity forward so an edit snapshots the NEXT revision
    // of the SAME variant rather than starting a new family at rev 1.
    skill.baseName = existing?.baseName ?? skill.baseName ?? name;
  } else {
    // POST — stamp authorship from the principal.
    skill.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
    skill.baseName = skill.baseName ?? skill.name;
  }

  // VERSIONING (KTD6): every create/update SNAPSHOTS an immutable revision and
  // upserts the live record, forking/advancing the variant `(baseName, repoId,
  // person)` instead of clobbering. The base (org-seeded) variant has empty
  // repo/author. `putNewVersion` also writes the live record under `skillKey`,
  // so the existing read path is unchanged.
  const stamped = await deps.repo.putNewVersion('SKILL', skill, {
    repoId: skill.repoId,
    authorUserId: skill.authorUserId,
  });
  return name ? ok({ skill: stamped }) : created({ skill: stamped });
}

/**
 * POST /skills/:name/promote — repoint the org-wide TRUE variant for a baseName.
 * NOT admin-gated: ANY authed org member may promote (the decided model). Body:
 * `{ variantId, rev? }`. Promotion ONLY repoints TRUE; it never edits or deletes
 * a variant. Returns the new pointer.
 */
export async function promoteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const variantId = (body as { variantId?: unknown })?.variantId;
  if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
  const revRaw = (body as { rev?: unknown })?.rev;
  const rev = typeof revRaw === 'number' ? revRaw : undefined;

  const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
  await deps.repo.setTrueVariant(orgScope(org), 'SKILL', pointer);
  return ok({ true: pointer });
}

export async function getSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return notFound();
  const skill = await deps.repo.getSkill(orgScope(org), name);
  if (!skill) return notFound();
  return ok({ skill });
}

/** Count the agents (in the org catalog) whose `skills[]` references a skill. */
async function usageCount(repo: Repo, org: string, name: string): Promise<number> {
  const agents = await repo.listAgents(org);
  return agents.filter((a) => a.skills.includes(name)).length;
}

export async function getUsage(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return ok({ name, count: 0 });
  const count = await usageCount(deps.repo, org, name);
  return ok({ name, count });
}

export async function deleteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');
  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  await deps.repo.deleteSkill(orgScope(org), name);
  return ok({ deleted: true });
}

/** Add a member ref to a bundle. The member may be a skill or another bundle. */
export async function addMember(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const member = (body as { member?: unknown })?.member;
  if (typeof member !== 'string' || !member) return badRequest('missing member');

  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');

  if (!bundle.members.includes(member)) {
    bundle.members = [...bundle.members, member];
    await deps.repo.putSkill(bundle);
  }
  return ok({ skill: bundle });
}

/**
 * Remove/eject a member from a bundle. The member skill record is left intact —
 * "eject" simply means it is no longer in the bundle but still exists standalone.
 */
export async function removeMember(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  const member = pathParam(event, 'member');
  if (!name || !member) return badRequest('missing name or member');

  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');

  bundle.members = bundle.members.filter((m) => m !== member);
  await deps.repo.putSkill(bundle);
  return ok({ skill: bundle });
}

/**
 * Dissolve a bundle: its members all remain as standalone skills (they already
 * exist as their own records), and the bundle record itself is deleted.
 */
export async function dissolveBundle(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  if (!canWriteOrgCatalog(principal, isAdmin(event))) return forbidden();
  const name = pathParam(event, 'name');
  if (!name) return badRequest('missing name');

  const org = await effectiveOrg(event, deps.repo);
  if (!org) return unauthorized();
  const bundle = await deps.repo.getSkill(orgScope(org), name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');

  const members = bundle.members;
  await deps.repo.deleteSkill(orgScope(org), name);
  return ok({ dissolved: true, members });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SkillsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const name = pathParam(event, 'name');

  // The scope-change endpoint is retired in the org-only catalog.
  if (method === 'POST' && path.endsWith('/scope')) return gone('scope changes are retired');
  if (method === 'POST' && path.endsWith('/promote')) return promoteSkill(event, deps);
  if (method === 'POST' && path.endsWith('/members')) return addMember(event, deps);
  if (method === 'DELETE' && pathParam(event, 'member')) return removeMember(event, deps);
  if (method === 'POST' && path.endsWith('/dissolve')) return dissolveBundle(event, deps);
  if (method === 'GET' && path.endsWith('/usage')) return getUsage(event, deps);
  if (method === 'POST') return createSkill(event, deps);
  if (method === 'PUT') return createSkill(event, deps);
  if (method === 'DELETE') return deleteSkill(event, deps);
  if (method === 'GET' && name) return getSkill(event, deps);
  return resolveSkills(event, deps);
}
