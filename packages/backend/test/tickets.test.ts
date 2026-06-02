import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, Ticket } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { createTicket, listTickets, transitionTicket } from '../src/rest/tickets.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U11 REST: tickets. Create + lifecycle transitions (with session/branch/PR
 * links), invalid-transition rejection, and project-owner scoping (404 for a
 * non-owner).
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
const PROJ = 'weekly-compass';

function project(owner: string): Project {
  return { id: PROJ, name: PROJ, repo: 'gh/acme/wc', ownerUserId: owner, liveSessionCount: 0 };
}

function statusEvent(tid: string, userId: string, body: Record<string, unknown>) {
  return httpEvent({
    method: 'POST',
    userId,
    rawPath: `/projects/${PROJ}/tickets/${tid}/status`,
    path: { pid: PROJ, tid },
    body,
  });
}

describe('create + list', () => {
  it('creates a ticket in backlog and lists it', async () => {
    await repo.putProject(project(MATT));
    const res = await createTicket(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { pid: PROJ },
        body: { id: 'WC-1', title: 'Reconcile view', priority: 'high' },
      }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 201 });
    expect(bodyOf<{ ticket: Ticket }>(res as { body: string }).ticket.status).toBe('backlog');

    const list = await listTickets(
      httpEvent({ method: 'GET', userId: MATT, path: { pid: PROJ } }),
      deps,
    );
    expect(
      bodyOf<{ tickets: Ticket[] }>(list as { body: string }).tickets.map((t) => t.id),
    ).toEqual(['WC-1']);
  });

  it('404s a project the caller does not own', async () => {
    await repo.putProject(project(ALICE));
    const res = await listTickets(
      httpEvent({ method: 'GET', userId: MATT, path: { pid: PROJ } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('status transitions', () => {
  beforeEach(async () => {
    await repo.putProject(project(MATT));
    await repo.putTicket({
      id: 'WC-1',
      projectId: PROJ,
      title: 'x',
      status: 'backlog',
      priority: 'medium',
    });
  });

  it('advances backlog -> in_progress and links a session', async () => {
    const res = await transitionTicket(
      statusEvent('WC-1', MATT, { to: 'in_progress', sessionId: 's-9' }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const t = bodyOf<{ ticket: Ticket }>(res as { body: string }).ticket;
    expect(t.status).toBe('in_progress');
    expect(t.sessionId).toBe('s-9');
  });

  it('advances in_review -> done, attaching a PR', async () => {
    await repo.putTicket({
      id: 'WC-1',
      projectId: PROJ,
      title: 'x',
      status: 'in_review',
      priority: 'medium',
    });
    const res = await transitionTicket(
      statusEvent('WC-1', MATT, { to: 'done', pr: 'gh#42' }),
      deps,
    );
    const t = bodyOf<{ ticket: Ticket }>(res as { body: string }).ticket;
    expect(t.status).toBe('done');
    expect(t.pr).toBe('gh#42');
  });

  it('rejects an invalid transition (backlog -> done)', async () => {
    const res = await transitionTicket(statusEvent('WC-1', MATT, { to: 'done' }), deps);
    expect(res).toMatchObject({ statusCode: 400 });
    // unchanged
    expect((await repo.getTicket(PROJ, 'WC-1'))?.status).toBe('backlog');
  });

  it('allows moving to icebox from any status', async () => {
    const res = await transitionTicket(statusEvent('WC-1', MATT, { to: 'icebox' }), deps);
    expect(res).toMatchObject({ statusCode: 200 });
    expect((await repo.getTicket(PROJ, 'WC-1'))?.status).toBe('icebox');
  });
});
