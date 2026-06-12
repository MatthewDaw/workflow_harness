/**
 * ts-authoring-routes-through-python-writer.test.ts — MAT-141 (U10) Gap 1.
 *
 * The DRY single-writer contract: the TS catalog-authoring fold path must author
 * the new skill revision (and its IDEAGOLD# golden case) through the ONE Python
 * author-revision writer — it must NOT write the revision itself.
 *
 * The earlier `python-writer-client.test.ts` only proved the TS *client* posts to
 * the Python endpoint in isolation; it did NOT prove the actual production
 * authoring path (`foldIdea`) uses that client. This test closes that gap: it
 * drives the real `foldIdea` handler with the Python writer injected and asserts
 * NO revision row and NO golden-case row are written to DynamoDB by TS.
 */

import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import { Repo } from '../src/db/repo.js';
import { foldIdea } from '../src/rest/skills.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { adminEvent, bodyOf } from './helpers/httpevent.js';
import { MATT, ORG, SCOPE, makeSkill as skill } from './helpers/factories.js';
import type { Idea, Skill } from '@harness/shared';
import type { AuthorRevisionRequest, AuthorRevisionResponse } from '../src/python-writer-client.js';

const TABLE = 'harness-test';
const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, TABLE);

let store: Map<string, Record<string, unknown>>;

beforeEach(() => {
  ddbMock.reset();
  store = installInMemoryTable(ddbMock);
});

function idea(over: Partial<Idea> = {}): Idea {
  return {
    ideaId: 'i-1',
    skillBaseName: 'reconcile',
    org: ORG,
    text: 'Always reconcile in the ledger currency.',
    sources: [{ sessionId: 's-1', segmentId: 'seg-1', seq: 1, snippet: 'ev1' }],
    status: 'open',
    corroborationVersion: 0,
    createdAt: 10,
    updatedAt: 10,
    ...over,
  };
}

/** SK rows that are skill REVISION snapshots written to the table. */
function revisionRows(): Array<Record<string, unknown>> {
  return [...store.values()].filter((it) => /^SKILL#.*#r\d+$/.test(String(it.SK)));
}

/** SK rows that are IDEAGOLD# golden cases written to the table. */
function goldenRows(): Array<Record<string, unknown>> {
  return [...store.values()].filter((it) => String(it.SK).startsWith('IDEAGOLD#'));
}

describe('test_ts_authoring_routes_through_python_writer (fold path)', () => {
  it('routes the revision + golden case through the Python writer and writes NEITHER in TS', async () => {
    // Seed a non-built-in skill + an open idea on it.
    await repo.putSkill(skill('reconcile', 'v1'));
    await repo.putIdea(idea());

    // Baseline: no revision rows or golden rows from the seed (putSkill writes the
    // live record only).
    expect(revisionRows()).toHaveLength(0);
    expect(goldenRows()).toHaveLength(0);

    // Inject the ONE Python writer. It records the request and returns a rev — it
    // is the ONLY thing allowed to author the revision/golden-case.
    const writerCalls: AuthorRevisionRequest[] = [];
    const authorRevision = async (req: AuthorRevisionRequest): Promise<AuthorRevisionResponse> => {
      writerCalls.push(req);
      return {
        org: req.org,
        baseName: req.baseName,
        variantId: req.variantId,
        rev: 2,
        truePointerUpdated: true,
        goldenCaseWritten: !!req.goldenCase,
      };
    };

    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'reconcile', ideaId: 'i-1' },
        rawPath: '/skills/reconcile/ideas/i-1/fold',
        body: { body: 'v1\n\n## Folded\nUse the ledger currency.' },
      }),
      { repo, authorRevision },
    );

    expect(res).toMatchObject({ statusCode: 200 });

    // (1) The Python writer was called exactly once with the fold revision + the
    // golden-case payload (one writer, one golden-capture path).
    expect(writerCalls).toHaveLength(1);
    const call = writerCalls[0]!;
    expect(call.org).toBe(ORG);
    expect(call.baseName).toBe('reconcile');
    expect(call.variantId).toBe(''); // base variant → empty id
    expect(call.body).toContain('Folded');
    expect(call.ideaId).toBe('i-1');
    expect(call.goldenCase).toBeTruthy();
    expect(call.goldenCase!.caseId).toBe('i-1');
    expect(call.goldenCase!.before).toBe('v1');
    expect(call.goldenCase!.after).toContain('Folded');

    // (2) THE PROOF: the TS path authored NO revision row and NO golden-case row.
    // Those records exist only because the single Python writer wrote them — and
    // the writer is a fake here, so the DynamoDB table must contain none.
    expect(revisionRows()).toHaveLength(0);
    expect(goldenRows()).toHaveLength(0);

    // (3) The TS handler still orchestrates the idea-state flip (its own job) and
    // carries the writer's rev forward.
    const { skill: stamped } = bodyOf<{ skill: Skill }>(res);
    expect(stamped.version).toBe(2);
    const folded = await repo.getIdea(ORG, 'reconcile', 'i-1');
    expect(folded?.status).toBe('folded');
    expect(folded?.foldedIntoRev).toBe(2);
  });

  it('forks a built-in through the Python writer with the fork variant id', async () => {
    const builtin: Skill = { ...skill('hq-add-skill', 'canonical'), source: 'built-in' };
    await repo.putSkill(builtin);
    await repo.putIdea(idea({ skillBaseName: 'hq-add-skill' }));

    const writerCalls: AuthorRevisionRequest[] = [];
    const authorRevision = async (req: AuthorRevisionRequest): Promise<AuthorRevisionResponse> => {
      writerCalls.push(req);
      return {
        org: req.org,
        baseName: req.baseName,
        variantId: req.variantId,
        rev: 1,
        truePointerUpdated: true,
        goldenCaseWritten: true,
      };
    };

    const res = await foldIdea(
      adminEvent({
        method: 'POST',
        userId: MATT,
        path: { name: 'hq-add-skill', ideaId: 'i-1' },
        rawPath: '/skills/hq-add-skill/ideas/i-1/fold',
        body: { body: 'canonical + fold', repoId: 'repo1', authorUserId: MATT },
      }),
      { repo, authorRevision },
    );

    expect(res).toMatchObject({ statusCode: 200 });
    expect(writerCalls).toHaveLength(1);
    // The fork's variant id is carried to the Python writer (the single writer
    // keys the revision under it) — not invented twice.
    expect(writerCalls[0]!.variantId).toBe('hq-add-skill#R#repo1#U#matt');
    // No TS-authored revision / golden rows.
    expect(revisionRows()).toHaveLength(0);
    expect(goldenRows()).toHaveLength(0);
  });
});

// Reference SCOPE so an unused-import lint never trips (it documents the org scope
// the fold writes under).
void SCOPE;
