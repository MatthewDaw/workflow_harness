// Backfill the `org` tag on legacy PROJECT records.
//
// Projects connected before org-stamping existed (commit 3eb77e1) have no `org`
// field. The projects list is now scoped strictly to the caller's effective org
// (packages/backend/src/rest/projects.ts → listProjects), so an untagged project
// is hidden from every org until it is tagged. This one-off assigns each untagged
// project an org = its OWNER's current org (USER#<owner>/PROFILE.org), so nothing
// silently disappears after the scope fix ships.
//
// Idempotent: a project that already has `org` is left untouched. A project whose
// owner has no resolvable org is reported and skipped (never guessed).
//
// Usage:
//   SEED_DRY_RUN=1 node infra/scripts/backfill-project-org.mjs   # report only
//   node infra/scripts/backfill-project-org.mjs                  # write
import { GetCommand, PutCommand, ScanCommand } from '@aws-sdk/lib-dynamodb';
import { TABLE, makeDocClient, importBackendDist, requireBackendDist } from './lib/common.mjs';

const DRY_RUN = Boolean(process.env.SEED_DRY_RUN);

requireBackendDist('backfill-org', 'db/keys.js');
const { userKey } = await importBackendDist('db', 'keys.js');

const doc = makeDocClient();

// 1. Enumerate every PROJECT META record (PK `PROJ#<id>`, SK `META`). A one-off
//    full scan is acceptable here — there is no GSI spanning all projects, and
//    this runs once. Paginate so large tables are fully covered.
const projects = [];
let ExclusiveStartKey;
do {
  const res = await doc.send(
    new ScanCommand({
      TableName: TABLE,
      FilterExpression: 'begins_with(PK, :proj) AND SK = :meta',
      ExpressionAttributeValues: { ':proj': 'PROJ#', ':meta': 'META' },
      ExclusiveStartKey,
    }),
  );
  for (const item of res.Items ?? []) projects.push(item);
  ExclusiveStartKey = res.LastEvaluatedKey;
} while (ExclusiveStartKey);

const untagged = projects.filter((p) => !p.org);
console.log(
  `[backfill-org] ${projects.length} project(s) total; ${untagged.length} untagged (no org).`,
);

// 2. Resolve each untagged project's org from its owner profile and tag it.
const ownerOrg = new Map(); // ownerUserId -> org (memoized profile reads)
let written = 0;
const skipped = [];
for (const project of untagged) {
  const owner = project.ownerUserId;
  if (!ownerOrg.has(owner)) {
    const res = await doc.send(new GetCommand({ TableName: TABLE, Key: userKey(owner) }));
    ownerOrg.set(owner, res.Item?.org);
  }
  const org = ownerOrg.get(owner);
  if (!org) {
    skipped.push(`${project.id} (owner ${owner} has no profile org)`);
    continue;
  }
  console.log(`[backfill-org] ${project.id}: org -> ${org} (owner ${owner})`);
  if (!DRY_RUN) {
    await doc.send(new PutCommand({ TableName: TABLE, Item: { ...project, org } }));
  }
  written++;
}

if (skipped.length) {
  console.warn(`[backfill-org] skipped ${skipped.length} (unresolvable owner org):`);
  for (const s of skipped) console.warn(`  - ${s}`);
}

if (DRY_RUN) {
  console.log(`[backfill-org] DRY RUN — would have tagged ${written} project(s); nothing written.`);
} else {
  console.log(`[backfill-org] tagged ${written} project(s).`);
}
