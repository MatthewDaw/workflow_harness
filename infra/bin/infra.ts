#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib/core';
import { AuthStack } from '../lib/auth-stack';
import { ApiStack } from '../lib/api-stack';
import { SiteStack } from '../lib/site-stack';
import { VectorsStack } from '../lib/vectors-stack';

const app = new cdk.App();

// All stacks deploy to the same account (from the ambient CDK CLI credentials)
// and to us-east-1 — required for the CloudFront/ACM path and to keep the API,
// table, and SPA colocated. Account is environment-agnostic at synth time when
// CDK_DEFAULT_ACCOUNT is unset, so `cdk synth` works with no credentials.
const env: cdk.Environment = {
  account: process.env.CDK_DEFAULT_ACCOUNT,
  region: 'us-east-1',
};

// U4 — Cognito user pool + app client for HQ web users.
const authStack = new AuthStack(app, 'AuthStack', { env });

// U1 (skill-idea loop) — Amazon S3 Vectors: one vector bucket + a fixed `skills`
// and `ideas` index (float32, 1536-dim, cosine), org-isolated via an `org`
// metadata filter, plus a least-privilege put/query managed policy the backend
// Lambdas attach to. Independent of ApiStack at synth time (the role attachment
// is wired from ApiStack in U3/U8). The superseded OpenSearch Serverless
// SearchStack (public network policy, idle-cost floor) is deliberately gone —
// synth-app.test.ts asserts no app stack ever reintroduces it (U20).
new VectorsStack(app, 'VectorsStack', { env });

// U5 — backend API: DynamoDB single-table + HTTP API + WebSocket API. The HTTP
// API's JWT authorizer trusts the AuthStack user pool, so ApiStack references it.
new ApiStack(app, 'ApiStack', {
  env,
  userPool: authStack.userPool,
  userPoolClient: authStack.userPoolClient,
});

// U29 — static site: private S3 bucket + CloudFront (OAC) serving the Vite build
// with SPA error routing. Synth-only here; deploy via the guarded CI workflow.
new SiteStack(app, 'SiteStack', { env });
