import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import type { Envelope, Event } from '@harness/shared';
import { Repo, type ConnectionRecord } from '../src/db/repo.js';
import { ingest, type EventDeps } from '../src/ws/event.js';
import { connect } from '../src/ws/connect.js';
import { disconnect } from '../src/ws/disconnect.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U6 event ingestion: append + projection upsert, out-of-order/duplicate seq,
 * status.change reflection, invalid-envelope rejection, and connect/disconnect
 * registry behaviour. Runs the handlers end-to-end through the real Repo against
 * an in-memory table; AWS is fully mocked.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const SESSION = 's-1';
const INSTANCE = 'inst-a';
const OWNER = 'matt';

/** A poster that records nothing and reports every connection alive. */
const noopPoster = { post: async () => true };

function deps(): EventDeps {
  return { repo, poster: noopPoster };
}

function wsEvent(body: unknown, connectionId = 'daemon-conn'): APIGatewayProxyWebsocketEventV2 {
  return {
    requestContext: { connectionId, domainName: 'd', stage: 'prod' },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  } as unknown as APIGatewayProxyWebsocketEventV2;
}

function env(seq: number, event: Event, ts = 1_700_000_000_000 + seq): Envelope {
  return { v: 1, instanceId: INSTANCE, host: 'matt@mbp', ts, seq, event };
}

const startEvent: Event = {
  kind: 'session.start',
  sessionId: SESSION,
  projectId: 'weekly-compass',
  host: 'matt@mbp',
  name: 'reconcile-variance',
};

/** Seed the daemon connection record (as `$connect` would). */
async function seedDaemonConn(connectionId = 'daemon-conn'): Promise<void> {
  const rec: ConnectionRecord = {
    connectionId,
    userId: OWNER,
    org: 'acme',
    instanceId: INSTANCE,
    role: 'daemon',
    connectedAt: 1,
  };
  await repo.putConnection(rec);
}

describe('$connect', () => {
  it('verifies the device token and stores the connection + instance index', async () => {
    const ev = {
      requestContext: { connectionId: 'c1', domainName: 'd', stage: 'prod' },
      queryStringParameters: { token: 'tok', instanceId: INSTANCE, role: 'daemon' },
    } as unknown as APIGatewayProxyWebsocketEventV2;

    const res = await connect(ev, {
      repo,
      verify: async () => ({ userId: OWNER, org: 'acme' }),
      now: () => 5,
    });
    expect(res).toMatchObject({ statusCode: 200 });

    const stored = await repo.getConnection('c1');
    expect(stored).toMatchObject({ userId: OWNER, instanceId: INSTANCE, role: 'daemon' });
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('c1');
  });

  it('rejects a daemon claiming an instanceId already owned by another user (hijack)', async () => {
    // matt owns INSTANCE via his daemon connection.
    await seedDaemonConn('matt-conn');
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('matt-conn');

    // alice tries to register the same instanceId — must be denied, and the
    // reverse index must still point at matt's connection.
    const ev = {
      requestContext: { connectionId: 'alice-conn', domainName: 'd', stage: 'prod' },
      queryStringParameters: { token: 'tok', instanceId: INSTANCE, role: 'daemon' },
    } as unknown as APIGatewayProxyWebsocketEventV2;
    const res = await connect(ev, {
      repo,
      verify: async () => ({ userId: 'alice', org: 'acme' }),
      now: () => 9,
    });
    expect(res).toMatchObject({ statusCode: 403 });
    expect(await repo.getConnection('alice-conn')).toBeUndefined();
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('matt-conn');
  });

  it('allows the same user to reclaim their own instanceId (reconnect)', async () => {
    await seedDaemonConn('old-conn');
    const ev = {
      requestContext: { connectionId: 'new-conn', domainName: 'd', stage: 'prod' },
      queryStringParameters: { token: 'tok', instanceId: INSTANCE, role: 'daemon' },
    } as unknown as APIGatewayProxyWebsocketEventV2;
    const res = await connect(ev, {
      repo,
      verify: async () => ({ userId: OWNER, org: 'acme' }),
      now: () => 9,
    });
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('new-conn');
  });

  it('rejects a missing or forged token without registering a connection', async () => {
    const noToken = {
      requestContext: { connectionId: 'c2', domainName: 'd', stage: 'prod' },
      queryStringParameters: {},
    } as unknown as APIGatewayProxyWebsocketEventV2;
    expect(
      await connect(noToken, {
        repo,
        verify: async () => ({ userId: 'x', org: 'y' }),
        now: () => 1,
      }),
    ).toMatchObject({ statusCode: 401 });

    const forged = {
      requestContext: { connectionId: 'c3', domainName: 'd', stage: 'prod' },
      queryStringParameters: { token: 'bad' },
    } as unknown as APIGatewayProxyWebsocketEventV2;
    expect(
      await connect(forged, {
        repo,
        verify: async () => {
          throw new Error('forged');
        },
        now: () => 1,
      }),
    ).toMatchObject({ statusCode: 401 });
    expect(await repo.getConnection('c3')).toBeUndefined();
  });
});

describe('event ingestion', () => {
  it('appends a tool.call and the projection reflects latest activity', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());

    const toolCall: Event = {
      kind: 'tool.call',
      sessionId: SESSION,
      tool: 'Bash',
      argsSummary: 'ls',
    };
    const res = await ingest(wsEvent(env(1, toolCall)), deps());
    expect(res).toMatchObject({ statusCode: 200 });

    const events = await repo.listEvents(SESSION);
    expect(events.map((e) => e.event.kind)).toEqual(['session.start', 'tool.call']);

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.name).toBe('reconcile-variance');
    expect(proj?.instanceId).toBe(INSTANCE);
    expect(proj?.ownerUserId).toBe(OWNER);
    expect(proj?.maxSeq).toBe(1);
    expect(proj?.lastEventAt).toBe(1_700_000_000_001);
  });

  it('accepts out-of-order seq and keeps the projection at the max seq', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());

    // seq 2 arrives before seq 1.
    const rename: Event = { kind: 'session.rename', sessionId: SESSION, name: 'newer-name' };
    await ingest(wsEvent(env(2, rename, 1_700_000_000_500)), deps());
    const lateRename: Event = { kind: 'session.rename', sessionId: SESSION, name: 'stale-name' };
    await ingest(wsEvent(env(1, lateRename, 1_700_000_000_200)), deps());

    const proj = await repo.getSessionById(SESSION);
    // The newer (seq 2) value wins; the late seq-1 event does not regress it.
    expect(proj?.name).toBe('newer-name');
    expect(proj?.maxSeq).toBe(2);

    // Both events are still persisted (out-of-order is stored, not dropped).
    const events = await repo.listEvents(SESSION);
    expect(events.map((e) => e.seq)).toEqual([0, 1, 2]);
  });

  it('ignores a duplicate seq (idempotent append, no projection regression)', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());
    const cost: Event = {
      kind: 'cost.tick',
      sessionId: SESSION,
      deltaUsd: 0.1,
      totalUsd: 0.1,
      tokens: 100,
    };
    await ingest(wsEvent(env(1, cost)), deps());

    // Replay the same seq with a different (stale) total — must be ignored.
    const dupe: Event = {
      kind: 'cost.tick',
      sessionId: SESSION,
      deltaUsd: 9,
      totalUsd: 9,
      tokens: 9,
    };
    const res = await ingest(wsEvent(env(1, dupe)), deps());
    expect(res).toMatchObject({ statusCode: 200, body: JSON.stringify({ stored: false }) });

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.costUsd).toBe(0.1);
    expect(proj?.tokens).toBe(100);
  });

  it('reflects status.change -> needs_input in the projection', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());

    const status: Event = {
      kind: 'status.change',
      sessionId: SESSION,
      from: 'active',
      to: 'needs_input',
    };
    await ingest(wsEvent(env(1, status)), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.status).toBe('needs_input');
  });

  it('updates the displayed name on session.rename (auto-naming)', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());
    const rename: Event = {
      kind: 'session.rename',
      sessionId: SESSION,
      name: 'add-reconciliation-view',
    };
    await ingest(wsEvent(env(1, rename)), deps());
    const proj = await repo.getSessionById(SESSION);
    expect(proj?.name).toBe('add-reconciliation-view');
  });

  it('rejects an invalid envelope without dropping the connection', async () => {
    await seedDaemonConn();
    const res = await ingest(wsEvent({ v: 1, junk: true }), deps());
    expect(res).toMatchObject({ statusCode: 400 });
    // Nothing was stored.
    expect(await repo.listEvents(SESSION)).toHaveLength(0);
  });

  it('fans an ingested event out to subscribed listeners', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());
    await repo.addListener(SESSION, 'web-conn');

    const posts: Array<{ connectionId: string; body: unknown }> = [];
    const poster = {
      post: async (connectionId: string, body: unknown) => {
        posts.push({ connectionId, body });
        return true;
      },
    };
    const tool: Event = { kind: 'tool.call', sessionId: SESSION, tool: 'Read', argsSummary: 'x' };
    await ingest(wsEvent(env(1, tool)), { repo, poster });

    expect(posts).toHaveLength(1);
    expect(posts[0]!.connectionId).toBe('web-conn');
    expect(posts[0]!.body).toMatchObject({ type: 'event' });
  });

  it('prunes a listener whose connection is gone', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());
    await repo.addListener(SESSION, 'dead-conn');

    const poster = { post: async () => false }; // GoneException -> false
    const tool: Event = { kind: 'tool.call', sessionId: SESSION, tool: 'Read', argsSummary: 'x' };
    await ingest(wsEvent(env(1, tool)), { repo, poster });

    expect(await repo.listListeners(SESSION)).toEqual([]);
  });
});

describe('session.start does NOT auto-connect the repo as a Project', () => {
  const PROJECT_ID = 'weekly-compass'; // matches startEvent.projectId

  it('ingests the session without creating a Project (connect is an explicit UI action)', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent)), deps());

    // The session projection is recorded...
    expect(await repo.getSessionById(startEvent.sessionId)).toBeDefined();
    // ...but the repo is not auto-connected: no Project, nothing in the list.
    expect(await repo.getProject(PROJECT_ID)).toBeUndefined();
    expect(await repo.listProjectsForUser(OWNER)).toHaveLength(0);
  });

  it('leaves an already-connected Project untouched on session.start', async () => {
    await seedDaemonConn();

    // The repo was connected via the UI (POST /projects → putProject).
    await repo.putProject({
      id: PROJECT_ID,
      name: 'Weekly Compass',
      repo: PROJECT_ID,
      ownerUserId: OWNER,
      progressPct: 42,
      liveSessionCount: 0,
    });

    await ingest(wsEvent(env(0, startEvent), 'daemon-conn'), deps());

    // The curated Project survives unchanged.
    const proj = await repo.getProject(PROJECT_ID);
    expect(proj?.name).toBe('Weekly Compass');
    expect(proj?.progressPct).toBe(42);
    const list = await repo.listProjectsForUser(OWNER);
    expect(list.filter((p) => p.id === PROJECT_ID)).toHaveLength(1);
  });
});

describe('$disconnect', () => {
  it('removes the connection and clears the instance reverse index (offline)', async () => {
    await seedDaemonConn('daemon-conn');
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('daemon-conn');

    const res = await disconnect(wsEvent('', 'daemon-conn'), { repo });
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.getConnection('daemon-conn')).toBeUndefined();
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBeUndefined();
  });

  it('does not clobber the reverse index if a reconnect already re-claimed it', async () => {
    await seedDaemonConn('old-conn');
    // A reconnect claims the instance under a new connectionId.
    await seedDaemonConn('new-conn');
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('new-conn');

    await disconnect(wsEvent('', 'old-conn'), { repo });
    // The newer claim survives.
    expect(await repo.getInstanceConnectionId(INSTANCE)).toBe('new-conn');
  });
});
