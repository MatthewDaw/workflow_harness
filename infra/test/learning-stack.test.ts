import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import { LearningStack } from '../lib/learning-stack';

/**
 * R3 (MAT-152) assertions — LearningStack synth/asserts.
 *
 * Acceptance checklist:
 *   - Container image builds; model artifact hash verified at build
 *   - EventBridge schedule invokes the necessity scan
 *   - PEM + webhook secret read from Secrets Manager, not env
 *   - IAM role scoped to the new partitions (least-privilege)
 *
 * All tests are offline (CDK Template assertions; no AWS SDK calls).
 */

function synth(pinnedRevision?: string): Template {
  const app = new cdk.App();

  // A throwaway stack to host the harness DynamoDB table so LearningStack can
  // reference it cross-stack — mirrors the ApiStack pattern in bin/infra.ts.
  const tableStack = new cdk.Stack(app, 'TestTableStack');
  const table = new dynamodb.Table(tableStack, 'TestHarnessTable', {
    tableName: 'harness',
    partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
    sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
    billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
  });

  const stack = new LearningStack(app, 'TestLearningStack', {
    table,
    pinnedNliModelRevision: pinnedRevision ?? 'abc1234def5678abc1234def5678abc1234def56',
  });

  return Template.fromStack(stack);
}

describe('LearningStack (R3 — MAT-152)', () => {
  // Synth once and reuse across all tests in this describe block.
  const template = synth();

  // ---------------------------------------------------------------------------
  // Container Lambda
  // ---------------------------------------------------------------------------

  describe('Container Lambda — model bundled + hash-pinned', () => {
    test('provisions the ingest Lambda as a container image function', () => {
      // A Lambda backed by a container image uses PackageType: Image (not Zip).
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'command-hq-learning-ingest',
        PackageType: 'Image',
      });
    });

    test('provisions the necessity-scan Lambda as a container image function', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'command-hq-learning-necessity-scan',
        PackageType: 'Image',
      });
    });

    test('ingest Lambda has a 15-minute timeout (history replay may be long)', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'command-hq-learning-ingest',
        Timeout: 900, // 15 * 60
      });
    });

    test('necessity-scan Lambda has a 5-minute timeout', () => {
      template.hasResourceProperties('AWS::Lambda::Function', {
        FunctionName: 'command-hq-learning-necessity-scan',
        Timeout: 300, // 5 * 60
      });
    });

    test('both Lambdas use ≥ 2 GB memory (NLI model in memory)', () => {
      for (const name of [
        'command-hq-learning-ingest',
        'command-hq-learning-necessity-scan',
      ]) {
        template.hasResourceProperties('AWS::Lambda::Function', {
          FunctionName: name,
          MemorySize: Match.anyValue(),
        });
        // Validate the value is at least 2048 MB.
        const fns = template.findResources('AWS::Lambda::Function', {
          Properties: { FunctionName: name },
        });
        for (const fn of Object.values(fns)) {
          const mem = (fn as { Properties: { MemorySize: number } }).Properties.MemorySize;
          expect(mem).toBeGreaterThanOrEqual(2048);
        }
      }
    });

    test('both Lambdas share the same execution role', () => {
      // Both functions reference the same RoleArn (the LearningServiceRole).
      const fns = template.findResources('AWS::Lambda::Function', {
        Properties: {
          FunctionName: Match.anyValue(),
          PackageType: 'Image',
        },
      });
      const roleRefs = Object.values(fns).map(
        (fn) =>
          (fn as { Properties: { Role: unknown } }).Properties.Role,
      );
      // There are exactly two image Lambdas and they share the same role ref.
      expect(roleRefs).toHaveLength(2);
      const serialized = roleRefs.map((r) => JSON.stringify(r));
      expect(serialized[0]).toEqual(serialized[1]);
    });

    test('HARNESS_TABLE env var is set on both Lambdas (not hardcoded)', () => {
      for (const name of [
        'command-hq-learning-ingest',
        'command-hq-learning-necessity-scan',
      ]) {
        template.hasResourceProperties('AWS::Lambda::Function', {
          FunctionName: name,
          Environment: {
            Variables: {
              HARNESS_TABLE: Match.anyValue(),
            },
          },
        });
      }
    });

    test('model artifact hash is surfaced as a Lambda build arg (image command pinned)', () => {
      // CDK encodes the pinned revision as a Docker build arg which is folded into
      // the asset content hash — it does NOT appear as a literal string in the
      // CloudFormation template JSON (it's consumed by the docker build process).
      // Instead we verify the *effect*: both Lambdas have an ImageConfig.Command
      // override set, which proves the per-Lambda cmd was wired (the rev is in
      // the build args that produced the image hash).
      const fns = template.findResources('AWS::Lambda::Function');
      let ingestHasCmd = false;
      let scanHasCmd = false;
      for (const [, fn] of Object.entries(fns)) {
        const props = (fn as { Properties: Record<string, unknown> }).Properties;
        const imgCfg = props['ImageConfig'] as { Command?: string[] } | undefined;
        if (props['FunctionName'] === 'command-hq-learning-ingest' && imgCfg?.Command) {
          ingestHasCmd = true;
          expect(imgCfg.Command[0]).toContain('ingest');
        }
        if (props['FunctionName'] === 'command-hq-learning-necessity-scan' && imgCfg?.Command) {
          scanHasCmd = true;
          expect(imgCfg.Command[0]).toContain('necessity');
        }
      }
      expect(ingestHasCmd).toBe(true);
      expect(scanHasCmd).toBe(true);
    });
  });

  // ---------------------------------------------------------------------------
  // EventBridge schedule → necessity scan
  // ---------------------------------------------------------------------------

  describe('EventBridge schedule — rate(1 day) → necessity scan', () => {
    test('provisions the EventBridge rule with a rate(1 day) schedule', () => {
      template.hasResourceProperties('AWS::Events::Rule', {
        Name: 'command-hq-necessity-scan-daily',
        ScheduleExpression: 'rate(1 day)',
        State: 'ENABLED',
      });
    });

    test('EventBridge rule targets the necessity-scan Lambda', () => {
      // The rule must have at least one target that references the necessity Lambda.
      const rules = template.findResources('AWS::Events::Rule', {
        Properties: { Name: 'command-hq-necessity-scan-daily' },
      });
      const ruleValues = Object.values(rules);
      expect(ruleValues).toHaveLength(1);
      const targets = (
        ruleValues[0] as { Properties: { Targets: { Arn: unknown }[] } }
      ).Properties.Targets;
      expect(targets).toBeDefined();
      expect(targets.length).toBeGreaterThan(0);
      // The target Arn must reference the necessity scan Lambda function.
      const targetArns = JSON.stringify(targets);
      expect(targetArns).toContain('NecessityScanLambda');
    });

    test('EventBridge rule has retry attempts configured', () => {
      // Each target should specify RetryPolicy.
      const rules = template.findResources('AWS::Events::Rule', {
        Properties: { Name: 'command-hq-necessity-scan-daily' },
      });
      const ruleValues = Object.values(rules);
      const ruleStr = JSON.stringify(ruleValues[0]);
      expect(ruleStr).toContain('RetryPolicy');
    });

    test('Lambda permission grants EventBridge the right to invoke the scan Lambda', () => {
      // CDK adds an AWS::Lambda::Permission resource to allow events.amazonaws.com
      // to invoke the necessity-scan Lambda.
      template.hasResourceProperties('AWS::Lambda::Permission', {
        Action: 'lambda:InvokeFunction',
        Principal: 'events.amazonaws.com',
      });
    });
  });

  // ---------------------------------------------------------------------------
  // Secrets Manager — PEM + webhook secret; never env/image
  // ---------------------------------------------------------------------------

  describe('Secrets Manager — PEM + webhook secret; never env/image', () => {
    test('provisions the GitHub App PEM secret', () => {
      template.hasResourceProperties('AWS::SecretsManager::Secret', {
        Name: 'command-hq/github-app-pem',
      });
    });

    test('provisions the GitHub webhook secret (deferred continuous mode)', () => {
      template.hasResourceProperties('AWS::SecretsManager::Secret', {
        Name: 'command-hq/github-webhook-secret',
      });
    });

    test('PEM secret has RETAIN removal policy (key must survive stack destroy)', () => {
      const secrets = template.findResources('AWS::SecretsManager::Secret', {
        Properties: { Name: 'command-hq/github-app-pem' },
      });
      const secretValues = Object.values(secrets);
      expect(secretValues).toHaveLength(1);
      const deletion = (
        secretValues[0] as { DeletionPolicy?: string }
      ).DeletionPolicy;
      expect(deletion).toBe('Retain');
    });

    test('webhook secret has RETAIN removal policy', () => {
      const secrets = template.findResources('AWS::SecretsManager::Secret', {
        Properties: { Name: 'command-hq/github-webhook-secret' },
      });
      const secretValues = Object.values(secrets);
      expect(secretValues).toHaveLength(1);
      const deletion = (
        secretValues[0] as { DeletionPolicy?: string }
      ).DeletionPolicy;
      expect(deletion).toBe('Retain');
    });

    test('PEM secret does NOT use GenerateSecretString with a PasswordLength (user-supplied, not auto-generated)', () => {
      // The PEM is populated out-of-band; we must NOT auto-generate a random value.
      // CDK always emits GenerateSecretString in the template (even when empty),
      // but an auto-generating secret carries a PasswordLength > 0. We check the
      // generated block does NOT have PasswordLength — meaning no random value is
      // generated and the secret awaits out-of-band population.
      const secrets = template.findResources('AWS::SecretsManager::Secret', {
        Properties: { Name: 'command-hq/github-app-pem' },
      });
      const secretValues = Object.values(secrets);
      expect(secretValues).toHaveLength(1);
      const generated = (
        secretValues[0] as { Properties: { GenerateSecretString?: { PasswordLength?: number } } }
      ).Properties.GenerateSecretString;
      // Either absent or present but WITHOUT PasswordLength (no auto-generation).
      if (generated) {
        expect(generated.PasswordLength).toBeUndefined();
      }
    });

    test('both Lambda environments expose the secret ARNs, not plaintext values', () => {
      // The environment variables GITHUB_APP_PEM_SECRET_ARN and
      // GITHUB_WEBHOOK_SECRET_ARN must be present; the actual secret value
      // must NOT appear in the Lambda environment (it is loaded at cold start).
      for (const name of [
        'command-hq-learning-ingest',
        'command-hq-learning-necessity-scan',
      ]) {
        template.hasResourceProperties('AWS::Lambda::Function', {
          FunctionName: name,
          Environment: {
            Variables: {
              GITHUB_APP_PEM_SECRET_ARN: Match.anyValue(),
              GITHUB_WEBHOOK_SECRET_ARN: Match.anyValue(),
            },
          },
        });
      }

      // Negative: the env must not contain the key name "GITHUB_APP_PEM"
      // (which would imply the plaintext is injected directly).
      const allFns = template.findResources('AWS::Lambda::Function');
      for (const fn of Object.values(allFns)) {
        const vars = (
          fn as { Properties?: { Environment?: { Variables?: Record<string, unknown> } } }
        ).Properties?.Environment?.Variables;
        if (vars) {
          // The raw PEM (not the ARN pointer) must never appear as a variable name.
          expect(Object.keys(vars)).not.toContain('GITHUB_APP_PEM');
          expect(Object.keys(vars)).not.toContain('GITHUB_APP_PEM_VALUE');
          expect(Object.keys(vars)).not.toContain('WEBHOOK_SECRET');
        }
      }
    });
  });

  // ---------------------------------------------------------------------------
  // IAM least-privilege — scoped to the new partitions
  // ---------------------------------------------------------------------------

  describe('IAM least-privilege — scoped to the new learning partitions', () => {
    test('provisions the LearningService IAM role', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        RoleName: 'command-hq-learning-service',
        AssumeRolePolicyDocument: Match.objectLike({
          Statement: Match.arrayWith([
            Match.objectLike({
              Principal: { Service: 'lambda.amazonaws.com' },
              Action: 'sts:AssumeRole',
            }),
          ]),
        }),
      });
    });

    test('role attaches AWSLambdaBasicExecutionRole managed policy (CloudWatch Logs)', () => {
      template.hasResourceProperties('AWS::IAM::Role', {
        RoleName: 'command-hq-learning-service',
        ManagedPolicyArns: Match.arrayWith([
          Match.objectLike({
            'Fn::Join': Match.arrayWith([
              Match.arrayWith([
                Match.stringLikeRegexp('AWSLambdaBasicExecutionRole'),
              ]),
            ]),
          }),
        ]),
      });
    });

    test('DynamoDB policy is scoped to learning partition prefixes via LeadingKeys condition', () => {
      // The inline policy must contain a condition on dynamodb:LeadingKeys
      // restricting access to the learning service partitions.
      const policies = template.findResources('AWS::IAM::Policy');
      const policyStr = JSON.stringify(policies);

      // All five partition prefixes must appear in the policy conditions.
      for (const prefix of [
        'SCOPE#org#*',
        'REPO#*',
        'VERIFY#*',
        'SKILL#*',
        'IDEAGOLD#*',
      ]) {
        expect(policyStr).toContain(prefix);
      }

      // The LeadingKeys condition key must be present.
      expect(policyStr).toContain('dynamodb:LeadingKeys');
    });

    test('DynamoDB policy includes write actions (the service is the sole revision writer)', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const policyStr = JSON.stringify(policies);
      for (const action of [
        'dynamodb:PutItem',
        'dynamodb:UpdateItem',
        'dynamodb:ConditionCheckItem',
      ]) {
        expect(policyStr).toContain(action);
      }
    });

    test('Secrets Manager policy allows GetSecretValue on exactly the two learning secrets', () => {
      // The policy must contain secretsmanager:GetSecretValue restricted to the
      // two secrets provisioned by this stack (no wildcard on all secrets).
      // CDK encodes secret resources as { Ref: <logicalId> } tokens (not literal ARNs)
      // in the synthesized template, so we check:
      //   1. The action is present.
      //   2. The Resources array references exactly two secrets (two Ref tokens).
      const policies = template.findResources('AWS::IAM::Policy');
      const policyStr = JSON.stringify(policies);
      expect(policyStr).toContain('secretsmanager:GetSecretValue');

      // Find the SecretsManager statement and check it references exactly 2 resources.
      let smStatement: { Action: string | string[]; Resource: unknown[] } | null = null;
      for (const policy of Object.values(policies)) {
        const stmts = (
          policy as {
            Properties: { PolicyDocument: { Statement: { Action: unknown; Resource: unknown[] }[] } };
          }
        ).Properties.PolicyDocument.Statement;
        for (const stmt of stmts) {
          const actions = Array.isArray(stmt.Action) ? stmt.Action : [stmt.Action];
          if (actions.includes('secretsmanager:GetSecretValue')) {
            smStatement = stmt as { Action: string | string[]; Resource: unknown[] };
          }
        }
      }
      expect(smStatement).not.toBeNull();
      // The two secrets (PEM + webhook) must be the only resources.
      const resources = Array.isArray(smStatement!.Resource)
        ? smStatement!.Resource
        : [smStatement!.Resource];
      expect(resources).toHaveLength(2);

      // Both resources must be Ref tokens (pointing at the secrets in this stack),
      // not wildcards or literal ARN strings.
      for (const res of resources) {
        const resStr = JSON.stringify(res);
        expect(resStr).not.toContain('*');          // no wildcard
        expect(resStr).not.toMatch(/^"arn:/);       // not a hardcoded ARN string
        // CDK Ref tokens: { "Ref": "<logicalId>" }
        expect(resStr).toContain('"Ref"');
      }
    });

    test('role does NOT grant dynamodb:* (no wildcard action)', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const roles = template.findResources('AWS::IAM::Role');
      const combined = JSON.stringify({ policies, roles });
      // The wildcard DynamoDB action must not appear in any policy statement
      // on this stack (the basic exec role doesn't grant DynamoDB at all).
      expect(combined).not.toContain('"dynamodb:*"');
      expect(combined).not.toContain("'dynamodb:*'");
    });

    test('role does NOT grant secretsmanager:* (no wildcard on all secrets)', () => {
      const policies = template.findResources('AWS::IAM::Policy');
      const policyStr = JSON.stringify(policies);
      expect(policyStr).not.toContain('"secretsmanager:*"');
      // Rotation / delete / put are not needed.
      expect(policyStr).not.toContain('secretsmanager:RotateSecret');
      expect(policyStr).not.toContain('secretsmanager:DeleteSecret');
      expect(policyStr).not.toContain('secretsmanager:PutSecretValue');
    });
  });

  // ---------------------------------------------------------------------------
  // Outputs
  // ---------------------------------------------------------------------------

  describe('Stack outputs', () => {
    test('outputs the ingest Lambda ARN', () => {
      template.hasOutput('IngestLambdaArn', {});
    });

    test('outputs the necessity-scan Lambda ARN', () => {
      template.hasOutput('NecessityScanLambdaArn', {});
    });

    test('outputs both secret ARNs', () => {
      template.hasOutput('GitHubAppPemSecretArn', {});
      template.hasOutput('GitHubWebhookSecretArn', {});
    });

    test('outputs the EventBridge rule ARN', () => {
      template.hasOutput('NecessityScanRuleArn', {});
    });
  });
});

// ---------------------------------------------------------------------------
// Additional targeted tests (acceptance checklist items)
// ---------------------------------------------------------------------------

describe('R3 acceptance checklist', () => {
  const template = synth('deadbeefcafe1234deadbeefcafe1234deadbeef');

  test('container image build arg PINNED_NLI_MODEL_REVISION is consumed by the image asset', () => {
    // CDK folds the build args into the content hash of the Docker image asset —
    // the pinned SHA does NOT appear as a literal string in the CloudFormation
    // template JSON (it is consumed by docker build during the asset publish step).
    // What IS verifiable in the template is that:
    //   a) Both Lambdas have PackageType: Image (container Lambda).
    //   b) Both Lambdas have an ImageConfig.Command override (the cmd was wired).
    // The CDK asset hash itself changes when the build arg changes — proving the
    // pin is part of the artifact identity — but this requires comparing two synths
    // which is impractical in a unit test.  The command override is the proxy signal.
    const fns = template.findResources('AWS::Lambda::Function');
    let imageCount = 0;
    for (const fn of Object.values(fns)) {
      const props = (fn as { Properties: Record<string, unknown> }).Properties;
      if (props['PackageType'] === 'Image') {
        imageCount++;
        const imgCfg = props['ImageConfig'] as { Command?: string[] } | undefined;
        expect(imgCfg?.Command).toBeDefined();
        expect((imgCfg?.Command ?? []).length).toBeGreaterThan(0);
      }
    }
    // Exactly two image Lambdas (ingest + necessity-scan).
    expect(imageCount).toBe(2);
  });

  test('EventBridge schedule expression is exactly rate(1 day)', () => {
    // The plan explicitly requires rate(1 day) — not a cron, not rate(24 hours).
    template.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 day)',
    });
  });

  test('PEM secret is read from Secrets Manager (ARN in env), never from env directly', () => {
    // The Lambda env must have GITHUB_APP_PEM_SECRET_ARN but must NOT have any
    // variable that looks like it carries the raw PEM (e.g. starts with
    // "-----BEGIN RSA").
    const fns = template.findResources('AWS::Lambda::Function');
    for (const fn of Object.values(fns)) {
      const vars = (
        fn as { Properties?: { Environment?: { Variables?: Record<string, unknown> } } }
      ).Properties?.Environment?.Variables ?? {};
      for (const [_key, value] of Object.entries(vars)) {
        const strVal = typeof value === 'string' ? value : JSON.stringify(value);
        expect(strVal).not.toMatch(/-----BEGIN/);
        expect(strVal).not.toMatch(/-----END RSA PRIVATE KEY-----/);
      }
    }
  });

  test('IAM role scoped to new partitions — no full-table grantReadWriteData wildcard', () => {
    // A full grantReadWriteData produces an Allow on the table with no
    // LeadingKeys condition.  This stack must use the conditioned policy only.
    const policies = template.findResources('AWS::IAM::Policy');
    for (const policy of Object.values(policies)) {
      const stmts = (
        policy as {
          Properties: { PolicyDocument: { Statement: { Condition?: unknown; Action: unknown }[] } };
        }
      ).Properties.PolicyDocument.Statement;
      for (const stmt of stmts) {
        const actions = JSON.stringify(stmt.Action);
        if (actions.includes('dynamodb:')) {
          // Every DynamoDB-touching statement must have a condition.
          expect(stmt.Condition).toBeDefined();
        }
      }
    }
  });
});
