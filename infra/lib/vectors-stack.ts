import * as cdk from 'aws-cdk-lib/core';
import * as s3vectors from 'aws-cdk-lib/aws-s3vectors';
import * as iam from 'aws-cdk-lib/aws-iam';
import { Construct } from 'constructs';

/**
 * U1 — S3 Vectors stack (skill-idea loop, Phase A).
 *
 * Provisions the Amazon S3 Vectors substrate the ideas loop searches over:
 * one vector bucket with two fixed indexes (skills + ideas),
 * (float32, 1536-dim, cosine), plus a least-privilege managed policy that the
 * stream-consumer / ideas Lambdas attach to for put+query.
 *
 * S3 Vectors is GA (Dec 2025), pay-per-use with no idle floor, AWS-native (stays
 * in-account, IAM, no new vendor/secret). It replaces the retired OpenSearch
 * Serverless SearchStack (idle-cost floor, public network policy).
 *
 * ---------------------------------------------------------------------------
 * INDEX-STRATEGY DECISION: one fixed index per data-type, `org` as a FILTERABLE
 * metadata key — NOT per-org indexes.
 * ---------------------------------------------------------------------------
 * The plan offered two shapes (per-org indexes `<org>-skills`/`<org>-ideas`, or
 * a single index keyed by an `org` filterable metadata key). We pick the single
 * index per type because:
 *
 *  1. Orgs are created at RUNTIME, not at deploy time. `infra/scripts/
 *     seed-all-orgs.mjs` discovers orgs by scanning `ORG#` META records, and the
 *     orgs REST handler clones the starter catalog into a brand-new org the
 *     moment a user creates one. Per-org CDK-provisioned indexes would force a
 *     `cdk deploy VectorsStack` on every org signup — infeasible. A single fixed
 *     index lets the backend isolate orgs with a query-time `org` metadata
 *     filter (every put stamps `org`; every query filters on it — see U8/U22),
 *     with no infra change per org.
 *
 *  2. S3 Vectors allows up to 10 FILTERABLE metadata keys per index, and ALL
 *     metadata keys are filterable BY DEFAULT (you opt keys OUT via
 *     `nonFilterableMetadataKeys`). `org` is one key — comfortably inside the
 *     limit even with room for `skillBaseName`, `embeddingVersion`, `repoId`,
 *     etc. So nothing is declared non-filterable here; the org filter is free.
 *
 *  3. Scale is a non-issue: S3 Vectors supports up to 2B vectors per index, and
 *     the catalog (skills × orgs, ideas × orgs) is orders of magnitude under
 *     that, so collapsing every org into one index per type costs nothing.
 *
 * Two indexes (not one) because skills and ideas are distinct corpora with
 * different lifecycles and are never compared to each other (U7 queries the
 * idea index scoped to a skill; U8 queries the skill index for a topic).
 */

/** OpenRouter `openai/text-embedding-3-small` output dimension. */
const EMBEDDING_DIMENSION = 1536;
/** S3 Vectors currently supports only float32. */
const VECTOR_DATA_TYPE = 'float32';
/** Cosine similarity matches the embedding-based retrieval the loop performs. */
const DISTANCE_METRIC = 'cosine';

export class VectorsStack extends cdk.Stack {
  readonly vectorBucket: s3vectors.CfnVectorBucket;
  readonly skillsIndex: s3vectors.CfnIndex;
  readonly ideasIndex: s3vectors.CfnIndex;
  /**
   * Least-privilege put+query policy scoped to this bucket's indexes. ApiStack
   * attaches it to the stream-consumer + ideas Lambda roles (see grant note
   * below) so those roles — and only those roles — can read/write vectors.
   */
  readonly accessPolicy: iam.ManagedPolicy;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // The bucket name must be globally-account+region unique, lowercase, 3–63
    // chars, no underscores. SSE-S3 (AES256) is the default encryption — no KMS
    // key, no extra cost — so we leave `encryptionConfiguration` unset.
    this.vectorBucket = new s3vectors.CfnVectorBucket(this, 'SkillIdeaVectorBucket', {
      vectorBucketName: 'command-hq-skill-idea-vectors',
    });
    // Retain on stack delete: a vector bucket can only be deleted when empty,
    // and the embeddings are derived state we never want a stack churn to drop.
    this.vectorBucket.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);

    // One fixed skill index. `org` is carried as a (filterable, by default)
    // metadata key on every vector — see the index-strategy note above. We
    // declare NO non-filterable keys so the org filter stays available.
    this.skillsIndex = new s3vectors.CfnIndex(this, 'SkillsIndex', {
      indexName: 'skills',
      vectorBucketName: this.vectorBucket.vectorBucketName!,
      dataType: VECTOR_DATA_TYPE,
      dimension: EMBEDDING_DIMENSION,
      distanceMetric: DISTANCE_METRIC,
    });
    // An index references the bucket by name, so it must outlive the bucket
    // resource creation; make the ordering explicit.
    this.skillsIndex.addDependency(this.vectorBucket);

    // One fixed idea index — same shape, distinct corpus (within-skill dedup /
    // corroboration in U7 queries this, scoped by an `org` + `skillBaseName`
    // metadata filter).
    this.ideasIndex = new s3vectors.CfnIndex(this, 'IdeasIndex', {
      indexName: 'ideas',
      vectorBucketName: this.vectorBucket.vectorBucketName!,
      dataType: VECTOR_DATA_TYPE,
      dimension: EMBEDDING_DIMENSION,
      distanceMetric: DISTANCE_METRIC,
    });
    this.ideasIndex.addDependency(this.vectorBucket);

    // ---- Least-privilege put/query policy -----------------------------------
    // Scoped to this bucket + its indexes only. The actions are exactly what the
    // loop needs: write vectors (PutVectors), read them back / similarity-search
    // (QueryVectors, GetVectors, ListVectors), and resolve the index/bucket
    // (GetIndex, GetVectorBucket). NO CreateIndex/DeleteVectorBucket — the
    // backend never mutates infra. The index ARN pattern is
    // `arn:aws:s3vectors:<region>:<acct>:bucket/<bucket>/index/<index>`.
    const bucketArn = this.vectorBucket.attrVectorBucketArn;
    this.accessPolicy = new iam.ManagedPolicy(this, 'VectorPutQueryPolicy', {
      managedPolicyName: 'command-hq-skill-idea-vectors-put-query',
      description:
        'Put/query access to the skill-idea S3 Vectors bucket + indexes. ' +
        'Attach to the stream-consumer and ideas Lambda roles only (U1/U3/U8).',
      statements: [
        new iam.PolicyStatement({
          effect: iam.Effect.ALLOW,
          actions: [
            's3vectors:PutVectors',
            's3vectors:QueryVectors',
            's3vectors:GetVectors',
            's3vectors:ListVectors',
            's3vectors:DeleteVectors',
            's3vectors:GetIndex',
            's3vectors:GetVectorBucket',
          ],
          // Bucket itself + every index under it (skills, ideas).
          resources: [bucketArn, `${bucketArn}/index/*`],
        }),
      ],
    });

    // Grant wiring is cross-stack by design: the consumer/ideas Lambda roles
    // live in ApiStack, which attaches `accessPolicy` to them (U3/U8). Until
    // then the policy is attached to no role and grants nothing — an unattached
    // managed policy is inert, so this stays least-privilege by construction.

    // ---- Outputs ------------------------------------------------------------
    new cdk.CfnOutput(this, 'VectorBucketName', {
      value: this.vectorBucket.vectorBucketName!,
    });
    new cdk.CfnOutput(this, 'VectorBucketArn', { value: bucketArn });
    new cdk.CfnOutput(this, 'SkillsIndexArn', { value: this.skillsIndex.attrIndexArn });
    new cdk.CfnOutput(this, 'IdeasIndexArn', { value: this.ideasIndex.attrIndexArn });
    new cdk.CfnOutput(this, 'VectorPutQueryPolicyArn', {
      value: this.accessPolicy.managedPolicyArn,
    });
  }
}
