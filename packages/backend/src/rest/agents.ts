import { BedrockRuntimeClient } from '@aws-sdk/client-bedrock-runtime';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import {
  agentSchema,
  resolveScoped,
  scopeChangeSchema,
  scopeRefSchema,
  type Agent,
  type ScopeContext,
  type ScopeRef,
} from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  bedrockOptimizeDeps,
  optimizeAgentPrompt,
  type OptimizeDeps,
  type OptimizeInput,
} from '../forge/optimize.js';
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
import { canReadScope, canWriteScope, isAdmin } from './scopeauth.js';

/**
 * REST: agents (U9) — scoped CRUD + scope elevate/demote + resolution.
 *
 *   GET    /agents?project=<pid>           — effective set for (caller, project),
 *                                            composing org+user+project tiers
 *                                            (narrowest scope wins on collision)
 *   POST   /agents                         — create at a scope (body.scope)
 *   GET    /agents/:name?tier=&id=         — one agent at an explicit scope
 *   PUT    /agents/:name                   — update at a scope (body.scope)
 *   DELETE /agents/:name?tier=&id=         — delete at a scope
 *   POST   /agents/:name/scope             — elevate/demote: rewrite the scope key
 *
 * The effective set is computed with `resolveScoped` from @harness/shared so the
 * web, wrapper, and backend all agree on collision resolution.
 */

export interface AgentsDeps {
  repo: Repo;
  /**
   * Injectable optimizer seam for POST /agents/{name}/optimize. Tests pass a
   * deterministic fake generate/judge; production omits it and the handler falls
   * back to `bedrockOptimizeDeps` with a live BedrockRuntimeClient.
   */
  optimize?: OptimizeDeps;
}

/** The org/user/project scopes visible to a (caller, project) context. */
function visibleScopes(ctx: ScopeContext): ScopeRef[] {
  const scopes: ScopeRef[] = [
    { tier: 'org', id: ctx.org },
    { tier: 'user', id: ctx.userId },
  ];
  if (ctx.projectId) scopes.push({ tier: 'project', id: ctx.projectId });
  return scopes;
}

/** Read an explicit scope from `?tier=&id=` query params. */
function scopeFromQuery(event: APIGatewayProxyEventV2): ScopeRef | undefined {
  const tier = queryParam(event, 'tier');
  const id = queryParam(event, 'id');
  const parsed = scopeRefSchema.safeParse({ tier, id });
  return parsed.success ? parsed.data : undefined;
}

export async function resolveAgents(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const projectId = queryParam(event, 'project');
  const ctx: ScopeContext = { org: principal.org, userId: principal.userId, projectId };

  const all = await deps.repo.listAgents(visibleScopes(ctx));
  const effective = resolveScoped(all, ctx);
  return ok({ agents: effective });
}

export async function createAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = agentSchema.safeParse(body);
  if (!parsed.success) return badRequest(parsed.error.message);
  const agent: Agent = parsed.data;

  if (!(await canWriteScope(agent.scope, principal, isAdmin(event), deps.repo))) return forbidden();

  await deps.repo.putAgent(agent);
  return created({ agent });
}

export async function getAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');

  // Gate the explicit-scope read: a missing read authorization is reported as a
  // 404 (not 403) so a caller cannot probe which scopes/items exist (IDOR).
  if (!(await canReadScope(scope, principal, deps.repo))) return notFound();

  const agent = await deps.repo.getAgent(scope, name);
  if (!agent) return notFound();
  return ok({ agent });
}

/**
 * Optimize a saved agent's prompt (U27 / AgentForge refine loop).
 *
 * Loads the agent at an explicit `?tier=&id=` scope (gated by `canReadScope`,
 * 404 on an unreadable scope to avoid IDOR enumeration), runs the injected
 * optimizer loop, and returns `{ optimizedPrompt, score, history }`. It does NOT
 * persist — the user reviews the result in the editor and saves via `saveAgent`.
 *
 * The optimizer deps are injectable (`deps.optimize`) so tests pass deterministic
 * fake generate/judge; the Lambda entry wires `bedrockOptimizeDeps` with a live
 * BedrockRuntimeClient (which is why the agents Lambda needs bedrock:InvokeModel).
 */
export async function optimizeAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');

  if (!(await canReadScope(scope, principal, deps.repo))) return notFound();

  const agent = await deps.repo.getAgent(scope, name);
  if (!agent) return notFound();

  const input: OptimizeInput = {
    prompt: agent.prompt,
    // A short description grounding the judge: the agent's name plus its skills.
    description: agent.skills.length
      ? `${agent.name} (skills: ${agent.skills.join(', ')})`
      : agent.name,
    skills: agent.skills,
    evidence: [],
  };

  const optimizeDeps = deps.optimize ?? bedrockOptimizeDeps(new BedrockRuntimeClient({}));
  const result = await optimizeAgentPrompt(input, optimizeDeps);

  return ok({
    optimizedPrompt: result.prompt,
    score: result.score,
    history: result.history,
  });
}

export async function deleteAgent(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const scope = scopeFromQuery(event);
  if (!name || !scope) return badRequest('missing name or scope');
  if (!(await canWriteScope(scope, principal, isAdmin(event), deps.repo))) return forbidden();

  await deps.repo.deleteAgent(scope, name);
  return ok({ deleted: true });
}

/**
 * Elevate/demote an agent. The new scope arrives in the body; we write the agent
 * at the new scope key and delete the old one, so a project `builder` can be
 * promoted to user then org (and back). Both the source and destination scope
 * must be writable by the caller.
 */
export async function changeScope(
  event: APIGatewayProxyEventV2,
  deps: AgentsDeps,
): Promise<APIGatewayProxyResultV2> {
  const principal = principalOf(event);
  if (!principal) return unauthorized();
  const name = pathParam(event, 'name');
  const fromScope = scopeFromQuery(event);
  if (!name || !fromScope) return badRequest('missing name or source scope');

  let body: unknown;
  try {
    body = parseBody(event);
  } catch {
    return badRequest('invalid JSON body');
  }
  const parsed = scopeChangeSchema.safeParse(body);
  if (!parsed.success) return badRequest(parsed.error.message);
  const toScope = parsed.data.scope;

  const admin = isAdmin(event);
  if (
    !(await canWriteScope(fromScope, principal, admin, deps.repo)) ||
    !(await canWriteScope(toScope, principal, admin, deps.repo))
  ) {
    return forbidden();
  }

  const existing = await deps.repo.getAgent(fromScope, name);
  if (!existing) return notFound();

  // Re-parse through the schema so stale PK/SK/GSI attributes read back from the
  // table are stripped before re-keying at the new scope.
  const moved: Agent = agentSchema.parse({ ...existing, scope: toScope });
  await deps.repo.putAgent(moved);
  // Avoid deleting if the key didn't change (same scope = no-op move).
  if (!(fromScope.tier === toScope.tier && fromScope.id === toScope.id)) {
    await deps.repo.deleteAgent(fromScope, name);
  }
  return ok({ agent: moved });
}

export async function handler(event: APIGatewayProxyEventV2): Promise<APIGatewayProxyResultV2> {
  const deps: AgentsDeps = { repo: defaultRepo() };
  const method = event.requestContext.http.method;
  const name = pathParam(event, 'name');
  const path = event.requestContext.http.path;
  const isScopeRoute = path.endsWith('/scope');
  const isOptimizeRoute = path.endsWith('/optimize');

  if (method === 'POST' && isOptimizeRoute) return optimizeAgent(event, deps);
  if (method === 'POST' && isScopeRoute) return changeScope(event, deps);
  if (method === 'POST') return createAgent(event, deps);
  if (method === 'PUT') return createAgent(event, deps); // upsert
  if (method === 'DELETE') return deleteAgent(event, deps);
  if (method === 'GET' && name) return getAgent(event, deps);
  return resolveAgents(event, deps);
}
