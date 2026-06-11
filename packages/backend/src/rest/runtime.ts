import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';
import type { APIGatewayProxyEventV2, APIGatewayProxyResultV2 } from 'aws-lambda';
import type { Principal } from '../auth/verify.js';
import { Repo } from '../db/repo.js';
import { getDb, type Db } from '../db/pg/client.js';

/**
 * Shared runtime wiring for the REST (HTTP API) handlers. Mirrors `ws/runtime`:
 * the Repo is created lazily and memoised across warm invocations, and tests
 * inject their own Repo so nothing here touches the network.
 */

let docClient: DynamoDBDocumentClient | undefined;
let repo: Repo | undefined;

export function defaultRepo(): Repo {
  if (!repo) {
    docClient ??= DynamoDBDocumentClient.from(new DynamoDBClient({}));
    repo = new Repo(docClient);
  }
  return repo;
}

/**
 * The Postgres (Neon) Drizzle client for the strategic-execution domain
 * (objectives + weekly — KTD7). Symmetric with `defaultRepo`: handlers take it as
 * a dep and tests inject a pglite instance instead, so nothing here touches the
 * network. Lazy + process-cached inside `getDb`.
 */
export function defaultDb(): Db {
  return getDb();
}

/**
 * Resolve the authenticated principal from the HTTP API request. The Cognito JWT
 * authorizer (U5) places verified claims on
 * `requestContext.authorizer.jwt.claims`; we read `sub` (the uid) and the org
 * claim from there. Returns undefined when no/!invalid authorizer context is
 * present so handlers can answer 401 uniformly.
 */
export function principalOf(event: APIGatewayProxyEventV2): Principal | undefined {
  const claims = (
    event.requestContext as {
      authorizer?: { jwt?: { claims?: Record<string, unknown> } };
    }
  ).authorizer?.jwt?.claims;
  if (!claims) return undefined;
  const userId = claims.sub;
  const orgValue = claims['custom:org'] ?? claims.org;
  if (typeof userId !== 'string' || typeof orgValue !== 'string') return undefined;
  const nameValue = claims.name ?? claims['custom:name'] ?? claims.email;
  const name = typeof nameValue === 'string' ? nameValue : undefined;
  return { userId, org: orgValue, ...(name ? { name } : {}) };
}

/** A JSON response with the given status and body. */
export function json(statusCode: number, body: unknown): APIGatewayProxyResultV2 {
  return {
    statusCode,
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  };
}

export const ok = (body: unknown): APIGatewayProxyResultV2 => json(200, body);
export const created = (body: unknown): APIGatewayProxyResultV2 => json(201, body);
/** 404 is used for both missing and not-owned resources (avoids enumeration). */
export const notFound = (): APIGatewayProxyResultV2 => json(404, { error: 'not found' });
export const badRequest = (message: string): APIGatewayProxyResultV2 =>
  json(400, { error: message });
export const unauthorized = (): APIGatewayProxyResultV2 => json(401, { error: 'unauthorized' });
export const forbidden = (): APIGatewayProxyResultV2 => json(403, { error: 'forbidden' });
/** 410 Gone — for retired endpoints (e.g. the scope-change route in the org catalog). */
export const gone = (message = 'gone'): APIGatewayProxyResultV2 => json(410, { error: message });
/** 409 Conflict — the request conflicts with the resource's state (e.g. an attempt
 * to mutate a canonical `built-in` in place; the caller must fork it or re-seed). */
export const conflict = (message = 'conflict'): APIGatewayProxyResultV2 =>
  json(409, { error: message });

/** A path parameter, or undefined. */
export function pathParam(event: APIGatewayProxyEventV2, name: string): string | undefined {
  return event.pathParameters?.[name];
}

/** A query-string parameter, or undefined. */
export function queryParam(event: APIGatewayProxyEventV2, name: string): string | undefined {
  return event.queryStringParameters?.[name];
}

/** Parse the JSON request body, or undefined if absent/blank. Throws on bad JSON. */
export function parseBody(event: APIGatewayProxyEventV2): unknown {
  if (!event.body) return undefined;
  const raw = event.isBase64Encoded
    ? Buffer.from(event.body, 'base64').toString('utf8')
    : event.body;
  return raw.trim() ? JSON.parse(raw) : undefined;
}

/** Sentinel `parseBodySafe` returns for malformed JSON (answer with `badRequest`). */
export const INVALID_JSON: unique symbol = Symbol('invalid-json');

/** `parseBody`, but malformed JSON yields the `INVALID_JSON` sentinel instead of throwing. */
export function parseBodySafe(event: APIGatewayProxyEventV2): unknown | typeof INVALID_JSON {
  try {
    return parseBody(event);
  } catch {
    return INVALID_JSON;
  }
}
