import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import { generateKeyPairSync } from 'node:crypto';
import { describe, expect, it } from 'vitest';
import {
  GitHubApp,
  PublicGitHubReader,
  type FetchLike,
  type FetchResponse,
} from '../src/github/app.js';
import {
  attributeCommits,
  buildFraming,
  parseCompletionFrontmatter,
  parsePrd,
  parseProgress,
  resolveProgressPct,
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

  it('includes an .html doc, deriving its title from <h1> and completion from frontmatter', async () => {
    const DOC_C =
      '---\ncompletion: 73\n---\n<html><body><h1>Gamma <em>plan</em></h1><p>body</p></body></html>';
    const fetch: FetchLike = async (url): Promise<FetchResponse> => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/commits?per_page=1')) return resp(200, [{ sha: 'sha-1' }]);
      if (url.includes('/git/trees/'))
        return resp(200, {
          tree: [
            { path: 'docs/plans/a.md', type: 'blob', sha: 'blob-a' },
            { path: 'docs/plans/c.html', type: 'blob', sha: 'blob-c' },
          ],
        });
      if (url.endsWith('/git/blobs/blob-a'))
        return resp(200, { encoding: 'base64', content: Buffer.from(DOC_A).toString('base64') });
      if (url.endsWith('/git/blobs/blob-c'))
        return resp(200, { encoding: 'base64', content: Buffer.from(DOC_C).toString('base64') });
      return resp(404, {});
    };
    const docs = await makeApp(fetch).listDocs();
    expect(docs.map((d) => d.path)).toEqual(['docs/plans/a.md', 'docs/plans/c.html']);
    expect(docs[1]).toMatchObject({ title: 'Gamma plan', completion: 73 });
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

  it('prefers docs/PRD.md completion over the docs/plans top-doc completion', async () => {
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/docs/PRD.md'))
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from('---\ncompletion: 42\n---\n# Project Requirements\nbody').toString(
            'base64',
          ),
        });
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
    expect(framing.progressPct).toBe(42); // docs/PRD.md (42) wins over top doc (58)
  });

  it('falls back to the docs/plans top doc when docs/PRD.md is absent', async () => {
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/docs/PRD.md')) return resp(404, { message: 'Not Found' });
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
    expect(framing.progressPct).toBe(58); // top doc completion
  });

  it('falls back to PROGRESS.md then 0 when neither docs/PRD.md nor a plan doc carry completion', async () => {
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/docs/PRD.md')) return resp(404, { message: 'Not Found' });
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
      // No docs/plans tree at all -> empty list, no top-doc completion.
      if (url.includes('/git/trees/')) return resp(200, { tree: [] });
      return resp(404, {});
    };
    const framing = await makeApp(fetch).readFramingWithCompletion();
    expect(framing.progressPct).toBe(62); // PROGRESS.md fixture's 62%
  });

  it('reads docs/PRD.md raw markdown via readPrdDoc (Contents API)', async () => {
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/docs/PRD.md'))
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from('# Project Requirements\nbody').toString('base64'),
        });
      return resp(404, {});
    };
    expect(await makeApp(fetch).readPrdDoc()).toContain('# Project Requirements');
  });

  it('falls back to docs/PRD.html when docs/PRD.md is 404', async () => {
    const fetch: FetchLike = async (url) => {
      if (url.includes('/access_tokens')) return resp(201, { token: 't' });
      if (url.includes('/contents/docs/PRD.md')) return resp(404, { message: 'Not Found' });
      if (url.includes('/contents/docs/PRD.html'))
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from('---\ncompletion: 31\n---\n<h1>Project Requirements</h1>').toString(
            'base64',
          ),
        });
      return resp(404, {});
    };
    expect(await makeApp(fetch).readPrdDoc()).toContain('<h1>Project Requirements</h1>');
  });
});

describe('PublicGitHubReader (no-auth public repo fallback)', () => {
  function recordingFetch(): { calls: { url: string; auth?: string }[]; fetch: FetchLike } {
    const calls: { url: string; auth?: string }[] = [];
    const fetch: FetchLike = async (url, init): Promise<FetchResponse> => {
      calls.push({ url, auth: init?.headers?.authorization });
      if (url.includes('/contents/docs/PRD.md')) {
        return resp(200, {
          encoding: 'base64',
          content: Buffer.from('# Project Requirements\n\n- ship it').toString('base64'),
        });
      }
      return resp(404, { message: 'Not Found' });
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

  it('reads docs/PRD.md without minting a token or sending Authorization', async () => {
    const { calls, fetch } = recordingFetch();
    const reader = new PublicGitHubReader('MatthewDaw/workflow_harness', fetch);

    const md = await reader.readPrdDoc();
    expect(md).toContain('# Project Requirements');

    // Never exchanges an installation token...
    expect(calls.some((c) => c.url.includes('/access_tokens'))).toBe(false);
    // ...and the content request carries no Authorization header.
    const contentCall = calls.find((c) => c.url.includes('/contents/docs/PRD.md'));
    expect(contentCall?.auth).toBeUndefined();
  });

  it('installationToken() is a no-op empty string', async () => {
    const { fetch } = recordingFetch();
    const reader = new PublicGitHubReader('owner/repo', fetch);
    expect(await reader.installationToken()).toBe('');
  });
});
