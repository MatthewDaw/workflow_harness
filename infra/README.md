# Command HQ infrastructure (CDK, TypeScript)

CDK app for the `claude+` / Command HQ backend and web hosting. The `cdk.json`
file tells the CDK Toolkit how to execute the app (`npx ts-node bin/infra.ts`).

## Stacks

All stacks deploy to **`us-east-1`** (required for the CloudFront/ACM path and to
keep the API, table, and SPA colocated) under the account from the ambient CDK
CLI credentials (`CDK_DEFAULT_ACCOUNT`).

| Stack         | Unit | What it provisions                                                                                                                                                       |
| ------------- | ---- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `AuthStack`   | U4   | Cognito user pool + web app client for HQ users.                                                                                                                         |
| `ApiStack`    | U5   | DynamoDB single-table (`harness`) + HTTP API (JWT auth) + WebSocket API (device/JWT auth) and the handler Lambdas. References `AuthStack`.                               |
| `SearchStack` | U27  | OpenSearch Serverless `VECTORSEARCH` collection for Forge (optional; brute-force fallback is the default).                                                               |
| `SiteStack`   | U29  | Private S3 bucket + CloudFront (Origin Access Control) serving the Vite SPA build, with SPA 403/404 → `/index.html` routing and a cache-invalidating `BucketDeployment`. |

## Useful commands

- `npm run build` compile TypeScript to JS
- `npm run watch` watch for changes and compile
- `npm run test` run the jest assertion tests (table/GSI/routes, OAC, SPA routing)
- `npx cdk synth` emit the synthesized CloudFormation templates for all stacks
- `npx cdk diff` compare deployed stacks with current state
- `npx cdk deploy` deploy stacks to your default AWS account/region

## Deploying

> **Important:** the `SiteStack` deployment uploads the contents of
> `packages/web/dist`, so the web app must be built first. From the repo root:
>
> ```bash
> npm run build -w @harness/web
> ```

### 1. Bootstrap (once per account + region)

CDK needs a bootstrap stack (asset bucket, deploy roles) in the target account
and region before the first deploy. Run this **once** per account/region:

```bash
# from infra/
npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1
```

### 2. Deploy

Deploy order is handled by CDK from the cross-stack references (`ApiStack`
depends on `AuthStack`). Deploy everything, or pick stacks explicitly:

```bash
# from infra/ — all stacks
npx cdk deploy --all

# or individually (AuthStack must precede ApiStack)
npx cdk deploy AuthStack ApiStack SiteStack
```

After a successful `SiteStack` deploy, the SPA is served from the CloudFront URL
emitted as the `SiteUrl` output; the `BucketDeployment` invalidates the
distribution (`/*`) so new builds are visible immediately.

### CI

`.github/workflows/deploy.yml` builds the web app, synths, and (on a guarded
manual trigger / `main` push with the right environment) deploys. Bootstrap is a
one-time manual prerequisite and is **not** performed by CI.
