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
 * U4 REST: weekly updates are now store/serve for a client-posted report. PUT
 * stores the report (free-form `done`/`plan` summaries + a never-blocking
 * `conformityScore`); re-store overwrites; publish marks validated and recomputes
 * the org roll-up (which derives completion from project progress, not the
 * report). Weekly is scoped to the project owner.
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

function getEvent(userId: string) {
  return httpEvent({
    method: 'GET',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/weekly/${WEEK}`,
    path: { pid: PROJ, week: WEEK },
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

describe('store + serve a posted report', () => {
  it('stores a report (validated false) and serves it back via GET', async () => {
    await repo.putProject(project(MATT));
    const res = await putWeekly(
      putEvent(MATT, {
        done: 'Shipped reconciliation and export.',
        plan: 'Harden the importer; start the dashboard.',
        conformityScore: 82,
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });

    const served = await getWeekly(getEvent(MATT), deps);
    const { update } = bodyOf<{ update: WeeklyUpdate }>(served as { body: string });
    expect(update.validated).toBe(false);
    expect(update.done).toBe('Shipped reconciliation and export.');
    expect(update.plan).toBe('Harden the importer; start the dashboard.');
    expect(update.conformityScore).toBe(82); // conformity round-trips
  });

  it('re-store overwrites the week', async () => {
    await repo.putProject(project(MATT));
    await putWeekly(putEvent(MATT, { done: 'first', plan: 'a' }), deps);
    await putWeekly(putEvent(MATT, { done: 'second', plan: 'b', conformityScore: 50 }), deps);
    const stored = await repo.getWeekly(PROJ, WEEK);
    expect(stored?.done).toBe('second');
    expect(stored?.plan).toBe('b');
    expect(stored?.conformityScore).toBe(50);
  });

  it('a report without a conformity score stores and serves without one', async () => {
    await repo.putProject(project(MATT));
    await putWeekly(putEvent(MATT, { done: 'work', plan: 'more work' }), deps);
    const stored = await repo.getWeekly(PROJ, WEEK);
    expect(stored?.conformityScore).toBeUndefined();
  });

  it('rejects a malformed body (out-of-range conformity score)', async () => {
    await repo.putProject(project(MATT));
    const res = await putWeekly(putEvent(MATT, { done: 'x', conformityScore: 150 }), deps);
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('404s for a non-owner', async () => {
    await repo.putProject(project('alice'));
    const res = await getWeekly(getEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('publish recomputes the org roll-up', () => {
  it('flips validated and recomputes objective % from project progress', async () => {
    // The project owns SO-a and reports 75% complete (the GitHub-sourced number).
    await repo.putProject({
      ...project(MATT),
      progressPct: 75,
      supportingOutcomeIds: ['so-a'],
    } as Project & { supportingOutcomeIds: string[] });
    await repo.putObjective({ id: 'so-a', org: ORG, level: 'supporting_outcome', title: 'SO A' });

    // Establish a baseline cached %.
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(75);

    await putWeekly(putEvent(MATT, { done: 'shipped', plan: 'next', conformityScore: 90 }), deps);

    const res = await publishWeekly(publishEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const { update } = bodyOf<{ update: WeeklyUpdate }>(res as { body: string });
    expect(update.validated).toBe(true);
    expect(update.conformityScore).toBe(90);
    // Publish re-ran the roll-up: the linked SO reflects the project's progress.
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(75);
  });

  it('404s publish for a missing week', async () => {
    await repo.putProject(project(MATT));
    const res = await publishWeekly(publishEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });
});
