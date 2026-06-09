import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type GoldenCase } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U18 — golden-case keys + repo CRUD. Golden cases are co-located with skills in
 * the org scope partition under an `IDEAGOLD#` SK prefix (sibling to `IDEA#`), so
 * — like ideas — they are invisible to `listSkills` (which scans `SKILL#`) and a
 * skill's whole golden set is one partition read. `caseId` is the folded
 * `ideaId`, so re-folding overwrites the case in place. Cases are org-scoped.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
const SKILL = 'hq-update-skills';

function goldenCase(over: Partial<GoldenCase> = {}): GoldenCase {
  return {
    caseId: 'i-1',
    skillBaseName: SKILL,
    org: ORG,
    ideaId: 'i-1',
    lesson: 'Prefer decimal money types; never use float for currency.',
    before: 'old body',
    after: 'new body with the lesson',
    foldedIntoRev: 2,
    createdAt: 1,
    ...over,
  };
}

describe('golden-case keys', () => {
  it('keys cases under the org scope partition by IDEAGOLD#<baseName>#<caseId>', () => {
    expect(k.goldenCaseKey(ORG, SKILL, 'i-1')).toEqual({
      PK: `SCOPE#org#${ORG}`,
      SK: `IDEAGOLD#${SKILL}#i-1`,
    });
    expect(k.goldenCasePrefixForSkill(ORG, SKILL)).toEqual({
      PK: `SCOPE#org#${ORG}`,
      skPrefix: `IDEAGOLD#${SKILL}#`,
    });
  });

  it('the IDEAGOLD# prefix never matches the SKILL# catalog scan prefix', () => {
    const { skPrefix } = k.goldenCasePrefixForSkill(ORG, SKILL);
    expect(skPrefix.startsWith('SKILL#')).toBe(false);
    expect(`IDEAGOLD#${SKILL}#i-1`.startsWith('SKILL#')).toBe(false);
  });
});

describe('Repo golden-case CRUD', () => {
  it('put / get / list a golden case for a skill', async () => {
    await repo.putGoldenCase(goldenCase());
    expect(await repo.getGoldenCase(ORG, SKILL, 'i-1')).toMatchObject({
      caseId: 'i-1',
      lesson: 'Prefer decimal money types; never use float for currency.',
      before: 'old body',
      after: 'new body with the lesson',
    });
    const all = await repo.listGoldenCasesForSkill(ORG, SKILL);
    expect(all).toHaveLength(1);
    expect(all[0]!.caseId).toBe('i-1');
  });

  it('re-folding the same idea overwrites its case in place (idempotent)', async () => {
    await repo.putGoldenCase(goldenCase({ after: 'first fold' }));
    await repo.putGoldenCase(goldenCase({ after: 'second fold', foldedIntoRev: 3 }));
    const all = await repo.listGoldenCasesForSkill(ORG, SKILL);
    expect(all).toHaveLength(1);
    expect(all[0]!.after).toBe('second fold');
    expect(all[0]!.foldedIntoRev).toBe(3);
  });

  it('golden cases do not leak into listSkills', async () => {
    // Seed a real skill record + a golden case in the same partition.
    await repo.putSkill({
      name: SKILL,
      scope: orgScope(ORG),
      kind: 'skill',
      description: '',
      source: 'local',
      members: [],
      body: 'b',
    });
    await repo.putGoldenCase(goldenCase());
    const skills = await repo.listSkills(ORG);
    expect(skills.map((s) => s.name)).toEqual([SKILL]);
    // The skill list never returns a golden-case row masquerading as a skill.
    expect(skills.some((s) => (s as { caseId?: string }).caseId !== undefined)).toBe(false);
  });

  it('cases are org-scoped: org B never lists org A cases', async () => {
    await repo.putGoldenCase(goldenCase({ org: 'acme' }));
    await repo.putGoldenCase(goldenCase({ org: 'globex', after: 'globex fold' }));
    const a = await repo.listGoldenCasesForSkill('acme', SKILL);
    const b = await repo.listGoldenCasesForSkill('globex', SKILL);
    expect(a).toHaveLength(1);
    expect(b).toHaveLength(1);
    expect(a[0]!.org).toBe('acme');
    expect(b[0]!.after).toBe('globex fold');
  });
});
