import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Skill } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { seedStarterForOrg } from '../src/seed/starter.js';
import { STARTER_BUNDLE_NAME } from '../src/seed/skills.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * A new org is pre-loaded with the command-hq-starter bundle + its members, cloned
 * from the template org's catalog. The user-granted built-ins (parked at user
 * scope) are NOT part of a new org's default and must not leak in.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

const TEMPLATE = 'acme';

/** Seed the template org with a starter bundle + two members, plus an unrelated
 *  org-skill and a user-granted skill that must NOT be cloned. */
async function seedTemplate(): Promise<void> {
  const mk = (over: Partial<Skill> & Pick<Skill, 'name' | 'scope' | 'kind'>): Skill => ({
    description: '',
    source: 'built-in',
    members: [],
    body: '',
    ...over,
  });
  await repo.putSkill(
    mk({ name: 'hq-one', scope: orgScope(TEMPLATE), kind: 'skill', body: 'one' }),
  );
  await repo.putSkill(
    mk({ name: 'hq-two', scope: orgScope(TEMPLATE), kind: 'skill', body: 'two' }),
  );
  await repo.putSkill(
    mk({
      name: STARTER_BUNDLE_NAME,
      scope: orgScope(TEMPLATE),
      kind: 'bundle',
      members: ['hq-one', 'hq-two'],
    }),
  );
  // An org skill that is NOT a starter member — must not be cloned.
  await repo.putSkill(mk({ name: 'extra', scope: orgScope(TEMPLATE), kind: 'skill' }));
  // A user-granted built-in (different scope) — must not be cloned.
  await repo.putSkill(mk({ name: 'gstack', scope: { tier: 'user', id: 'system' }, kind: 'skill' }));
}

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
  delete process.env.HQ_REPO_SKILLS_DIR; // force the clone path (no disk fallback)
});

describe('seedStarterForOrg', () => {
  it('clones the starter bundle + its members (only) into a new org', async () => {
    await seedTemplate();
    const written = await seedStarterForOrg(repo, 'newco');
    expect(written).toBe(3); // bundle + 2 members

    const catalog = await repo.listSkills('newco');
    const names = catalog.map((s) => s.name).sort();
    expect(names).toEqual([STARTER_BUNDLE_NAME, 'hq-one', 'hq-two']);
    // Bodies came along so a daemon can materialize them.
    expect(catalog.find((s) => s.name === 'hq-one')?.body).toBe('one');
    // Non-starter org skill + the user-granted built-in were NOT cloned.
    expect(names).not.toContain('extra');
    expect(names).not.toContain('gstack');
  });

  it('never clones onto the template org itself', async () => {
    await seedTemplate();
    expect(await seedStarterForOrg(repo, TEMPLATE)).toBe(0);
  });

  it('writes nothing (no throw) when there is no template + no disk source', async () => {
    expect(await seedStarterForOrg(repo, 'newco')).toBe(0);
    expect(await repo.listSkills('newco')).toEqual([]);
  });
});
