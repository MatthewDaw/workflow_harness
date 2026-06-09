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

// NOTE: the OpenSearch Serverless `SearchStack` (the superseded fuzzy-Forge
// vector index) is intentionally NOT synthesized here. Its network policy is
// public-from-anywhere and it would otherwise deploy under `cdk deploy --all`.
// The default Forge backend is the brute-force cosine fallback over DynamoDB;
// `infra/lib/search-stack.ts` is kept on disk, unreferenced, for if a real
// vector index is ever justified (U20). The skill-idea loop instead uses the
// GA Amazon S3 Vectors `VectorsStack` below (no idle floor, no public policy).

// U1 (skill-idea loop) — Amazon S3 Vectors: one vector bucket + a fixed `skills`
// and `ideas` index (float32, 1024-dim, cosine), org-isolated via an `org`
// metadata filter, plus a least-privilege put/query managed policy the backend
// Lambdas attach to. Independent of ApiStack at synth time (the role attachment
// is wired from ApiStack in U3/U8). This is the GA replacement for the excluded
// Classic SearchStack above.
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
