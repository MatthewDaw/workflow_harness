import { describe, expect, it } from 'vitest';
import type { WeeklyCommit, WeeklyPlan } from '@harness/shared';
import {
  createCommit,
  deleteCommit,
  getCommit,
  getPlan,
  listPlansForProject,
  listProjectsForOrg,
  listWeekCommits,
  updateCommit,
  upsertPlan,
  upsertProjectMirror,
} from '../src/db/pg/weeklyRepo.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { Repo } from '../src/db/repo.js';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { mockClient } from 'aws-sdk-client-mock';

/**
 * U2: the Postgres weekly relations + query layer (pglite-backed). Exercises the
 * two relations (KTD1 — `weekly_plans` + `weekly_commits`), the SO-or-orphan DB
 * CHECK (KTD10), the slim `projects` mirror (KTD7), and the org-scoped keyset
 * pagination (R7/R10).
 */

const PROJECT = 'proj-1';
const WEEK = '2026-W23';

const plan = (over: Partial<WeeklyPlan> = {}): WeeklyPlan => ({
  projectId: PROJECT,
  isoWeek: WEEK,
  status: 'DRAFT',
  posture: 'focus',
  ...over,
});

let seq = 0;
const commit = (over: Partial<WeeklyCommit> & { id?: string } = {}): WeeklyCommit => ({
  id: over.id ?? `c-${++seq}`,
  projectId: PROJECT,
  isoWeek: WEEK,
  title: over.title ?? 'do the thing',
  supportingOutcomeId: over.orphanReason ? undefined : (over.supportingOutcomeId ?? 'so-1'),
  alsoAdvances: [],
  category: 'Delivery',
  priorityNumeric: 1,
  status: 'planned',
  carryDepth: 0,
  ...over,
});

describe('weeklyRepo: plans (pglite)', () => {
  it('upsert + get round-trips a plan, omitting absent timestamps', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    const got = await getPlan(db, PROJECT, WEEK);
    expect(got).toEqual({ projectId: PROJECT, isoWeek: WEEK, status: 'DRAFT', posture: 'focus' });
    expect(got).not.toHaveProperty('lockedAt');
  });

  it('upsert overwrites status + stamps on the (projectId, isoWeek) key', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await upsertPlan(db, plan({ status: 'LOCKED', lockedAt: 1234 }));
    const got = await getPlan(db, PROJECT, WEEK);
    expect(got).toMatchObject({ status: 'LOCKED', lockedAt: 1234 });
    expect(await listPlansForProject(db, PROJECT)).toHaveLength(1); // upsert, not a 2nd row
  });

  it('getPlan is a miss for an unknown week', async () => {
    const db = await makePgliteDb();
    expect(await getPlan(db, PROJECT, '2099-W01')).toBeUndefined();
  });
});

describe('weeklyRepo: commits (pglite)', () => {
  it('upsert a plan + 3 commits; listWeekCommits returns 3', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await createCommit(db, commit({ id: 'a', priorityNumeric: 1 }));
    await createCommit(db, commit({ id: 'b', priorityNumeric: 3 }));
    await createCommit(db, commit({ id: 'c', orphanReason: 'Incident', priorityNumeric: 2 }));
    const got = await listWeekCommits(db, PROJECT, WEEK);
    expect(got).toHaveLength(3);
    // self-sorts by derived priority desc
    expect(got.map((c) => c.id)).toEqual(['b', 'c', 'a']);
  });

  it('the SO-or-orphan CHECK rejects a commit with neither at the DB layer', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    const bad = commit({ id: 'x' });
    // Force "neither" past the type system to exercise the DB CHECK (the Zod
    // refinement guards the REST layer; this is the DB-level half — KTD10).
    delete (bad as { supportingOutcomeId?: string }).supportingOutcomeId;
    await expect(createCommit(db, bad)).rejects.toThrow();
  });

  it('an orphan commit (orphanReason, no SO) inserts and reads back', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await createCommit(db, commit({ id: 'o', orphanReason: 'KTLO' }));
    const got = await getCommit(db, 'o');
    expect(got).toMatchObject({ orphanReason: 'KTLO' });
    expect(got).not.toHaveProperty('supportingOutcomeId');
  });

  it('updateCommit patches actual fields and returns the updated commit', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await createCommit(db, commit({ id: 'u' }));
    const updated = await updateCommit(db, 'u', { status: 'done', actualOutcome: 'shipped' });
    expect(updated).toMatchObject({ id: 'u', status: 'done', actualOutcome: 'shipped' });
  });

  it('updateCommit returns undefined for a missing commit', async () => {
    const db = await makePgliteDb();
    expect(await updateCommit(db, 'nope', { status: 'done' })).toBeUndefined();
  });

  it('deleteCommit removes a commit; reports false when absent', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await createCommit(db, commit({ id: 'd' }));
    expect(await deleteCommit(db, 'd')).toBe(true);
    expect(await getCommit(db, 'd')).toBeUndefined();
    expect(await deleteCommit(db, 'd')).toBe(false);
  });

  it('alsoAdvances round-trips as a text[] default []', async () => {
    const db = await makePgliteDb();
    await upsertPlan(db, plan());
    await createCommit(db, commit({ id: 'm', alsoAdvances: ['so-2', 'so-3'] }));
    expect((await getCommit(db, 'm'))?.alsoAdvances).toEqual(['so-2', 'so-3']);
  });
});

describe('weeklyRepo: projects mirror + org listing (pglite)', () => {
  it('listProjectsForOrg honors limit + cursor round-trip', async () => {
    const db = await makePgliteDb();
    for (const id of ['p1', 'p2', 'p3', 'p4', 'p5']) {
      await upsertProjectMirror(db, { id, org: 'acme', ownerUserId: 'u1', name: id });
    }
    const page1 = await listProjectsForOrg(db, 'acme', { limit: 2 });
    expect(page1.items.map((p) => p.id)).toEqual(['p1', 'p2']);
    expect(page1.nextCursor).toBe('p2');

    const page2 = await listProjectsForOrg(db, 'acme', { limit: 2, cursor: page1.nextCursor });
    expect(page2.items.map((p) => p.id)).toEqual(['p3', 'p4']);
    expect(page2.nextCursor).toBe('p4');

    const page3 = await listProjectsForOrg(db, 'acme', { limit: 2, cursor: page2.nextCursor });
    expect(page3.items.map((p) => p.id)).toEqual(['p5']);
    expect(page3.nextCursor).toBeUndefined(); // last page
  });

  it('listProjectsForOrg is org-scoped (cross-org isolation)', async () => {
    const db = await makePgliteDb();
    await upsertProjectMirror(db, { id: 'a', org: 'acme', ownerUserId: 'u', name: 'A' });
    await upsertProjectMirror(db, { id: 'b', org: 'other', ownerUserId: 'u', name: 'B' });
    const page = await listProjectsForOrg(db, 'acme');
    expect(page.items.map((p) => p.id)).toEqual(['a']);
  });

  it('upsertProjectMirror overwrites name on the id key (no duplicate row)', async () => {
    const db = await makePgliteDb();
    await upsertProjectMirror(db, { id: 'a', org: 'acme', ownerUserId: 'u', name: 'first' });
    await upsertProjectMirror(db, { id: 'a', org: 'acme', ownerUserId: 'u', name: 'second' });
    const page = await listProjectsForOrg(db, 'acme');
    expect(page.items).toEqual([{ id: 'a', org: 'acme', ownerUserId: 'u', name: 'second' }]);
  });

  it('a project write through the Repo lands a mirror row visible to listProjectsForOrg', async () => {
    const db = await makePgliteDb();
    const ddbMock = mockClient(DynamoDBDocumentClient);
    installInMemoryTable(ddbMock);
    const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
    const repo = new Repo(doc, 'harness-test', db);
    await repo.putProject({
      id: 'proj-x',
      name: 'Project X',
      repo: 'gh/acme/x',
      ownerUserId: 'u1',
      org: 'acme',
      liveSessionCount: 0,
      enabledSkills: [],
      enabledBundles: [],
      enabledAgents: [],
      enabledWorkflows: [],
      enabledAgentBundles: [],
      enabledMcpServers: [],
    });
    const page = await listProjectsForOrg(db, 'acme');
    expect(page.items).toEqual([
      { id: 'proj-x', org: 'acme', ownerUserId: 'u1', name: 'Project X' },
    ]);
    ddbMock.restore();
  });
});
