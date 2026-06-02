import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  resolveScoped,
  scopeRefSchema,
  skillSchema,
  type ScopeContext,
  type ScopeRef,
  type Skill,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
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
import { canWriteScope, isAdmin } from './scopeauth.js';

/**
 * REST: skills + bundles (U9).
 *
 *   GET    /skills?project=<pid>          — effective set (narrowest wins)
 *   POST   /skills                        — create a skill or bundle
 *   GET    /skills/:name?tier=&id=        — one skill at an explicit scope
 *   DELETE /skills/:name?tier=&id=        — delete (returns blast radius if in use)
 *   POST   /skills/:name/members          — add a member ref to a bundle
 *   DELETE /skills/:name/members/:member  — remove/eject a member (it stays standalone)
 *   POST   /skills/:name/dissolve         — flatten a bundle: members standalone, bundle removed
 *   GET    /skills/:name/usage?tier=&id=  — count of agents depending on the skill
 *
 * Bundles hold member *refs* (names). A member may itself be a bundle (nesting);
 * resolution is transitive. Ejecting a member only edits the bundle — the member
 * skill record is untouched, so it remains usable standalone.
 */

export interface SkillsDeps {
  repo: Repo;
}

function visibleScopes(ctx: ScopeContext): ScopeRef[] {
  const scopes: ScopeRef[] = [
    { tier: 'org', id: ctx.org },
    { tier: 'user', id: ctx.userId },
  ];
  if (ctx.projectId) scopes.push({ tier: 'project', id: ctx.projectId });
  return scopes;
}

function scopeFromQuery(event: APIGatewayProxyEventV2): ScopeRef | undefined {
  const parsed = scopeRefSchema.safeParse({
    tier: queryParam(event, 'tier'),
    id: queryParam(event, 'id'),
  });
  return parsed.success ? parsed.data : undefined;
}

/**
 * Flatten a bundle's members transitively into the set of leaf-skill names.
 * Nested bundles are expanded; a cycle is guarded by a visited set. Resolution is
 * over the effective skill set for the context so members compose across tiers.
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
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const projectId = queryParam(event, 'project');
  const ctx: ScopeContext = { org: principal.org, userId: principal.userId, projectId };

  const all = await deps.repo.listSkills(visibleScopes(ctx));
  const effective = resolveScoped(all, ctx);
  const byName = new Map(effective.map((s) => [s.name, s]));
  // Annotate bundles with their transitively-resolved leaf members.
  const annotated = effective.map((s) =>
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
  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = skillSchema.safeParse(body);
  if (!parsed.success) return badRequest(parsed.error.message);
  const skill: Skill = parsed.data;
  if (!canWriteScope(skill.scope, principal, isAdmin(event))) return forbidden();
  await deps.repo.putSkill(skill);
  return created({ skill });
}

export async function getSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  const skill = await deps.repo.getSkill(scope, name);
  if (!skill) return notFound();
  return ok({ skill });
}

/** Count the agents (in the same scope set) whose `skills[]` references a skill. */
async function usageCount(repo: Repo, scope: ScopeRef, name: string): Promise<number> {
  const agents = await repo.listAgents([scope]);
  return agents.filter((a) => a.skills.includes(name)).length;
}

export async function getUsage(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  const count = await usageCount(deps.repo, scope, name);
  return ok({ name, count });
}

export async function deleteSkill(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  if (!canWriteScope(scope, principal, isAdmin(event))) return forbidden();

  // Surface the blast radius: agents that would lose this skill.
  const count = await usageCount(deps.repo, scope, name);
  await deps.repo.deleteSkill(scope, name);
  return ok({ deleted: true, usageCount: count });
}

/** Add a member ref to a bundle. The member may be a skill or another bundle. */
export async function addMember(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  if (!canWriteScope(scope, principal, isAdmin(event))) return forbidden();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const member = (body as { member?: unknown })?.member;
  if (typeof member !== 'string' || !member) return badRequest('missing member');

  const bundle = await deps.repo.getSkill(scope, name);
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
  const name = pathParam(event, 'name');
  const member = pathParam(event, 'member');
  const scope = scopeFromQuery(event);
  if (!name || !member || !scope) return badRequest('missing name, member, or scope');
  if (!canWriteScope(scope, principal, isAdmin(event))) return forbidden();

  const bundle = await deps.repo.getSkill(scope, name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');

  bundle.members = bundle.members.filter((m) => m !== member);
  await deps.repo.putSkill(bundle);
  return ok({ skill: bundle });
}

/**
 * Dissolve a bundle: its members all remain as standalone skills (they already
 * exist as their own records), and the bundle record itself is deleted. Returns
 * the freed members.
 */
export async function dissolveBundle(
  event: APIGatewayProxyEventV2,
  deps: SkillsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  if (!canWriteScope(scope, principal, isAdmin(event))) return forbidden();

  const bundle = await deps.repo.getSkill(scope, name);
  if (!bundle) return notFound();
  if (bundle.kind !== 'bundle') return badRequest('not a bundle');

  const members = bundle.members;
  await deps.repo.deleteSkill(scope, name);
  return ok({ dissolved: true, members });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: SkillsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const path = event.requestContext.http.path;
  const name = pathParam(event, 'name');

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
