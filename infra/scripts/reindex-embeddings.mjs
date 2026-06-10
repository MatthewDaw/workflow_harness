// Reindex EVERY skill's embedding to a TARGET embedding version, then swap the
// active-version pointer (U5). This is the backfill the version guard demands:
// `S3Vectors.queryTopK` REFUSES to compare a query vector to index vectors of a
// different `embeddingVersion` (cross-version cosine is meaningless), so when the
// embedding model/version changes, every skill vector must be regenerated under
// the new version BEFORE queries can run again.
//
// The migration is ordered so it is safe to run against a live index:
//   1. Re-embed every skill (× every org) under the TARGET version and upsert the
//      vector stamped with that version. Until this finishes, the ACTIVE version
//      is still the OLD one — so any query mid-reindex compares old-version query
//      vectors to the still-present old-version index vectors and succeeds.
//   2. Only AFTER every re-embed succeeds, swap the active-version pointer to the
//      target. From then on, queries are stamped target and compare against the
//      now-target index. A failure before the swap leaves the active version
//      untouched (no half-migrated query space).
//
// Orgs are enumerated exactly like `seed-all-orgs.mjs` (scan `ORG#<name>`/`META`),
// and skills are read from the live `SKILL#<name>` records (version side-records —
// `#r<N>` / `#TRUE` — are skipped; only the live record carries the canonical
// desc+body the embedding is built from). The embed + vector-write reuse the SAME
// compiled backend modules (`BedrockEmbedder`, `S3Vectors`, `skillContentHash`)
// the stream consumer (U3) uses, so the reindexed vectors are byte-identical to
// what a live write would produce. Run `npm run build -w @harness/backend` first.
//
// Usage:
//   BEDROCK_EMBEDDING_VERSION=titan-embed-text-v3 \
//     node infra/scripts/reindex-embeddings.mjs --target titan-embed-text-v3
//   REINDEX_DRY_RUN=1 node infra/scripts/reindex-embeddings.mjs --target <v>  # report only
//   HARNESS_TABLE=harness AWS_REGION=us-east-1 node infra/scripts/reindex-embeddings.mjs ...
//
// Idempotent: every vector is upserted by key, so re-running converges. The active
// version is persisted to a config record AND echoed as the operator action
// (update the deployed Lambda's `BEDROCK_EMBEDDING_VERSION`), since the running
// backend reads the active version from its environment.
import { pathToFileURL } from 'node:url';
import { PutCommand } from '@aws-sdk/lib-dynamodb';
import {
  TABLE,
  makeDocClient,
  importBackendDist,
  requireBackendDist,
  listOrgNames,
  listLiveSkills,
} from './lib/common.mjs';

/** The config record that records the active embedding version (post-swap). */
const ACTIVE_VERSION_KEY = { PK: 'CONFIG#EMBEDDING', SK: 'ACTIVE_VERSION' };

/** Parse `--target <version>` (or env BEDROCK_EMBEDDING_VERSION) from argv. */
function resolveTargetVersion(argv) {
  const i = argv.indexOf('--target');
  if (i !== -1 && argv[i + 1]) return argv[i + 1];
  return process.env.BEDROCK_EMBEDDING_VERSION;
}

/**
 * Re-embed every skill in every org under the target version and swap the active
 * pointer. Dependency-injected so a test can drive it with mock embed/vector/db
 * and assert: all skills re-embed, the swap fires exactly once AFTER every embed,
 * and the swap is skipped on a failure (mid-reindex queries keep the old version).
 *
 * deps:
 *   orgs            — list of org names (or a loader)
 *   skillsForOrg(org) -> [{ name, baseName?, description?, body? }]
 *   embed(text)     -> { vector, embeddingVersion }  (must already stamp `target`)
 *   putVectors(index, items) -> void
 *   contentHash(description, body) -> string
 *   skillVectorKey(org, baseName) -> string
 *   skillIndex      — the skills index name
 *   swapActiveVersion(target) -> void   (the pointer flip; runs LAST)
 *   dryRun          — when true, report only; no embed/put/swap
 */
export async function reindexAll(target, deps) {
  if (!target) throw new Error('reindex-embeddings: a target embedding version is required');
  const orgs = Array.isArray(deps.orgs) ? deps.orgs : await deps.orgs();
  let embedded = 0;
  for (const org of orgs) {
    const skills = await deps.skillsForOrg(org);
    for (const skill of skills) {
      const baseName = skill.baseName ?? skill.name;
      const description = skill.description ?? '';
      const body = skill.body ?? '';
      if (deps.dryRun) {
        embedded += 1;
        continue;
      }
      const { vector, embeddingVersion } = await deps.embed(`${description}\n\n${body}`);
      // The embed MUST stamp the target version (the run sets BEDROCK_EMBEDDING_VERSION);
      // guard against a misconfigured run that would write old-version vectors.
      if (embeddingVersion !== target) {
        throw new Error(
          `reindex-embeddings: embed stamped '${embeddingVersion}' but target is '${target}' — ` +
            'set BEDROCK_EMBEDDING_VERSION=<target> for the reindex run before re-embedding.',
        );
      }
      const hash = deps.contentHash(description, body);
      await deps.putVectors(deps.skillIndex, [
        {
          key: deps.skillVectorKey(org, baseName),
          vector,
          metadata: { org, skillBaseName: baseName, embeddingVersion, descHash: hash },
        },
      ]);
      embedded += 1;
    }
  }
  // Swap the active-version pointer LAST — only after every skill is re-embedded.
  // A throw above skips this, leaving the active version (and thus every query)
  // on the OLD version until a clean run completes.
  if (!deps.dryRun) await deps.swapActiveVersion(target);
  return { orgs: orgs.length, embedded, activeVersion: deps.dryRun ? undefined : target };
}

async function main() {
  requireBackendDist(
    'reindex-embeddings',
    'embeddings/bedrock.js',
    'embeddings/s3vectors.js',
    'ws/streamConsumer.js',
  );
  const target = resolveTargetVersion(process.argv.slice(2));
  if (!target) {
    console.error(
      '[reindex-embeddings] no target version — pass `--target <version>` or set ' +
        'BEDROCK_EMBEDDING_VERSION.',
    );
    process.exit(1);
  }
  const dryRun = Boolean(process.env.REINDEX_DRY_RUN);

  const { BedrockEmbedder } = await importBackendDist('embeddings', 'bedrock.js');
  const { S3Vectors, SKILL_VECTOR_INDEX, skillVectorKey } = await importBackendDist(
    'embeddings',
    's3vectors.js',
  );
  const { skillContentHash } = await importBackendDist('ws', 'streamConsumer.js');
  const { isVersionSideRecord } = await importBackendDist('db', 'keys.js');

  const doc = makeDocClient();
  // The embed run MUST stamp the target — pin it for this process so the compiled
  // `activeEmbeddingVersion()` resolves to `target` while re-embedding.
  process.env.BEDROCK_EMBEDDING_VERSION = target;
  const embedder = new BedrockEmbedder();
  const vectors = new S3Vectors();

  const orgs = await listOrgNames(doc, TABLE);
  console.log(
    `[reindex-embeddings] target='${target}' | ${orgs.length} org(s)` +
      `${dryRun ? ' | DRY RUN — no embed/put/swap' : ''}: ${orgs.join(', ')}`,
  );

  const result = await reindexAll(target, {
    orgs,
    skillsForOrg: (org) => listLiveSkills(doc, TABLE, org, isVersionSideRecord),
    embed: (text) => embedder.embed(text),
    putVectors: (index, items) => vectors.putVectors(index, items),
    contentHash: skillContentHash,
    skillVectorKey,
    skillIndex: SKILL_VECTOR_INDEX,
    dryRun,
    // Persist the swap so the active version is recorded in-table for auditing;
    // the AUTHORITATIVE flip is the operator updating the Lambda env (echoed below).
    swapActiveVersion: async (v) => {
      await doc.send(
        new PutCommand({
          TableName: TABLE,
          Item: { ...ACTIVE_VERSION_KEY, version: v, swappedAt: new Date().toISOString() },
        }),
      );
    },
  });

  if (dryRun) {
    console.log(
      `[reindex-embeddings] DRY RUN — would re-embed ${result.embedded} skill(s) across ` +
        `${result.orgs} org(s) and swap the active version to '${target}'. Nothing written.`,
    );
    return;
  }
  console.log(
    `[reindex-embeddings] done: re-embedded ${result.embedded} skill(s) across ${result.orgs} ` +
      `org(s); active version swapped to '${target}'.`,
  );
  console.log(
    `[reindex-embeddings] ACTION: set BEDROCK_EMBEDDING_VERSION='${target}' on the deployed ` +
      'stream-consumer + query Lambdas (redeploy ApiStack) so the running backend stamps + ' +
      'compares against the reindexed version.',
  );
}

// Only auto-run when invoked directly (not when imported by a test).
if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().catch((err) => {
    console.error('[reindex-embeddings] failed:', err);
    process.exit(1);
  });
}
