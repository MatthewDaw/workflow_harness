// Shared setup for the operator/seed scripts in infra/scripts: repo-root
// resolution, the DynamoDB table/region env defaults, the document client, and
// the compiled-backend-dist dynamic import every script reuses.
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';

// lib/ is one level deeper than the scripts, so the repo root is three up.
export const repoRoot = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '..',
  '..',
  '..',
);

export const TABLE = process.env.HARNESS_TABLE ?? 'harness';
export const REGION = process.env.AWS_REGION ?? 'us-east-1';

/** A DynamoDB document client for the scripts' REGION. */
export function makeDocClient() {
  return DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
}

/**
 * Dynamic-import a compiled module from `packages/backend/dist/<...rel>`.
 * `npm run build -w @harness/backend` must run first so the dist exists.
 */
export function importBackendDist(...rel) {
  return import(pathToFileURL(path.join(repoRoot, 'packages', 'backend', 'dist', ...rel)).href);
}
