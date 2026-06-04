import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { resolveSkills } from '../src/rest/skills.js';
import {
  buildSeedSkills,
  seedSkills,
  STARTER_BUNDLE_NAME,
  type SeedSkillFile,
} from '../src/seed/skills.js';
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
  { name: 'hq-update-progress', description: 'push completion to GitHub', body: '# hq-update-progress\nbody' },
  { name: 'hq-weekly-update', description: 'weekly report to HQ', body: '# hq-weekly-update\nbody' },
  { name: 'hq-startforge', description: 'open a forge boundary', body: '# hq-startforge\nbody' },
  { name: 'hq-endforge', description: 'distill an agent', body: '# hq-endforge\nbody' },
  { name: 'hq-optimize-agent', description: 'refine an agent prompt', body: '# hq-optimize-agent\nbody' },
];
const STANDALONE_FILES: SeedSkillFile[] = [
  { name: 'gstack', description: 'headless browser QA', body: '# gstack\nbody' },
  { name: 'compound-engineering', description: 'install the CE plugin', body: '# compound-engineering\nbody' },
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

    // Every file is seeded as a standalone catalog skill...
    expect(skills).toHaveLength(FILES.length);
    expect(bundles).toHaveLength(1);

    for (const s of skills) {
      expect(s.scope).toEqual({ tier: 'org', id: ORG });
      expect(s.source).toBe('built-in');
      expect(s.createdBy).toEqual({ userId: 'system', name: 'system' });
      expect(s.body.length).toBeGreaterThan(0);
    }

    // ...but only the hq-* skills are members of command-hq-starter.
    const bundle = bundles[0] as Skill;
    expect(bundle.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle.scope).toEqual({ tier: 'org', id: ORG });
    expect(bundle.members).toEqual(HQ_FILES.map((f) => f.name));
    expect(bundle.members).not.toContain('gstack');
    expect(bundle.members).not.toContain('compound-engineering');
  });
});

describe('seedSkills', () => {
  it('is idempotent — running twice leaves one record per skill', async () => {
    await seedSkills(repo, ORG, FILES, MANIFEST);
    await seedSkills(repo, ORG, FILES, MANIFEST);

    const stored = await repo.listSkills(ORG);
    // N skills + 1 bundle, no duplicates from the second run.
    expect(stored).toHaveLength(FILES.length + 1);
    const names = stored.map((s) => s.name).sort();
    expect(names).toContain(STARTER_BUNDLE_NAME);
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
