// Opt a single project into the HumanLayer set: union the 'humanlayer-ace' bundle's
// MEMBER skill names (the wrapper materializes by exact member name — an enabled
// bundle record has an empty body and is NOT expanded client-side), the 6 research
// agents, and the humanlayer-approvals + humanlayer-contact MCP servers into the
// project's enabledSkills / enabledAgents / enabledMcpServers. Idempotent (set-union).
//
// Reads the authoritative names from the org#SEED_ORG catalog so it only enables
// items that actually exist. Reuses the compiled projectKey for parity.
//
// Usage:
//   SEED_ORG="test org" PROJECT_ID=workflow-harness SEED_DRY_RUN=1 node infra/scripts/optin-humanlayer.mjs
//   SEED_ORG="test org" PROJECT_ID=workflow-harness node infra/scripts/optin-humanlayer.mjs
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import { existsSync } from 'node:fs';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import {
  DynamoDBDocumentClient,
  GetCommand,
  QueryCommand,
  PutCommand,
} from '@aws-sdk/lib-dynamodb';

const here = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(here, '..', '..');
const backendDist = path.join(repoRoot, 'packages', 'backend', 'dist');

const ORG = process.env.SEED_ORG;
const PROJECT_ID = process.env.PROJECT_ID;
const BUNDLE = process.env.BUNDLE ?? 'humanlayer-ace';
const TABLE = process.env.HARNESS_TABLE ?? 'harness';
const REGION = process.env.AWS_REGION ?? 'us-east-1';

if (!ORG || !PROJECT_ID) {
  console.error('[optin-hl] SEED_ORG and PROJECT_ID are both required (no defaults).');
  process.exit(1);
}
if (!existsSync(path.join(backendDist, 'db', 'keys.js'))) {
  console.error(
    `[optin-hl] missing ${backendDist}/db/keys.js — run \`npm run build -w @harness/backend\` first.`,
  );
  process.exit(1);
}
const { projectKey, scopePartition } = await import(
  pathToFileURL(path.join(backendDist, 'db', 'keys.js')).href
);

const doc = DynamoDBDocumentClient.from(new DynamoDBClient({ region: REGION }));
const orgPart = scopePartition({ tier: 'org', id: ORG });

const queryNames = async (skPrefix, predicate = () => true) => {
  const res = await doc.send(
    new QueryCommand({
      TableName: TABLE,
      KeyConditionExpression: 'PK = :pk AND begins_with(SK, :sk)',
      ExpressionAttributeValues: { ':pk': orgPart, ':sk': skPrefix },
    }),
  );
  return (res.Items ?? []).filter(predicate).map((i) => i.name);
};

// 1. Authoritative names from the org#SEED_ORG catalog.
const bundle = await doc.send(
  new GetCommand({ TableName: TABLE, Key: { PK: orgPart, SK: `SKILL#${BUNDLE}` } }),
);
const memberSkills = bundle.Item?.members ?? [];
if (memberSkills.length === 0) {
  console.error(`[optin-hl] bundle '${BUNDLE}' not found / empty in org#${ORG}`);
  process.exit(1);
}
const agentNames = await queryNames('AGENT#');
const HL_MCP = new Set(['humanlayer-approvals', 'humanlayer-contact']);
const mcpNames = await queryNames('MCPSERVER#', (m) => HL_MCP.has(m.name));

// 2. Load the project record.
const projRes = await doc.send(new GetCommand({ TableName: TABLE, Key: projectKey(PROJECT_ID) }));
const project = projRes.Item;
if (!project) {
  console.error(`[optin-hl] project '${PROJECT_ID}' not found`);
  process.exit(1);
}

const union = (arr, add) => Array.from(new Set([...(arr ?? []), ...add]));
const beforeS = (project.enabledSkills ?? []).length;
const beforeA = (project.enabledAgents ?? []).length;
const beforeM = (project.enabledMcpServers ?? []).length;
project.enabledSkills = union(project.enabledSkills, memberSkills);
project.enabledAgents = union(project.enabledAgents, agentNames);
project.enabledMcpServers = union(project.enabledMcpServers, mcpNames);

const summary =
  `project '${PROJECT_ID}': skills ${beforeS}->${project.enabledSkills.length} (+${memberSkills.length} members), ` +
  `agents ${beforeA}->${project.enabledAgents.length} (+${agentNames.length}), ` +
  `mcp ${beforeM}->${project.enabledMcpServers.length} (+${mcpNames.length})`;

if (process.env.SEED_DRY_RUN) {
  console.log(`[dry-run] ${summary}`);
  console.log(`[dry-run] agents=[${agentNames.join(', ')}]  mcp=[${mcpNames.join(', ')}]`);
  console.log('[optin-hl] DRY RUN — nothing written.');
} else {
  await doc.send(new PutCommand({ TableName: TABLE, Item: project }));
  console.log(`[optin-hl] wrote ${summary}`);
}
