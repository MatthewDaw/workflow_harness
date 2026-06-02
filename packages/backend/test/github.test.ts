import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { createHmac, generateKeyPairSync } from 'node:crypto';
import { beforeEach, describe, expect, it } from 'vitest';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { Project, Ticket } from '@harness/shared';
import { Repo } from '../src/db/repo.js';
import { installInMemoryTable } from './helpers/memtable.js';
import { GitHubApp, type FetchLike, type FetchResponse } from '../src/github/app.js';
import {
  attributeCommits,
  buildFraming,
  extractTicketIds,
  parsePrd,
  parseProgress,
  type RawCommit,
} from '../src/github/history.js';
import {
  handlePullRequest,
  handleWebhook,
  targetStatusFor,
  verifySignature,
} from '../src/github/webhooks.js';

/**
 * U26 GitHub integration. PRD/PROGRESS parsing + framing, commit attribution by
 * ticket id (Weekly "done" source), the App client's file/commit reads against
 * recorded fixtures (no network), and webhook signature verification + PR-driven
 * ticket transitions.
 */

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name: string): string =>
  readFileSync(join(here, 'fixtures', 'github', name), 'utf8');

const ddbMock = mockClient(DynamoDBDocumentClient);
const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
const repo = new Repo(doc, 'harness-test');

beforeEach(() => {
  ddbMock.reset();
  installInMemoryTable(ddbMock);
});

describe('history: PRD/PROGRESS parsing + framing', () => {
  it('reads the goal and owned Supporting Outcomes from PRD.md', () => {
    const prd = parsePrd(fixture('PRD.md'));
    expect(prd.goal).toContain('single weekly view');
    expect(prd.supportingOutcomeIds.sort()).toEqual(['SO-EXPORT', 'SO-RECONCILE']);
  });

  it('reads the progress percentage from PROGRESS.md', () => {
    expect(parseProgress(fixture('PROGRESS.md'))).toBe(62);
  });

  it('flags missing files instead of throwing', () => {
    const framing = buildFraming(undefined, undefined);
    expect(framing.missingFiles).toEqual(['PRD.md', 'PROGRESS.md']);
    expect(framing.goal).toBeUndefined();
    expect(framing.supportingOutcomeIds).toEqual([]);
  });

  it('builds full framing when both files are present', () => {
    const framing = buildFraming(fixture('PRD.md'), fixture('PROGRESS.md'));
    expect(framing.missingFiles).toEqual([]);
    expect(framing.progressPct).toBe(62);
    expect(framing.supportingOutcomeIds).toContain('SO-RECONCILE');
  });
});

describe('history: commit attribution', () => {
  it('extracts distinct ticket ids from branch/PR/message text', () => {
    expect(extractTicketIds('feat/WC-37-recon', 'WC-37 and WC-40', undefined).sort()).toEqual([
      'WC-37',
      'WC-40',
    ]);
  });

  it('attributes a week of commits to tickets, leaving unrelated ones unattributed', () => {
    const raw = JSON.parse(fixture('commits-week.json')) as RawCommit[];
    const commits = attributeCommits(raw);
    const byTicket = (id: string) => commits.filter((c) => c.ticketIds.includes(id));
    expect(byTicket('WC-37')).toHaveLength(2);
    expect(byTicket('WC-40')).toHaveLength(1);
    // The `chore: bump deps` commit references no ticket -> unattributed.
    expect(commits.find((c) => c.sha === 'a4b4c4d4')?.ticketIds).toEqual([]);
  });
});

describe('app: GitHub App client (recorded fixtures, no network)', () => {
  // A real RSA key so the App JWT actually signs/verifies.
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const privateKeyPem = privateKey.export({ type: 'pkcs8', format: 'pem' }).toString();

  function fakeFetch(): { calls: string[]; fetch: FetchLike } {
    const calls: string[] = [];
    const fetch: FetchLike = async (url): Promise<FetchResponse> => {
      calls.push(url);
      if (url.includes('/access_tokens')) {
        return resp(201, { token: 'ghs_installation_token' });
      }
      if (url.includes('/contents/PRD.md')) {
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from(fixture('PRD.md')).toString('base64'),
        });
      }
      if (url.includes('/contents/PROGRESS.md')) {
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from(fixture('PROGRESS.md')).toString('base64'),
        });
      }
      if (url.includes('/contents/MISSING.md')) {
        return resp(404, { message: 'Not Found' });
      }
      if (url.includes('/commits')) {
        return resp(200, JSON.parse(fixture('commits-week.json')));
      }
      return resp(404, {});
    };
    return { calls, fetch };
  }

  function resp(status: number, body: unknown): FetchResponse {
    return {
      status,
      ok: status >= 200 && status < 300,
      text: async () => JSON.stringify(body),
      json: async () => body,
    };
  }

  const makeApp = (fetch: FetchLike) =>
    new GitHubApp(
      { appId: '12345', privateKeyPem, installationId: '99', repo: 'acme/weekly-compass' },
      fetch,
    );

  it('mints an App JWT signed by the App key', async () => {
    const { fetch } = fakeFetch();
    const jwt = await makeApp(fetch).appJwt();
    // Three base64url segments.
    expect(jwt.split('.')).toHaveLength(3);
  });

  it('reads PRD/PROGRESS into framing via the contents API', async () => {
    const { fetch } = fakeFetch();
    const framing = await makeApp(fetch).readFraming();
    expect(framing.goal).toContain('single weekly view');
    expect(framing.progressPct).toBe(62);
    expect(framing.missingFiles).toEqual([]);
  });

  it('returns undefined for a missing file (404)', async () => {
    const { fetch } = fakeFetch();
    expect(await makeApp(fetch).readFile('MISSING.md')).toBeUndefined();
  });

  it('lists a week of attributed commits', async () => {
    const { fetch } = fakeFetch();
    const commits = await makeApp(fetch).listCommits('2026-05-25T00:00:00Z');
    expect(commits).toHaveLength(4);
    expect(commits.filter((c) => c.ticketIds.includes('WC-37'))).toHaveLength(2);
  });
});

describe('webhooks: signature verification', () => {
  const secret = 'shhh';
  const body = JSON.stringify({ hello: 'world' });
  const sign = (b: string) => 'sha256=' + createHmac('sha256', secret).update(b).digest('hex');

  it('accepts a correctly-signed payload', () => {
    expect(verifySignature(body, sign(body), secret)).toBe(true);
  });

  it('rejects a tampered payload', () => {
    expect(verifySignature(body + 'x', sign(body), secret)).toBe(false);
  });

  it('rejects a missing or malformed signature header', () => {
    expect(verifySignature(body, undefined, secret)).toBe(false);
    expect(verifySignature(body, 'md5=abc', secret)).toBe(false);
  });
});

describe('webhooks: PR-driven ticket transitions', () => {
  const secret = 'webhook-secret';
  const REPO = 'acme/weekly-compass';
  const PROJ = 'weekly-compass';

  const project = (): Project => ({
    id: PROJ,
    name: PROJ,
    repo: `gh/${REPO}`,
    ownerUserId: 'matt',
    liveSessionCount: 0,
  });
  const ticket = (status: Ticket['status']): Ticket => ({
    id: 'WC-37',
    projectId: PROJ,
    title: 'Reconciliation',
    status,
    priority: 'high',
  });

  beforeEach(async () => {
    await repo.putProject(project());
    await repo.linkRepoToProject(REPO, PROJ);
  });

  it('maps PR actions to target statuses', () => {
    expect(targetStatusFor({ action: 'opened' })).toBe('in_review');
    expect(targetStatusFor({ action: 'closed', pull_request: { merged: true } })).toBe('done');
    expect(targetStatusFor({ action: 'closed', pull_request: { merged: false } })).toBeUndefined();
  });

  it('moves WC-37 to in_review on PR open (linked by branch)', async () => {
    await repo.putTicket(ticket('in_progress'));
    const result = await handlePullRequest(
      {
        action: 'opened',
        repository: { full_name: REPO },
        pull_request: { number: 42, head: { ref: 'feat/WC-37-recon' }, html_url: 'http://pr/42' },
      },
      { repo, secret },
    );
    expect(result.moved).toEqual([{ ticketId: 'WC-37', to: 'in_review' }]);
    const t = await repo.getTicket(PROJ, 'WC-37');
    expect(t?.status).toBe('in_review');
    expect(t?.pr).toBe('http://pr/42');
  });

  it('moves WC-37 to done on PR merge (linked by title)', async () => {
    await repo.putTicket(ticket('in_review'));
    const result = await handlePullRequest(
      {
        action: 'closed',
        repository: { full_name: REPO },
        pull_request: { number: 42, title: 'WC-37 reconciliation', merged: true },
      },
      { repo, secret },
    );
    expect(result.moved).toEqual([{ ticketId: 'WC-37', to: 'done' }]);
    expect((await repo.getTicket(PROJ, 'WC-37'))?.status).toBe('done');
  });

  it('skips an invalid transition rather than failing', async () => {
    await repo.putTicket(ticket('done'));
    const result = await handlePullRequest(
      {
        action: 'opened',
        repository: { full_name: REPO },
        pull_request: { head: { ref: 'WC-37' } },
      },
      { repo, secret },
    );
    expect(result.moved).toEqual([]);
    expect((await repo.getTicket(PROJ, 'WC-37'))?.status).toBe('done');
  });

  it('rejects an unsigned webhook before any state change', async () => {
    await repo.putTicket(ticket('in_progress'));
    const raw = JSON.stringify({
      action: 'opened',
      repository: { full_name: REPO },
      pull_request: { head: { ref: 'WC-37' } },
    });
    const res = await handleWebhook(
      { rawBody: raw, eventType: 'pull_request', signature: 'sha256=bad' },
      { repo, secret },
    );
    expect(res.statusCode).toBe(401);
    expect((await repo.getTicket(PROJ, 'WC-37'))?.status).toBe('in_progress');
  });

  it('processes a correctly-signed webhook end-to-end', async () => {
    await repo.putTicket(ticket('in_progress'));
    const raw = JSON.stringify({
      action: 'opened',
      repository: { full_name: REPO },
      pull_request: { number: 7, head: { ref: 'feat/WC-37' }, html_url: 'http://pr/7' },
    });
    const sig = 'sha256=' + createHmac('sha256', secret).update(raw).digest('hex');
    const res = await handleWebhook(
      { rawBody: raw, eventType: 'pull_request', signature: sig },
      { repo, secret },
    );
    expect(res.statusCode).toBe(200);
    expect((await repo.getTicket(PROJ, 'WC-37'))?.status).toBe('in_review');
  });
});
