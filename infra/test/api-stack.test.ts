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

  test('sources DEVICE_TOKEN_SECRET from env, not the literal placeholder', () => {
    // U20: the real signing secret is wired from the deploy env (CI secret).
    const prev = process.env.DEVICE_TOKEN_SECRET;
    process.env.DEVICE_TOKEN_SECRET = 'from-env-secret';
    try {
      const t = synth();
      t.hasResourceProperties('AWS::Lambda::Function', {
        Environment: Match.objectLike({
          Variables: Match.objectLike({ DEVICE_TOKEN_SECRET: 'from-env-secret' }),
        }),
      });
      // The placeholder fallback must not leak into the synthesized template.
      const fns = t.findResources('AWS::Lambda::Function');
      for (const res of Object.values(fns)) {
        const vars = (res as { Properties?: { Environment?: { Variables?: Record<string, unknown> } } })
          .Properties?.Environment?.Variables;
        if (vars && 'DEVICE_TOKEN_SECRET' in vars) {
          expect(vars.DEVICE_TOKEN_SECRET).not.toBe('placeholder-dev-secret');
        }
      }
    } finally {
      if (prev === undefined) delete process.env.DEVICE_TOKEN_SECRET;
      else process.env.DEVICE_TOKEN_SECRET = prev;
    }
  });
});
