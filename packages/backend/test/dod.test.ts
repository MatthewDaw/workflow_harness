import { describe, expect, it } from 'vitest';
import type { DefinitionOfDone } from '@harness/shared';
import { getDod, putDod } from '../src/rest/dod.js';
import { memRepoHarness } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * Plan-mapping feature 1: the org-wide Definition of Done. GET returns the floor
 * default when unset; PUT (admin-gated, org-scoped) round-trips a tightened DoD.
 * Advisory — the endpoint stores/serves config, it never gates anything.
 */

const { repo } = memRepoHarness();
const deps = { repo };

const MATT = 'matt';
const ORG = 'acme';

describe('GET /dod', () => {
  it('returns the org-wide floor default when none is set', async () => {
    const res = await getDod(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { dod } = bodyOf<{ dod: DefinitionOfDone }>(res as { body: string });
    expect(dod).toEqual({ requiresUnitTests: true, requiresProdE2E: false });
  });

  it('401s an unauthenticated caller', async () => {
    const res = await getDod(httpEvent({ method: 'GET', userId: null }), deps);
    expect(res).toMatchObject({ statusCode: 401 });
  });
});

describe('PUT /dod', () => {
  it('admin can set a tightened DoD and GET reads it back (round-trip)', async () => {
    const put = await putDod(
      httpEvent({
        method: 'PUT',
        userId: MATT,
        org: ORG,
        admin: true,
        body: { requiresUnitTests: true, requiresProdE2E: true, notes: 'prod E2E green' },
      }),
      deps,
    );
    expect(put).toMatchObject({ statusCode: 200 });
    const { dod } = bodyOf<{ dod: DefinitionOfDone }>(put as { body: string });
    expect(dod).toEqual({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'prod E2E green',
    });

    const got = await getDod(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    expect(bodyOf<{ dod: DefinitionOfDone }>(got as { body: string }).dod).toEqual(dod);
  });

  it('applies schema defaults on a partial body', async () => {
    const put = await putDod(
      httpEvent({ method: 'PUT', userId: MATT, org: ORG, admin: true, body: {} }),
      deps,
    );
    const { dod } = bodyOf<{ dod: DefinitionOfDone }>(put as { body: string });
    expect(dod).toEqual({ requiresUnitTests: true, requiresProdE2E: false });
  });

  it('forbids a non-admin (advisory config is still admin-curated)', async () => {
    const res = await putDod(
      httpEvent({ method: 'PUT', userId: MATT, org: ORG, body: { requiresProdE2E: true } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 403 });
    // Nothing was written: GET still returns the default.
    const got = await getDod(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    expect(bodyOf<{ dod: DefinitionOfDone }>(got as { body: string }).dod.requiresProdE2E).toBe(
      false,
    );
  });

  it('is org-scoped: a write in one org does not leak into another', async () => {
    await putDod(
      httpEvent({
        method: 'PUT',
        userId: MATT,
        org: ORG,
        admin: true,
        body: { requiresProdE2E: true },
      }),
      deps,
    );
    const other = await getDod(httpEvent({ method: 'GET', userId: 'eve', org: 'evil-corp' }), deps);
    expect(bodyOf<{ dod: DefinitionOfDone }>(other as { body: string }).dod.requiresProdE2E).toBe(
      false,
    );
  });

  it('rejects a malformed body (non-boolean flag)', async () => {
    const res = await putDod(
      httpEvent({
        method: 'PUT',
        userId: MATT,
        org: ORG,
        admin: true,
        body: { requiresUnitTests: 'nope' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});
