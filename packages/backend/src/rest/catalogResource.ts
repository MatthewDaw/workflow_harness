import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { ScopeRef } from '@harness/shared';
import { orgScope } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import type { CatalogKind } from '../db/keys.js';
import {
  badRequest,
  conflict,
  created,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
  unauthorized,
} from './runtime.js';
import { isBuiltin, requireOrgCatalogAdmin, requireOrgCatalogAuth } from './scopeauth.js';
import { effectiveOrg } from './membership.js';
import { resolvePrincipal } from './bearerAuth.js';
import { withAuthorNames } from './authorNames.js';
import { flattenBundle } from '../catalog/bundles.js';

/**
 * Generic org-catalog REST resource. Skills, agents, MCP servers, and workflows
 * share the same CRUD surface over the same persistence model:
 *
 *   GET    /<kind>                — the caller's org catalog (merged with their
 *                                   user scope; a user-scoped item shadows an
 *                                   org-scoped one of the same name)
 *   GET    /<kind>/:name          — one record from the org catalog
 *   POST   /<kind>                — create (server forces org scope + createdBy; admin)
 *   PUT    /<kind>/:name          — update (scope/createdBy immutable; admin)
 *   DELETE /<kind>/:name          — delete (admin)
 *   POST   /<kind>/:name/promote  — repoint the org-wide TRUE variant pointer
 *
 * Canonical built-ins are owned by the git seed (`createdBy.userId === 'system'`):
 * an in-place write/delete of the BASE variant is a 409 — REST callers fork
 * (set repoId + authorUserId) or change the seed and re-seed.
 *
 * VERSIONING (KTD6): every create/update SNAPSHOTS an immutable revision and
 * upserts the live record via `putNewVersion`, forking/advancing the variant
 * `(baseName, repoId, person)` instead of clobbering. Promotion ONLY repoints
 * TRUE; it never edits or deletes a variant.
 */

/** The structural shape every catalog record kind shares. */
type CatalogRecord = {
  name: string;
  scope: ScopeRef;
  baseName?: string;
  repoId?: string;
  authorUserId?: string;
  createdBy?: { userId: string; name: string };
  kind?: string;
  members?: string[];
} & Record<string, unknown>;

/** The slice of a Zod schema the generic create handler needs. */
interface SchemaLike {
  safeParse(
    input: unknown,
  ): { success: true; data: unknown } | { success: false; error: { message: string } };
}

export interface CatalogHandlersConfig {
  kind: CatalogKind;
  schema: SchemaLike;
  /** Human label in the 409 messages: 'skill' | 'agent' | 'MCP server' | 'workflow'. */
  label: string;
  /** Response key for a single record ('skill' | 'agent' | 'mcpServer' | 'workflow'). */
  responseKey: string;
  /** Response key for the catalog list ('skills' | 'agents' | 'mcpServers' | 'workflows'). */
  listKey: string;
  /** Where the canonical seed lives, for the 409 messages (e.g. 'catalog/skills'). */
  seedHint: string;
  repoOps: {
    list(repo: Repo, org: string, userId?: string): Promise<CatalogRecord[]>;
    get(repo: Repo, scope: ScopeRef, name: string): Promise<CatalogRecord | undefined>;
    del(repo: Repo, scope: ScopeRef, name: string): Promise<void>;
  };
  /**
   * Present for kinds with bundles (skills/agents): the list annotates bundles
   * with `resolvedMembers`, and create refuses a non-bundle write over an
   * existing bundle of the same name (sync upserts by name, so a colliding
   * local item would otherwise wipe the bundle's members).
   */
  bundles?: { noun: string; push: string; localName: string };
  /**
   * Promote admin-gating asymmetry (deliberate, preserved): skills' promote is
   * the skill-edit authority — same server-side admin as writing a revision —
   * while agents/mcp-servers/workflows promote is open to any authed member.
   */
  adminGatedPromote?: boolean;
  /** GET /:name resolves `createdBy.name` via withAuthorNames (skills only today). */
  enrichGetWithAuthorNames?: boolean;
}

type Handler = (
  event: APIGatewayProxyEventV2,
  deps: { repo: Repo },
) => Promise<APIGatewayProxyResultV2>;

export interface CatalogHandlers {
  list: Handler;
  get: Handler;
  create: Handler;
  remove: Handler;
  promote: Handler;
  /**
   * The generic route tail (promote + CRUD + list). A module matches its
   * kind-specific sub-routes first, then falls back here; `overrides` swaps in a
   * kind-specific promote/remove (skills' golden-replay promote, vector-cleanup
   * delete) while keeping the shared dispatch order.
   */
  dispatch(
    event: APIGatewayProxyEventV2,
    deps: { repo: Repo },
    overrides?: { promote?: Handler; remove?: Handler },
  ): Promise<APIGatewayProxyResultV2>;
}

export function makeCatalogHandlers(cfg: CatalogHandlersConfig): CatalogHandlers {
  const list: Handler = async (event, deps) => {
    const principal = await resolvePrincipal(event);
    if (!principal) return unauthorized();
    const org = (await effectiveOrg(event, deps.repo)) ?? principal.org;
    if (!org) return ok({ [cfg.listKey]: [] });

    const all = await cfg.repoOps.list(deps.repo, org, principal.userId);
    let annotated: CatalogRecord[] = all;
    if (cfg.bundles) {
      const byName = new Map(all.map((s) => [s.name, s])) as Map<
        string,
        { kind: string; members: string[] }
      >;
      annotated = all.map((s) =>
        s.kind === 'bundle'
          ? { ...s, resolvedMembers: flattenBundle({ members: s.members ?? [] }, byName) }
          : s,
      );
    }
    return ok({ [cfg.listKey]: await withAuthorNames(deps.repo, annotated) });
  };

  const get: Handler = async (event, deps) => {
    const gate = await requireOrgCatalogAuth(event, deps.repo);
    if ('error' in gate) return gate.error;
    const name = pathParam(event, 'name');
    if (!name) return badRequest('missing name');
    if (!gate.auth.org) return notFound();
    const item = await cfg.repoOps.get(deps.repo, orgScope(gate.auth.org), name);
    if (!item) return notFound();
    if (cfg.enrichGetWithAuthorNames) {
      const [enriched] = await withAuthorNames(deps.repo, [item]);
      return ok({ [cfg.responseKey]: enriched });
    }
    return ok({ [cfg.responseKey]: item });
  };

  const create: Handler = async (event, deps) => {
    const gate = await requireOrgCatalogAdmin(event, deps.repo);
    if ('error' in gate) return gate.error;
    const { principal, org } = gate.auth;

    const name = pathParam(event, 'name');
    const body = parseBodySafe(event);
    if (body === INVALID_JSON) return badRequest('invalid JSON body');

    // Force org scope (ignore any client-supplied scope) and parse the rest.
    const candidate = { ...(body as Record<string, unknown>), scope: orgScope(org) };
    const parsed = cfg.schema.safeParse(candidate);
    if (!parsed.success) return badRequest(parsed.error.message);
    const item = parsed.data as CatalogRecord;

    // Reject an in-place write to a canonical built-in's BASE variant (no
    // repo/author); forking is still allowed — that is how a project customizes
    // a built-in without touching the canonical.
    const targetName = name ?? item.name;
    const existing = await cfg.repoOps.get(deps.repo, orgScope(org), targetName);
    if (isBuiltin(existing) && !item.repoId && !item.authorUserId) {
      return conflict(
        `"${targetName}" is a canonical built-in ${cfg.label} — fork it (set repoId + ` +
          `authorUserId) or change it in ${cfg.seedHint} and re-seed; in-place writes are rejected.`,
      );
    }

    if (cfg.bundles && existing?.kind === 'bundle' && item.kind !== 'bundle') {
      return conflict(
        `"${targetName}" is already ${cfg.bundles.noun} in the org catalog — ${cfg.bundles.push} ` +
          `under the same name would clobber it and wipe its members. Rename the local ` +
          `${cfg.bundles.localName} (or the bundle) so their names don't collide.`,
      );
    }

    if (name) {
      // PUT /:name — update; preserve the createdBy stamp, and carry the variant
      // identity forward so an edit snapshots the NEXT revision of the SAME
      // variant rather than starting a new family at rev 1.
      item.createdBy = existing?.createdBy ?? item.createdBy;
      item.baseName = existing?.baseName ?? item.baseName ?? name;
    } else {
      // POST — stamp authorship from the principal.
      item.createdBy = { userId: principal.userId, name: principal.name ?? principal.userId };
      item.baseName = item.baseName ?? item.name;
    }

    const stamped = await deps.repo.putNewVersion(cfg.kind, item, {
      repoId: item.repoId,
      authorUserId: item.authorUserId,
    });
    return name ? ok({ [cfg.responseKey]: stamped }) : created({ [cfg.responseKey]: stamped });
  };

  const remove: Handler = async (event, deps) => {
    const gate = await requireOrgCatalogAdmin(event, deps.repo);
    if ('error' in gate) return gate.error;
    const name = pathParam(event, 'name');
    if (!name) return badRequest('missing name');
    const existing = await cfg.repoOps.get(deps.repo, orgScope(gate.auth.org), name);
    if (isBuiltin(existing)) {
      return conflict(
        `"${name}" is a canonical built-in ${cfg.label} — remove it from ${cfg.seedHint} and ` +
          `re-seed; it cannot be deleted via REST.`,
      );
    }
    await cfg.repoOps.del(deps.repo, orgScope(gate.auth.org), name);
    return ok({ deleted: true });
  };

  const promote: Handler = async (event, deps) => {
    const gate = cfg.adminGatedPromote
      ? await requireOrgCatalogAdmin(event, deps.repo)
      : await requireOrgCatalogAuth(event, deps.repo);
    if ('error' in gate) return gate.error;
    const name = pathParam(event, 'name');
    if (!name) return badRequest('missing name');
    if (!gate.auth.org) return unauthorized();
    const org = gate.auth.org;

    const body = parseBodySafe(event);
    if (body === INVALID_JSON) return badRequest('invalid JSON body');
    const variantId = (body as { variantId?: unknown })?.variantId;
    if (typeof variantId !== 'string' || !variantId) return badRequest('missing variantId');
    const revRaw = (body as { rev?: unknown })?.rev;
    const rev = typeof revRaw === 'number' ? revRaw : undefined;

    const pointer = { baseName: name, variantId, ...(rev !== undefined ? { rev } : {}) };
    await deps.repo.setTrueVariant(orgScope(org), cfg.kind, pointer);
    return ok({ true: pointer });
  };

  const dispatch: CatalogHandlers['dispatch'] = async (event, deps, overrides = {}) => {
    const method = event.requestContext.http.method;
    const path = event.requestContext.http.path;
    const name = pathParam(event, 'name');

    if (method === 'POST' && path.endsWith('/promote'))
      return (overrides.promote ?? promote)(event, deps);
    if (method === 'POST') return create(event, deps);
    if (method === 'PUT') return create(event, deps); // upsert
    if (method === 'DELETE') return (overrides.remove ?? remove)(event, deps);
    if (method === 'GET' && name) return get(event, deps);
    return list(event, deps);
  };

  return { list, get, create, remove, promote, dispatch };
}
