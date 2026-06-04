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
const FILES: SeedSkillFile[] = [
  { name: 'update-progress', description: 'push completion to GitHub', body: '# update-progress\nbody' },
  { name: 'weekly-update', description: 'weekly report to HQ', body: '# weekly-update\nbody' },
  { name: 'startforge', description: 'open a forge boundary', body: '# startforge\nbody' },
  { name: 'endforge', description: 'distill an agent', body: '# endforge\nbody' },
  { name: 'optimize-agent', description: 'refine an agent prompt', body: '# optimize-agent\nbody' },
];

describe('buildSeedSkills', () => {
  it('builds one org-scope built-in skill per file plus a bundle of all of them', () => {
    const records = buildSeedSkills(ORG, FILES);
    const skills = records.filter((r) => r.kind === 'skill');
    const bundles = records.filter((r) => r.kind === 'bundle');

    expect(skills).toHaveLength(FILES.length);
    expect(bundles).toHaveLength(1);

    for (const s of skills) {
      expect(s.scope).toEqual({ tier: 'org', id: ORG });
      expect(s.source).toBe('built-in');
      expect(s.createdBy).toEqual({ userId: 'system', name: 'system' });
      expect(s.body.length).toBeGreaterThan(0);
    }

    const bundle = bundles[0] as Skill;
    expect(bundle.name).toBe(STARTER_BUNDLE_NAME);
    expect(bundle.scope).toEqual({ tier: 'org', id: ORG });
    expect(bundle.members).toEqual(FILES.map((f) => f.name));
  });
});

describe('seedSkills', () => {
  it('is idempotent — running twice leaves one record per skill', async () => {
    await seedSkills(repo, ORG, FILES);
    await seedSkills(repo, ORG, FILES);

    const stored = await repo.listSkills(ORG);
    // N skills + 1 bundle, no duplicates from the second run.
    expect(stored).toHaveLength(FILES.length + 1);
    const names = stored.map((s) => s.name).sort();
    expect(names).toContain(STARTER_BUNDLE_NAME);
  });

  it('makes the bundle resolve for any user in the org with its members flattened', async () => {
    await seedSkills(repo, ORG, FILES);

    // A user who has registered nothing of their own still sees the org bundle.
    const res = await resolveSkills(
      httpEvent({ method: 'GET', userId: 'someone-else', org: ORG }),
      { repo },
    );
    const { skills } = bodyOf<{ skills: (Skill & { resolvedMembers?: string[] })[] }>(res);
    const bundle = skills.find((s) => s.name === STARTER_BUNDLE_NAME);

    expect(bundle).toBeDefined();
    expect(bundle?.kind).toBe('bundle');
    expect(bundle?.resolvedMembers?.sort()).toEqual(FILES.map((f) => f.name).sort());
  });
});
