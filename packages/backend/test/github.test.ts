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
  parseCompletionFrontmatter,
  parsePrd,
  parseProgress,
  resolveProgressPct,
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

describe('history: completion: frontmatter (U7)', () => {
  const doc = (completion: string) =>
    `---\ntitle: Migration plan\ncompletion: ${completion}\nstatus: in_progress\n---\n\n# Plan\n\nbody`;

  it('parses `completion: 58` frontmatter to 58', () => {
    expect(parseCompletionFrontmatter(doc('58'))).toBe(58);
  });

  it('parses a quoted/percent value `completion: "58%"`', () => {
    expect(parseCompletionFrontmatter(doc('"58%"'))).toBe(58);
  });

  it('clamps an out-of-range value to 0..100', () => {
    expect(parseCompletionFrontmatter(doc('150'))).toBe(100);
  });

  it('returns undefined when there is no frontmatter or no completion key', () => {
    expect(parseCompletionFrontmatter('# Plan\n\nno frontmatter here')).toBeUndefined();
    expect(parseCompletionFrontmatter('---\ntitle: x\n---\nbody')).toBeUndefined();
    expect(parseCompletionFrontmatter(undefined)).toBeUndefined();
  });

  it('only reads leading frontmatter, not a `completion:` later in the body', () => {
    expect(parseCompletionFrontmatter('# Plan\n\ncompletion: 99 in prose')).toBeUndefined();
  });

  it('prefers frontmatter `completion:` over the PROGRESS.md fallback', () => {
    expect(resolveProgressPct(doc('58'), 'Progress: 12%')).toBe(58);
  });

  it('falls back to PROGRESS.md % when frontmatter is missing', () => {
    expect(resolveProgressPct('# Plan\nno fm', 'Progress: 42%')).toBe(42);
  });

  it('defaults to 0 when neither source has a percentage', () => {
    expect(resolveProgressPct(undefined, undefined)).toBe(0);
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

describe('app: docs/plans tree + content + cache (U8)', () => {
  const { privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 });
  const privateKeyPem = privateKey.export({ type: 'pkcs8', format: 'pem' }).toString();

  const DOC_A = '---\ncompletion: 58\n---\n# Alpha plan\nbody';
  const DOC_B = '# Beta plan\nno frontmatter';

  function docsFetch(opts: { sha?: string } = {}) {
    const sha = opts.sha ?? 'sha-1';
    const calls: string[] = [];
    const fetch: FetchLike = async (url): Promise<FetchResponse> => {
      calls.push(url);
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/commits?per_page=1')) return resp(200, [{ sha }]);
      if (url.includes('/git/trees/')) {
        return resp(200, {
          tree: [
            { path: 'docs/plans/a.md', type: 'blob', sha: 'blob-a' },
            { path: 'docs/plans/b.md', type: 'blob', sha: 'blob-b' },
            { path: 'src/index.ts', type: 'blob', sha: 'blob-x' },
            { path: 'docs/plans', type: 'tree', sha: 'tree-1' },
          ],
        });
      }
      if (url.endsWith('/git/blobs/blob-a'))
        return resp(200, { encoding: 'base64', content: Buffer.from(DOC_A).toString('base64') });
      if (url.endsWith('/git/blobs/blob-b'))
        return resp(200, { encoding: 'base64', content: Buffer.from(DOC_B).toString('base64') });
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
      { appId: '1', privateKeyPem, installationId: '9', repo: 'acme/weekly-compass' },
      fetch,
    );

  it('lists docs/plans/*.md with per-doc completion + derived title', async () => {
    const { fetch } = docsFetch();
    const docs = await makeApp(fetch).listDocs();
    expect(docs.map((d) => d.path)).toEqual(['docs/plans/a.md', 'docs/plans/b.md']);
    expect(docs[0]).toMatchObject({ title: 'Alpha plan', completion: 58 });
    expect(docs[1].completion).toBeUndefined();
  });

  it('serves a second call from cache without re-fetching blobs (rate-limit-safe)', async () => {
    const { calls, fetch } = docsFetch();
    const app = makeApp(fetch);
    await app.listDocs();
    const blobCallsAfterFirst = calls.filter((u) => u.includes('/git/blobs/')).length;
    expect(blobCallsAfterFirst).toBe(2);
    await app.listDocs(); // same SHA, within TTL -> cached
    const blobCallsAfterSecond = calls.filter((u) => u.includes('/git/blobs/')).length;
    expect(blobCallsAfterSecond).toBe(2); // no additional blob fetches
  });

  it('returns raw markdown for one doc by repo-relative path', async () => {
    const { fetch } = docsFetch();
    const md = await makeApp(fetch).readDocContent('docs/plans/a.md');
    expect(md).toContain('# Alpha plan');
  });

  it('returns undefined for a path outside docs/plans or an unknown doc', async () => {
    const { fetch } = docsFetch();
    const app = makeApp(fetch);
    expect(await app.readDocContent('src/index.ts')).toBeUndefined();
    expect(await app.readDocContent('docs/plans/missing.md')).toBeUndefined();
  });

  it('treats an absent docs/plans tree as an empty list (not an error)', async () => {
    const empty: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/commits?per_page=1')) return resp(200, [{ sha: 's' }]);
      if (url.includes('/git/trees/')) return resp(200, { tree: [{ path: 'README.md', type: 'blob', sha: 'r' }] });
      return resp(404, {});
    };
    expect(await makeApp(empty).listDocs()).toEqual([]);
  });

  it('reads framing with completion sourced from the top doc frontmatter', async () => {
    // PRD + PROGRESS via contents API; completion via the top docs/plans doc.
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/PRD.md'))
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from(fixture('PRD.md')).toString('base64'),
        });
      if (url.includes('/contents/PROGRESS.md'))
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from(fixture('PROGRESS.md')).toString('base64'),
        });
      if (url.includes('/commits?per_page=1')) return resp(200, [{ sha: 'sha-1' }]);
      if (url.includes('/git/trees/'))
        return resp(200, { tree: [{ path: 'docs/plans/a.md', type: 'blob', sha: 'blob-a' }] });
      if (url.endsWith('/git/blobs/blob-a'))
        return resp(200, { encoding: 'base64', content: Buffer.from(DOC_A).toString('base64') });
      return resp(404, {});
    };
    const framing = await makeApp(fetch).readFramingWithCompletion();
    expect(framing.progressPct).toBe(58); // top doc completion wins over PROGRESS.md's 62
    expect(framing.goal).toContain('single weekly view');
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
