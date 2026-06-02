#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { InfraStack } from '../lib/infra-stack';
import { AuthStack } from '../lib/auth-stack';
import { ApiStack } from '../lib/api-stack';
import { SearchStack } from '../lib/search-stack';

const app = new cdk.App();

const authStack = new AuthStack(app, 'AuthStack');

// U27 (optional, synth-only) — Forge vector index. The default backend is the
// brute-force cosine fallback; this provisions OpenSearch Serverless for when
// volume justifies it. Never deploy from here.
new SearchStack(app, 'SearchStack');

// U5 — backend API: DynamoDB single-table + HTTP API + WebSocket API. The HTTP
// API's JWT authorizer trusts the AuthStack user pool, so ApiStack references it.
new ApiStack(app, 'ApiStack', {
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
});

new InfraStack(app, 'InfraStack', {
  /* If you don't specify 'env', this stack will be environment-agnostic.
   * Account/Region-dependent features and context lookups will not work,
   * but a single synthesized template can be deployed anywhere. */
  /* Uncomment the next line to specialize this stack for the AWS Account
   * and Region that are implied by the current CLI configuration. */
  // env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION },
  /* Uncomment the next line if you know exactly what Account and Region you
   * want to deploy the stack to. */
  // env: { account: '123456789012', region: 'us-east-1' },
  /* For more information, see https://docs.aws.amazon.com/cdk/latest/guide/environments.html */
});
