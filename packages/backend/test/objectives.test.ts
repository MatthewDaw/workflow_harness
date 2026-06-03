import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { ObjectiveNode, Project } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { createObjective, getObjective, listObjectives } from '../src/rest/objectives.js';
import { buildTree, leafPct, recomputeRollup } from '../src/projections/rollup.js';
import type { ProjectProgress } from '../src/projections/rollup.js';
import { recomputeOrgRollup } from '../src/projections/rollupRepo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';
import type { ObjectiveTreeNode } from '../src/projections/rollup.js';

/**
 * U10 REST: objectives + roll-up. CRUD (admin), tree assembly, and bottom-up
 * roll-up propagation from project completion (the GitHub-sourced `progressPct`
 * stored on the project that owns each Supporting Outcome).
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

/** A project carries its stored completion + the Supporting Outcomes it owns. */
function progress(progressPct: number, supportingOutcomeIds: string[]): ProjectProgress {
  return { progressPct, supportingOutcomeIds };
}

/**
 * Persist a project with the given completion + owned Supporting Outcomes. The
 * extra `supportingOutcomeIds` ride along on the stored item (a later GitHub
 * read unit populates them from framing); the roll-up reads them back.
 */
async function putProjectProgress(progressPct: number, soIds: string[]): Promise<void> {
  await repo.putProject({
    id: PROJ,
    name: PROJ,
    repo: 'gh/acme/wc',
    ownerUserId: MATT,
    liveSessionCount: 0,
    progressPct,
    supportingOutcomeIds: soIds,
  } as Project & { supportingOutcomeIds: string[] });
}

/** A small RCDO tree: rally -> outcome -> two supporting outcomes (leaves). */
async function seedTree(): Promise<void> {
  await repo.putObjective(node('rally', 'rally_cry'));
  await repo.putObjective(node('out', 'outcome', 'rally'));
  await repo.putObjective(node('so-a', 'supporting_outcome', 'out'));
  await repo.putObjective(node('so-b', 'supporting_outcome', 'out'));
}

describe('pure roll-up', () => {
  it("leaf % is the owning project's stored progress", () => {
    expect(leafPct('so-a', [progress(60, ['so-a'])])).toBe(60);
  });

  it('a leaf with no owning project is 0%', () => {
    expect(leafPct('empty', [progress(80, ['so-a'])])).toBe(0);
  });

  it('a leaf owned by two projects averages their progress', () => {
    expect(leafPct('so-a', [progress(40, ['so-a']), progress(80, ['so-a'])])).toBe(60);
  });

  it('internal nodes average their children, ignoring orphan links', () => {
    const nodes = [
      node('rally', 'rally_cry'),
      node('out', 'outcome', 'rally'),
      node('so-a', 'supporting_outcome', 'out'),
      node('so-b', 'supporting_outcome', 'out'),
    ];
    const projects: ProjectProgress[] = [
      progress(100, ['so-a']), // so-a = 100
      progress(0, ['so-b']), // so-b = 0
      progress(100, ['ghost']), // orphan link, ignored
    ];
    const out = recomputeRollup({ nodes, projects });
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
  it("raising a project's completion lifts its SO and propagates to the rally cry", async () => {
    await seedTree();
    await putProjectProgress(0, ['so-a']);

    // Initially nothing done.
    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(0);

    // The project reports 100% complete on so-a; recompute.
    await putProjectProgress(100, ['so-a']);
    await recomputeOrgRollup(repo, ORG, [PROJ]);

    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(100);
    expect((await repo.getObjective(ORG, 'out'))?.pct).toBe(50); // so-a 100, so-b 0
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(50);
  });

  it('a project with no stored progress leaves its SO at 0%', async () => {
    await seedTree();
    await repo.putProject({
      id: PROJ,
      name: PROJ,
      repo: 'gh/acme/wc',
      ownerUserId: MATT,
      liveSessionCount: 0,
      supportingOutcomeIds: ['so-a'],
    } as Project & { supportingOutcomeIds: string[] });

    await recomputeOrgRollup(repo, ORG, [PROJ]);
    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(0);
  });
});

describe('REST objectives', () => {
  it('GET /objectives returns the tree with cached roll-ups', async () => {
    await seedTree();
    await putProjectProgress(100, ['so-a']);
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

  it('GET /objectives/:id returns the node without a linked-tickets fan-out', async () => {
    await seedTree();
    const res = await getObjective(
      httpEvent({ method: 'GET', userId: MATT, org: ORG, path: { id: 'so-a' } }),
      deps,
    );
    const body = bodyOf<{ node: ObjectiveNode; linkedTickets?: unknown }>(res as { body: string });
    expect(body.node.id).toBe('so-a');
    expect(body.linkedTickets).toBeUndefined();
  });
});
