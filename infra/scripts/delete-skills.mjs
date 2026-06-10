// One-off: delete named skill records from the deployed catalog (the seed only
// upserts, so removed/renamed skills must be deleted explicitly).
// Usage: SEED_ORG=<org> node infra/scripts/delete-skills.mjs name1 name2 ...
import { DeleteCommand } from '@aws-sdk/lib-dynamodb';
import { TABLE, makeDocClient, importBackendDist, importSharedDist } from './lib/common.mjs';

const ORG = process.env.SEED_ORG;
if (!ORG) {
  console.error('[delete-skills] SEED_ORG is required (no default — refuses to guess the org).');
  process.exit(1);
}
const names = process.argv.slice(2);
if (!names.length) {
  console.error('no names given');
  process.exit(1);
}

const { skillKey } = await importBackendDist('db', 'keys.js');
const { orgScope } = await importSharedDist('delete-skills');

const doc = makeDocClient();
for (const name of names) {
  await doc.send(new DeleteCommand({ TableName: TABLE, Key: skillKey(orgScope(ORG), name) }));
  console.log(`[delete-skills] deleted ${name} @ org#${ORG}`);
}
