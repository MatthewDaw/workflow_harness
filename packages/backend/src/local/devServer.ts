import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { readFileSync, readdirSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { mockClient } from 'aws-sdk-client-mock';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DeleteCommand,
  DynamoDBDocumentClient,
  GetCommand,
  PutCommand,
  QueryCommand,
  UpdateCommand,
} from '@aws-sdk/lib-dynamodb';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import { CognitoJwtVerifier } from 'aws-jwt-verify';

import { handler as projectsHandler } from '../rest/projects.js';
import { handler as sessionsHandler } from '../rest/sessions.js';
import { handler as agentsHandler } from '../rest/agents.js';
import { handler as skillsHandler } from '../rest/skills.js';
import { handler as mcpServersHandler } from '../rest/mcpServers.js';
import { handler as objectivesHandler } from '../rest/objectives.js';
import { handler as dodHandler } from '../rest/dod.js';
import { handler as orgsHandler } from '../rest/orgs.js';
import { handler as weeklyHandler } from '../rest/weekly.js';
import { handler as memoriesHandler } from '../rest/memories.js';
import { handler as deviceHandler } from '../rest/device.js';
import { defaultRepo } from '../rest/runtime.js';
import { seedSkills, type BundleManifest, type SeedSkillFile } from '../seed/skills.js';

/**
 * Local dev backend for Command HQ.
 *
 * The real backend is AWS Lambda + DynamoDB; there is no managed local runtime,
 * so the SPA's `/api` calls have nothing to talk to in `npm run dev`. This server
 * stands the **real** REST handlers up on a plain Node HTTP server, routing each
 * URL like `infra/lib/api-stack.ts` (the API Gateway route table) so each request
 * reaches the right module `handler` with the path params it expects.
 *
 * It runs in one of two modes, chosen by whether the Cognito pool env vars
 * (`USER_POOL_ID` + `USER_POOL_CLIENT_ID` — the same names the deployed Lambdas
 * and `rest/bearerAuth.ts` read) are present:
 *
 *  - OFFLINE (default, no pool vars): persistence is an in-memory DynamoDB (the
 *    shape the handler tests use, via `aws-sdk-client-mock`) so `defaultRepo()`
 *    talks to a Map; auth is a synthetic admin principal derived from the SPA's
 *    mock token. Nothing touches AWS. State resets on restart.
 *  - REAL (pool vars set): the mock is NOT installed, so `defaultRepo()` hits the
 *    **real deployed `harness` DynamoDB table** with the ambient AWS credentials,
 *    and we reproduce the API Gateway Cognito JWT authorizer — verifying the
 *    request's ID token with `CognitoJwtVerifier` (the library the gateway itself
 *    uses) and passing its raw claims through on `requestContext`. The dev-skills
 *    seed is skipped so production data is never written by a local start.
 *
 * GitHub-backed reads (project framing/docs) fall back to an unauthenticated
 * public reader and degrade to "stale" when unreachable — exactly as in prod.
 * This file is dev-only; it is never bundled into a Lambda.
 */

const PORT = Number(process.env.HQ_DEV_API_PORT ?? 8787);

// ---- Mode selection --------------------------------------------------------
// Presence of the Cognito pool vars flips the server from offline (in-memory +
// synthetic principal) to real (deployed table + verified Cognito tokens),
// mirroring how the SPA and `rest/bearerAuth.ts` choose Cognito-vs-mock.
const COGNITO = (() => {
  const userPoolId = process.env.USER_POOL_ID;
  const clientId = process.env.USER_POOL_CLIENT_ID;
  return userPoolId && clientId ? { userPoolId, clientId } : undefined;
})();
const REAL_MODE = COGNITO !== undefined;

// Lazily-built ID-token verifier, reused across requests (it caches the JWKS
// internally). This stands in for the gateway's HttpJwtAuthorizer: it yields the
// full verified claim set, which we drop onto the event verbatim — so handlers
// see exactly what they would behind API Gateway (incl. `custom:admin`).
let idTokenVerifier: ReturnType<typeof CognitoJwtVerifier.create> | undefined;
function verifier() {
  if (!COGNITO) return undefined;
  idTokenVerifier ??= CognitoJwtVerifier.create({
    userPoolId: COGNITO.userPoolId,
    clientId: COGNITO.clientId,
    tokenUse: 'id',
  });
  return idTokenVerifier;
}

// ---- In-memory DynamoDB ----------------------------------------------------
// Ported from packages/backend/test/helpers/memtable.ts. Honours the exact
// command shapes the Repo emits (conditional Put, Get, Delete, Query with
// begins_with(SK), and SET UpdateCommands), so the real Repo runs unchanged.

interface KeyShape {
  PK: string;
  SK: string;
}
const keyOf = (item: KeyShape): string => `${item.PK}::${item.SK}`;

class ConditionalCheckFailed extends Error {
  override name = 'ConditionalCheckFailedException';
}

function installInMemoryTable(): void {
  const store = new Map<string, Record<string, unknown>>();
  const ddbMock = mockClient(DynamoDBDocumentClient);

  ddbMock.on(PutCommand).callsFake((input) => {
    const item = input.Item as Record<string, unknown> & KeyShape;
    if (input.ConditionExpression?.includes('attribute_not_exists(PK)')) {
      if (store.has(keyOf(item))) throw new ConditionalCheckFailed();
    }
    store.set(keyOf(item), { ...item });
    return {};
  });

  ddbMock.on(GetCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    return { Item: store.get(keyOf(key)) };
  });

  ddbMock.on(UpdateCommand).callsFake((input) => {
    const key = input.Key as KeyShape;
    const existing = store.get(keyOf(key));
    const values = (input.ExpressionAttributeValues ?? {}) as Record<string, unknown>;
    const names = (input.ExpressionAttributeNames ?? {}) as Record<string, string>;
    const cond = input.ConditionExpression ?? '';

    if (cond.includes('attribute_exists(PK)') && !existing) {
      throw new ConditionalCheckFailed();
    }
    const guardMatch = cond.match(/#?(\w+)\s*=\s*(:\w+)/);
    if (existing && guardMatch && cond.includes('#s =')) {
      const attr = names['#s'] ?? 's';
      const expected = values[guardMatch[2] as string];
      if ((existing as Record<string, unknown>)[attr] !== expected) {
        throw new ConditionalCheckFailed();
      }
    }

    const next: Record<string, unknown> = { ...(existing ?? key) };
    const setClause = (input.UpdateExpression ?? '').replace(/^\s*SET\s+/i, '');
    for (const assignment of setClause.split(',')) {
      const m = assignment.trim().match(/^(#?[\w]+)\s*=\s*(:[\w]+)\s*$/);
      if (!m) continue;
      const attr = m[1]!.startsWith('#') ? (names[m[1]!] ?? m[1]!.slice(1)) : m[1]!;
      next[attr] = values[m[2]!];
    }
    store.set(keyOf(key), next);
    return {};
  });

  ddbMock.on(DeleteCommand).callsFake((input) => {
    store.delete(keyOf(input.Key as KeyShape));
    return {};
  });

  ddbMock.on(QueryCommand).callsFake((input) => {
    const values = (input.ExpressionAttributeValues ?? {}) as Record<string, string>;
    const pk = values[':pk'];
    const skPrefix = values[':sk'];

    const onIndex = input.IndexName !== undefined;
    const partKey = onIndex ? 'GSI1PK' : 'PK';
    const sortKey = onIndex ? 'GSI1SK' : 'SK';

    let items = [...store.values()].filter((it) => {
      if ((it as Record<string, unknown>)[partKey] !== pk) return false;
      if (!onIndex && skPrefix !== undefined) {
        return String((it as unknown as KeyShape).SK).startsWith(skPrefix);
      }
      return true;
    });
    items.sort((a, b) =>
      String((a as Record<string, unknown>)[sortKey] ?? '').localeCompare(
        String((b as Record<string, unknown>)[sortKey] ?? ''),
      ),
    );
    if (input.ScanIndexForward === false) items.reverse();
    if (typeof input.Limit === 'number') items = items.slice(0, input.Limit);
    return { Items: items };
  });

  // Force `defaultRepo()` (created lazily inside the handlers) onto the mocked
  // client by constructing one now; aws-sdk-client-mock patches the prototype, so
  // every later instance is intercepted too.
  DynamoDBDocumentClient.from(new DynamoDBClient({ region: 'us-east-1' }));
}

// ---- Request → API Gateway event mapping -----------------------------------

type RestHandler = (event: APIGatewayProxyEventV2) => Promise<APIGatewayProxyResultV2>;

interface Route {
  re: RegExp;
  handler: RestHandler;
}

// Ordered most-specific-first; weekly is matched before the generic /projects
// routes because it shares the `/projects/:id/...` prefix. Named groups become
// the `pathParameters` each module handler reads.
const ROUTES: Route[] = [
  { re: /^\/device\/(?:start|poll|approve)$/, handler: deviceHandler },

  // Membership / org onboarding. /me + /me/org (switch) + /orgs(/join) reach the
  // orgs handler; placed before the generic resource routes (order matters), and
  // /me/org before /me so the bare-/me route does not shadow it.
  { re: /^\/me\/org$/, handler: orgsHandler },
  { re: /^\/me$/, handler: orgsHandler },
  { re: /^\/orgs\/join$/, handler: orgsHandler },
  { re: /^\/orgs$/, handler: orgsHandler },

  { re: /^\/projects\/(?<pid>[^/]+)\/weekly\/(?<week>[^/]+)\/publish$/, handler: weeklyHandler },
  { re: /^\/projects\/(?<pid>[^/]+)\/weekly\/(?<week>[^/]+)$/, handler: weeklyHandler },
  { re: /^\/projects\/(?<pid>[^/]+)\/weekly$/, handler: weeklyHandler },

  { re: /^\/projects\/(?<pid>[^/]+)\/memories$/, handler: memoriesHandler },

  {
    re: /^\/projects\/(?<projectId>[^/]+)\/skills\/(?<skillName>[^/]+)$/,
    handler: projectsHandler,
  },
  {
    re: /^\/projects\/(?<projectId>[^/]+)\/agents\/(?<agentName>[^/]+)$/,
    handler: projectsHandler,
  },
  {
    re: /^\/projects\/(?<projectId>[^/]+)\/mcp-servers\/(?<name>[^/]+)$/,
    handler: projectsHandler,
  },
  {
    re: /^\/projects\/(?<projectId>[^/]+)\/bundles\/(?<bundleName>[^/]+)$/,
    handler: projectsHandler,
  },
  { re: /^\/projects\/(?<id>[^/]+)\/learnings$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)\/requirements$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)\/wireframe$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)\/refresh$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)\/docs\/content$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)\/docs$/, handler: projectsHandler },
  { re: /^\/projects\/(?<id>[^/]+)$/, handler: projectsHandler },
  { re: /^\/projects$/, handler: projectsHandler },

  { re: /^\/sessions\/(?<id>[^/]+)\/control$/, handler: sessionsHandler },
  { re: /^\/sessions\/(?<id>[^/]+)$/, handler: sessionsHandler },
  { re: /^\/sessions$/, handler: sessionsHandler },

  { re: /^\/agents\/(?<name>[^/]+)\/scope$/, handler: agentsHandler },
  { re: /^\/agents\/(?<name>[^/]+)$/, handler: agentsHandler },
  { re: /^\/agents$/, handler: agentsHandler },

  // Fold an idea into a new revision (U16) — most specific first so the
  // two-segment `ideas/{ideaId}/fold` path is matched before `/{name}`.
  {
    re: /^\/skills\/(?<name>[^/]+)\/ideas\/(?<ideaId>[^/]+)\/fold$/,
    handler: skillsHandler,
  },
  { re: /^\/skills\/(?<name>[^/]+)\/members\/(?<member>[^/]+)$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)\/members$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)\/dissolve$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)\/usage$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)\/scope$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)\/promote$/, handler: skillsHandler },
  { re: /^\/skills\/(?<name>[^/]+)$/, handler: skillsHandler },
  { re: /^\/skills$/, handler: skillsHandler },

  // MCP servers mirror the skills catalog routes minus the bundle verbs
  // (members/dissolve) and the retired scope verb. Most-specific first so
  // `/usage` is matched before the bare `/{name}` route.
  { re: /^\/mcp-servers\/(?<name>[^/]+)\/usage$/, handler: mcpServersHandler },
  { re: /^\/mcp-servers\/(?<name>[^/]+)$/, handler: mcpServersHandler },
  { re: /^\/mcp-servers$/, handler: mcpServersHandler },

  { re: /^\/objectives\/(?<id>[^/]+)$/, handler: objectivesHandler },
  { re: /^\/objectives$/, handler: objectivesHandler },

  { re: /^\/dod$/, handler: dodHandler },
];

/** The bearer token from the `Authorization` header, if any. */
function bearerToken(req: IncomingMessage): string | undefined {
  const raw = req.headers['authorization'];
  if (!raw) return undefined;
  const m = /^Bearer\s+(.+)$/i.exec(raw.trim());
  return m ? m[1] : undefined;
}

/** Derive a stable dev principal from the SPA's mock bearer token (offline mode). */
function mockClaims(req: IncomingMessage): Record<string, unknown> {
  const m = /^mock-token-(.+)$/.exec(bearerToken(req) ?? '');
  const userId = m ? m[1]! : 'dev-user';
  // Admin so the admin-gated routes (objectives, dod, org catalog writes) work in
  // local dev. The org is fixed so a single login is a single tenant.
  return {
    sub: userId,
    'custom:org': process.env.HQ_DEV_ORG ?? 'dev-org',
    name: 'Dev User',
    'custom:admin': 'true',
  };
}

/**
 * The JWT claims to place on the event, or undefined to leave the request
 * unauthenticated (so the handler answers 401 via `principalOf`, exactly as a
 * public/HttpNoneAuthorizer route would behind the gateway). In REAL mode this
 * verifies the request's Cognito ID token; in OFFLINE mode it fabricates the
 * synthetic dev principal.
 */
async function resolveClaims(req: IncomingMessage): Promise<Record<string, unknown> | undefined> {
  if (!REAL_MODE) return mockClaims(req);
  const token = bearerToken(req);
  if (!token) return undefined;
  try {
    return (await verifier()!.verify(token)) as unknown as Record<string, unknown>;
  } catch {
    // Forged/expired/wrong-audience token: no claims → handler enforces 401.
    return undefined;
  }
}

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = [];
    req.on('data', (c: Buffer) => chunks.push(c));
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')));
    req.on('error', reject);
  });
}

function buildEvent(
  req: IncomingMessage,
  path: string,
  query: URLSearchParams,
  pathParameters: Record<string, string>,
  body: string,
  claims: Record<string, unknown> | undefined,
): APIGatewayProxyEventV2 {
  const queryStringParameters: Record<string, string> = {};
  for (const [key, value] of query) queryStringParameters[key] = value;

  const headers: Record<string, string> = {};
  for (const [key, value] of Object.entries(req.headers)) {
    if (typeof value === 'string') headers[key] = value;
  }

  return {
    version: '2.0',
    routeKey: '$default',
    rawPath: path,
    rawQueryString: query.toString(),
    headers,
    queryStringParameters: Object.keys(queryStringParameters).length
      ? queryStringParameters
      : undefined,
    pathParameters: Object.keys(pathParameters).length ? pathParameters : undefined,
    body: body || undefined,
    isBase64Encoded: false,
    requestContext: {
      http: { method: req.method ?? 'GET', path },
      // Authorized requests carry verified claims; an unauthenticated request
      // omits the authorizer so `principalOf` returns undefined (→ handler 401).
      ...(claims ? { authorizer: { jwt: { claims } } } : {}),
    },
  } as unknown as APIGatewayProxyEventV2;
}

async function handle(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const url = new URL(req.url ?? '/', `http://localhost:${PORT}`);
  // Accept paths with or without the `/api` prefix (the Vite proxy may forward
  // either depending on its rewrite config).
  const path = url.pathname.replace(/^\/api(?=\/|$)/, '') || '/';

  if (req.method === 'OPTIONS') {
    res.writeHead(204).end();
    return;
  }

  const match = ROUTES.map((route) => ({ route, m: route.re.exec(path) })).find(
    (x) => x.m !== null,
  );
  if (!match || !match.m) {
    res.writeHead(404, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'no local route', path }));
    return;
  }

  const pathParameters: Record<string, string> = {};
  for (const [key, value] of Object.entries(match.m.groups ?? {})) {
    if (value !== undefined) pathParameters[key] = decodeURIComponent(value);
  }

  try {
    const body = await readBody(req);
    const claims = await resolveClaims(req);
    const event = buildEvent(req, path, url.searchParams, pathParameters, body, claims);
    const result = (await match.route.handler(event)) as Exclude<APIGatewayProxyResultV2, string>;
    const statusCode = result.statusCode ?? 200;
    const headers = { 'content-type': 'application/json', ...(result.headers ?? {}) };
    res.writeHead(statusCode, headers as Record<string, string>);
    res.end(typeof result.body === 'string' ? result.body : JSON.stringify(result.body ?? {}));
  } catch (err) {
    // Surface handler crashes as 500s instead of hanging the socket.
    res.writeHead(500, { 'content-type': 'application/json' });
    res.end(JSON.stringify({ error: 'dev backend error', detail: String(err) }));
    console.error(`[dev-api] ${req.method} ${path} failed:`, err);
  }
}

// ---- Default skills seed ---------------------------------------------------
// The in-memory store starts empty, so without this the Skills tab is blank on
// every fresh `npm run dev` — even though the repo ships the bundled Command HQ
// skills. We mirror what a deploy does (infra/scripts/seed-skills.mjs): read the
// repo's `catalog/skills/<name>/SKILL.md` set + `bundles.json` manifest and write
// them through the canonical `seedSkills` builder at the dev org. Idempotent, so
// re-seeding is harmless if the process keeps state.

const REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
  '..',
);
const SKILLS_DIR = path.join(REPO_ROOT, 'catalog', 'skills');
const BUNDLES_MANIFEST = path.join(SKILLS_DIR, 'bundles.json');

/**
 * Parse `name` + (folded) `description` from a SKILL.md YAML front matter block.
 * The repo skills use `description: >-` folded scalars, so we gather indented
 * continuation lines until the next top-level key or the closing `---`. Ported
 * from infra/scripts/seed-skills.mjs so the dev seed matches the deploy seed.
 */
function parseFrontmatter(md: string): { name?: string; description: string } {
  const lines = md.split(/\r?\n/);
  if (lines[0]?.trim() !== '---') return { name: undefined, description: '' };
  let name: string | undefined;
  const descParts: string[] = [];
  let inDesc = false;
  for (let i = 1; i < lines.length; i++) {
    const line = lines[i] ?? '';
    if (line.trim() === '---') break;
    const top = /^([A-Za-z0-9_-]+):\s?(.*)$/.exec(line);
    if (top && !line.startsWith(' ')) {
      inDesc = false;
      const [, key, value] = top;
      if (key === 'name') name = value!.trim();
      else if (key === 'description') {
        inDesc = true;
        const v = value!.trim();
        if (v && v !== '>-' && v !== '>' && v !== '|' && v !== '|-') descParts.push(v);
      }
      continue;
    }
    if (inDesc && line.trim()) descParts.push(line.trim());
  }
  return { name, description: descParts.join(' ').trim() };
}

function readSkillFiles(): SeedSkillFile[] {
  if (!existsSync(SKILLS_DIR)) return [];
  const files: SeedSkillFile[] = [];
  for (const entry of readdirSync(SKILLS_DIR, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    const skillMd = path.join(SKILLS_DIR, entry.name, 'SKILL.md');
    if (!existsSync(skillMd)) continue;
    const body = readFileSync(skillMd, 'utf8');
    const { name, description } = parseFrontmatter(body);
    files.push({ name: name ?? entry.name, description, body });
  }
  return files.sort((a, b) => a.name.localeCompare(b.name));
}

function readBundleManifest(): BundleManifest {
  if (!existsSync(BUNDLES_MANIFEST)) return {};
  try {
    return JSON.parse(readFileSync(BUNDLES_MANIFEST, 'utf8')) as BundleManifest;
  } catch (err) {
    console.warn(`[dev-api] could not parse ${BUNDLES_MANIFEST}; seeding no bundles:`, err);
    return {};
  }
}

async function seedDevSkills(): Promise<void> {
  const org = process.env.HQ_DEV_ORG ?? 'dev-org';
  const files = readSkillFiles();
  if (files.length === 0) {
    console.warn(`[dev-api] no SKILL.md files under ${SKILLS_DIR}; Skills tab will be empty.`);
    return;
  }
  const manifest = readBundleManifest();
  const records = await seedSkills(defaultRepo(), org, files, manifest);
  const skills = records.filter((r) => r.kind === 'skill').length;
  const bundles = records.filter((r) => r.kind === 'bundle').length;
  console.log(`[dev-api] seeded ${skills} skills + ${bundles} bundle(s) at org#${org}.`);
}

async function start(): Promise<void> {
  // Expose the repo's skills dir so the orgs handler's starter-seed (POST /orgs)
  // can fall back to reading SKILL.md from disk in local dev — including REAL mode,
  // where a freshly created org has no template catalog to clone from yet.
  process.env.HQ_REPO_SKILLS_DIR ??= SKILLS_DIR;

  if (REAL_MODE) {
    // Real deployed table via the ambient AWS credentials. Do NOT install the
    // in-memory mock and do NOT seed dev skills — production data is read/written
    // as-is, and the seed must never pollute the live catalog.
    process.env.HARNESS_TABLE ??= 'harness';
  } else {
    installInMemoryTable();
    process.env.HARNESS_TABLE ??= 'harness-local';
    await seedDevSkills();
  }

  createServer((req, res) => {
    void handle(req, res);
  }).listen(PORT, () => {
    console.log(`[dev-api] Command HQ local backend on http://localhost:${PORT}`);
    if (REAL_MODE) {
      console.log(
        `[dev-api] REAL mode → DynamoDB '${process.env.HARNESS_TABLE}' ` +
          `(region ${process.env.AWS_REGION ?? 'default'}); verifying Cognito ID tokens. ` +
          'Writes hit PRODUCTION data.',
      );
    } else {
      console.log(
        '[dev-api] OFFLINE mode → in-memory store (resets on restart); dev principal = admin',
      );
    }
  });
}

void start();
