// One-off: delete a project (and its partition + repo pointer) from the deployed
// table, via the same Repo.deleteProject the REST handler uses.
// Usage: node infra/scripts/delete-project.mjs <projectId | owner/repo>
import { pathToFileURL, fileURLToPath } from 'node:url';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const dist = path.resolve(here, '..', '..', 'packages', 'backend', 'dist');
const { Repo } = await import(pathToFileURL(path.join(dist, 'db', 'repo.js')).href);

const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';
const arg = process.argv[2];
if (!arg) {
  console.error('usage: delete-project.mjs <projectId | owner/repo>');
  process.exit(1);
}

const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
const repo = new Repo(doc, TABLE);

let id = arg;
if (arg.includes('/')) {
  const resolved = await repo.getProjectIdForRepo(arg);
  if (!resolved) {
    console.error(`no project linked to repo ${arg}`);
    process.exit(1);
  }
  id = resolved;
}
const before = await repo.getProject(id);
console.log(
  `[delete-project] target id=${id} name=${before?.name ?? '(missing)'} repo=${before?.repo ?? '-'}`,
);
const res = await repo.deleteProject(id);
console.log(`[delete-project] deleted=${res.deleted}`);
