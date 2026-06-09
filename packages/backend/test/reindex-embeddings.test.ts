import { describe, expect, it, vi } from 'vitest';
// The reindex script (U5). `reindexAll` is the pure, dependency-injected core —
// it imports the heavy backend modules only inside `main()`, so importing it here
// for the unit under test touches no AWS clients and no compiled dist.
import { reindexAll } from '../../../infra/scripts/reindex-embeddings.mjs';

/**
 * U5 — reindex-embeddings backfill. We drive `reindexAll` with mock embed / vector
 * / org-walk deps and assert: it re-embeds EVERY skill across EVERY org under the
 * target version and flips the active-version pointer; the swap happens LAST (so a
 * query mid-reindex still resolves the OLD version); and a target/stamp mismatch
 * aborts WITHOUT swapping. `contentHash` + `skillVectorKey` are trivial stand-ins.
 */

const contentHash = (description: string, body: string) => `h(${description}|${body})`;
const skillVectorKey = (org: string, baseName: string) => `${org}#${baseName}`;

/** A mock embedder that stamps the given version on every embedding. */
function embedderFor(version: string) {
  return vi.fn(async (_text: string) => ({ vector: Array(1536).fill(0.1), embeddingVersion: version }));
}

const TWO_ORGS = {
  acme: [
    { name: 'hq-update-skills', description: 'edit skills', body: 'the body' },
    { name: 'hq-orchestrate', baseName: 'hq-orchestrate', description: 'orchestrate', body: 'b' },
  ],
  abc: [{ name: 'hq-add-skill', description: 'add', body: 'b2' }],
} as const;

function deps(version: string, overrides: Record<string, unknown> = {}) {
  return {
    orgs: ['acme', 'abc'],
    skillsForOrg: (org: 'acme' | 'abc') => Promise.resolve([...TWO_ORGS[org]]),
    embed: embedderFor(version),
    putVectors: vi.fn(async () => {}),
    contentHash,
    skillVectorKey,
    skillIndex: 'skills',
    swapActiveVersion: vi.fn(async () => {}),
    ...overrides,
  };
}

describe('reindexAll (U5)', () => {
  it('re-embeds every skill across every org under the target version', async () => {
    const d = deps('v2');
    const res = await reindexAll('v2', d);

    // 2 acme + 1 abc = 3 skills embedded + put.
    expect(d.embed).toHaveBeenCalledTimes(3);
    expect(d.putVectors).toHaveBeenCalledTimes(3);
    expect(res).toMatchObject({ orgs: 2, embedded: 3, activeVersion: 'v2' });

    // Every written vector is stamped target and keyed <org>#<baseName> with hash.
    const written = d.putVectors.mock.calls.map((c) => c[1][0]);
    expect(written).toContainEqual(
      expect.objectContaining({
        key: 'acme#hq-update-skills',
        metadata: expect.objectContaining({
          org: 'acme',
          skillBaseName: 'hq-update-skills',
          embeddingVersion: 'v2',
          descHash: 'h(edit skills|the body)',
        }),
      }),
    );
    expect(written).toContainEqual(
      expect.objectContaining({ key: 'abc#hq-add-skill', metadata: expect.objectContaining({ org: 'abc' }) }),
    );
  });

  it('flips the active version exactly once, AFTER all re-embeds (old version mid-reindex)', async () => {
    const calls: string[] = [];
    const d = deps('v2', {
      embed: vi.fn(async () => {
        calls.push('embed');
        return { vector: Array(1536).fill(0.1), embeddingVersion: 'v2' };
      }),
      swapActiveVersion: vi.fn(async (v: string) => {
        calls.push(`swap:${v}`);
      }),
    });

    await reindexAll('v2', d);

    // The swap is the LAST action — every embed precedes it. So any query issued
    // mid-reindex (before this final swap) still resolves the OLD active version.
    expect(calls).toEqual(['embed', 'embed', 'embed', 'swap:v2']);
    expect(d.swapActiveVersion).toHaveBeenCalledTimes(1);
    expect(d.swapActiveVersion).toHaveBeenCalledWith('v2');
  });

  it('does NOT swap when an embed stamps a version other than the target', async () => {
    // Misconfigured run: embedder still stamps v1 while target is v2 → abort.
    const d = deps('v1');
    await expect(reindexAll('v2', d)).rejects.toThrow(/stamped 'v1' but target is 'v2'/);
    expect(d.swapActiveVersion).not.toHaveBeenCalled();
  });

  it('does NOT swap when a vector write fails partway (active version stays old)', async () => {
    const d = deps('v2', {
      putVectors: vi
        .fn()
        .mockResolvedValueOnce(undefined)
        .mockRejectedValueOnce(new Error('ServiceUnavailableException')),
    });
    await expect(reindexAll('v2', d)).rejects.toThrow('ServiceUnavailableException');
    expect(d.swapActiveVersion).not.toHaveBeenCalled();
  });

  it('dry run reports counts without embedding, writing, or swapping', async () => {
    const d = deps('v2', { dryRun: true });
    const res = await reindexAll('v2', d);
    expect(d.embed).not.toHaveBeenCalled();
    expect(d.putVectors).not.toHaveBeenCalled();
    expect(d.swapActiveVersion).not.toHaveBeenCalled();
    expect(res).toMatchObject({ orgs: 2, embedded: 3, activeVersion: undefined });
  });

  it('requires a target version', async () => {
    await expect(reindexAll('', deps('v2'))).rejects.toThrow(/target embedding version is required/);
  });
});
