// U23 probe — list every skill whose embedding is MISSING or STALE.
//
// The skill-idea loop fails to EMPTY-STATE: a skill that never embedded (the
// stream-consumer record DLQ'd, the seed never fired the stream, a model-version
// bump left the vector behind) just stops matching topics. No error, no empty
// alarm — the skill silently drops out of association and a hundred sessions
// later it is no better for it. This probe surfaces exactly those skills so the
// gap is VISIBLE instead of inferred from an absence of ideas.
//
// For every live skill (× every org) it compares the skill's CURRENT content
// against its stored vector:
//   - `missing`       — no vector at the skill's key (`<org>#<skillBaseName>`).
//   - `stale-hash`    — a vector exists but its `descHash` ≠ the skill's current
//                       `skillContentHash(description, body)` (desc/body changed,
//                       re-embed never landed).
//   - `stale-version` — a vector exists but its `embeddingVersion` ≠ the active
//                       version (a model bump left this one un-reindexed; U5).
// A skill whose stored vector matches both hash and version is HEALTHY and is
// omitted from the report.
//
// The org walk + live-skill read mirror `reindex-embeddings.mjs` (scan
// `ORG#<name>`/`META` for orgs; `SCOPE#org#<org>` / `SKILL#` minus version
// side-records for skills), and the hash / vector-key / active-version logic
// reuse the SAME compiled backend modules the stream consumer (U3) writes with,
// so "healthy" here means byte-identical to what a live write produced. Run
// `npm run build -w @harness/backend` first.
//
// Usage:
//   node infra/scripts/skills-missing-embeddings.mjs
//   node infra/scripts/skills-missing-embeddings.mjs --json     # machine-readable
//   HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/skills-missing-embeddings.mjs
//
// Read-only: it fetches vectors + scans the table; it writes nothing. A non-empty
// report exits 1 so it can gate a CI / cron check; clean exits 0.
import { pathToFileURL } from 'node:url';
import {
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
  listOrgNames,
  listLiveSkills,
} from './lib/common.mjs';

/**
 * The pure probe core (dependency-injected so a unit test drives it with mocks).
 * For each org, reads its live skills and the stored vectors at their keys, then
 * classifies each skill as healthy (omitted) or missing/stale (reported).
 *
 * deps:
 *   orgs                  — list of org names (or a loader returning one)
 *   skillsForOrg(org)     -> [{ name, baseName?, description?, body? }]
 *   getVectors(index, keys) -> Map<key, metadata>   (absent key = no vector)
 *   contentHash(description, body) -> string
 *   skillVectorKey(org, baseName) -> string
 *   skillIndex            — the skills index name
 *   activeVersion         — the active embedding version stamp to require
 *
 * Returns { orgs, skillsChecked, stale: [{ org, skillBaseName, key, reason,
 * expectedHash, foundHash?, expectedVersion, foundVersion? }] }.
 */
export async function probeMissingEmbeddings(deps) {
  const orgs = Array.isArray(deps.orgs) ? deps.orgs : await deps.orgs();
  const stale = [];
  let skillsChecked = 0;
  for (const org of orgs) {
    const skills = await deps.skillsForOrg(org);
    if (skills.length === 0) continue;
    // Map each skill's expected vector key, then fetch them all in one batch.
    const byKey = new Map();
    for (const skill of skills) {
      const baseName = skill.baseName ?? skill.name;
      byKey.set(deps.skillVectorKey(org, baseName), skill);
    }
    const found = await deps.getVectors(deps.skillIndex, [...byKey.keys()]);
    for (const [key, skill] of byKey) {
      skillsChecked += 1;
      const baseName = skill.baseName ?? skill.name;
      const expectedHash = deps.contentHash(skill.description ?? '', skill.body ?? '');
      const meta = found.get(key);
      if (!meta) {
        stale.push({
          org,
          skillBaseName: baseName,
          key,
          reason: 'missing',
          expectedHash,
          expectedVersion: deps.activeVersion,
        });
        continue;
      }
      const foundHash = meta.descHash;
      const foundVersion = meta.embeddingVersion;
      if (foundHash !== expectedHash) {
        stale.push({
          org,
          skillBaseName: baseName,
          key,
          reason: 'stale-hash',
          expectedHash,
          foundHash,
          expectedVersion: deps.activeVersion,
          foundVersion,
        });
        continue;
      }
      // A present-but-different version stamp means a model bump left this vector
      // un-reindexed (U5) — it lives in an incomparable space, so it is stale.
      // An untagged legacy vector (no stamp) is treated as same-space (matches U5).
      if (typeof foundVersion === 'string' && foundVersion !== deps.activeVersion) {
        stale.push({
          org,
          skillBaseName: baseName,
          key,
          reason: 'stale-version',
          expectedHash,
          foundHash,
          expectedVersion: deps.activeVersion,
          foundVersion,
        });
      }
    }
  }
  return { orgs: orgs.length, skillsChecked, stale };
}

async function main() {
  requireBackendDist(
    'skills-missing-embeddings',
    'embeddings/s3vectors.js',
    'embeddings/bedrock.js',
    'ws/streamConsumer.js',
    'db/keys.js',
  );
  const asJson = process.argv.slice(2).includes('--json');

  const { S3Vectors, SKILL_VECTOR_INDEX, skillVectorKey } = await importBackendDist(
    'embeddings',
    's3vectors.js',
  );
  const { activeEmbeddingVersion } = await importBackendDist('embeddings', 'bedrock.js');
  const { skillContentHash } = await importBackendDist('ws', 'streamConsumer.js');
  const { isVersionSideRecord } = await importBackendDist('db', 'keys.js');

  const doc = makeDocClient();
  const vectors = new S3Vectors();
  const orgs = await listOrgNames(doc, TABLE);

  const result = await probeMissingEmbeddings({
    orgs,
    skillsForOrg: (org) => listLiveSkills(doc, TABLE, org, isVersionSideRecord),
    getVectors: (index, keys) => vectors.getVectors(index, keys),
    contentHash: skillContentHash,
    skillVectorKey,
    skillIndex: SKILL_VECTOR_INDEX,
    activeVersion: activeEmbeddingVersion(),
  });

  if (asJson) {
    console.log(JSON.stringify(result, null, 2));
  } else {
    console.log(
      `[skills-missing-embeddings] checked ${result.skillsChecked} skill(s) across ` +
        `${result.orgs} org(s) — ${result.stale.length} missing/stale.`,
    );
    for (const s of result.stale) {
      console.log(
        `  ${s.reason.padEnd(13)} ${s.org}/${s.skillBaseName} (${s.key})` +
          (s.reason === 'stale-version' ? ` found='${s.foundVersion}' want='${s.expectedVersion}'` : ''),
      );
    }
  }
  // A non-empty report is a fault worth surfacing — exit non-zero so a CI/cron
  // check fails loudly instead of failing to empty-state.
  if (result.stale.length > 0) process.exitCode = 1;
}

// Only auto-run when invoked directly (not when imported by a test).
if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().catch((err) => {
    console.error('[skills-missing-embeddings] failed:', err);
    process.exit(1);
  });
}
