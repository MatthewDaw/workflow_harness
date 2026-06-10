import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { ScopeRef } from '@harness/shared';
import { orgScope } from '@harness/shared';
import type { Repo } from '../db/repo.js';
import {
  badRequest,
  conflict,
  notFound,
  ok,
  parseBodySafe,
  INVALID_JSON,
  pathParam,
} from './runtime.js';
import { isBuiltin, requireOrgCatalogAdmin } from './scopeauth.js';
import { flattenBundle } from '../catalog/bundles.js';

export { flattenBundle };

/** A catalog record that can be a bundle of named members. */
interface BundleLike {
  kind: string;
  members: string[];
  source?: string;
  createdBy?: { userId?: string };
}

export interface BundleMemberOps<T extends BundleLike> {
  get(repo: Repo, scope: ScopeRef, name: string): Promise<T | undefined>;
  put(repo: Repo, bundle: T): Promise<void>;
  del(repo: Repo, scope: ScopeRef, name: string): Promise<void>;
  /** Response body key for the mutated bundle (`skill` / `agent`). */
  responseKey: string;
  /** Noun in the canonical built-in 409 (`bundle` / `agent bundle`). */
  builtinLabel: string;
  /** Where the seed's members live (e.g. `catalog/skills/bundles.json`). */
  seedPath: string;
}

/**
 * The bundle membership verbs shared verbatim by skills and agents:
 *
 *   addMember    POST   /:name/members          — add a member ref
 *   removeMember DELETE /:name/members/:member  — eject (member stays standalone)
 *   dissolve     POST   /:name/dissolve         — members standalone, bundle removed
 *
 * All three are admin-gated catalog writes; a canonical built-in bundle is
 * git-seed owned, so in-place member edits are rejected (409) — change the seed
 * manifest and re-seed instead.
 */
export function makeBundleMemberHandlers<T extends BundleLike>(ops: BundleMemberOps<T>) {
  const builtinConflict = (name: string): APIGatewayProxyResultV2 =>
    conflict(
      `"${name}" is a canonical built-in ${ops.builtinLabel} — change its members in ` +
        `${ops.seedPath} and re-seed; in-place edits are rejected.`,
    );

  /** Load the named record iff it is a writable (non-built-in) bundle. */
  async function writableBundle(
    repo: Repo,
    org: string,
    name: string,
  ): Promise<{ bundle: T } | { error: APIGatewayProxyResultV2 }> {
    const bundle = await ops.get(repo, orgScope(org), name);
    if (!bundle) return { error: notFound() };
    if (bundle.kind !== 'bundle') return { error: badRequest('not a bundle') };
    if (isBuiltin(bundle)) return { error: builtinConflict(name) };
    return { bundle };
  }

  return {
    async addMember(
      event: APIGatewayProxyEventV2,
      deps: { repo: Repo },
    ): Promise<APIGatewayProxyResultV2> {
      const gate = await requireOrgCatalogAdmin(event, deps.repo);
      if ('error' in gate) return gate.error;
      const name = pathParam(event, 'name');
      if (!name) return badRequest('missing name');

      const body = parseBodySafe(event);
      if (body === INVALID_JSON) return badRequest('invalid JSON body');
      const member = (body as { member?: unknown })?.member;
      if (typeof member !== 'string' || !member) return badRequest('missing member');

      const found = await writableBundle(deps.repo, gate.auth.org, name);
      if ('error' in found) return found.error;
      const { bundle } = found;

      if (!bundle.members.includes(member)) {
        bundle.members = [...bundle.members, member];
        await ops.put(deps.repo, bundle);
      }
      return ok({ [ops.responseKey]: bundle });
    },

    async removeMember(
      event: APIGatewayProxyEventV2,
      deps: { repo: Repo },
    ): Promise<APIGatewayProxyResultV2> {
      const gate = await requireOrgCatalogAdmin(event, deps.repo);
      if ('error' in gate) return gate.error;
      const name = pathParam(event, 'name');
      const member = pathParam(event, 'member');
      if (!name || !member) return badRequest('missing name or member');

      const found = await writableBundle(deps.repo, gate.auth.org, name);
      if ('error' in found) return found.error;
      const { bundle } = found;

      bundle.members = bundle.members.filter((m) => m !== member);
      await ops.put(deps.repo, bundle);
      return ok({ [ops.responseKey]: bundle });
    },

    async dissolve(
      event: APIGatewayProxyEventV2,
      deps: { repo: Repo },
    ): Promise<APIGatewayProxyResultV2> {
      const gate = await requireOrgCatalogAdmin(event, deps.repo);
      if ('error' in gate) return gate.error;
      const name = pathParam(event, 'name');
      if (!name) return badRequest('missing name');

      const found = await writableBundle(deps.repo, gate.auth.org, name);
      if ('error' in found) return found.error;
      const { bundle } = found;

      const members = bundle.members;
      await ops.del(deps.repo, orgScope(gate.auth.org), name);
      return ok({ dissolved: true, members });
    },
  };
}
