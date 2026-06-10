// One-off: delete named skill records from the deployed catalog (the seed only
// upserts, so removed/renamed skills must be deleted explicitly).
// Usage: SEED_ORG=personasearch node infra/scripts/delete-skills.mjs name1 name2 ...
import { DeleteCommand } from '@aws-sdk/lib-dynamodb';
import { TABLE, makeDocClient, importBackendDist } from './lib/common.mjs';

const { skillKey } = await importBackendDist('db', 'keys.js');
const { orgScope } = await importBackendDist(
  '..',
  'node_modules',
  '@harness',
  'shared',
  'dist',
  'index.js',
).catch(async () => await import('@harness/shared'));

const ORG = process.env.SEED_ORG ?? 'personasearch';
const names = process.argv.slice(2);
if (!names.length) {
  console.error('no names given');
  process.exit(1);
}

const doc = makeDocClient();
for (const name of names) {
  await doc.send(new DeleteCommand({ TableName: TABLE, Key: skillKey(orgScope(ORG), name) }));
  console.log(`[delete-skills] deleted ${name} @ org#${ORG}`);
}
