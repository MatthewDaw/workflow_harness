import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Memory, MemoryInput, Project } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { listMemories, reconcileMemories } from '../src/rest/memories.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * REST: project memories. The daemon PUTs the caller's WHOLE current memory set;
 * the handler stamps each with the caller's identity + now and reconciles the
 * caller's stored set to it (adds/updates land, on-disk deletions propagate),
 * scoped to the caller's own author key so users never clobber each other. GET
 * serves the project's whole set (all authors) to the project owner only.
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
const ALICE = 'alice';
const ORG = 'acme';
const PROJ = 'weekly-compass';

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

function putEvent(userId: string, memories: MemoryInput[]) {
  return httpEvent({
    method: 'PUT',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/memories`,
    path: { pid: PROJ },
    body: { memories },
  });
}

function getEvent(userId: string) {
  return httpEvent({
    method: 'GET',
    userId,
    org: ORG,
    rawPath: `/projects/${PROJ}/memories`,
    path: { pid: PROJ },
  });
}

const mem = (name: string, content: string): MemoryInput => ({ name, content });

describe('store + serve a posted memory set', () => {
  it('PUT stamps userId from the principal and GET returns the stored set', async () => {
    await repo.putProject(project(MATT));
    const res = await reconcileMemories(
      putEvent(MATT, [mem('deploy', 'use cdk deploy'), mem('db', 'real table is harness')]),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });

    const served = await listMemories(getEvent(MATT), deps);
    const { memories } = bodyOf<{ memories: Memory[] }>(served as { body: string });
    expect(memories).toHaveLength(2);
    for (const m of memories) {
      expect(m.userId).toBe(MATT); // stamped from the principal, not the payload
      expect(m.projectId).toBe(PROJ);
      expect(typeof m.updatedAt).toBe('number');
    }
    expect(memories.map((m) => m.name).sort()).toEqual(['db', 'deploy']);
  });

  it('GET 404s for a non-owner (owner-gated tab, no enumeration)', async () => {
    await repo.putProject(project(ALICE));
    const res = await listMemories(getEvent(MATT), deps);
    expect(res).toMatchObject({ statusCode: 404 });
  });

  it('GET 401s with no principal', async () => {
    await repo.putProject(project(MATT));
    const res = await listMemories(
      httpEvent({ method: 'GET', userId: null, rawPath: `/projects/${PROJ}/memories`, path: { pid: PROJ } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 401 });
  });

  it('rejects a malformed body (not the reconcile shape)', async () => {
    await repo.putProject(project(MATT));
    const res = await reconcileMemories(
      httpEvent({
        method: 'PUT',
        userId: MATT,
        org: ORG,
        rawPath: `/projects/${PROJ}/memories`,
        path: { pid: PROJ },
        body: { memories: 'not-an-array' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });
});

describe('full reconcile per author', () => {
  it('a later PUT omitting a memory REMOVES it; a different author is untouched', async () => {
    await repo.putProject(project(MATT));

    // Two authors sync into the same project.
    await reconcileMemories(putEvent(MATT, [mem('a', 'A'), mem('b', 'B')]), deps);
    await reconcileMemories(putEvent(ALICE, [mem('x', 'X')]), deps);

    // Matt re-syncs WITHOUT 'b' (deleted on disk) and with 'a' updated.
    await reconcileMemories(putEvent(MATT, [mem('a', 'A2')]), deps);

    const all = await repo.listMemories(PROJ);
    const matt = all.filter((m) => m.userId === MATT);
    const alice = all.filter((m) => m.userId === ALICE);

    // Matt's set is reconciled: 'b' is gone, 'a' is updated.
    expect(matt.map((m) => m.name)).toEqual(['a']);
    expect(matt[0]?.content).toBe('A2');
    // Alice's memories are completely untouched by Matt's reconcile.
    expect(alice.map((m) => m.name)).toEqual(['x']);
    expect(alice[0]?.content).toBe('X');
  });

  it('an empty array clears the caller set without touching others', async () => {
    await repo.putProject(project(MATT));
    await reconcileMemories(putEvent(MATT, [mem('a', 'A')]), deps);
    await reconcileMemories(putEvent(ALICE, [mem('x', 'X')]), deps);

    await reconcileMemories(putEvent(MATT, []), deps);

    const all = await repo.listMemories(PROJ);
    expect(all.filter((m) => m.userId === MATT)).toHaveLength(0);
    expect(all.filter((m) => m.userId === ALICE)).toHaveLength(1);
  });

  it('does not gate the PUT on project ownership (a collaborator may sync)', async () => {
    // Alice owns the project; Matt (a collaborator) syncs his own memories.
    await repo.putProject(project(ALICE));
    const res = await reconcileMemories(putEvent(MATT, [mem('note', 'from a non-owner')]), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    const all = await repo.listMemories(PROJ);
    expect(all.find((m) => m.userId === MATT)?.content).toBe('from a non-owner');
  });
});

describe('repo.replaceUserMemories (add/update/delete + per-user isolation)', () => {
  const stamp = (userId: string, name: string, content: string): Memory => ({
    projectId: PROJ,
    userId,
    name,
    content,
    updatedAt: 1,
  });

  it('adds, updates, and deletes the author set while isolating other authors', async () => {
    await repo.replaceUserMemories(PROJ, MATT, [stamp(MATT, 'a', 'A'), stamp(MATT, 'b', 'B')]);
    await repo.replaceUserMemories(PROJ, ALICE, [stamp(ALICE, 'a', 'alice-A')]);

    // Reconcile Matt: update 'a', drop 'b', add 'c'.
    await repo.replaceUserMemories(PROJ, MATT, [stamp(MATT, 'a', 'A2'), stamp(MATT, 'c', 'C')]);

    const all = await repo.listMemories(PROJ);
    const matt = all.filter((m) => m.userId === MATT);
    expect(matt.map((m) => m.name).sort()).toEqual(['a', 'c']);
    expect(matt.find((m) => m.name === 'a')?.content).toBe('A2');

    // Alice's same-named 'a' is a distinct record (keyed by userId) and untouched.
    const alice = all.filter((m) => m.userId === ALICE);
    expect(alice).toHaveLength(1);
    expect(alice[0]?.content).toBe('alice-A');
  });
});
