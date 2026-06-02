import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, WeeklyUpdate } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { getWeekly, publishWeekly, putWeekly } from '../src/rest/weekly.js';
import { recomputeOrgRollup } from '../src/projections/rollupRepo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U11 REST: weekly updates. Store a draft, re-store overwrites, publish marks
 * validated and feeds the roll-up; weekly scoped to the project owner.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const MATT = 'matt';
const ORG = 'acme';
const PROJ = 'weekly-compass';
const WEEK = '2026-W23';

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

function putEvent(userId: string, body: unknown) {
  return httpEvent({
    method: 'PUT',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}`,
    path: { pid: PROJ, week: WEEK },
    body,
  });
}

function publishEvent(userId: string) {
  return httpEvent({
    method: 'POST',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}/publish`,
    path: { pid: PROJ, week: WEEK },
  });
}

describe('store + overwrite', () => {
  it('stores a draft (validated false) and re-store overwrites the week', async () => {
    await repo.putProject(project(MATT));
    await putWeekly(putEvent(MATT, { done: [{ text: 'a' }], plan: [{ text: 'b' }] }), deps);
    let stored = await repo.getWeekly(PROJ, WEEK);
    expect(stored?.validated).toBe(false);
    expect(stored?.done).toHaveLength(1);

    await putWeekly(putEvent(MATT, { done: [{ text: 'a' }, { text: 'c' }], plan: [] }), deps);
    stored = await repo.getWeekly(PROJ, WEEK);
    expect(stored?.done).toHaveLength(2);
    expect(stored?.plan).toHaveLength(0);
  });

  it('404s for a non-owner', async () => {
    await repo.putProject(project('alice'));
    const res = await getWeekly(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { pid: PROJ, week: WEEK } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('publish feeds the roll-up', () => {
  it('flips validated and moves the linked objective %', async () => {
    await repo.putProject(project(MATT));
    await repo.putObjective({ id: 'so-a', org: ORG, level: 'supporting_outcome', title: 'SO A' });
    // Establish a baseline cached %.
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(0);

    await putWeekly(
      putEvent(MATT, { done: [{ text: 'shipped', objectiveId: 'so-a', completionPct: 75 }] }),
      deps,
    );
    // Draft alone should not move %.
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(0);

    const res = await publishWeekly(publishEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    expect(bodyOf<{ update: WeeklyUpdate }>(res as { body: string }).update.validated).toBe(true);
    // Publish re-ran the roll-up: the linked SO now reflects the completion.
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(75);
  });

  it('404s publish for a missing week', async () => {
    await repo.putProject(project(MATT));
    const res = await publishWeekly(publishEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });
});
