// Register the starter workflow(s) into ONE org's catalog — by default NOT
// org-wide.
//
// Modeled EXACTLY on seed-humanlayer-agents.mjs: it reads no markdown tree
// (workflows are structured data, not files), reuses the compiled backend's
// `buildSeedWorkflows` + `STARTER_WORKFLOWS` + `workflowKey` so records are
// byte-identical to what the REST layer reads, and is keyed on a REQUIRED
// `SEED_ORG` so the set lands in exactly one org. The org-wide propagation lives
// in seed-all-orgs.mjs (which calls the same builder for every org).
//
// Run `npm run build -w @harness/backend` first.
//
// Usage:
//   SEED_ORG=<org> SEED_DRY_RUN=1 node infra/scripts/seed-workflows.mjs   # report only
//   SEED_ORG=<org> node infra/scripts/seed-workflows.mjs                  # write to `harness`
import { existsSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { DynamoDBDocumentClient, PutCommand } from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG;
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

if (!ORG) {
  console.error('[seed-workflows] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}

if (!existsSync(path.join(backendDist, 'seed', 'workflows.js'))) {
  console.error(
    `[seed-workflows] missing ${backendDist}/seed/workflows.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}
const { buildSeedWorkflows, STARTER_WORKFLOWS } = await import(
  pathToFileURL(path.join(backendDist, 'seed', 'workflows.js')).href
);
const { workflowKey } = await import(pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href);

// buildSeedWorkflows: one org-scoped kind:'workflow' record per starter file,
// each parsed/validated through workflowSchema (system authorship, base variant).
const records = buildSeedWorkflows(ORG, STARTER_WORKFLOWS);

if (process.env.SEED_DRY_RUN) {
  for (const r of records) {
    console.log(
      `[dry-run] workflow ${r.name} @ ${r.scope.tier}#${r.scope.id} nodes=${r.nodes.length}`,
    );
  }
  console.log(
    `[seed-workflows] DRY RUN — ${records.length} workflow records targeting org#${ORG} in ${TABLE}. Nothing written.`,
  );
} else {
  const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
  for (const record of records) {
    await doc.send(
      new PutCommand({
        TableName: TABLE,
        Item: { ...workflowKey(record.scope, record.name), ...record },
      }),
    );
  }
  console.log(`[seed-workflows] wrote ${records.length} workflow records to ${TABLE} at org#${ORG}.`);
}
