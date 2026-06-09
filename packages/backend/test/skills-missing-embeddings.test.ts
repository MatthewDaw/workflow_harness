import { describe, expect, it, vi } from 'vitest';
// The probe script (U23). `probeMissingEmbeddings` is the pure, dependency-
// injected core — it imports the heavy backend modules only inside `main()`, so
// importing it here touches no AWS clients and no compiled dist.
import { probeMissingEmbeddings } from '../../../infra/scripts/skills-missing-embeddings.mjs';

/**
 * U23 — skills-missing-embeddings probe. We drive `probeMissingEmbeddings` with a
 * mock skill catalog + a mock stored-vector map and assert it surfaces exactly
 * the skills whose embedding is MISSING or STALE (and omits the healthy ones):
 *  - a skill with NO stored vector            → reason `missing`;
 *  - a vector whose `descHash` is out of date → reason `stale-hash`;
 *  - a vector stamped an old version          → reason `stale-version`;
 *  - a vector matching hash + version         → healthy, NOT reported;
 *  - org scoping: keys are built `<org>#<baseName>` and never cross orgs.
 */

const contentHash = (description: string, body: string) => `h(${description}|${body})`;
const skillVectorKey = (org: string, baseName: string) => `${org}#${baseName}`;
const ACTIVE = 'titan-embed-text-v2';

function deps(over: Record<string, unknown> = {}) {
  return {
    orgs: ['acme'],
    skillsForOrg: vi.fn(async () => [
      { name: 'fresh', description: 'd1', body: 'b1' }, // healthy
      { name: 'changed', description: 'd2-new', body: 'b2' }, // stale-hash
      { name: 'never', description: 'd3', body: 'b3' }, // missing
      { name: 'oldmodel', description: 'd4', body: 'b4' }, // stale-version
    ]),
    // The stored vectors: `fresh` matches; `changed`'s stored hash is the OLD
    // content; `never` is absent; `oldmodel` is stamped an old version.
    getVectors: vi.fn(async (_index: string, keys: string[]) => {
      const m = new Map<string, Record<string, unknown>>();
      if (keys.includes('acme#fresh')) {
        m.set('acme#fresh', { descHash: contentHash('d1', 'b1'), embeddingVersion: ACTIVE });
      }
      if (keys.includes('acme#changed')) {
        m.set('acme#changed', { descHash: contentHash('d2-OLD', 'b2'), embeddingVersion: ACTIVE });
      }
      // 'acme#never' intentionally absent.
      if (keys.includes('acme#oldmodel')) {
        m.set('acme#oldmodel', { descHash: contentHash('d4', 'b4'), embeddingVersion: 'v1' });
      }
      return m;
    }),
    contentHash,
    skillVectorKey,
    skillIndex: 'skills',
    activeVersion: ACTIVE,
    ...over,
  };
}

describe('probeMissingEmbeddings (U23)', () => {
  it('surfaces missing, stale-hash, and stale-version skills; omits the healthy one', async () => {
    const d = deps();
    const res = await probeMissingEmbeddings(d);

    expect(res).toMatchObject({ orgs: 1, skillsChecked: 4 });
    const byName = Object.fromEntries(res.stale.map((s: { skillBaseName: string }) => [s.skillBaseName, s]));

    // The healthy skill is NOT reported.
    expect(byName.fresh).toBeUndefined();

    expect(byName.never).toMatchObject({ reason: 'missing', key: 'acme#never', org: 'acme' });
    expect(byName.changed).toMatchObject({ reason: 'stale-hash', key: 'acme#changed' });
    expect(byName.oldmodel).toMatchObject({
      reason: 'stale-version',
      foundVersion: 'v1',
      expectedVersion: ACTIVE,
    });
    expect(res.stale).toHaveLength(3);
  });

  it('fetches each skill at its <org>#<baseName> key', async () => {
    const d = deps();
    await probeMissingEmbeddings(d);
    const keys = d.getVectors.mock.calls[0]![1];
    expect(keys).toEqual(['acme#fresh', 'acme#changed', 'acme#never', 'acme#oldmodel']);
  });

  it('treats an untagged legacy vector (no version stamp) as healthy when the hash matches', async () => {
    const d = deps({
      skillsForOrg: vi.fn(async () => [{ name: 'legacy', description: 'dl', body: 'bl' }]),
      getVectors: vi.fn(async () =>
        // No embeddingVersion stamp — same-space per the U5 convention.
        new Map([['acme#legacy', { descHash: contentHash('dl', 'bl') }]]),
      ),
    });
    const res = await probeMissingEmbeddings(d);
    expect(res.stale).toHaveLength(0);
  });

  it('uses a baseName when present (a forked variant keys on its base)', async () => {
    const d = deps({
      skillsForOrg: vi.fn(async () => [
        { name: 'reconcile#R#repo1#U#matt', baseName: 'reconcile', description: 'd', body: 'b' },
      ]),
      getVectors: vi.fn(async (_i: string, keys: string[]) => {
        expect(keys).toEqual(['acme#reconcile']); // keyed on baseName, not the variant name
        return new Map();
      }),
    });
    const res = await probeMissingEmbeddings(d);
    expect(res.stale[0]).toMatchObject({ skillBaseName: 'reconcile', key: 'acme#reconcile', reason: 'missing' });
  });

  it('scopes per org — keys never cross org boundaries', async () => {
    const seen: string[] = [];
    const d = deps({
      orgs: ['acme', 'abc'],
      skillsForOrg: vi.fn(async (org: string) => [{ name: 's', description: 'd', body: 'b' }]),
      getVectors: vi.fn(async (_i: string, keys: string[]) => {
        seen.push(...keys);
        return new Map();
      }),
    });
    await probeMissingEmbeddings(d);
    expect(seen).toEqual(['acme#s', 'abc#s']);
  });
});
