import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Envelope, Event, Project, SessionProjection } from '@harness/shared';
import { Repo, type ConnectionRecord } from '../src/db/repo.js';
import { STALE_WINDOW_MS, control, getSession, listSessions } from '../src/rest/sessions.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { bodyOf, httpEvent } from './helpers/httpevent.js';

/**
 * U8 REST: sessions. Live filter, live-first ordering, uid scoping, paged event
 * replay, and 404 for a non-owned session.
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

function project(id: string, owner: string): Project {
  return { id, name: id, repo: `gh/acme/${id}`, ownerUserId: owner, liveSessionCount: 0 };
}

// Default lastEventAt to "now" so a session in a live status counts as fresh
// under read-time freshness; tests that want a stale session pass an old value.
function session(
  id: string,
  projectId: string,
  owner: string,
  status: SessionProjection['status'],
  lastEventAt = Date.now(),
): SessionProjection {
  return {
    sessionId: id,
    projectId,
    name: id,
    host: 'h',
    ownerUserId: owner,
    status,
    tokens: 0,
    costUsd: 0,
    startedAt: 1,
    lastEventAt,
    maxSeq: 0,
  };
}

function env(seq: number, event: Event): Envelope {
  return { v: 1, instanceId: 'i', host: 'h', ts: 1_700_000_000_000 + seq, seq, event };
}

describe('GET /sessions', () => {
  it('lists the caller sessions live-first, scoped to uid', async () => {
    const now = Date.now();
    await repo.putProject(project('p1', MATT));
    // idle-1 has the most recent activity but a non-live status; live-1 is active
    // and fresh, so it must still sort first (live-first beats recency).
    await repo.putSessionProjection(session('idle-1', 'p1', MATT, 'idle', now - 100));
    await repo.putSessionProjection(session('live-1', 'p1', MATT, 'active', now - 5000));
    await repo.putSessionProjection(session('alice-live', 'p2', ALICE, 'active', now - 1000));

    const res = await listSessions(httpEvent({ method: 'GET', userId: MATT }), deps);
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    expect(sessions.map((s) => s.sessionId)).toEqual(['live-1', 'idle-1']); // live first; alice excluded
  });

  it('?live=true returns only live sessions for the caller', async () => {
    await repo.putProject(project('p1', MATT));
    await repo.putSessionProjection(session('idle-1', 'p1', MATT, 'idle'));
    await repo.putSessionProjection(session('live-1', 'p1', MATT, 'active'));
    await repo.putSessionProjection(session('alice-live', 'p2', ALICE, 'active'));

    const res = await listSessions(
      httpEvent({ method: 'GET', userId: MATT, query: { live: 'true' } }),
      deps,
    );
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    expect(sessions.map((s) => s.sessionId)).toEqual(['live-1']);
  });

  it('?live=true excludes a stale active session (silent daemon) but keeps a fresh one', async () => {
    const now = Date.now();
    await repo.putProject(project('p1', MATT));
    // Fresh: heartbeated within the stale window — a genuinely-alive session.
    await repo.putSessionProjection(session('fresh', 'p1', MATT, 'active', now - 1000));
    // Stale: status is still active, but the daemon died (e.g. power loss) and
    // stopped heartbeating, so the last event is older than the stale window.
    await repo.putSessionProjection(
      session('stale', 'p1', MATT, 'active', now - STALE_WINDOW_MS - 1000),
    );

    const res = await listSessions(
      httpEvent({ method: 'GET', userId: MATT, query: { live: 'true' } }),
      deps,
    );
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    expect(sessions.map((s) => s.sessionId)).toEqual(['fresh']);
  });

  it('treats a stale active session as not-live in the full firehose ordering', async () => {
    const now = Date.now();
    await repo.putProject(project('p1', MATT));
    // The stale session has the most recent-looking status but a silent daemon;
    // the fresh-but-idle session is genuinely-not-live too. The fresh active one
    // must sort first. We assert the stale active session is NOT ranked live.
    await repo.putSessionProjection(session('fresh-live', 'p1', MATT, 'active', now - 500));
    await repo.putSessionProjection(
      session('stale-active', 'p1', MATT, 'active', now - STALE_WINDOW_MS - 1),
    );

    const res = await listSessions(httpEvent({ method: 'GET', userId: MATT }), deps);
    const { sessions } = bodyOf<{ sessions: SessionProjection[] }>(res as { body: string });
    // Stale active sorts below the fresh live session despite both being "active".
    expect(sessions[0]!.sessionId).toBe('fresh-live');
  });
});

describe('GET /sessions/:id', () => {
  it('returns the projection plus a page of events for the owner', async () => {
    await repo.putSessionProjection(session('s-1', 'p1', MATT, 'active'));
    await repo.appendEvent(
      env(0, { kind: 'session.start', sessionId: 's-1', projectId: 'p1', host: 'h', name: 'n' }),
    );
    await repo.appendEvent(
      env(1, { kind: 'tool.call', sessionId: 's-1', tool: 'Bash', argsSummary: 'ls' }),
    );

    const res = await getSession(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 's-1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 200 });
    const { session: s, events } = bodyOf<{ session: SessionProjection; events: Envelope[] }>(
      res as { body: string },
    );
    expect(s.sessionId).toBe('s-1');
    expect(events.map((e) => e.seq)).toEqual([0, 1]);
  });

  it('404s a session the caller does not own', async () => {
    await repo.putSessionProjection(session('s-1', 'p1', ALICE, 'active'));
    const res = await getSession(
      httpEvent({ method: 'GET', userId: MATT, path: { id: 's-1' } }),
      deps,
    );
    expect(res).toMatchObject({ statusCode: 404 });
  });
});

describe('POST /sessions/:id/control', () => {
  const INSTANCE = 'inst-a';

  function recordingPoster() {
    const posts: Array<{ connectionId: string; body: unknown }> = [];
    return {
      posts,
      poster: {
        post: async (connectionId: string, body: unknown) => {
          posts.push({ connectionId, body });
          return true;
        },
      },
    };
  }

  async function seedOwnedSessionWithDaemon(): Promise<void> {
    await repo.putSessionProjection({
      ...session('s-1', 'p1', MATT, 'active'),
      instanceId: INSTANCE,
    });
    const conn: ConnectionRecord = {
      connectionId: 'daemon-conn',
      userId: MATT,
      org: 'acme',
      role: 'daemon',
      instanceId: INSTANCE,
      connectedAt: 1,
    };
    await repo.putConnection(conn);
  }

  it('routes a control frame to the owning daemon and returns 202', async () => {
    await seedOwnedSessionWithDaemon();
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/sessions/s-1/control',
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'answer: 42' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 202 });
    expect(posts).toHaveLength(1);
    expect(posts[0]!.connectionId).toBe('daemon-conn');
    expect(posts[0]!.body).toMatchObject({
      type: 'control',
      sessionId: 's-1',
      action: 'inject',
      payload: { text: 'answer: 42' },
    });
  });

  it('shuts down a ghost session (no live daemon) by marking it done, returning 202', async () => {
    // Owned session whose daemon is gone — no INSTCONN reverse-index record.
    await repo.putSessionProjection({
      ...session('ghost-1', 'p1', MATT, 'needs_input'),
      instanceId: 'dead-inst',
    });
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        rawPath: '/sessions/ghost-1/control',
        path: { id: 'ghost-1' },
        body: { action: 'shutdown', payload: {} },
      }),
      { repo, poster },
    );
    // No daemon to route to, but the terminate is authoritative: marks done + 202.
    expect(res).toMatchObject({ statusCode: 202 });
    expect(posts).toHaveLength(0);
    const after = await repo.getSessionById('ghost-1');
    expect(after?.status).toBe('done');
  });

  it('still 502s a non-terminating control (inject) when the daemon is offline', async () => {
    await repo.putSessionProjection({
      ...session('s-2', 'p1', MATT, 'active'),
      instanceId: 'dead-inst',
    });
    const { poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { id: 's-2' },
        body: { action: 'inject', payload: { text: 'hi' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 502 });
  });

  it('404s a control frame from a non-owner and routes nothing', async () => {
    await seedOwnedSessionWithDaemon();
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: ALICE,
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'rm -rf /' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 404 });
    expect(posts).toHaveLength(0);
  });

  it('404s control for an unknown session (no enumeration)', async () => {
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 'ghost' }, body: { action: 'pause' } }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 404 });
    expect(posts).toHaveLength(0);
  });

  it('rejects an invalid control body (400)', async () => {
    await seedOwnedSessionWithDaemon();
    const { poster } = recordingPoster();
    const res = await control(
      httpEvent({ method: 'POST', userId: MATT, path: { id: 's-1' }, body: { action: 'nope' } }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 400 });
  });

  it('502s when the owning daemon is offline', async () => {
    await repo.putSessionProjection({
      ...session('s-1', 'p1', MATT, 'active'),
      instanceId: INSTANCE,
    });
    const { posts, poster } = recordingPoster();
    const res = await control(
      httpEvent({
        method: 'POST',
        userId: MATT,
        path: { id: 's-1' },
        body: { action: 'inject', payload: { text: 'hi' } },
      }),
      { repo, poster },
    );
    expect(res).toMatchObject({ statusCode: 502 });
    expect(posts).toHaveLength(0);
  });
});
