import { SignJWT, importPKCS8 } from 'jose';
import type { GitCommit, ProjectFraming } from '@harness/shared';
import { attributeCommits, buildFraming, type RawCommit } from './history.js';

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
}
