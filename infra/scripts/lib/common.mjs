// Shared setup for the operator/seed scripts in infra/scripts: repo-root
// resolution, the DynamoDB table/region env defaults, the document client, the
// compiled-dist dynamic imports, and the read-only scan/query helpers the
// scripts reuse.
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand, QueryCommand, ScanCommand } from '@aws-sdk/lib-dynamodb';

// lib/ is one level deeper than the scripts, so the repo root is three up.
export const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
);

export const TABLE = process.env.HARNESS_TABLE ?? 'harness';
export const REGION = process.env.AWS_REGION ?? 'us-east-1';

/** The compiled backend output every script reuses record builders/keys from. */
export const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

/** The org-scope partition prefix the skill records live under (mirrors keys.ts). */
export const ORG_SCOPE_PREFIX = 'SCOPE#org#';

/** A DynamoDB document client for the scripts' REGION. */
export function makeDocClient() {
  return DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
}

/**
 * Dynamic-import a compiled module from `packages/backend/dist/<...rel>`.
 * `npm run build -w @harness/backend` must run first so the dist exists.
 */
export function importBackendDist(...rel) {
  return import(pathToFileURL(path.join(backendDist, ...rel)).href);
}

/**
 * Exit(1) with a build hint unless every `packages/backend/dist/<relPath>`
 * exists ('/'-separated rel paths, e.g. 'seed/skills.js').
 */
export function requireBackendDist(tag, ...relPaths) {
  for (const rel of relPaths) {
    const p = path.join(backendDist, ...rel.split('/'));
    if (!existsSync(p)) {
      console.error(`[${tag}] missing ${p} — run \`npm run build -w @harness/backend\` first.`);
      process.exit(1);
    }
  }
}

/**
 * Dynamic-import the compiled `@harness/shared` entry (packages/shared/dist).
 * Exits(1) with a build hint when the dist is missing.
 */
export function importSharedDist(tag = 'scripts') {
  const entry = path.join(repoRoot, 'packages', 'shared', 'dist', 'index.js');
  if (!existsSync(entry)) {
    console.error(`[${tag}] missing ${entry} — run \`npm run build -w @harness/shared\` first.`);
    process.exit(1);
  }
  return import(pathToFileURL(entry).href);
}

/**
 * Enumerate every org by scanning for its META record (`PK = ORG#<name>`,
 * `SK = META`). Paginated so it survives a table larger than one scan page.
 */
export async function listOrgNames(doc, table) {
  const names = [];
  let ExclusiveStartKey;
  do {
    const res = await doc.send(
      new ScanCommand({
        TableName: table,
        FilterExpression: 'SK = :meta AND begins_with(PK, :orgp)',
        ExpressionAttributeValues: { ':meta': 'META', ':orgp': 'ORG#' },
        ProjectionExpression: 'PK',
        ExclusiveStartKey,
      }),
    );
    for (const item of res.Items ?? []) {
      if (typeof item.PK === 'string' && item.PK.startsWith('ORG#')) {
        names.push(item.PK.slice('ORG#'.length));
      }
    }
    ExclusiveStartKey = res.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return names;
}

/**
 * List the LIVE skill records for one org: items in `SCOPE#org#<org>` whose SK
 * begins `SKILL#` and is NOT a version side-record (`#r<N>` / `#TRUE`). Paginated.
 */
export async function listLiveSkills(doc, table, org, isVersionSideRecord) {
  const skills = [];
  let ExclusiveStartKey;
  do {
    const res = await doc.send(
      new ScanCommand({
        TableName: table,
        FilterExpression: 'PK = :pk AND begins_with(SK, :skp)',
        ExpressionAttributeValues: { ':pk': `${ORG_SCOPE_PREFIX}${org}`, ':skp': 'SKILL#' },
        ExclusiveStartKey,
      }),
    );
    for (const item of res.Items ?? []) {
      if (typeof item.SK !== 'string' || isVersionSideRecord(item.SK)) continue;
      if (typeof item.name !== 'string') continue;
      skills.push(item);
    }
    ExclusiveStartKey = res.LastEvaluatedKey;
  } while (ExclusiveStartKey);
  return skills;
}

/** Query the items in partition `pk` whose SK begins with `skPrefix`. */
export async function queryByPrefix(doc, table, pk, skPrefix) {
  const res = await doc.send(
    new QueryCommand({
      TableName: table,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
      ExpressionAttributeValues: { ':pk': pk, ':sk': skPrefix },
    }),
  );
  return res.Items ?? [];
}

/**
 * Register one humanlayer stdio MCP server into the REQUIRED `SEED_ORG`'s
 * catalog (org-scoped, never org-wide). Validates through the compiled
 * `mcpServerSchema` and writes via `mcpServerKey`, so the record is
 * byte-identical to what `POST /mcp-servers` would accept. SEED_DRY_RUN=1
 * reports without writing. `env` stays {} — env values land in DynamoDB as
 * plaintext, so secrets are supplied where the daemon runs, never here.
 */
export async function registerMcpServer({ name, args, logTag, command = 'humanlayer' }) {
  const ORG = process.env.SEED_ORG;
  if (!ORG) {
    console.error(`[${logTag}] SEED_ORG is required (no default — refuses to guess the org).`);
    process.exit(1);
  }
  requireBackendDist(logTag, 'db/keys.js');
  const { mcpServerSchema, orgScope } = await importSharedDist(logTag);
  const { mcpServerKey } = await importBackendDist('db', 'keys.js');

  const candidate = {
    name,
    scope: orgScope(ORG),
    transport: 'stdio',
    command,
    args,
    env: {},
    createdBy: { userId: 'system', name: 'system' },
  };
  const parsed = mcpServerSchema.safeParse(candidate);
  if (!parsed.success) {
    console.error(`[${logTag}] record failed mcpServerSchema validation:`);
    console.error(parsed.error.message);
    process.exit(1);
  }
  const server = parsed.data;

  if (process.env.SEED_DRY_RUN) {
    const key = mcpServerKey(server.scope, server.name);
    console.log(`[dry-run] mcp-server ${server.name} @ ${server.scope.tier}#${server.scope.id}`);
    console.log(`[dry-run] key ${JSON.stringify(key)}`);
    console.log(`[dry-run] item ${JSON.stringify({ ...key, ...server })}`);
    console.log(
      `[${logTag}] DRY RUN — 1 MCP-server record (${server.name}) targeting org#${ORG} in ${TABLE}. Nothing written.`,
    );
    return;
  }
  const doc = makeDocClient();
  await doc.send(
    new PutCommand({
      TableName: TABLE,
      Item: { ...mcpServerKey(server.scope, server.name), ...server },
    }),
  );
  console.log(
    `[${logTag}] wrote 1 MCP-server record (${server.name}) to ${TABLE} at org#${ORG}.`,
  );
}
