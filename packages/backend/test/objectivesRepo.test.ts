import { describe, expect, it } from 'vitest';
import type { ObjectiveNode } from '@harness/shared';
import {
  deleteObjective,
  getObjective,
  listObjectives,
  putObjective,
} from '../src/db/pg/objectivesRepo.js';
import { makePgliteDb } from './helpers/pgharness.js';

/**
 * U16: objectives persistence on Postgres (pglite-backed). Same contract the
 * Dynamo `Repo` exposed — put/get/delete/list over `ObjectiveNode`, org-scoped —
 * so the REST + roll-up callers repoint with no shape change.
 */

const ORG = 'acme';
const node = (over: Partial<ObjectiveNode> & Pick<ObjectiveNode, 'id'>): ObjectiveNode => ({
  org: ORG,
  level: 'supporting_outcome',
  title: over.title ?? `T-${over.id}`,
  ...over,
});

describe('objectivesRepo (pglite)', () => {
  it('put + get round-trips a node, omitting absent optional fields', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'rc1', level: 'rally_cry', title: 'Rally Cry' }));

    const got = await getObjective(db, ORG, 'rc1');
    expect(got).toEqual({ id: 'rc1', org: ORG, level: 'rally_cry', title: 'Rally Cry' });
    // No parentId / pct were set, so they must be absent (not null) — matches the Dynamo shape.
    expect(got).not.toHaveProperty('parentId');
    expect(got).not.toHaveProperty('pct');
  });

  it('round-trips parentId and pct when present', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'so1', parentId: 'out1', pct: 42 }));

    const got = await getObjective(db, ORG, 'so1');
    expect(got).toMatchObject({ id: 'so1', parentId: 'out1', pct: 42 });
  });

  it('upsert overwrites an existing node on the composite (org, id) key', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'o1', title: 'first', pct: 10 }));
    await putObjective(db, node({ id: 'o1', title: 'second', pct: 90 }));

    const got = await getObjective(db, ORG, 'o1');
    expect(got).toMatchObject({ title: 'second', pct: 90 });
    expect(await listObjectives(db, ORG)).toHaveLength(1); // upsert, not a second row
  });

  it('lists only the requested org (cross-org isolation)', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'a', org: ORG }));
    await putObjective(db, { id: 'b', org: 'other', level: 'rally_cry', title: 'B' });

    const acme = await listObjectives(db, ORG);
    expect(acme.map((n) => n.id)).toEqual(['a']);
    expect(acme.every((n) => n.org === ORG)).toBe(true);
  });

  it('getObjective is a miss for a non-existent id and for the wrong org', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'a', org: ORG }));
    expect(await getObjective(db, ORG, 'missing')).toBeUndefined();
    expect(await getObjective(db, 'other', 'a')).toBeUndefined(); // id exists, wrong org
  });

  it('delete removes a node and is a no-op when absent', async () => {
    const db = await makePgliteDb();
    await putObjective(db, node({ id: 'a' }));
    await deleteObjective(db, ORG, 'a');
    expect(await getObjective(db, ORG, 'a')).toBeUndefined();
    await deleteObjective(db, ORG, 'a'); // idempotent — no throw
  });
});
