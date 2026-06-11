# Deploy runbook — Weekly Commit Lifecycle (Postgres cutover)

This ships the ST6 weekly commit lifecycle and moves the strategic-execution
domain (objectives + weekly) onto **Neon serverless Postgres** (KTD7). The
operational/log/session domain stays on DynamoDB. Plan:
`docs/plans/2026-06-10-006-feat-weekly-commit-lifecycle-plan.md`.

> **One human prerequisite the automation cannot do for you:** create a Neon
> project (free tier) and paste its connection string into Secrets Manager. Neon
> is an external managed Postgres reached over its HTTP driver — there is no
> Aurora cluster, VPC, or RDS instance to provision.

All stacks deploy to **us-east-1** under the ambient AWS CLI account.

---

## 0. Prerequisites

- AWS CLI credentials for the target account (`aws sts get-caller-identity` works).
- This branch checked out, deps installed (`npm install` from the repo root).
- `@harness/web/src/vite-env.d.ts` is committed (it used to be gitignored — fixed
  on this branch — so a fresh checkout typechecks).

## 1. Create the Neon database (manual, ~2 min)

1. Sign in at https://neon.tech and create a project in (or near) `us-east-1`.
2. Copy the **pooled** connection string — it looks like:
   `postgresql://<user>:<pw>@<endpoint>.neon.tech/<db>?sslmode=require`.

## 2. Store the connection string as a secret

The `ApiStack` injects this into every Lambda as `DATABASE_URL` via a
dynamic Secrets Manager reference (`api-stack.ts`).

```bash
aws secretsmanager create-secret \
  --name command-hq/neon-database-url \
  --secret-string 'postgresql://<user>:<pw>@<endpoint>.neon.tech/<db>?sslmode=require'
# (re-running later? use: aws secretsmanager put-secret-value --secret-id command-hq/neon-database-url --secret-string '...')
```

## 3. Create the schema + import existing data

Builds the backend, then runs the idempotent schema migration against Neon and
imports the existing objective tree + any legacy prose weekly records out of
DynamoDB so nothing is lost in the cutover. Idempotent — safe to re-run.

```bash
# from the repo root
npm run build -w @harness/shared
npm run build -w @harness/backend

DATABASE_URL='postgresql://...neon...?sslmode=require' \
HARNESS_TABLE=harness \
  npm run migrate:pg -w @harness/backend
```

Expected output: `schema ready`, then `objectives: imported N`, `weekly:
imported M`. (Omit `HARNESS_TABLE` to create the schema only, no data import.)

## 4. Deploy the API

```bash
cd infra
npx cdk deploy ApiStack          # picks up the new weekly/transition/manager routes + DATABASE_URL
```

If this is the first deploy to the account/region, bootstrap once first:
`npx cdk bootstrap aws://<ACCOUNT_ID>/us-east-1`.

## 5. Build + deploy the web app

`SiteStack`'s `BucketDeployment` uploads `packages/web/dist`, so build first.

```bash
# from the repo root
npm run build -w @harness/web
cd infra && npx cdk deploy SiteStack   # invalidates CloudFront /* automatically
```

The SPA serves from the `SiteUrl` output (the CloudFront URL).

---

## Verification (post-deploy)

1. **Objectives unchanged:** open the Objectives screen — the RCDO tree renders
   off Postgres (percentages now derive from reconciled weekly commits; an SO
   with no reconciled data reads 0% until its first reconcile — intended).
2. **Weekly lifecycle:** open a project's **Weekly** tab → add itemized commits
   (each needs a Supporting Outcome **or** an orphan reason) → **Lock** →
   **Start/Complete reconciliation** → confirm incomplete items carry into next
   week's DRAFT.
3. **Manager brief:** set a `managerUserId` on a couple of users
   (`POST /me/manager`); the manager sees `/weekly/manager` with the
   exception/concentration brief.
4. **Agent path:** run `/hq-weekly-update` in a connected claude+ repo — it
   proposes plan-anchored SO-linked commits, locks, and reconciles from git.

## Rollback

The previous `ApiStack` (Dynamo-only weekly prose) is the prior CloudFormation
template. To roll back: redeploy the previous commit's `ApiStack`. The Neon data
is additive and the Dynamo objectives/weekly items are left intact by the
migration (it reads, never deletes), so a rollback loses no source data.

## Notes / follow-ups

- The Neon **HTTP** driver has no interactive transactions; `reconcile/complete`
  runs its transaction against the `PgDb` (real on pglite in tests). For
  production transactional guarantees, swap to the Neon **WebSocket** driver
  (`drizzle-orm/neon-serverless`) — deferred per the plan, still VPC-free.
- `.github/workflows/deploy.yml` can run steps 4–5; steps 1–3 (Neon + secret +
  migration) are the one-time manual prerequisite.
