import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { marshall } from '@aws-sdk/util-dynamodb';
import type { DynamoDBRecord, DynamoDBStreamEvent } from 'aws-lambda';
import type { Envelope, Event, ObjectiveNode, Project } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { consume, type StreamConsumerDeps } from '../src/ws/streamConsumer.js';
import * as k from '../src/db/keys.js';
import { installInMemoryTable } from './helpers/memtable.js';

/**
 * U5/U10 Streams backstop: a DynamoDB-stream record for an appended event must
 * re-fold the session projection (idempotent, converges to the same state the
 * inline ingestion path produced), and a project-progress change must drive the
 * objective roll-up — exactly what "Streams drive projections/roll-ups" promises.
 */

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

function deps(): StreamConsumerDeps {
  return { repo };
}

const SESSION = 's-1';
const INSTANCE = 'inst-a';

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

/** Build an INSERT stream record carrying an event envelope as its NewImage. */
function eventRecord(envelope: Envelope): DynamoDBRecord {
  const item = { ...k.eventKey(envelope.event.sessionId, envelope.seq), ...envelope };
  return {
    eventName: 'INSERT',
    eventID: `evt-${envelope.seq}`,
    dynamodb: {
      Keys: marshall(k.eventKey(envelope.event.sessionId, envelope.seq)),
      NewImage: marshall(item, { removeUndefinedValues: true }),
    },
  } as unknown as DynamoDBRecord;
}

/** Build a MODIFY stream record for a project META item (progress change). */
function projectRecord(
  oldProject: Record<string, unknown> | undefined,
  newProject: Record<string, unknown>,
): DynamoDBRecord {
  const id = newProject.id as string;
  return {
    eventName: oldProject ? 'MODIFY' : 'INSERT',
    eventID: `proj-${id}`,
    dynamodb: {
      Keys: marshall(k.projectKey(id)),
      ...(oldProject
        ? {
            OldImage: marshall(
              { ...k.projectKey(id), ...oldProject },
              { removeUndefinedValues: true },
            ),
          }
        : {}),
      NewImage: marshall({ ...k.projectKey(id), ...newProject }, { removeUndefinedValues: true }),
    },
  } as unknown as DynamoDBRecord;
}

function streamEvent(...records: DynamoDBRecord[]): DynamoDBStreamEvent {
  return { Records: records };
}

describe('stream consumer — session projection backstop', () => {
  it('re-folds an appended event into the session projection', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.name).toBe('reconcile-variance');
    expect(proj?.projectId).toBe('weekly-compass');
    expect(proj?.maxSeq).toBe(0);
  });

  it('advances latest-activity fields as later events stream through', async () => {
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());
    const msg: Event = {
      kind: 'assistant.msg',
      sessionId: SESSION,
      tokens: 200,
    };
    await consume(streamEvent(eventRecord(env(1, msg))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.maxSeq).toBe(1);
    expect(proj?.tokens).toBe(200);
  });

  it('is idempotent: re-processing the same event record does not regress state', async () => {
    const msg: Event = {
      kind: 'assistant.msg',
      sessionId: SESSION,
      tokens: 200,
    };
    await consume(streamEvent(eventRecord(env(0, startEvent))), deps());
    await consume(streamEvent(eventRecord(env(1, msg))), deps());
    // Redeliver seq 1 (Streams at-least-once) — must be a no-op.
    await consume(streamEvent(eventRecord(env(1, msg))), deps());

    const proj = await repo.getSessionById(SESSION);
    expect(proj?.maxSeq).toBe(1);
    expect(proj?.tokens).toBe(200);
  });

  it('tolerates non-event / malformed records without throwing', async () => {
    const noise: DynamoDBRecord = {
      eventName: 'INSERT',
      eventID: 'noise-1',
      dynamodb: {
        Keys: marshall({ PK: 'CONN#abc', SK: 'META' }),
        NewImage: marshall({ PK: 'CONN#abc', SK: 'META', connectionId: 'abc' }),
      },
    } as unknown as DynamoDBRecord;
    const remove: DynamoDBRecord = {
      eventName: 'REMOVE',
      eventID: 'rm-1',
      dynamodb: { Keys: marshall(k.eventKey(SESSION, 9)) },
    } as unknown as DynamoDBRecord;

    await expect(consume(streamEvent(noise, remove), deps())).resolves.toBeUndefined();
    // Nothing projected from noise.
    expect(await repo.getSessionById(SESSION)).toBeUndefined();
  });
});

describe('stream consumer — objective roll-up driver', () => {
  const ORG = 'acme';

  function node(id: string, level: ObjectiveNode['level'], parentId?: string): ObjectiveNode {
    return { id, org: ORG, level, title: id, parentId };
  }

  async function seedTree(): Promise<void> {
    await repo.putObjective(node('rally', 'rally_cry'));
    await repo.putObjective(node('out', 'outcome', 'rally'));
    await repo.putObjective(node('so-a', 'supporting_outcome', 'out'));
    await repo.putObjective(node('so-b', 'supporting_outcome', 'out'));
  }

  const baseProject = (
    progressPct?: number,
  ): Project & { org: string; supportingOutcomeIds: string[] } => ({
    id: 'weekly-compass',
    name: 'weekly-compass',
    repo: 'gh/acme/wc',
    ownerUserId: 'matt',
    liveSessionCount: 0,
    org: ORG,
    supportingOutcomeIds: ['so-a'],
    ...(progressPct === undefined ? {} : { progressPct }),
  });

  it('recomputes the org roll-up when a project progress increases', async () => {
    await seedTree();
    // The stored project must be readable by recomputeOrgRollup.
    await repo.putProject(baseProject(100));

    await consume(streamEvent(projectRecord(baseProject(0), baseProject(100))), deps());

    expect((await repo.getObjective(ORG, 'so-a'))?.pct).toBe(100);
    expect((await repo.getObjective(ORG, 'out'))?.pct).toBe(50); // so-a 100, so-b 0
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBe(50);
  });

  it('skips recompute when no roll-up-relevant field changed', async () => {
    await seedTree();
    await repo.putProject(baseProject(100));
    // Same progress + same SOs on both images: name-only churn -> no recompute.
    const oldImg = { ...baseProject(100), name: 'old-name' };
    const newImg = { ...baseProject(100), name: 'new-name' };

    await consume(streamEvent(projectRecord(oldImg, newImg)), deps());

    // No roll-up was driven, so the objective pct stays absent (unwritten).
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBeUndefined();
  });

  it('skips a project record carrying no org (cannot place into a tree)', async () => {
    await seedTree();
    const noOrg = {
      id: 'p2',
      name: 'p2',
      repo: 'gh/x/y',
      ownerUserId: 'matt',
      liveSessionCount: 0,
      progressPct: 100,
    };
    await expect(
      consume(streamEvent(projectRecord({ ...noOrg, progressPct: 0 }, noOrg)), deps()),
    ).resolves.toBeUndefined();
    expect((await repo.getObjective(ORG, 'rally'))?.pct).toBeUndefined();
  });
});
