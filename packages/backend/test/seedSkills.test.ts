import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { resolveSkills } from '../src/rest/skills.js';
import {
  assertBaseVariantOnly,
  buildSeedSkills,
  seedSkills,
  STARTER_BUNDLE_NAME,
  type SeedSkillFile,
} from '../src/seed/skills.js';
import { orgScope } from '@harness/shared';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U22: org-scope seed for the skills bundled with Command HQ + claude+. The
 * builder is pure; `seedSkills` is an idempotent upsert; after seeding, the
 * existing `resolveSkills` shows the bundle to any user in the org.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
// The `hq-*` files are product skills (bundle members); the non-`hq-` files are
// standalone catalog skills that must NOT join the starter bundle.
const HQ_FILES: SeedSkillFile[] = [
  {
    name: 'hq-update-progress',
    description: 'push completion to GitHub',
    body: '# hq-update-progress\nbody',
  },
  {
    name: 'hq-weekly-update',
    description: 'weekly report to HQ',
    body: '# hq-weekly-update\nbody',
  },
  { name: 'hq-startforge', description: 'open a forge boundary', body: '# hq-startforge\nbody' },
  { name: 'hq-endforge', description: 'distill an agent', body: '# hq-endforge\nbody' },
  {
    name: 'hq-optimize-agent',
    description: 'refine an agent prompt',
    body: '# hq-optimize-agent\nbody',
  },
];
const STANDALONE_FILES: SeedSkillFile[] = [
  { name: 'gstack', description: 'headless browser QA', body: '# gstack\nbody' },
  {
    name: 'compound-engineering',
    description: 'install the CE plugin',
    body: '# compound-engineering\nbody',
  },
];
const FILES: SeedSkillFile[] = [...HQ_FILES, ...STANDALONE_FILES];
// Manifest: only the product skills belong to command-hq-starter.
const MANIFEST = {
  [STARTER_BUNDLE_NAME]: {
    description: 'Skills bundled with Command HQ + claude+.',
    members: HQ_FILES.map((f) => f.name),
  },
};

describe('buildSeedSkills', () => {
  it('seeds one skill per file but bundles only the manifest-declared members', () => {
    const records = buildSeedSkills(ORG, FILES, MANIFEST);
    const skills = records.filter((r) => r.kind === 'skill');
    const bundles = records.filter((r) => r.kind === 'bundle');

    // Every file is seeded as a catalog skill...
    expect(skills).toHaveLength(FILES.length);
    expect(bundles).toHaveLength(1);

    for (const s of skills) {
      expect(s.source).toBe('built-in');
      expect(s.createdBy).toEqual({ userId: 'system', name: 'system' });
      expect(s.body.length).toBeGreaterThan(0);
      // Versioning: a seeded record is the BASE variant of its name at rev 1.
      expect(s.baseName).toBe(s.name);
      expect(s.variantId).toBe(s.name);
      expect(s.version).toBe(1);
      // Whole-dir storage: absent an explicit `files`, the seed falls back to a
      // SKILL.md-only map carrying the body.
      expect(s.files).toEqual({ 'SKILL.md': s.body });
    }

    // ...but only the hq-* skills are members of command-hq-starter.
    const bundle = bundles[0] as Skill;
    expect(bundle.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle.scope).toEqual({ tier: 'org', id: ORG });
    expect(bundle.members).toEqual(HQ_FILES.map((f) => f.name));
    expect(bundle.members).not.toContain('gstack');
    expect(bundle.members).not.toContain('compound-engineering');
  });

  it('preserves an explicit whole-directory files map (SKILL.md + siblings)', () => {
    const withDir: SeedSkillFile[] = [
      {
        name: 'hq-update-progress',
        description: 'push completion to GitHub',
        body: '# hq-update-progress\nbody',
        files: {
          'SKILL.md': '# hq-update-progress\nbody',
          'scripts/run.sh': 'echo hi',
          'reference/notes.md': 'notes',
        },
      },
    ];
    const records = buildSeedSkills(ORG, withDir, {
      [STARTER_BUNDLE_NAME]: { description: 'b', members: ['hq-update-progress'] },
    });
    const seeded = records.find((r) => r.name === 'hq-update-progress')!;
    expect(seeded.files).toEqual({
      'SKILL.md': '# hq-update-progress\nbody',
      'scripts/run.sh': 'echo hi',
      'reference/notes.md': 'notes',
    });
  });

  it('seeds only seeded-bundle members at org scope; non-members go user-narrow', () => {
    const GRANT = 'grant-owner';
    const records = buildSeedSkills(ORG, FILES, MANIFEST, GRANT);
    const orgScoped = records
      .filter((r) => r.kind === 'skill' && r.scope.tier === 'org')
      .map((r) => r.name);
    const userScoped = records.filter((r) => r.kind === 'skill' && r.scope.tier === 'user');

    // The org-wide default is EXACTLY the command-hq-starter members.
    expect(orgScoped.sort()).toEqual(HQ_FILES.map((f) => f.name).sort());

    // The non-bundle skills are NOT in the org default — they seed at the grant
    // owner's user scope so a fresh org cannot see them.
    expect(orgScoped).not.toContain('gstack');
    expect(orgScoped).not.toContain('compound-engineering');
    expect(userScoped.map((s) => s.name).sort()).toEqual(['compound-engineering', 'gstack']);
    for (const s of userScoped) {
      expect(s.scope).toEqual({ tier: 'user', id: GRANT });
    }
  });
});

describe('seedSkills', () => {
  it('is idempotent — running twice leaves one record per skill', async () => {
    await seedSkills(repo, ORG, FILES, MANIFEST);
    await seedSkills(repo, ORG, FILES, MANIFEST);

    // The org catalog (no viewer) holds only the org-default set: the starter
    // bundle members + the bundle record. The non-member skills seeded at the
    // grant owner's user scope are NOT in the org-only listing.
    const stored = await repo.listSkills(ORG);
    expect(stored).toHaveLength(HQ_FILES.length + 1);
    const names = stored.map((s) => s.name).sort();
    expect(names).toContain(STARTER_BUNDLE_NAME);
    expect(names).not.toContain('gstack');
    expect(names).not.toContain('compound-engineering');
  });

  it('makes the bundle resolve for any user in the org with its members flattened', async () => {
    await seedSkills(repo, ORG, FILES, MANIFEST);

    // A user who has registered nothing of their own still sees the org bundle.
    const res = await resolveSkills(
      httpEvent({ method: 'GET', userId: 'someone-else', org: ORG }),
      { repo },
    );
    const { skills } = bodyOf<{ skills: (Skill & { resolvedMembers?: string[] })[] }>(res);
    const bundle = skills.find((s) => s.name === STARTER_BUNDLE_NAME);

    expect(bundle).toBeDefined();
    expect(bundle?.kind).toBe('bundle');
    expect(bundle?.resolvedMembers?.sort()).toEqual(HQ_FILES.map((f) => f.name).sort());
  });
});

/**
 * U19 — SEED-SAFE PROMOTION. The seed owns the BASE variant only. The builder
 * asserts every record is a base variant (no fork identity, variantId ===
 * baseName === name), and a re-seed updates the base record via `putSkill` while
 * NEVER touching the per-baseName `#TRUE` pointer — so a fork promoted to the org
 * default survives a re-seed untouched, yet the base content still refreshes.
 */
describe('U19: seed-safe promotion (base-variant-only writes + #TRUE guard)', () => {
  it('assertBaseVariantOnly throws on a fork record', () => {
    const [base] = buildSeedSkills(ORG, HQ_FILES.slice(0, 1), {
      [STARTER_BUNDLE_NAME]: { description: 'b', members: [HQ_FILES[0]!.name] },
    });
    expect(() => assertBaseVariantOnly([base!])).not.toThrow();
    // A fork (repoId/authorUserId set) is rejected — the seed must never write it.
    expect(() =>
      assertBaseVariantOnly([{ ...base!, repoId: 'r1', authorUserId: 'matt' }]),
    ).toThrow(/fork/i);
    // A non-base variantId is rejected too.
    expect(() => assertBaseVariantOnly([{ ...base!, variantId: 'other' }])).toThrow(/base/i);
  });

  it('buildSeedSkills only ever produces base variants', () => {
    for (const r of buildSeedSkills(ORG, FILES, MANIFEST)) {
      expect(r.repoId).toBeUndefined();
      expect(r.authorUserId).toBeUndefined();
      expect(r.variantId).toBe(r.baseName);
      expect(r.baseName).toBe(r.name);
    }
  });

  it('a re-seed after a promote leaves the promoted #TRUE intact, but refreshes base content', async () => {
    const SCOPE = orgScope(ORG);
    const NAME = HQ_FILES[0]!.name; // an org-scoped bundle member
    const oneManifest = {
      [STARTER_BUNDLE_NAME]: { description: 'b', members: [NAME] },
    };

    // 1) Initial seed writes the base variant's live record (via `putSkill`). The
    //    seed never sets a `#TRUE` pointer — promotion is explicit — so there is no
    //    TRUE yet.
    await seedSkills(repo, ORG, [HQ_FILES[0]!], oneManifest);
    expect(await repo.getTrueVariant(SCOPE, 'SKILL', NAME)).toBeUndefined();

    // 2) A user FORKS the built-in (a repo-scoped fold) and a human PROMOTES the
    //    fork to the org default — exactly the U16/U19 fold→promote outcome.
    const base = await repo.getSkill(SCOPE, NAME);
    await repo.putNewVersion(
      'SKILL',
      { ...base!, body: 'forked + folded', repoId: 'repoX', authorUserId: 'matt' },
      { repoId: 'repoX', authorUserId: 'matt' },
    );
    const forkVariantId = `${NAME}#R#repoX#U#matt`;
    await repo.setTrueVariant(SCOPE, 'SKILL', {
      baseName: NAME,
      variantId: forkVariantId,
      rev: 1,
    });

    // 3) Re-seed with CHANGED base content (a catalog/skills edit + re-seed).
    const changed: SeedSkillFile = {
      name: NAME,
      description: 'updated description',
      body: 'updated base body',
    };
    await seedSkills(repo, ORG, [changed], oneManifest);

    // The promoted #TRUE STILL points at the fork — the re-seed never clobbered it.
    const afterTrue = await repo.getTrueVariant(SCOPE, 'SKILL', NAME);
    expect(afterTrue).toEqual({ baseName: NAME, variantId: forkVariantId, rev: 1 });

    // ...yet the BASE variant's live record DID refresh to the new content.
    const liveBase = await repo.getSkill(SCOPE, NAME);
    expect(liveBase?.body).toBe('updated base body');
    expect(liveBase?.description).toBe('updated description');
    // The live base record is still the base variant (no fork identity).
    expect(liveBase?.variantId).toBe(NAME);
    expect(liveBase?.repoId).toBeUndefined();
  });
});
