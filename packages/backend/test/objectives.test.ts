import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { ObjectiveNode, Ticket, WeeklyUpdate } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { createObjective, getObjective, listObjectives } from '../src/rest/objectives.js';
import { buildTree, leafPct, recomputeRollup } from '../src/projections/rollup.js';
import { recomputeOrgRollup } from '../src/projections/rollupRepo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';
import type { ObjectiveTreeNode } from '../src/projections/rollup.js';

/**
 * U10 REST: objectives + roll-up. CRUD (admin), tree assembly, and bottom-up
 * roll-up propagation from linked tickets and published weekly updates.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');
const deps = { repo };

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const ORG = 'acme';
const MATT = 'matt';
const PROJ = 'weekly-compass';

function node(id: string, level: ObjectiveNode['level'], parentId?: string): ObjectiveNode {
  return { id, org: ORG, level, title: id, parentId };
}

function ticket(id: string, status: Ticket['status'], objectiveId?: string): Ticket {
  return { id, projectId: PROJ, title: id, status, priority: 'medium', objectiveId };
}

/** A small RCDO tree: rally -> outcome -> two supporting outcomes (leaves). */
async function seedTree(): Promise<void> {
  await repo.putObjective(node('rally', 'rally_cry'));
  await repo.putObjective(node('out', 'outcome', 'rally'));
  await repo.putObjective(node('so-a', 'supporting_outcome', 'out'));
  await repo.putObjective(node('so-b', 'supporting_outcome', 'out'));
  await repo.putProject({
    id: PROJ,
    name: PROJ,
    repo: 'gh/acme/wc',
    ownerUserId: MATT,
    liveSessionCount: 0,
  });
}

describe('pure roll-up', () => {
  it('leaf % is the done-fraction of its linked tickets', () => {
    const tickets = [
      ticket('t1', 'done', 'so-a'),
      ticket('t2', 'in_progress', 'so-a'),
      ticket('t3', 'done', 'other'),
    ];
    expect(leafPct('so-a', tickets, [])).toBe(50);
  });

  it('a leaf with no linked work is 0%', () => {
    expect(leafPct('empty', [], [])).toBe(0);
  });

  it('internal nodes average their children, ignoring orphan links', () => {
    const nodes = [
      node('rally', 'rally_cry'),
      node('out', 'outcome', 'rally'),
      node('so-a', 'supporting_outcome', 'out'),
      node('so-b', 'supporting_outcome', 'out'),
    ];
    const tickets = [
      ticket('t1', 'done', 'so-a'), // so-a = 100
      ticket('t2', 'backlog', 'so-b'), // so-b = 0
      ticket('orphan', 'done', 'ghost'), // ignored
    ];
    const out = recomputeRollup({ nodes, tickets, weeklyItems: [] });
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

describe('Streams-triggered org roll-up', () => {
  it('completing a ticket raises its SO and propagates to the rally cry', async () => {
    await seedTree();
    await repo.putTicket(ticket('t1', 'in_progress', 'so-a'));

    // Initially nothing done.
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(0);

    // Complete the ticket; recompute.
    await repo.putTicket(ticket('t1', 'done', 'so-a'));
    await recomputeOrgRollup(repo, ORG, [PROJ]);

    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(100);
    expect((await repo.getObjective(ORG, 'out'))?.pct).toBe(50); // so-a 100, so-b 0
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(50);
  });

  it('a published weekly update moves the linked outcome %; a draft does not', async () => {
    await seedTree();
    const draft: WeeklyUpdate = {
      projectId: PROJ,
      isoWeek: '2026-W23',
      done: [{ text: 'shipped', objectiveId: 'so-a', completionPct: 80 }],
      plan: [],
      validated: false,
    };
    await repo.putWeekly(draft);
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(0); // draft ignored

    await repo.putWeekly({ ...draft, validated: true });
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(80); // published moves it
  });
});

describe('REST objectives', () => {
  it('GET /objectives returns the tree with cached roll-ups', async () => {
    await seedTree();
    await repo.putTicket(ticket('t1', 'done', 'so-a'));
    await recomputeOrgRollup(repo, ORG, [PROJ]);

    const res = await listObjectives(httpEvent({ method: 'GET', userId: MATT, org: ORG }), deps);
    const { tree } = bodyOf<{ tree: ObjectiveTreeNode[] }>(res as { body: string });
    expect(tree[0]!.id).toBe('rally');
    expect(tree[0]!.pct).toBe(50);
  });

  it('POST /objectives requires admin and forces the caller org', async () => {
    const nonAdmin = await createObjective(
      httpEvent({ method: 'POST', userId: MATT, org: ORG, body: node('r', 'rally_cry') }),
      deps,
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
      deps,
    );
    expect(admin).toMatchObject({ statusCode: 201 });
    expect(await repo.getObjective(ORG, 'r')).toBeDefined(); // org forced to acme
  });

  it('GET /objectives/:id returns the node and its linked tickets', async () => {
    await seedTree();
    await repo.putTicket(ticket('t1', 'done', 'so-a'));
    const res = await getObjective(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { id: 'so-a' } }),
      deps,
    );
    const { node: n, linkedTickets } = bodyOf<{ node: ObjectiveNode; linkedTickets: Ticket[] }>(
      res as { body: string },
    );
    expect(n.id).toBe('so-a');
    expect(linkedTickets.map((t) => t.id)).toEqual(['t1']);
  });
});
