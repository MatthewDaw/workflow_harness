import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as events from 'aws-cdk-lib/aws-events';
import * as targets from 'aws-cdk-lib/aws-events-targets';
import { Construct } from 'constructs';

/**
 * R3 — Learning service infra: container Lambda + EventBridge schedule +
 * Secrets Manager + IAM least-privilege (MAT-152).
 *
 * What this stack provisions:
 *
 *  1. **Container Lambda** — packages the Python learning service with the NLI
 *     cross-encoder model bundled in the image.  The model artifact is
 *     hash-pinned via the `PINNED_NLI_MODEL_REVISION` build arg so a HuggingFace
 *     namespace-hijack / weight substitution cannot silently flip verdicts.
 *     Cold-start is acceptable for v1 (user-triggered ingest job; no delivery
 *     clock).  The necessity-scan handler is exported from the same image as a
 *     separate Lambda (same code, different CMD) so the ingest job and the daily
 *     scan each run at their own concurrency.
 *
 *  2. **EventBridge rule** — `rate(1 day)` schedule triggers the necessity-scan
 *     Lambda, supplying the org + optional skill_base_name in the event JSON.
 *     The event shape matches `necessity.scheduled_scan_handler`.
 *
 *  3. **Secrets Manager** — two secrets:
 *     - `command-hq/github-app-pem`  — the GitHub App RSA private key (PEM).
 *       Never in the image, never in env vars, never logged.  Loaded at cold
 *       start by the ingest Lambda via boto3 `get_secret_value` and held
 *       in-process; rotation = new key → Secrets Manager → delete old.
 *     - `command-hq/github-webhook-secret` — the HMAC webhook secret (deferred
 *       continuous mode).  Provisioned now (empty placeholder, or adopt existing)
 *       so the secret ARN is stable for when the webhook receiver ships.
 *
 *  4. **IAM least-privilege** — the Lambda execution role is scoped to the
 *     exact DynamoDB partition prefixes the learning service reads/writes:
 *       - `SCOPE#org#*`  — idea, anchor, and processed-PR cursor records
 *       - `REPO#*`       — repo-scoped anchor index records
 *       - `VERIFY#*`     — verification/supersession audit records
 *       - `SKILL#*`      — skill revision reads (the service is the sole writer)
 *       - `IDEAGOLD#*`   — golden case records
 *     No `dynamodb:*` on the whole table.  Compared with the existing
 *     `grantReadWriteData` pattern in ApiStack (which uses full table access),
 *     this follows the plan's "IAM least-privilege scoped to the new partitions"
 *     requirement.
 *
 * Deferred (continuous webhook):
 *   A thin ack Lambda + SQS queue (no ML deps, no cold-start pressure) is
 *   explicitly NOT included in v1.  When added it will HMAC-verify the
 *   webhook body and enqueue to SQS; this container Lambda consumes from SQS.
 *   The `command-hq/github-webhook-secret` secret is already provisioned here
 *   so the ARN is stable.
 */

export interface LearningStackProps extends cdk.StackProps {
  /** The harness DynamoDB table the learning service reads/writes. */
  readonly table: dynamodb.ITable;
  /**
   * The pinned git revision of the NLI model weights on HuggingFace.
   * Passed as a build arg so a HF namespace-hijack / weight substitution that
   * preserves the revision tag cannot silently flip verdicts.
   * Defaults to "main" (acceptable only for local smoke; CI must supply a SHA).
   */
  readonly pinnedNliModelRevision?: string;
  /**
   * The sha256 hex digest of the model weight files (all shards, name-prefixed).
   * Verified at Docker build time; the build FAILS if the digest does not match.
   * Leave undefined to skip verification (local dev only — never in production).
   *
   * To obtain the digest for a new model revision, run once without this arg,
   * note the "weights digest (sha256): <hex>" line in the build log, then pin it.
   */
  readonly pinnedNliModelSha256?: string;
}

/**
 * The REPO ROOT is the Docker build context — not the package directory.
 * The Dockerfile lives at packages/learning-service/Dockerfile and references
 * both packages/learning-service and agent-families (at repo root), so the
 * context must be the repo root.
 */
const REPO_ROOT = path.join(__dirname, '..', '..');

export class LearningStack extends cdk.Stack {
  /** The container Lambda that runs the ingest job (user-triggered, v1). */
  readonly ingestLambda: lambda.DockerImageFunction;
  /** The container Lambda that runs the daily necessity scan (EventBridge). */
  readonly necessityScanLambda: lambda.DockerImageFunction;
  /** Execution role shared by both Lambdas (same image, same permissions). */
  readonly executionRole: iam.Role;
  /** GitHub App PEM secret. */
  readonly githubAppPemSecret: secretsmanager.ISecret;
  /** GitHub webhook secret (deferred continuous mode; provisioned now for stable ARN). */
  readonly githubWebhookSecret: secretsmanager.ISecret;
  /** EventBridge rule for the daily necessity scan. */
  readonly necessityScanRule: events.Rule;

  constructor(scope: Construct, id: string, props: LearningStackProps) {
    super(scope, id, props);

    const { table } = props;
    const pinnedRevision = props.pinnedNliModelRevision ?? 'main';
    const pinnedSha256 = props.pinnedNliModelSha256 ?? '';

    // ---- Secrets Manager secrets -------------------------------------------

    // GitHub App RSA private key (PEM).  This is created here as a managed
    // secret with no generated value — the real PEM is populated out-of-band
    // (e.g. `aws secretsmanager put-secret-value --secret-id ... --secret-string
    // "$(cat app.pem)"`).  `RETAIN` so a stack destroy doesn't obliterate the key.
    //
    // NOTE: an existing secret can be adopted via `GITHUB_APP_PEM_SECRET_ARN`
    // in the environment or the `githubAppPemSecretArn` CDK context.
    const existingPemArn =
      process.env.GITHUB_APP_PEM_SECRET_ARN ??
      (this.node.tryGetContext('githubAppPemSecretArn') as string | undefined);

    this.githubAppPemSecret = existingPemArn
      ? secretsmanager.Secret.fromSecretCompleteArn(
          this,
          'GitHubAppPemSecret',
          existingPemArn,
        )
      : new secretsmanager.Secret(this, 'GitHubAppPemSecret', {
          secretName: 'command-hq/github-app-pem',
          description:
            'GitHub App RSA private key (PEM) for the Verified Learning ingest service. ' +
            'Populate out-of-band; never env/image; never logged.',
          // No `generateSecretString` — the PEM is user-supplied, not generated.
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

    // GitHub webhook secret (HMAC; deferred continuous mode).  Provisioned now
    // for a stable ARN; the value is populated when the webhook receiver ships.
    const existingWebhookArn =
      process.env.GITHUB_WEBHOOK_SECRET_ARN ??
      (this.node.tryGetContext('githubWebhookSecretArn') as string | undefined);

    this.githubWebhookSecret = existingWebhookArn
      ? secretsmanager.Secret.fromSecretCompleteArn(
          this,
          'GitHubWebhookSecret',
          existingWebhookArn,
        )
      : new secretsmanager.Secret(this, 'GitHubWebhookSecret', {
          secretName: 'command-hq/github-webhook-secret',
          description:
            'HMAC signing secret for the GitHub pull_request webhook (deferred continuous mode). ' +
            'Provisioned now for a stable ARN; populate when the webhook receiver ships.',
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

    // ---- IAM execution role (least-privilege) --------------------------------
    //
    // Scoped to the exact DynamoDB partition prefixes the learning service owns:
    //   SCOPE#org#*   — idea records, anchor index, processed-PR cursors
    //   REPO#*        — repo-scoped anchor + branch-session map records
    //   VERIFY#*      — verification / supersession audit log
    //   SKILL#*       — skill revisions (Python is the sole writer)
    //   IDEAGOLD#*    — golden case records
    //
    // The leading-key condition is `dynamodb:LeadingKeys` which scopes by the
    // PK value.  Because the table uses a composite key (PK + SK) and the
    // partition prefixes are on PK, a LeadingKeys condition on the PK prefixes
    // gives exact partition isolation with no SK constraint needed.
    //
    // Secrets Manager: `GetSecretValue` on the two secrets; no rotate/delete.
    this.executionRole = new iam.Role(this, 'LearningServiceRole', {
      roleName: 'command-hq-learning-service',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      description:
        'Least-privilege execution role for the Verified Learning Lambda. ' +
        'Scoped to SCOPE#org#*, REPO#*, VERIFY#*, SKILL#*, IDEAGOLD#* partitions only.',
      managedPolicies: [
        // Basic Lambda execution (CloudWatch Logs).
        iam.ManagedPolicy.fromAwsManagedPolicyName(
          'service-role/AWSLambdaBasicExecutionRole',
        ),
      ],
    });

    // DynamoDB least-privilege: the new learning partitions only.
    // Using a single PolicyStatement with multiple PK prefix conditions (OR'd
    // via the `ForAnyValue:StringLike` set operator on `dynamodb:LeadingKeys`).
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'DynamoLearningPartitions',
        effect: iam.Effect.ALLOW,
        actions: [
          'dynamodb:GetItem',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:DeleteItem',
          'dynamodb:Query',
          'dynamodb:BatchGetItem',
          'dynamodb:BatchWriteItem',
          'dynamodb:TransactWriteItems',
          'dynamodb:ConditionCheckItem',
        ],
        resources: [table.tableArn, `${table.tableArn}/index/*`],
        conditions: {
          // Allow access only when the request's leading key matches one of
          // the learning-service partition prefixes.  `ForAnyValue:StringLike`
          // is the IAM set operator that ORs across the key values supplied.
          'ForAnyValue:StringLike': {
            'dynamodb:LeadingKeys': [
              'SCOPE#org#*',
              'REPO#*',
              'VERIFY#*',
              'SKILL#*',
              'IDEAGOLD#*',
            ],
          },
        },
      }),
    );

    // Secrets Manager: get-only on the two learning secrets.
    this.executionRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'SecretsManagerLearning',
        effect: iam.Effect.ALLOW,
        actions: ['secretsmanager:GetSecretValue'],
        resources: [
          this.githubAppPemSecret.secretArn,
          this.githubWebhookSecret.secretArn,
        ],
      }),
    );

    // Common image build args.
    //
    // PINNED_NLI_MODEL_REVISION: git commit SHA (or tag) on HuggingFace —
    //   prevents a future HF push from silently swapping weights.
    // PINNED_NLI_MODEL_SHA256: sha256 hex digest of the downloaded weight
    //   files (all shards, name-prefixed) — verified at build time so the
    //   image FAILS to build if weights are substituted.  Leave empty for
    //   local smoke; always supply a real digest in CI/production.
    const imageBuildArgs: Record<string, string> = {
      PINNED_NLI_MODEL_REVISION: pinnedRevision,
      PINNED_NLI_MODEL_SHA256: pinnedSha256,
    };

    // ---- Container image per Lambda -----------------------------------------
    //
    // The build context is the REPO ROOT (not packages/learning-service) because
    // the Dockerfile copies both packages/learning-service AND agent-families,
    // which lives at the repo root.  Docker forbids COPY paths that escape the
    // build context, so the context must encompass both directories.
    //
    // The Dockerfile path is supplied explicitly via `file` so CDK knows where
    // to find it relative to the (repo-root) context.
    //
    // Each Lambda gets its own `DockerImageCode` so we can supply a different
    // `cmd` override per function (CDK's AssetImageCodeProps.cmd is how you
    // override the Dockerfile CMD for a container Lambda).  Both images share
    // the same source dir and build args — Docker layer caching means the
    // second build is almost free.
    const ingestImageCode = lambda.DockerImageCode.fromImageAsset(REPO_ROOT, {
      buildArgs: imageBuildArgs,
      file: 'packages/learning-service/Dockerfile',
      // Override the default Dockerfile CMD (webhook handler) with the ingest handler.
      cmd: ['learning_service.entrypoints.ingest.lambda_handler'],
    });

    const necessityScanImageCode = lambda.DockerImageCode.fromImageAsset(
      REPO_ROOT,
      {
        buildArgs: imageBuildArgs,
        file: 'packages/learning-service/Dockerfile',
        // Override CMD with the scheduled necessity-scan handler.
        cmd: ['learning_service.necessity.scheduled_scan_handler'],
      },
    );

    // Common environment for both Lambdas.
    const commonEnv: Record<string, string> = {
      HARNESS_TABLE: table.tableName,
      LS_MODE: 'shadow',          // shadow-first; flip to enforce after calibration
      LS_NLI_MODE: 'passthrough', // real model (bundled in image)
      LS_JUDGE_MODE: 'replay',    // judge fixtures until calibrated
      // PEM and webhook secret are loaded at cold start via boto3 GetSecretValue;
      // only the ARN is surfaced in the environment (not the plaintext value).
      GITHUB_APP_PEM_SECRET_ARN: this.githubAppPemSecret.secretArn,
      GITHUB_WEBHOOK_SECRET_ARN: this.githubWebhookSecret.secretArn,
    };

    // ---- Ingest Lambda (user-triggered, v1) ---------------------------------
    this.ingestLambda = new lambda.DockerImageFunction(this, 'IngestLambda', {
      functionName: 'command-hq-learning-ingest',
      description:
        'Verified Learning — user-triggered PR history replay. ' +
        'Invoked directly (no cold-start clock); v1 trigger.',
      code: ingestImageCode,
      role: this.executionRole,
      timeout: cdk.Duration.minutes(15), // history replay may be long
      memorySize: 2048,                  // model in memory
      environment: commonEnv,
    });

    // ---- Necessity-scan Lambda (EventBridge daily) --------------------------
    this.necessityScanLambda = new lambda.DockerImageFunction(
      this,
      'NecessityScanLambda',
      {
        functionName: 'command-hq-learning-necessity-scan',
        description:
          'Verified Learning — daily necessity scan over folded ideas. ' +
          'Driven by EventBridge rate(1 day); demotes useless ideas within one cycle.',
        code: necessityScanImageCode,
        role: this.executionRole,
        timeout: cdk.Duration.minutes(5),
        memorySize: 2048,
        environment: commonEnv,
      },
    );

    // ---- EventBridge rule: rate(1 day) → necessity scan --------------------
    //
    // The event JSON matches `necessity.scheduled_scan_handler`'s GLOBAL scan
    // path: when `org` and `skill_base_name` are absent, the handler enumerates
    // ALL (org, skill) pairs with folded non-authored ideas and scans each.
    // This is correct for the daily EventBridge trigger — no org or skill needs
    // to be baked into the rule.
    //
    // Event shape (global scan — no org/skill):
    //   {
    //     "source": "eventbridge.scheduled",
    //     "detail-type": "NecessityScanScheduled",
    //     "detail": {
    //       "necessity_sample_rate": 1.0,
    //       "necessity_min_firings": 0
    //     }
    //   }
    //
    // Operators may run a targeted scan by invoking the Lambda directly with
    // an explicit { "org": "...", "skill_base_name": "..." } payload.
    this.necessityScanRule = new events.Rule(this, 'NecessityScanSchedule', {
      ruleName: 'command-hq-necessity-scan-daily',
      description:
        'Daily necessity gate scan — demotes folded ideas that no longer improve ' +
        'their golden-case outcome. Fires once per day; targets the necessity-scan Lambda.',
      schedule: events.Schedule.rate(cdk.Duration.days(1)),
      // The rule fires even if the Lambda is not yet fully calibrated — the
      // handler respects the advisory/soft-block enforcement gate internally.
      enabled: true,
    });

    // Wire the EventBridge rule to the necessity-scan Lambda.
    // No org/skill_base_name in the event → handler performs a GLOBAL scan
    // (enumerates all orgs/skills with folded non-authored ideas).
    this.necessityScanRule.addTarget(
      new targets.LambdaFunction(this.necessityScanLambda, {
        event: events.RuleTargetInput.fromObject({
          source: 'eventbridge.scheduled',
          'detail-type': 'NecessityScanScheduled',
          detail: {
            necessity_sample_rate: 1.0,
            necessity_min_firings: 0,
          },
        }),
        retryAttempts: 2,
      }),
    );

    // ---- Outputs -----------------------------------------------------------
    new cdk.CfnOutput(this, 'IngestLambdaArn', {
      value: this.ingestLambda.functionArn,
      description: 'ARN of the learning ingest Lambda (user-triggered v1)',
    });
    new cdk.CfnOutput(this, 'NecessityScanLambdaArn', {
      value: this.necessityScanLambda.functionArn,
      description: 'ARN of the daily necessity-scan Lambda (EventBridge)',
    });
    new cdk.CfnOutput(this, 'GitHubAppPemSecretArn', {
      value: this.githubAppPemSecret.secretArn,
      description: 'Secrets Manager ARN for the GitHub App PEM (populate out-of-band)',
    });
    new cdk.CfnOutput(this, 'GitHubWebhookSecretArn', {
      value: this.githubWebhookSecret.secretArn,
      description: 'Secrets Manager ARN for the GitHub webhook secret (deferred)',
    });
    new cdk.CfnOutput(this, 'NecessityScanRuleArn', {
      value: this.necessityScanRule.ruleArn,
      description: 'ARN of the EventBridge rule driving the daily necessity scan',
    });
  }
}
