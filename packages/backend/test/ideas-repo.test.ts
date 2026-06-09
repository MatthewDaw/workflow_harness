import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { orgScope, type Idea, type UnassignedEntry } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U6 — idea + unassigned-bin keys, schemas, and repo CRUD with conditional
 * corroboration. Ideas are co-located with skills in the org scope partition
 * under an `IDEA#` SK prefix, so they are invisible to `listSkills` (which scans
 * `SKILL#`). Corroboration is derived (distinct sessions). The conditional
 * corroboration write mirrors `putSessionProjectionConditional` so a
 * fold↔corroboration race is safe.
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

function idea(over: Partial<Idea> = {}): Idea {
  return {
    ideaId: 'i-1',
    skillBaseName: SKILL,
    org: ORG,
    text: 'Prefer decimal money types; never use float for currency.',
    sources: [{ sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'raw provenance' }],
    status: 'open',
    corroborationVersion: 0,
    createdAt: 1,
    updatedAt: 1,
    ...over,
  };
}

describe('idea / bin keys', () => {
  it('keys ideas under the org scope partition by IDEA#<baseName>#<ideaId>', () => {
    expect(k.ideaKey(ORG, SKILL, 'i-1')).toEqual({
      PK: `SCOPE#org#${ORG}`,
      SK: `IDEA#${SKILL}#i-1`,
    });
    expect(k.ideaPrefixForSkill(ORG, SKILL)).toEqual({
      PK: `SCOPE#org#${ORG}`,
      skPrefix: `IDEA#${SKILL}#`,
    });
    expect(k.ideaPrefixForOrg(ORG)).toEqual({ PK: `SCOPE#org#${ORG}`, skPrefix: 'IDEA#' });
  });

  it('the IDEA# prefix never matches the SKILL# catalog scan prefix', () => {
    // The catalog list scans `SKILL#`; an idea SK begins `IDEA#`, so a
    // begins_with(SK, 'SKILL#') never matches it.
    expect(k.ideaKey(ORG, SKILL, 'i-1').SK.startsWith('SKILL#')).toBe(false);
    expect(k.ideaKey(ORG, SKILL, 'i-1').SK.startsWith('IDEA#')).toBe(true);
  });

  it('keys bin entries under the org scope partition by IDEABIN#<entryId>', () => {
    expect(k.unassignedBinKey(ORG, 'e-1')).toEqual({
      PK: `SCOPE#org#${ORG}`,
      SK: 'IDEABIN#e-1',
    });
    expect(k.unassignedBinPrefix(ORG)).toEqual({ PK: `SCOPE#org#${ORG}`, skPrefix: 'IDEABIN#' });
  });
});

describe('putIdea / getIdea / listIdeasForSkill / listIdeasForOrg', () => {
  it('round-trips an idea', async () => {
    await repo.putIdea(idea());
    const got = await repo.getIdea(ORG, SKILL, 'i-1');
    expect(got).toMatchObject({ ideaId: 'i-1', skillBaseName: SKILL, org: ORG, status: 'open' });
    expect(got!.text).toContain('decimal money');
  });

  it('lists ideas for one skill family', async () => {
    await repo.putIdea(idea({ ideaId: 'i-1' }));
    await repo.putIdea(idea({ ideaId: 'i-2' }));
    await repo.putIdea(idea({ ideaId: 'j-1', skillBaseName: 'other-skill' }));
    const forSkill = await repo.listIdeasForSkill(ORG, SKILL);
    expect(forSkill.map((i) => i.ideaId).sort()).toEqual(['i-1', 'i-2']);
  });

  it('lists every idea in an org across skills', async () => {
    await repo.putIdea(idea({ ideaId: 'i-1' }));
    await repo.putIdea(idea({ ideaId: 'j-1', skillBaseName: 'other-skill' }));
    const all = await repo.listIdeasForOrg(ORG);
    expect(all).toHaveLength(2);
  });

  it('deletes an idea', async () => {
    await repo.putIdea(idea());
    await repo.deleteIdea(ORG, SKILL, 'i-1');
    expect(await repo.getIdea(ORG, SKILL, 'i-1')).toBeUndefined();
  });
});

describe('ideas do NOT leak into listSkills', () => {
  it('a skill-list read returns no idea rows even when co-located', async () => {
    // A real skill record + an idea in the SAME org scope partition.
    await repo.putSkill({
      name: SKILL,
      scope: orgScope(ORG),
      kind: 'skill',
      description: 'edit skills',
      source: 'built-in',
      members: [],
      body: '# skill',
    });
    await repo.putIdea(idea());

    const skills = await repo.listSkills(ORG);
    expect(skills.map((s) => s.name)).toEqual([SKILL]);
    // No idea bled through: every returned row is a real skill (no IDEA# SK).
    expect(
      skills.some((s) => (s as unknown as { SK?: string }).SK?.startsWith('IDEA#')),
    ).toBe(false);
    // The idea is still independently queryable.
    expect(await repo.listIdeasForSkill(ORG, SKILL)).toHaveLength(1);
  });
});

describe('corroborateIdeaConditional (optimistic concurrency)', () => {
  it('creates with expectedVersion=undefined and bumps the version to 0', async () => {
    const res = await repo.corroborateIdeaConditional(
      idea({ corroborationVersion: 999 }),
      undefined,
    );
    expect(res.written).toBe(true);
    const got = await repo.getIdea(ORG, SKILL, 'i-1');
    // (undefined ?? -1) + 1 === 0
    expect(got!.corroborationVersion).toBe(0);
  });

  it('a second create on an existing idea fails the attribute_not_exists guard', async () => {
    await repo.corroborateIdeaConditional(idea(), undefined);
    const res = await repo.corroborateIdeaConditional(idea({ text: 'clobber' }), undefined);
    expect(res.written).toBe(false);
    expect((await repo.getIdea(ORG, SKILL, 'i-1'))!.text).not.toBe('clobber');
  });

  it('an update at the current version succeeds and bumps the version', async () => {
    await repo.corroborateIdeaConditional(idea(), undefined); // version -> 0
    const current = await repo.getIdea(ORG, SKILL, 'i-1');
    const res = await repo.corroborateIdeaConditional(
      {
        ...current!,
        sources: [
          ...current!.sources,
          { sessionId: 's-2', segmentId: 'seg-1', seq: 4, snippet: '' },
        ],
      },
      current!.corroborationVersion, // 0
    );
    expect(res.written).toBe(true);
    const got = await repo.getIdea(ORG, SKILL, 'i-1');
    expect(got!.corroborationVersion).toBe(1);
    expect(new Set(got!.sources.map((s) => s.sessionId)).size).toBe(2);
  });

  it('rejects a write against a STALE version (a concurrent writer won)', async () => {
    await repo.corroborateIdeaConditional(idea(), undefined); // version -> 0
    const stale = await repo.getIdea(ORG, SKILL, 'i-1'); // version 0

    // A concurrent fold advances the idea (0 -> 1).
    const winner = await repo.corroborateIdeaConditional(
      { ...stale!, status: 'folded', foldedIntoRev: 2 },
      0,
    );
    expect(winner.written).toBe(true);

    // Our stale corroboration still expects version 0 and is rejected (no clobber).
    const loser = await repo.corroborateIdeaConditional(
      { ...stale!, text: 'stale clobber' },
      0,
    );
    expect(loser.written).toBe(false);
    const got = await repo.getIdea(ORG, SKILL, 'i-1');
    expect(got!.status).toBe('folded'); // the fold survived
    expect(got!.text).not.toBe('stale clobber');
  });
});

describe('putUnassigned / listUnassignedForOrg', () => {
  const entry = (over: Partial<UnassignedEntry> = {}): UnassignedEntry => ({
    entryId: 'e-1',
    org: ORG,
    text: 'off-catalog topic with no matching skill',
    sources: [{ sessionId: 's-9', segmentId: 'seg-1', seq: 0, snippet: '' }],
    createdAt: 1,
    updatedAt: 1,
    ...over,
  });

  it('lists bin entries per org', async () => {
    await repo.putUnassigned(entry({ entryId: 'e-1' }));
    await repo.putUnassigned(entry({ entryId: 'e-2' }));
    // A different org's bin entry must not leak in.
    await repo.putUnassigned(entry({ entryId: 'e-3', org: 'other-org' }));

    const bin = await repo.listUnassignedForOrg(ORG);
    expect(bin.map((b) => b.entryId).sort()).toEqual(['e-1', 'e-2']);
    expect(await repo.listUnassignedForOrg('other-org')).toHaveLength(1);
  });

  it('bin entries never appear in the idea reads', async () => {
    await repo.putUnassigned(entry());
    expect(await repo.listIdeasForOrg(ORG)).toHaveLength(0);
  });
});
