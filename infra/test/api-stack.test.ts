import * as cdk from 'aws-cdk-lib/core';
import { Template, Match } from 'aws-cdk-lib/assertions';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { ApiStack } from '../lib/api-stack';

/**
 * U5 assertions: the synthesized API stack must expose the single-table with
 * GSI1 + Streams, and the WebSocket API must declare the three custom routes
 * (event/subscribe/control) the daemons and web client depend on.
 */

function synth(): Template {
  const app = new cdk.App();
  // A throwaway stack to host a user pool + client the ApiStack can reference,
  // mirroring how AuthStack supplies them in bin/infra.ts.
  const authStack = new cdk.Stack(app, 'TestAuthStack');
  const userPool = new cognito.UserPool(authStack, 'TestPool');
  const userPoolClient = userPool.addClient('TestClient');

  const stack = new ApiStack(app, 'TestApiStack', { userPool, userPoolClient });
  return Template.fromStack(stack);
}

describe('ApiStack', () => {
  const template = synth();

  test('provisions the harness DynamoDB table with GSI1 and Streams', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'harness',
      KeySchema: [
        { AttributeName: 'PK', KeyType: 'HASH' },
        { AttributeName: 'SK', KeyType: 'RANGE' },
      ],
      StreamSpecification: { StreamViewType: 'NEW_AND_OLD_IMAGES' },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({
          IndexName: 'GSI1',
          KeySchema: [
            { AttributeName: 'GSI1PK', KeyType: 'HASH' },
            { AttributeName: 'GSI1SK', KeyType: 'RANGE' },
          ],
        }),
      ]),
    });
  });

  test('enables DynamoDB TimeToLive on the `ttl` attribute (device-auth reaping)', () => {
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      TableName: 'harness',
      TimeToLiveSpecification: {
        AttributeName: 'ttl',
        Enabled: true,
      },
    });
  });

  test('routes the sessions control plane (POST /sessions/{id}/control)', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
      RouteKey: 'POST /sessions/{id}/control',
    });
  });

  test('routes the skills scope endpoint (POST /skills/{name}/scope)', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
      RouteKey: 'POST /skills/{name}/scope',
    });
  });

  test('routes the skill-edit verbs: promote + idea fold (U16)', () => {
    // Promote (repoint TRUE) and fold (snapshot a revision from an idea) are both
    // the skills Lambda and both skill-edit gated; they are `noAuth` at the gateway
    // so the claude+ device token reaches the handler (admin decided server-side).
    for (const RouteKey of [
      'POST /skills/{name}/promote',
      'POST /skills/{name}/ideas/{ideaId}/fold',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey,
        AuthorizationType: 'NONE',
      });
    }
  });

  test('opens catalog WRITE + opt-in routes to the device token (AuthorizationType NONE)', () => {
    // The claude+ device token is HS256; the gateway JWT authorizer would reject
    // it, so every catalog write (and the project opt-in) is PUBLIC at the gateway
    // and authenticated + admin-gated in-handler. This is what makes /hq-add-skill
    // (and hq-add-mcp / hq-update-agent) able to author directly.
    for (const routeKey of [
      'POST /skills',
      'PUT /skills/{name}',
      'DELETE /skills/{name}',
      'POST /skills/{name}/members',
      'POST /skills/{name}/dissolve',
      'POST /agents',
      'POST /mcp-servers',
      'POST /projects/{projectId}/skills/{skillName}',
      'POST /projects/{projectId}/bundles/{bundleName}',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
        AuthorizationType: 'NONE',
      });
    }
  });

  test('routes the MCP servers catalog (collection, by-name verbs, usage)', () => {
    // The MCP servers catalog mirrors skills MINUS the bundle/scope verbs: a
    // GET/POST collection, GET/PUT/DELETE by name, and a GET usage sub-route.
    for (const routeKey of [
      'GET /mcp-servers',
      'POST /mcp-servers',
      'GET /mcp-servers/{name}',
      'PUT /mcp-servers/{name}',
      'DELETE /mcp-servers/{name}',
      'GET /mcp-servers/{name}/usage',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
    // No bundle/dissolve/scope verbs leak in (MCP servers are flat, org-only).
    for (const routeKey of [
      'POST /mcp-servers/{name}/members',
      'POST /mcp-servers/{name}/dissolve',
      'POST /mcp-servers/{name}/scope',
    ]) {
      const routes = template.findResources('AWS::ApiGatewayV2::Route', {
        Properties: { RouteKey: routeKey },
      });
      expect(Object.keys(routes)).toHaveLength(0);
    }
  });

  test('throttles the HTTP API stage to bound public /device/* abuse', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Stage', {
      DefaultRouteSettings: Match.objectLike({
        ThrottlingRateLimit: Match.anyValue(),
        ThrottlingBurstLimit: Match.anyValue(),
      }),
    });
  });

  test('exposes the event/subscribe/control WebSocket routes', () => {
    for (const routeKey of ['event', 'subscribe', 'control']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
  });

  test('declares the WebSocket lifecycle routes with a $connect authorizer', () => {
    for (const routeKey of ['$connect', '$disconnect', '$default']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
    // $connect is guarded by a Lambda (REQUEST) authorizer.
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'REQUEST',
    });
  });

  test('fronts the REST handlers with an HTTP API behind a JWT authorizer', () => {
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      ProtocolType: 'HTTP',
    });
    template.hasResourceProperties('AWS::ApiGatewayV2::Authorizer', {
      AuthorizerType: 'JWT',
    });
  });

  test('routes the project-scoped requirements/refresh/docs handlers', () => {
    // These backend handlers (rest_projects) were implemented but unrouted; the
    // HTTP API must expose them as project-scoped routes.
    for (const routeKey of [
      'GET /projects/{id}/requirements',
      'PUT /projects/{id}/requirements',
      'POST /projects/{id}/refresh',
      'GET /projects/{id}/docs',
      'GET /projects/{id}/docs/content',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
  });

  test('routes the project opt-in handlers (skills/agents/mcp-servers/bundles) under {projectId}', () => {
    // These four opt-in routes are dispatched by the projects Lambda's internal
    // path router; on a deployed HTTP API an unregistered path 404s at the
    // gateway before reaching the Lambda, so each must be a dedicated route. The
    // first-segment param MUST be `{projectId}` (what the handler reads via
    // pathParam(event, 'projectId')), not `{id}`.
    for (const routeKey of [
      'POST /projects/{projectId}/skills/{skillName}',
      'DELETE /projects/{projectId}/skills/{skillName}',
      'POST /projects/{projectId}/agents/{agentName}',
      'DELETE /projects/{projectId}/agents/{agentName}',
      'POST /projects/{projectId}/mcp-servers/{name}',
      'DELETE /projects/{projectId}/mcp-servers/{name}',
      'POST /projects/{projectId}/bundles/{bundleName}',
      'DELETE /projects/{projectId}/bundles/{bundleName}',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
  });

  test('scopes the weekly routes under /projects/{pid} (rest_weekly)', () => {
    // The weekly handler requires a `pid` path param; the routes must be
    // project-scoped (not the bare /weekly registrations).
    for (const routeKey of [
      'GET /projects/{pid}/weekly',
      'PUT /projects/{pid}/weekly',
      'GET /projects/{pid}/weekly/{week}',
      'PUT /projects/{pid}/weekly/{week}',
      'POST /projects/{pid}/weekly/{week}/publish',
    ]) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }
    // The old bare /weekly routes must be gone.
    for (const routeKey of ['GET /weekly', 'POST /weekly/{week}/publish']) {
      const routes = template.findResources('AWS::ApiGatewayV2::Route', {
        Properties: { RouteKey: routeKey },
      });
      expect(Object.keys(routes)).toHaveLength(0);
    }
  });

  test('routes the device-auth handlers (start/poll PUBLIC, approve JWT)', () => {
    // claude+ device-code login: start/poll are public (the CLI has no token
    // yet); approve requires the signed-in browser's JWT.
    for (const routeKey of ['POST /device/start', 'POST /device/poll', 'POST /device/approve']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
      });
    }

    // start/poll OVERRIDE the default JWT authorizer to be PUBLIC (NONE).
    for (const routeKey of ['POST /device/start', 'POST /device/poll']) {
      template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
        RouteKey: routeKey,
        AuthorizationType: 'NONE',
      });
    }

    // approve inherits the JWT authorizer (carries an AuthorizerId, type JWT).
    template.hasResourceProperties('AWS::ApiGatewayV2::Route', {
      RouteKey: 'POST /device/approve',
      AuthorizationType: 'JWT',
      AuthorizerId: Match.anyValue(),
    });
  });

  test('pins HTTP API CORS to the CloudFront origin, never "*"', () => {
    // U20: CORS must be scoped to the SPA's CloudFront origin (+ local dev),
    // not the permissive wildcard.
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      ProtocolType: 'HTTP',
      CorsConfiguration: Match.objectLike({
        AllowOrigins: Match.arrayWith(['https://d13sqkbwzqe38l.cloudfront.net']),
      }),
    });
    // And the wildcard origin is absent.
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      ProtocolType: 'HTTP',
      CorsConfiguration: Match.objectLike({
        AllowOrigins: Match.not(Match.arrayWith(['*'])),
      }),
    });
  });

  test('provisions a Secrets Manager secret for the device-token signing key', () => {
    // Hardening: the HMAC signing secret is a managed Secrets Manager secret with
    // a generated value, never a literal in the template.
    template.hasResourceProperties('AWS::SecretsManager::Secret', {
      Name: 'command-hq/device-token-secret',
      GenerateSecretString: Match.objectLike({
        PasswordLength: Match.anyValue(),
      }),
    });
  });

  test('injects DEVICE_TOKEN_SECRET as a Secrets Manager resolve reference, never the placeholder', () => {
    // The synthesized template must carry a `{{resolve:secretsmanager:...}}`
    // dynamic reference (CloudFormation resolves it at deploy time), NOT the
    // 'placeholder-dev-secret' literal that previously leaked into prod.
    const fns = template.findResources('AWS::Lambda::Function');
    let sawDeviceSecret = false;
    for (const res of Object.values(fns)) {
      const vars = (
        res as { Properties?: { Environment?: { Variables?: Record<string, unknown> } } }
      ).Properties?.Environment?.Variables;
      if (vars && 'DEVICE_TOKEN_SECRET' in vars) {
        sawDeviceSecret = true;
        const value = vars.DEVICE_TOKEN_SECRET;
        expect(value).not.toBe('placeholder-dev-secret');
        // The dynamic reference synthesizes to a CloudFormation intrinsic (an
        // `Fn::Join` that builds the `{{resolve:secretsmanager:...}}` token and a
        // `Ref` to the managed secret), NOT a plaintext string.
        const serialized = JSON.stringify(value);
        expect(serialized).toContain('{{resolve:secretsmanager:');
        expect(serialized).toContain('DeviceTokenSecret');
      }
    }
    expect(sawDeviceSecret).toBe(true);
  });

  test('consumes the DynamoDB stream with a Lambda EventSourceMapping', () => {
    // The table's stream (NEW_AND_OLD_IMAGES) must drive a Lambda consumer — the
    // projection/roll-up backstop — via an event source mapping.
    template.hasResourceProperties('AWS::Lambda::EventSourceMapping', {
      StartingPosition: 'TRIM_HORIZON',
      EventSourceArn: Match.anyValue(),
    });
    // At least one mapping exists.
    const mappings = template.findResources('AWS::Lambda::EventSourceMapping');
    expect(Object.keys(mappings).length).toBeGreaterThan(0);
  });

  test('alarms on stream-consumer DLQ depth (U23 observability)', () => {
    // The DLQ exists (records that exhaust retries land here).
    template.hasResourceProperties('AWS::SQS::Queue', {
      QueueName: 'command-hq-stream-consumer-dlq',
    });
    // A CloudWatch alarm watches its depth: ANY visible message breaches, so a
    // skill/topic embed that gave up after retries is VISIBLE instead of failing
    // to empty-state. Missing data is the healthy (empty-queue) case, not a breach.
    template.hasResourceProperties('AWS::CloudWatch::Alarm', {
      AlarmName: 'command-hq-stream-consumer-dlq-depth',
      MetricName: 'ApproximateNumberOfMessagesVisible',
      Namespace: 'AWS/SQS',
      ComparisonOperator: 'GreaterThanThreshold',
      Threshold: 0,
      EvaluationPeriods: 1,
      TreatMissingData: 'notBreaching',
    });
  });
});
