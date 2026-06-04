// One-off: delete named skill records from the deployed catalog (the seed only
// upserts, so removed/renamed skills must be deleted explicitly).
// Usage: SEED_ORG=personasearch node infra/scripts/delete-skills.mjs name1 name2 ...
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, DeleteCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const dist = path.join(repoRoot, 'packages', 'backend', 'dist');
const { skillKey } = await import(pathToFileURL(path.join(dist, 'db', 'keys.js')).href);
const { orgScope } = await import(pathToFileURL(path.join(dist, '..', 'node_modules', '@harness', 'shared', 'dist', 'index.js')).href).catch(async () => await import('@harness/shared'));

const ORG = process.env.SEED_ORG ?? 'personasearch';
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';
const names = process.argv.slice(2);
if (!names.length) { console.error('no names given'); process.exit(1); }

const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
for (const name of names) {
  await doc.send(new DeleteCommand({ TableName: TABLE, Key: skillKey(orgScope(ORG), name) }));
  console.log(`[delete-skills] deleted ${name} @ org#${ORG}`);
}
