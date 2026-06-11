import { beforeEach, describe, expect, it } from 'vitest';
import type { ObjectiveNode, WeeklyCommit } from '@harness/shared';
import { createObjective, getObjective, listObjectives } from '../src/rest/objectives.js';
import { buildTree, leafPct, recomputeRollup } from '../src/projections/rollup.js';
import type { CommitCredit } from '../src/projections/rollup.js';
import { recomputeOrgRollup } from '../src/projections/rollupRepo.js';
import {
  getObjective as pgGetObjective,
  putObjective as pgPutObjective,
} from '../src/db/pg/objectivesRepo.js';
import {
  createCommit as repoCreateCommit,
  upsertPlan,
} from '../src/db/pg/weeklyRepo.js';
import type { PgDb } from '../src/db/pg/migrate.js';
import { memRepoHarness } from './helpers/memtable.js';
import { makePgliteDb } from './helpers/pgharness.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';
import type { ObjectiveTreeNode } from '../src/projections/rollup.js';
import { randomUUID } from 'node:crypto';

/**
 * U10 REST: objectives + roll-up. CRUD (admin), tree assembly, and bottom-up
 * roll-up propagation — now from RECONCILED WEEKLY COMMITS (KTD5/U6), the single
 * source of truth (the GitHub `progressPct` feed is removed).
 *
 * Objectives + the weekly relations both live in Postgres (KTD7/U16): a fresh
 * pglite DB per test holds the RCDO tree AND the reconciled commits; the Dynamo
 * mem-repo still holds projects + profiles. The roll-up gather reads reconciled
 * commit credits straight off Postgres.
 */

const { repo } = memRepoHarness();

let db: PgDb;
beforeEach(async () => {
  db = await makePgliteDb();
});
const deps = () => ({ repo, db });

const ORG = 'acme';
const MATT = 'matt';
const PROJ = 'weekly-compass';
const WEEK = '2026-W23';

function node(id: string, level: ObjectiveNode['level'], parentId?: string): ObjectiveNode {
  return { id, org: ORG, level, title: id, parentId };
}

/** A reconciled commit credit (primary SO link + reconciled status) for the pure roll-up. */
function credit(supportingOutcomeId: string, status: CommitCredit['status']): CommitCredit {
  return { supportingOutcomeId, status };
}

/** A small RCDO tree in Postgres: rally -> outcome -> two supporting outcomes (leaves). */
async function seedTree(): Promise<void> {
  await pgPutObjective(db, node('rally', 'rally_cry'));
  await pgPutObjective(db, node('out', 'outcome', 'rally'));
  await pgPutObjective(db, node('so-a', 'supporting_outcome', 'out'));
  await pgPutObjective(db, node('so-b', 'supporting_outcome', 'out'));
}

/**
 * Insert a RECONCILED week with the given commits (the roll-up source). The plan
 * is stamped RECONCILED so `recomputeOrgRollup`'s gather (latest-reconciled
 * window) picks up its commits.
 */
async function seedReconciledWeek(
  commits: { so?: string; orphanReason?: WeeklyCommit['orphanReason']; status: WeeklyCommit['status'] }[],
  week = WEEK,
): Promise<void> {
  await upsertPlan(db, { projectId: PROJ, isoWeek: week, status: 'RECONCILED', posture: 'focus' });
  for (const c of commits) {
    await repoCreateCommit(db, {
      id: randomUUID(),
      projectId: PROJ,
      isoWeek: week,
      title: 'commit',
      ...(c.so ? { supportingOutcomeId: c.so } : {}),
      ...(c.orphanReason ? { orphanReason: c.orphanReason } : {}),
      alsoAdvances: [],
      category: c.orphanReason ?? 'Delivery',
      priorityNumeric: 1,
      status: c.status,
      carryDepth: 0,
    });
  }
}

describe('pure roll-up', () => {
  it('leaf % is the mean reconciled completion of the SO commits (2 done, 1 partial = 83.33)', () => {
    expect(leafPct('so-a', [credit('so-a', 'done'), credit('so-a', 'done'), credit('so-a', 'partial')])).toBeCloseTo(83.33, 1);
  });

  it('a leaf with no reconciled commits is 0% (no progressPct fallback)', () => {
    expect(leafPct('empty', [credit('so-a', 'done')])).toBe(0);
  });

  it('done is full credit, dropped/planned are zero', () => {
    expect(leafPct('so-a', [credit('so-a', 'done')])).toBe(100);
    expect(leafPct('so-a', [credit('so-a', 'dropped'), credit('so-a', 'planned')])).toBe(0);
  });

  it('internal nodes average their children, ignoring orphan links', () => {
    const nodes = [
      node('rally', 'rally_cry'),
      node('out', 'outcome', 'rally'),
      node('so-a', 'supporting_outcome', 'out'),
      node('so-b', 'supporting_outcome', 'out'),
    ];
    const commits: CommitCredit[] = [
      credit('so-a', 'done'), // so-a = 100
      credit('so-b', 'dropped'), // so-b = 0
      credit('ghost', 'done'), // orphan link, ignored
    ];
    const out = recomputeRollup({ nodes, commits });
    const byId = new Map(out.map((n) => [n.id, n.pct]));
    expect(byId.get('so-a')).toBe(100);
    expect(byId.get('so-b')).toBe(0);
    expect(byId.get('out')).toBe(50); // mean of children
    expect(byId.get('rally')).toBe(50); // propagates to the top
  });
});

describe('tree assembly', () => {
  it('nests nodes by parentId under roots', () => {
    const roots = buildTree([
      node('rally', 'rally_cry'),
      node('out', 'outcome', 'rally'),
      node('so', 'supporting_outcome', 'out'),
    ]);
    expect(roots).toHaveLength(1);
    const rally = roots[0]!;
    expect(rally.id).toBe('rally');
    expect(rally.children[0]!.id).toBe('out');
    expect(rally.children[0]!.children[0]!.id).toBe('so');
  });
});

describe('reconciled-commit org roll-up', () => {
  it('a reconciled SO lifts its node and propagates to the rally cry', async () => {
    await seedTree();
    // so-a fully done; so-b has no reconciled commits → stays 0%.
    await seedReconciledWeek([{ so: 'so-a', status: 'done' }]);

    await recomputeOrgRollup(db, repo, ORG, [PROJ]);

    expect((await pgGetObjective(db, ORG, 'so-a'))?.pct).toBe(100);
    expect((await pgGetObjective(db, ORG, 'so-b'))?.pct).toBe(0);
    expect((await pgGetObjective(db, ORG, 'out'))?.pct).toBe(50); // so-a 100, so-b 0
    expect((await pgGetObjective(db, ORG, 'rally'))?.pct).toBe(50);
  });

  it('an SO with no reconciled weekly data reads 0% (progressPct removed)', async () => {
    await seedTree();
    await recomputeOrgRollup(db, repo, ORG, [PROJ]);
    expect((await pgGetObjective(db, ORG, 'so-a'))?.pct).toBe(0);
  });

  it('an orphan-only reconciled week contributes nothing to any leaf', async () => {
    await seedTree();
    await seedReconciledWeek([{ orphanReason: 'Incident', status: 'done' }]);
    await recomputeOrgRollup(db, repo, ORG, [PROJ]);
    expect((await pgGetObjective(db, ORG, 'so-a'))?.pct).toBe(0);
    expect((await pgGetObjective(db, ORG, 'rally'))?.pct).toBe(0);
  });
});

describe('REST objectives', () => {
  it('GET /objectives returns the tree with cached roll-ups', async () => {
    await seedTree();
    await seedReconciledWeek([{ so: 'so-a', status: 'done' }]);
    await recomputeOrgRollup(db, repo, ORG, [PROJ]);

    const res = await listObjectives(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps());
    const { tree } = bodyOf<{ tree: ObjectiveTreeNode[] }>(res as { body: string });
    expect(tree[0]!.id).toBe('rally');
    expect(tree[0]!.pct).toBe(50);
  });

  it('POST /objectives requires admin and forces the caller org', async () => {
    const nonAdmin = await createObjective(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: node('r', 'rally_cry') }),
      deps(),
    );
    expect(nonAdmin).toMatchObject({ statusCode: 403 });

    const admin = await createObjective(
      httpEvent({
        method: 'POST',
        userId: MATT,
        org: ORG,
        admin: true,
        body: { id: 'r', level: 'rally_cry', title: 'R', org: 'evil-corp' },
      }),
      deps(),
    );
    expect(admin).toMatchObject({ statusCode: 201 });
    expect(await pgGetObjective(db, ORG, 'r')).toBeDefined(); // org forced to acme
  });

  it('GET /objectives/:id returns the node without a linked-tickets fan-out', async () => {
    await seedTree();
    const res = await getObjective(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { id: 'so-a' } }),
      deps(),
    );
    const body = bodyOf<{ node: ObjectiveNode; linkedTickets?: unknown }>(res as { body: string });
    expect(body.node.id).toBe('so-a');
    expect(body.linkedTickets).toBeUndefined();
  });
});