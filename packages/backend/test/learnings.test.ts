import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';
import type { APIGatewayProxyWebsocketEventV2 } from 'aws-lambda';
import type { Envelope, Event, LearningRecord } from '@harness/shared';
import { Repo, type ConnectionRecord } from '../src/db/repo.js';
import { ingest, type EventDeps } from '../src/ws/event.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U3 — learnings store + ingest (topic-focus logging). A `session.learning`
 * event is persisted as an append record under the OWNING PROJECT partition
 * (resolved from the session pointer, since post-start events carry no
 * projectId), queryable per-project in one `begins_with(SK, 'LEARN#')` read, and
 * idempotent on (sessionId, turnId). It is NEVER folded into the projection.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

const PROJECT = 'weekly-compass';
const SESSION = 's-1';
const INSTANCE = 'inst-a';
const OWNER = 'matt';

const noopPoster = { post: async () => true };
function deps(): EventDeps {
  return { repo, poster: noopPoster };
}

function wsEvent(body: unknown, connectionId = 'daemon-conn'): APIGatewayProxyWebsocketEventV2 {
  return {
    requestContext: { connectionId, domainName: 'd', stage: 'prod' },
    body: JSON.stringify(body),
  } as unknown as APIGatewayProxyWebsocketEventV2;
}

function env(seq: number, event: Event, ts = 1_700_000_000_000 + seq): Envelope {
  return { v: 1, instanceId: INSTANCE, host: 'matt@mbp', ts, seq, event };
}

function startEvent(sessionId = SESSION, projectId = PROJECT): Event {
  return {
    kind: 'session.start',
    sessionId,
    projectId,
    host: 'matt@mbp',
    name: 'reconcile-variance',
  };
}

function learning(
  over: Partial<Extract<Event, { kind: 'session.learning' }>> = {},
): Extract<Event, { kind: 'session.learning' }> {
  return {
    kind: 'session.learning',
    sessionId: SESSION,
    segmentId: 'seg-1',
    topicLabel: 'reconciliation',
    stream: 'impl',
    text: 'prefer decimal money, never float',
    turnId: 't-1',
    ...over,
  };
}

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

describe('learningKey / learningPrefix', () => {
  it('keys learnings under the project partition by sessionId#turnId', () => {
    expect(k.learningKey(PROJECT, SESSION, 't-1')).toEqual({
      PK: `PROJ#${PROJECT}`,
      SK: `LEARN#${SESSION}#t-1`,
    });
    expect(k.learningPrefix(PROJECT)).toEqual({ PK: `PROJ#${PROJECT}`, skPrefix: 'LEARN#' });
  });

  it("a session's learnings sort deterministically by LEARN#<sessionId>#<turnId>", () => {
    expect(k.learningKey(PROJECT, SESSION, 't-1').SK < k.learningKey(PROJECT, SESSION, 't-2').SK).toBe(
      true,
    );
  });
});

describe('putLearning / listLearnings', () => {
  const base: LearningRecord = {
    projectId: PROJECT,
    sessionId: SESSION,
    segmentId: 'seg-1',
    topicLabel: 'reconciliation',
    stream: 'impl',
    text: 'prefer decimal money',
    turnId: 't-1',
    ts: 1_700_000_000_000,
    seq: 5,
  };

  it('writes an UNCONDITIONAL put under the project partition', async () => {
    await repo.putLearning(base);
    const call = ddbMock.commandCalls(PutCommand).at(-1)!.args[0].input;
    expect(call.Item).toMatchObject(k.learningKey(PROJECT, SESSION, 't-1'));
    // Idempotency is by SK, NOT a conditional write that would error on overwrite.
    expect(call.ConditionExpression).toBeUndefined();
  });

  it('re-putting the same (sessionId, turnId) overwrites in place (no duplicate)', async () => {
    await repo.putLearning(base);
    await repo.putLearning({ ...base, text: 're-emitted after judge retry' });
    const out = await repo.listLearnings(PROJECT);
    expect(out).toHaveLength(1);
    expect(out[0]!.text).toBe('re-emitted after judge retry');
  });

  it('lists a project corpus across multiple sessions in one partition read', async () => {
    await repo.putLearning(base);
    await repo.putLearning({ ...base, sessionId: 's-2', turnId: 't-9' });
    const out = await repo.listLearnings(PROJECT);
    expect(out).toHaveLength(2);
    expect(out.map((l) => l.sessionId).sort()).toEqual(['s-1', 's-2']);
  });
});

describe('ingest: session.learning', () => {
  it('writes a learning item under the project partition; listing returns it', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent())), deps());

    await ingest(wsEvent(env(1, learning())), deps());

    const out = await repo.listLearnings(PROJECT);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      projectId: PROJECT,
      sessionId: SESSION,
      segmentId: 'seg-1',
      stream: 'impl',
      text: 'prefer decimal money, never float',
      turnId: 't-1',
      ts: 1_700_000_000_001,
      seq: 1,
    });
  });

  it('does NOT fold the learning into the session projection', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent())), deps());

    await ingest(wsEvent(env(1, learning())), deps());

    const proj = await repo.getSessionById(SESSION);
    // maxSeq/lastEventAt stay at the session.start values — the learning never
    // advanced the projection.
    expect(proj?.maxSeq).toBe(0);
    expect(proj?.lastEventAt).toBe(1_700_000_000_000);
  });

  it('sorts impl + doc learnings for one session deterministically by turnId', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent())), deps());

    // Emit the doc learning (t-2) BEFORE the impl learning (t-1); the read still
    // comes back ordered by LEARN#<sessionId>#<turnId>.
    await ingest(
      wsEvent(env(1, learning({ stream: 'doc', turnId: 't-2', docRef: 'docs/plans/x.html' }))),
      deps(),
    );
    await ingest(wsEvent(env(2, learning({ stream: 'impl', turnId: 't-1' }))), deps());

    const out = await repo.listLearnings(PROJECT);
    expect(out.map((l) => l.turnId)).toEqual(['t-1', 't-2']);
    expect(out[0]!.stream).toBe('impl');
    expect(out[1]!.stream).toBe('doc');
    expect(out[1]!.docRef).toBe('docs/plans/x.html');
  });

  it('drops a learning whose session has no pointer record, without throwing', async () => {
    await seedDaemonConn();
    // No session.start ingested -> no pointer for SESSION.
    const res = await ingest(wsEvent(env(1, learning())), deps());
    expect(res).toMatchObject({ statusCode: 200 });
    expect(await repo.listLearnings(PROJECT)).toHaveLength(0);
  });

  it('re-ingesting the same (sessionId, turnId) does not duplicate', async () => {
    await seedDaemonConn();
    await ingest(wsEvent(env(0, startEvent())), deps());
    await ingest(wsEvent(env(1, learning())), deps());
    // A daemon restart re-emits the same learning under a fresh seq.
    await ingest(wsEvent(env(7, learning({ text: 'same turn, re-emitted' }))), deps());

    const out = await repo.listLearnings(PROJECT);
    expect(out).toHaveLength(1);
    expect(out[0]!.text).toBe('same turn, re-emitted');
  });

  it('returns project learnings across multiple sessions in one partition read', async () => {
    await seedDaemonConn();
    // Two sessions, same project.
    await ingest(wsEvent(env(0, startEvent('s-1'))), deps());
    await ingest(wsEvent(env(0, startEvent('s-2'))), deps());

    await ingest(wsEvent(env(1, learning({ sessionId: 's-1', turnId: 't-1' }))), deps());
    await ingest(wsEvent(env(1, learning({ sessionId: 's-2', turnId: 't-1' }))), deps());

    const out = await repo.listLearnings(PROJECT);
    expect(out).toHaveLength(2);
    expect(out.map((l) => l.sessionId).sort()).toEqual(['s-1', 's-2']);
  });
});
