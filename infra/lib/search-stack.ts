import * as cdk from 'aws-cdk-lib/core';
import * as oss from 'aws-cdk-lib/aws-opensearchserverless';
import { Construct } from 'constructs';

/**
 * U27 (optional) — Forge vector search stack: an OpenSearch Serverless
 * collection of type VECTORSEARCH for the k-NN backend (KTD7).
 *
 * This is synth-only scaffolding. The default Forge backend is the brute-force
 * cosine fallback over DynamoDB-stored vectors (see
 * packages/backend/src/forge/search.ts), gated by `FORGE_VECTOR_BACKEND`. This
 * collection is provisioned for when volume justifies a real vector index; the
 * Lambda search path flips to it via that flag. NEVER deploy this from here —
 * `cdk synth` only.
 *
 * A serverless collection requires three companion policies (encryption,
 * network, data-access) or `CreateCollection` is rejected at deploy time, so all
 * three are declared here even though we only synth.
 */
export class SearchStack extends cdk.Stack {
  readonly collection: oss.CfnCollection;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    const collectionName = 'forge-sessions';

    // Encryption policy (AWS-owned key) — required before the collection.
    const encryption = new oss.CfnSecurityPolicy(this, 'ForgeEncryptionPolicy', {
      name: 'forge-encryption',
      type: 'encryption',
      policy: JSON.stringify({
        Rules: [{ ResourceType: 'collection', Resource: [`collection/${collectionName}`] }],
        AWSOwnedKey: true,
      }),
    });

    // Network policy — collection + dashboards reachable (public for the synth
    // template; tighten to VPC endpoints before any real deploy).
    const network = new oss.CfnSecurityPolicy(this, 'ForgeNetworkPolicy', {
      name: 'forge-network',
      type: 'network',
      policy: JSON.stringify([
        {
          Rules: [
            { ResourceType: 'collection', Resource: [`collection/${collectionName}`] },
            { ResourceType: 'dashboard', Resource: [`collection/${collectionName}`] },
          ],
          AllowFromPublic: true,
        },
      ]),
    });

    this.collection = new oss.CfnCollection(this, 'ForgeCollection', {
      name: collectionName,
      type: 'VECTORSEARCH',
      description: 'Forge session-summary embeddings for k-NN agent proposals (U27).',
    });
    this.collection.addDependency(encryption);
    this.collection.addDependency(network);

    new cdk.CfnOutput(this, 'CollectionEndpoint', {
      value: this.collection.attrCollectionEndpoint,
    });
  }
}
