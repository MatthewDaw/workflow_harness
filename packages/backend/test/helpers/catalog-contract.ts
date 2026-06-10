import { describe, expect, it } from 'vitest';
import type { APIGatewayProxyEventV2 } from 'aws-lambda';
import type { Repo } from '../../src/db/repo.js';
import { MATT, ORG, SCOPE } from './factories.js';
import { adminEvent, bodyOf, httpEvent } from './httpevent.js';

/**
 * The shared org-catalog REST contract: every catalog resource (agents, skills,
 * mcp servers, workflows) lists at org scope, admin-gates writes, forces org
 * scope + stamps/preserves createdBy, snapshots rev 1 + a TRUE pointer on
 * create, promotes by repointing TRUE, and treats seed-owned built-ins as
 * fork-only. Resource-specific behavior (bundles, cascades, validation, fold,
 * golden replay, ...) stays in the resource's own test file.
 */

type Handler = (event: APIGatewayProxyEventV2) => Promise<unknown>;

export interface OrgCatalogContractConfig<
  R extends { name: string; scope?: unknown; createdBy?: unknown },
> {
  /** Response keys: create returns `{ [noun]: R }`, list returns `{ [plural]: R[] }`. */
  noun: string;
  plural: string;
  /** URL segment when it differs from `plural` (e.g. 'mcp-servers'). */
  pathPrefix?: string;
  /** Version-store entity tag, e.g. 'AGENT'. */
  entity: Parameters<Repo['getTrueVariant']>[1];
  repo: Repo;
  /** Two resource names for the list test; the first drives the other suites. */
  sampleNames: [string, string];
  builtinName: string;
  make: (name: string) => R;
  makeBuiltin: (name: string) => R;
  /** A modified copy for the PUT test + the assertion that the change landed. */
  mutate: (r: R) => R;
  assertMutated: (stored: R | undefined) => void;
  /** Handlers pre-bound to their deps. */
  create: Handler;
  remove: Handler;
  get: Handler;
  list: Handler;
  promote: Handler;
  /** Direct repo seam: put/read one record at org scope. */
  put: (r: R) => Promise<unknown>;
  read: (name: string) => Promise<R | undefined>;
  /** Skills gate promote on admin (skill-edit authority); the rest allow members. */
  nonAdminPromote: 'allowed' | 'forbidden';
  /** Extra check that a rejected built-in write left the record untouched. */
  assertBuiltinUntouched?: (stored: R | undefined) => void;
}

export function describeOrgCatalogContract<
  R extends { name: string; scope?: unknown; createdBy?: unknown },
>(cfg: OrgCatalogContractConfig<R>): void {
  const [nameA, nameB] = cfg.sampleNames;
  const prefix = cfg.pathPrefix ?? cfg.plural;

  const createdOf = (res: unknown): R & { baseName?: string; variantId?: string; version?: number } =>
    bodyOf<Record<string, R & { baseName?: string; variantId?: string; version?: number }>>(
      res as { body?: string },
    )[cfg.noun]!;

  describe(`POST /${cfg.plural} (admin-gated org write + createdBy)`, () => {
    it('forbids a non-admin create', async () => {
      const res = await cfg.create(
        httpEvent({ method: 'POST', userId: MATT, org: ORG, body: cfg.make(nameA) }),
      );
      expect(res).toMatchObject({ statusCode: 403 });
    });

    it('creates at org scope for an admin, ignoring a client scope, and stamps createdBy', async () => {
      const res = await cfg.create(
        adminEvent({
          method: 'POST',
          userId: MATT,
          // client tries to sneak a user scope; server must override to org.
          body: { ...cfg.make(nameA), scope: { tier: 'user', id: 'someone' } },
        }),
      );
      expect(res).toMatchObject({ statusCode: 201 });
      const stored = await cfg.read(nameA);
      expect(stored?.scope).toEqual({ tier: 'org', id: ORG });
      expect(stored?.createdBy).toEqual({ userId: MATT, name: MATT });
    });
  });

  describe(`PUT /${cfg.plural}/:name (preserves createdBy)`, () => {
    it('updates but keeps the original createdBy', async () => {
      await cfg.put({ ...cfg.make(nameA), createdBy: { userId: 'alice', name: 'Alice' } });
      const res = await cfg.create(
        adminEvent({
          method: 'PUT',
          userId: MATT,
          path: { name: nameA },
          body: cfg.mutate(cfg.make(nameA)),
        }),
      );
      expect(res).toMatchObject({ statusCode: 200 });
      const stored = await cfg.read(nameA);
      cfg.assertMutated(stored);
      expect(stored?.createdBy).toEqual({ userId: 'alice', name: 'Alice' });
    });
  });

  describe(`GET /${cfg.plural} (org catalog)`, () => {
    it('returns the org catalog for the caller', async () => {
      await cfg.put(cfg.make(nameA));
      await cfg.put(cfg.make(nameB));
      const res = await cfg.list(httpEvent({ method: 'GET', userId: MATT, org: ORG }));
      const records = bodyOf<Record<string, R[]>>(res as { body?: string })[cfg.plural]!;
      expect(records.map((r) => r.name).sort()).toEqual([nameA, nameB].sort());
    });

    it('is empty when the org has no records', async () => {
      const res = await cfg.list(httpEvent({ method: 'GET', userId: MATT, org: ORG }));
      expect(bodyOf<Record<string, R[]>>(res as { body?: string })[cfg.plural]).toEqual([]);
    });
  });

  describe(`GET /${cfg.plural}/:name`, () => {
    it('reads a record from the org catalog', async () => {
      await cfg.put(cfg.make(nameA));
      const res = await cfg.get(
        httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: nameA } }),
      );
      expect(res).toMatchObject({ statusCode: 200 });
    });

    it('404s a missing record', async () => {
      const res = await cfg.get(
        httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { name: 'ghost' } }),
      );
      expect(res).toMatchObject({ statusCode: 404 });
    });
  });

  describe(`DELETE /${cfg.plural}/:name`, () => {
    it('deletes for an admin', async () => {
      await cfg.put(cfg.make(nameA));
      const res = await cfg.remove(
        adminEvent({ method: 'DELETE', userId: MATT, path: { name: nameA } }),
      );
      expect(res).toMatchObject({ statusCode: 200 });
      expect(await cfg.read(nameA)).toBeUndefined();
    });

    it('forbids a non-admin delete', async () => {
      await cfg.put(cfg.make(nameA));
      const res = await cfg.remove(
        httpEvent({ method: 'DELETE', userId: MATT, org: ORG, path: { name: nameA } }),
      );
      expect(res).toMatchObject({ statusCode: 403 });
    });
  });

  describe('versioning: create snapshots rev 1 + promote repoints TRUE', () => {
    const promoteEvent = (userId: string, variantId: string, admin = false) =>
      httpEvent({
        method: 'POST',
        userId,
        org: ORG,
        admin,
        path: { name: nameA },
        rawPath: `/${prefix}/${nameA}/promote`,
        body: { variantId, rev: 1 },
      });

    it('stamps version fields on create and initializes the TRUE pointer', async () => {
      const res = await cfg.create(adminEvent({ method: 'POST', userId: MATT, body: cfg.make(nameA) }));
      expect(res).toMatchObject({ statusCode: 201 });
      const created = createdOf(res);
      expect(created.variantId).toBe(nameA);
      expect(created.baseName).toBe(nameA);
      expect(created.version).toBe(1);
      const truth = await cfg.repo.getTrueVariant(SCOPE, cfg.entity, nameA);
      expect(truth).toMatchObject({ baseName: nameA, variantId: nameA, rev: 1 });
    });

    if (cfg.nonAdminPromote === 'allowed') {
      it('a non-admin member may promote (not admin-gated) — repoints TRUE', async () => {
        await cfg.create(adminEvent({ method: 'POST', userId: MATT, body: cfg.make(nameA) }));
        const variantId = `${nameA}#R#r#U#bob`;
        const res = await cfg.promote(promoteEvent('bob', variantId));
        expect(res).toMatchObject({ statusCode: 200 });
        expect(await cfg.repo.getTrueVariant(SCOPE, cfg.entity, nameA)).toEqual({
          baseName: nameA,
          variantId,
          rev: 1,
        });
      });
    } else {
      it('forbids a non-admin promote (reconciled with the revision-write gate)', async () => {
        await cfg.create(adminEvent({ method: 'POST', userId: MATT, body: cfg.make(nameA) }));
        const res = await cfg.promote(promoteEvent('bob', `${nameA}#R#r#U#bob`));
        expect(res).toMatchObject({ statusCode: 403 });
      });

      it('repoints TRUE to the given variant for an admin', async () => {
        await cfg.create(adminEvent({ method: 'POST', userId: MATT, body: cfg.make(nameA) }));
        const variantId = `${nameA}#R#r#U#${MATT}`;
        const res = await cfg.promote(promoteEvent(MATT, variantId, true));
        expect(res).toMatchObject({ statusCode: 200 });
        expect(await cfg.repo.getTrueVariant(SCOPE, cfg.entity, nameA)).toEqual({
          baseName: nameA,
          variantId,
          rev: 1,
        });
      });
    }
  });

  describe(`built-in ${cfg.plural} are fork-only via REST (git-seed owned)`, () => {
    it('rejects an in-place PUT to a built-in (409), leaving it untouched', async () => {
      await cfg.put(cfg.makeBuiltin(cfg.builtinName));
      const res = await cfg.create(
        adminEvent({
          method: 'PUT',
          userId: MATT,
          path: { name: cfg.builtinName },
          body: cfg.make(cfg.builtinName),
        }),
      );
      expect(res).toMatchObject({ statusCode: 409 });
      cfg.assertBuiltinUntouched?.(await cfg.read(cfg.builtinName));
    });

    it('rejects deleting a built-in (409)', async () => {
      await cfg.put(cfg.makeBuiltin(cfg.builtinName));
      const res = await cfg.remove(
        adminEvent({ method: 'DELETE', userId: MATT, path: { name: cfg.builtinName } }),
      );
      expect(res).toMatchObject({ statusCode: 409 });
      expect(await cfg.read(cfg.builtinName)).toBeDefined();
    });

    it('ALLOWS forking a built-in (repoId + authorUserId set)', async () => {
      await cfg.put(cfg.makeBuiltin(cfg.builtinName));
      const res = await cfg.create(
        adminEvent({
          method: 'PUT',
          userId: MATT,
          path: { name: cfg.builtinName },
          body: { ...cfg.make(cfg.builtinName), repoId: 'repo1', authorUserId: MATT },
        }),
      );
      expect(res).toMatchObject({ statusCode: 200 });
    });
  });
}
