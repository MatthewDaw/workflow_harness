import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import { VectorsStack } from '../lib/vectors-stack';

/**
 * U1 assertions (CDK assertion tests only — no runtime behavior):
 *
 *  - the synthesized stack provisions one S3 Vectors bucket and the two fixed
 *    indexes (skills + ideas), shaped for the embeddings (float32, 1536-dim, cosine);
 *  - it ships a least-privilege put/query managed policy scoped to that bucket's
 *    indexes — and grants those actions to NO role by itself (the attachment is
 *    deferred to ApiStack in U3/U8, so synth shows an unattached/ inert policy);
 *  - the Classic OpenSearch `SearchStack` is NOT part of this stack.
 */
function synth(): Template {
  const app = new cdk.App();
  const stack = new VectorsStack(app, 'TestVectorsStack', {
    env: { account: '123456789012', region: 'us-east-1' },
  });
  return Template.fromStack(stack);
}

describe('VectorsStack', () => {
  const template = synth();

  test('provisions exactly one S3 Vectors bucket', () => {
    template.resourceCountIs('AWS::S3Vectors::VectorBucket', 1);
    template.hasResourceProperties('AWS::S3Vectors::VectorBucket', {
      VectorBucketName: 'command-hq-skill-idea-vectors',
    });
  });

  test('the vector bucket is retained on stack delete (derived state, delete-when-empty)', () => {
    template.hasResource('AWS::S3Vectors::VectorBucket', {
      DeletionPolicy: 'Retain',
    });
  });

  test('provisions the skills and ideas indexes (float32, 1536, cosine)', () => {
    template.resourceCountIs('AWS::S3Vectors::Index', 2);

    for (const indexName of ['skills', 'ideas']) {
      template.hasResourceProperties('AWS::S3Vectors::Index', {
        IndexName: indexName,
        DataType: 'float32',
        Dimension: 1536,
        DistanceMetric: 'cosine',
      });
    }
  });

  test('indexes declare NO non-filterable metadata keys (org filter stays available)', () => {
    // We rely on `org` being a filterable metadata key (the S3 Vectors default).
    // Declaring it non-filterable would break per-org isolation at query time, so
    // assert no index opts any key out of filtering.
    const indexes = template.findResources('AWS::S3Vectors::Index');
    for (const [, res] of Object.entries(indexes)) {
      const cfg = (res as any).Properties?.MetadataConfiguration;
      // Either absent entirely, or present with no NonFilterableMetadataKeys.
      const nonFilterable = cfg?.NonFilterableMetadataKeys;
      expect(nonFilterable === undefined || nonFilterable.length === 0).toBe(true);
    }
  });

  test('ships a least-privilege put/query managed policy scoped to the bucket indexes', () => {
    template.resourceCountIs('AWS::IAM::ManagedPolicy', 1);
    template.hasResourceProperties('AWS::IAM::ManagedPolicy', {
      ManagedPolicyName: 'command-hq-skill-idea-vectors-put-query',
      PolicyDocument: Match.objectLike({
        Statement: Match.arrayWith([
          Match.objectLike({
            Effect: 'Allow',
            Action: Match.arrayWith([
              's3vectors:PutVectors',
              's3vectors:QueryVectors',
            ]),
            // Scoped to the bucket ARN and `<bucketArn>/index/*` — never `*`.
            Resource: Match.arrayWith([
              Match.objectLike({ 'Fn::GetAtt': Match.arrayWith(['SkillIdeaVectorBucket']) }),
            ]),
          }),
        ]),
      }),
    });
  });

  test('the put/query policy grants no infra-mutating vector actions', () => {
    const policies = template.findResources('AWS::IAM::ManagedPolicy');
    const doc = JSON.stringify(policies);
    // The backend never creates/deletes indexes or the bucket itself.
    expect(doc).not.toMatch(/s3vectors:CreateIndex/);
    expect(doc).not.toMatch(/s3vectors:DeleteIndex/);
    expect(doc).not.toMatch(/s3vectors:CreateVectorBucket/);
    expect(doc).not.toMatch(/s3vectors:DeleteVectorBucket/);
  });

  test('the policy is attached to NO role at synth (attachment deferred to ApiStack U3/U8)', () => {
    // An unattached managed policy is inert. The grant to the stream-consumer /
    // ideas Lambda roles happens in ApiStack once those roles are wired, so this
    // stack must not bind the policy to any role/user/group on its own.
    const policies = template.findResources('AWS::IAM::ManagedPolicy');
    for (const [, res] of Object.entries(policies)) {
      const props = (res as any).Properties ?? {};
      expect(props.Roles).toBeUndefined();
      expect(props.Users).toBeUndefined();
      expect(props.Groups).toBeUndefined();
    }
  });

  test('does not provision the Classic OpenSearch SearchStack resources', () => {
    template.resourceCountIs('AWS::OpenSearchServerless::Collection', 0);
    template.resourceCountIs('AWS::OpenSearchServerless::SecurityPolicy', 0);
  });
});
