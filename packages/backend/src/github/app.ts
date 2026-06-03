import { SignJWT, importPKCS8 } from 'jose';
import type { GitCommit, ProjectFraming } from '@harness/shared';
import {
  attributeCommits,
  buildFraming,
  parseCompletionFrontmatter,
  type RawCommit,
} from './history.js';

/**
 * GitHub App client (U26, KTD9).
 *
 * Reads `PRD.md` / `PROGRESS.md`, lists a week's commits (the Weekly "done"
 * source), and exchanges the App's private key for short-lived installation
 * tokens. All network access goes through an injectable `fetch`-shaped function
 * so tests drive it with recorded fixtures and never touch GitHub.
 *
 * Auth model: a GitHub App authenticates to the REST API in two steps —
 *   1. mint a short (<=10 min) RS256 JWT signed with the App private key,
 *      whose `iss` is the App id;
 *   2. POST that JWT to `/app/installations/<id>/access_tokens` to get an
 *      installation token scoped to one install (one org/owner).
 * The installation token then authorizes content/commits reads.
 */

export interface GitHubAppConfig {
  appId: string;
  /** PEM-encoded PKCS#8 private key for the App. */
  privateKeyPem: string;
  /** Installation id for the connected account (per project). */
  installationId: string;
  /** `owner/repo` the project is connected to. */
  repo: string;
}

/** A minimal fetch-shaped response the client consumes (subset of the DOM type). */
export interface FetchResponse {
  status: number;
  ok: boolean;
  text(): Promise<string>;
  json(): Promise<unknown>;
}

export type FetchLike = (
  url: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
  },
) => Promise<FetchResponse>;

const GITHUB_API = 'https://api.github.com';

/** One file in the repo tree (the subset of the Git Trees API we use). */
export interface TreeEntry {
  /** Repo-relative path, e.g. `docs/plans/2026-06-03-foo.md`. */
  path: string;
  /** `blob` (file) or `tree` (directory). */
  type: 'blob' | 'tree';
  sha: string;
  size?: number;
}

/** A docs/plans document with its parsed `completion:` frontmatter (U8). */
export interface DocEntry {
  /** Repo-relative path. */
  path: string;
  /** Display title (the leading `# ` heading, else the filename). */
  title: string;
  /** `completion:` percentage from frontmatter, or undefined when absent. */
  completion?: number;
}

/** Default docs-tree cache TTL (ms): short, to stay rate-limit-safe (U8/R2). */
const DOCS_CACHE_TTL_MS = 30_000;

export class GitHubApp {
  constructor(
    private readonly cfg: GitHubAppConfig,
    private readonly fetchImpl: FetchLike,
    /** Overridable clock (ms) for deterministic JWT iat/exp in tests. */
    private readonly now: () => number = Date.now,
  ) {}

  /**
   * Mint the App JWT (RS256, <=10 min). `iat` is back-dated 60s to tolerate
   * clock skew, per GitHub's guidance.
   */
  async appJwt(): Promise<string> {
    const key = await importPKCS8(this.cfg.privateKeyPem, 'RS256');
    const iat = Math.floor(this.now() / 1000) - 60;
    return new SignJWT({})
      .setProtectedHeader({ alg: 'RS256' })
      .setIssuedAt(iat)
      .setExpirationTime(iat + 600)
      .setIssuer(this.cfg.appId)
      .sign(key);
  }

  /** Exchange the App JWT for an installation access token. */
  async installationToken(): Promise<string> {
    const jwt = await this.appJwt();
    const res = await this.fetchImpl(
      `${GITHUB_API}/app/installations/${this.cfg.installationId}/access_tokens`,
      {
        method: 'POST',
        headers: this.headers(jwt),
      },
    );
    if (!res.ok) {
      throw new Error(`installation token exchange failed: ${res.status}`);
    }
    const body = (await res.json()) as { token?: string };
    if (!body.token) throw new Error('installation token missing in response');
    return body.token;
  }

  private headers(token: string): Record<string, string> {
    return {
      authorization: `Bearer ${token}`,
      accept: 'application/vnd.github+json',
      'x-github-api-version': '2022-11-28',
      'user-agent': 'command-hq',
    };
  }

  /**
   * Read a UTF-8 file from the repo's default branch (or `ref`). Returns
   * undefined on 404 so callers can record it as a missing file rather than
   * throwing.
   */
  async readFile(path: string, ref?: string): Promise<string | undefined> {
    const token = await this.installationToken();
    const q = ref ? `?ref=${encodeURIComponent(ref)}` : '';
    const res = await this.fetchImpl(`${GITHUB_API}/repos/${this.cfg.repo}/contents/${path}${q}`, {
      headers: this.headers(token),
    });
    if (res.status === 404) return undefined;
    if (!res.ok) throw new Error(`readFile ${path} failed: ${res.status}`);
    const body = (await res.json()) as { content?: string; encoding?: string };
    if (typeof body.content !== 'string') return undefined;
    const raw =
      body.encoding === 'base64'
        ? Buffer.from(body.content, 'base64').toString('utf8')
        : body.content;
    return raw;
  }

  /**
   * Read PRD.md + PROGRESS.md and build the project framing (goal, owned
   * Supporting Outcomes, progress %, missing files).
   */
  async readFraming(): Promise<ProjectFraming> {
    const [prd, progress] = await Promise.all([
      this.readFile('PRD.md'),
      this.readFile('PROGRESS.md'),
    ]);
    return buildFraming(prd, progress);
  }

  /**
   * Read the project framing (U7) with progress sourced the new way: prefer the
   * top `docs/plans/` doc's `completion:` frontmatter, fall back to the legacy
   * `PROGRESS.md %`, finally 0. Goal + owned Supporting Outcomes still come from
   * `PRD.md`. Read-only; no writes to GitHub anywhere.
   */
  async readFramingWithCompletion(): Promise<ProjectFraming> {
    const [framing, docs] = await Promise.all([this.readFraming(), this.listDocs().catch(() => [])]);
    // The "top" doc is the lexicographically-first under docs/plans/ (our docs are
    // date-prefixed, so this is the earliest/canonical plan). listDocs() is sorted.
    const topCompletion = docs[0]?.completion;
    const progressPct = topCompletion ?? framing.progressPct ?? 0;
    return { ...framing, progressPct };
  }

  /**
   * List commits since an ISO timestamp (the week boundary), attributed to
   * tickets by id. Drives the Weekly "done" section (U28).
   */
  async listCommits(sinceIso: string, untilIso?: string): Promise<GitCommit[]> {
    const token = await this.installationToken();
    const params = new URLSearchParams({ since: sinceIso, per_page: '100' });
    if (untilIso) params.set('until', untilIso);
    const res = await this.fetchImpl(
      `${GITHUB_API}/repos/${this.cfg.repo}/commits?${params.toString()}`,
      { headers: this.headers(token) },
    );
    if (!res.ok) throw new Error(`listCommits failed: ${res.status}`);
    const body = (await res.json()) as RawCommit[];
    return attributeCommits(body ?? []);
  }

  // --- U8: docs/plans tree + content (read-only, SHA-keyed cache) ----------

  /**
   * SHA-keyed cache of the resolved `docs/plans/` docs list. Keyed on the latest
   * commit SHA of the default branch so a refresh only re-walks the tree + fetches
   * blobs when the repo actually changed; otherwise we serve the cached list and
   * issue no upstream blob fetches (R2: rate-limit budget).
   */
  private docsCache?: { sha: string; at: number; docs: DocEntry[] };

  /** The latest commit SHA on the default branch (the cache key for U8). */
  async latestCommitSha(): Promise<string | undefined> {
    const token = await this.installationToken();
    const res = await this.fetchImpl(
      `${GITHUB_API}/repos/${this.cfg.repo}/commits?per_page=1`,
      { headers: this.headers(token) },
    );
    if (!res.ok) throw new Error(`latestCommitSha failed: ${res.status}`);
    const body = (await res.json()) as { sha?: string }[];
    return body?.[0]?.sha;
  }

  /**
   * Recursively read the repo tree at `ref` via the Git Trees API
   * (`?recursive=1`), returning the file entries under `prefix` (default
   * `docs/plans/`). Read-only. An absent path yields an empty list, not an error.
   */
  async readTree(prefix = 'docs/plans/', ref = 'HEAD'): Promise<TreeEntry[]> {
    const token = await this.installationToken();
    const res = await this.fetchImpl(
      `${GITHUB_API}/repos/${this.cfg.repo}/git/trees/${encodeURIComponent(ref)}?recursive=1`,
      { headers: this.headers(token) },
    );
    if (res.status === 404) return [];
    if (!res.ok) throw new Error(`readTree failed: ${res.status}`);
    const body = (await res.json()) as { tree?: TreeEntry[] };
    const tree = body.tree ?? [];
    return tree.filter((e) => e.type === 'blob' && e.path.startsWith(prefix));
  }

  /**
   * Read a single blob by its git SHA via the Blobs API. The Contents API caps
   * inline content at 1MB; the Blobs API does not, so per-file content fetches go
   * through here. Returns undefined on 404.
   */
  async readBlob(sha: string): Promise<string | undefined> {
    const token = await this.installationToken();
    const res = await this.fetchImpl(`${GITHUB_API}/repos/${this.cfg.repo}/git/blobs/${sha}`, {
      headers: this.headers(token),
    });
    if (res.status === 404) return undefined;
    if (!res.ok) throw new Error(`readBlob ${sha} failed: ${res.status}`);
    const body = (await res.json()) as { content?: string; encoding?: string };
    if (typeof body.content !== 'string') return undefined;
    return body.encoding === 'base64'
      ? Buffer.from(body.content, 'base64').toString('utf8')
      : body.content;
  }

  /**
   * The `docs/plans/` documents with their parsed `completion:` frontmatter,
   * served from a SHA-keyed cache. A second call within the TTL while the latest
   * commit SHA is unchanged returns the cached list without re-fetching any blob
   * (the rate-limit-safe path). Empty/absent `docs/plans/` yields an empty list.
   */
  async listDocs(prefix = 'docs/plans/'): Promise<DocEntry[]> {
    const sha = await this.latestCommitSha();
    const now = this.now();
    if (
      this.docsCache &&
      sha !== undefined &&
      this.docsCache.sha === sha &&
      now - this.docsCache.at < DOCS_CACHE_TTL_MS
    ) {
      return this.docsCache.docs;
    }

    const entries = await this.readTree(prefix, sha ?? 'HEAD');
    const docs: DocEntry[] = await Promise.all(
      entries
        .filter((e) => e.path.endsWith('.md'))
        .map(async (e) => {
          const md = await this.readBlob(e.sha);
          return {
            path: e.path,
            title: docTitle(e.path, md),
            completion: parseCompletionFrontmatter(md),
          };
        }),
    );
    docs.sort((a, b) => a.path.localeCompare(b.path));
    if (sha !== undefined) this.docsCache = { sha, at: now, docs };
    return docs;
  }

  /**
   * Raw markdown for one `docs/plans/` document by repo-relative path. Resolves
   * the blob SHA from the tree (so it honours the 1MB Contents-API limit via the
   * Blobs API) and returns undefined when the path is not a known doc.
   */
  async readDocContent(path: string, prefix = 'docs/plans/'): Promise<string | undefined> {
    if (!path.startsWith(prefix)) return undefined;
    const sha = await this.latestCommitSha();
    const entries = await this.readTree(prefix, sha ?? 'HEAD');
    const entry = entries.find((e) => e.path === path);
    if (!entry) return undefined;
    return this.readBlob(entry.sha);
  }
}

/** Derive a doc title: the leading `# ` heading (skipping frontmatter), else the basename. */
function docTitle(path: string, md: string | undefined): string {
  const base = path.split('/').pop() ?? path;
  if (!md) return base;
  const withoutFm = md.replace(/^﻿?---\s*\r?\n[\s\S]*?\r?\n---\s*(?:\r?\n|$)/, '');
  const heading = withoutFm.match(/^\s*#\s+(.+)$/m);
  return heading?.[1]?.trim() || base;
}
