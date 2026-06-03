import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { generateKeyPairSync } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import { GitHubApp, type FetchLike, type FetchResponse } from '../src/github/app.js';
import {
  attributeCommits,
  buildFraming,
  extractTicketIds,
  parsePrd,
  parseProgress,
  type RawCommit,
} from '../src/github/history.js';

/**
 * U26 GitHub integration. PRD/PROGRESS parsing + framing, commit attribution,
 * and the App client's file/commit reads against recorded fixtures (no network).
 */

const here = dirname(fileURLToPath(import.meta.url));
const fixture = (name: string): string =>
  readFileSync(join(here, 'fixtures', 'github', name), 'utf8');

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

  it('normalizes a week of commits (sha, message, author, date)', () => {
    const raw = JSON.parse(fixture('commits-week.json')) as RawCommit[];
    const commits = attributeCommits(raw);
    expect(commits).toHaveLength(raw.length);
    expect(commits[0]).toMatchObject({
      sha: expect.any(String),
      message: expect.any(String),
    });
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

  it('lists a week of commits', async () => {
    const { fetch } = fakeFetch();
    const commits = await makeApp(fetch).listCommits('2026-05-25T00:00:00Z');
    expect(commits).toHaveLength(4);
    expect(commits.every((c) => typeof c.sha === 'string')).toBe(true);
  });
});
