import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpNoneAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2';
import {
  HttpLambdaIntegration,
  WebSocketLambdaIntegration,
} from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import {
  HttpJwtAuthorizer,
  WebSocketLambdaAuthorizer,
} from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { Construct } from 'constructs';

/**
 * U5 — API stack: the serverless backend.
 *
 *  - a DynamoDB single-table (`harness`) with overloaded PK/SK, GSI1 (live
 *    sessions, user→projects), and Streams (projections/roll-ups);
 *  - an HTTP API (REST) guarded by a Cognito JWT authorizer, with CORS for the
 *    SPA, routing to the real bundled `@harness/backend` handlers;
 *  - a WebSocket API (ingest + live + control) with `$connect`/`$disconnect`/
 *    `$default` + the custom `event`/`subscribe`/`control` routes and a device-
 *    token Lambda authorizer on `$connect`.
 *
 * Each Lambda's code is the esbuild bundle produced by
 * `infra/scripts/bundle-backend.mjs` (run before synth/deploy), referenced via
 * `lambda.Code.fromAsset(cdk.bundles/<key>)` with the CJS export `index.handler`.
 */

export interface ApiStackProps extends cdk.StackProps {
  readonly userPool: cognito.IUserPool;
  readonly userPoolClient: cognito.IUserPoolClient;
}

const BUNDLES = path.join(__dirname, '..', 'cdk.bundles');

// The SPA's CloudFront origin — must match the AuthStack OAuth callback domain
// (`d13sqkbwzqe38l.cloudfront.net`). Overridable via env for a custom domain.
// CORS is pinned to this origin (plus the local Vite dev origin) — never `*`.
const SITE_ORIGIN = process.env.SITE_ORIGIN ?? 'https://d13sqkbwzqe38l.cloudfront.net';
const ALLOWED_ORIGINS = [SITE_ORIGIN, 'http://localhost:5173'];

export class ApiStack extends cdk.Stack {
  readonly table: dynamodb.Table;
  readonly httpApi: apigwv2.HttpApi;
  readonly webSocketApi: apigwv2.WebSocketApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // ---- DynamoDB single-table ------------------------------------------------
    this.table = new dynamodb.Table(this, 'HarnessTable', {
      tableName: 'harness',
      partitionKey: { name: 'PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'SK', type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      stream: dynamodb.StreamViewType.NEW_AND_OLD_IMAGES,
      pointInTimeRecoverySpecification: { pointInTimeRecoveryEnabled: true },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.table.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Lambda scaffolding ---------------------------------------------------
    const commonEnv: Record<string, string> = {
      HARNESS_TABLE: this.table.tableName,
      USER_POOL_ID: props.userPool.userPoolId,
      USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
      // Sourced from the deploy env (CI wires it from the `DEVICE_TOKEN_SECRET`
      // GitHub secret). The literal fallback is a local-synth-only convenience
      // and must never reach a real deploy — CI always sets the env var.
      DEVICE_TOKEN_SECRET: process.env.DEVICE_TOKEN_SECRET ?? 'placeholder-dev-secret',
    };

    const makeFn = (id: string, bundleKey: string): lambda.Function =>
      new lambda.Function(this, id, {
        runtime: lambda.Runtime.NODEJS_20_X,
        architecture: lambda.Architecture.ARM_64,
        handler: 'index.handler',
        code: lambda.Code.fromAsset(path.join(BUNDLES, bundleKey)),
        timeout: cdk.Duration.seconds(15),
        memorySize: 256,
        environment: commonEnv,
      });

    const grantRead = (fn: lambda.Function) => this.table.grantReadData(fn);
    const grantReadWrite = (fn: lambda.Function) => this.table.grantReadWriteData(fn);

    // ---- HTTP API (REST) ------------------------------------------------------
    const projectsFn = makeFn('RestProjectsFn', 'rest_projects');
    const sessionsFn = makeFn('RestSessionsFn', 'rest_sessions');
    const agentsFn = makeFn('RestAgentsFn', 'rest_agents');
    const skillsFn = makeFn('RestSkillsFn', 'rest_skills');
    const objectivesFn = makeFn('RestObjectivesFn', 'rest_objectives');
    const weeklyFn = makeFn('RestWeeklyFn', 'rest_weekly');
    const deviceFn = makeFn('RestDeviceFn', 'rest_device');
    grantReadWrite(projectsFn);
    grantRead(sessionsFn);
    grantReadWrite(agentsFn);
    grantReadWrite(skillsFn);
    grantReadWrite(objectivesFn);
    grantReadWrite(weeklyFn);
    grantReadWrite(deviceFn);

    const region = cdk.Stack.of(this).region;
    const jwtIssuer = `https://cognito-idp.${region}.amazonaws.com/${props.userPool.userPoolId}`;
    const jwtAuthorizer = new HttpJwtAuthorizer('HqJwtAuthorizer', jwtIssuer, {
      authorizerName: 'command-hq-jwt',
      jwtAudience: [props.userPoolClient.userPoolClientId],
    });

    this.httpApi = new apigwv2.HttpApi(this, 'HqHttpApi', {
      apiName: 'command-hq-http',
      defaultAuthorizer: jwtAuthorizer,
      // The SPA is served from CloudFront (a different origin) and sends a bearer
      // token, so CORS must allow it. Preflight (OPTIONS) is handled by API
      // Gateway before the authorizer runs.
      corsPreflight: {
        allowOrigins: ALLOWED_ORIGINS,
        allowMethods: [
          apigwv2.CorsHttpMethod.GET,
          apigwv2.CorsHttpMethod.POST,
          apigwv2.CorsHttpMethod.PUT,
          apigwv2.CorsHttpMethod.DELETE,
          apigwv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ['authorization', 'content-type'],
        maxAge: cdk.Duration.hours(1),
      },
    });

    const M = apigwv2.HttpMethod;
    const r = (
      routePath: string,
      methods: apigwv2.HttpMethod[],
      fn: lambda.Function,
      integrationId: string,
      authorizer?: apigwv2.IHttpRouteAuthorizer,
    ) =>
      this.httpApi.addRoutes({
        path: routePath,
        methods,
        integration: new HttpLambdaIntegration(integrationId, fn),
        // When omitted, the HTTP API's defaultAuthorizer (JWT) applies. Pass an
        // HttpNoneAuthorizer to OVERRIDE the default and make a route PUBLIC.
        ...(authorizer ? { authorizer } : {}),
      });

    r('/projects', [M.GET, M.POST], projectsFn, 'Projects');
    r('/projects/{id}', [M.GET], projectsFn, 'ProjectById');
    r('/projects/{id}/requirements', [M.GET, M.PUT], projectsFn, 'ProjectRequirements');
    r('/projects/{id}/refresh', [M.POST], projectsFn, 'ProjectRefresh');
    r('/projects/{id}/docs', [M.GET], projectsFn, 'ProjectDocs');
    r('/projects/{id}/docs/content', [M.GET], projectsFn, 'ProjectDocContent');

    r('/sessions', [M.GET], sessionsFn, 'Sessions');
    r('/sessions/{id}', [M.GET], sessionsFn, 'SessionById');

    r('/agents', [M.GET, M.POST], agentsFn, 'Agents');
    r('/agents/{name}', [M.GET, M.PUT, M.DELETE], agentsFn, 'AgentByName');
    r('/agents/{name}/scope', [M.POST], agentsFn, 'AgentScope');

    r('/skills', [M.GET, M.POST], skillsFn, 'Skills');
    r('/skills/{name}', [M.GET, M.PUT, M.DELETE], skillsFn, 'SkillByName');
    r('/skills/{name}/members', [M.POST], skillsFn, 'SkillMembers');
    r('/skills/{name}/members/{member}', [M.DELETE], skillsFn, 'SkillMemberDelete');
    r('/skills/{name}/dissolve', [M.POST], skillsFn, 'SkillDissolve');
    r('/skills/{name}/usage', [M.GET], skillsFn, 'SkillUsage');

    r('/objectives', [M.GET, M.POST, M.PUT], objectivesFn, 'Objectives');
    r('/objectives/{id}', [M.GET, M.DELETE], objectivesFn, 'ObjectiveById');

    r('/projects/{pid}/weekly', [M.GET, M.PUT], weeklyFn, 'Weekly');
    r('/projects/{pid}/weekly/{week}', [M.GET, M.PUT], weeklyFn, 'WeeklyByWeek');
    r('/projects/{pid}/weekly/{week}/publish', [M.POST], weeklyFn, 'WeeklyPublish');

    // ---- Device-auth (claude+ device-code login) ------------------------------
    // start/poll are PUBLIC: the CLI hits them before it has any token. They must
    // OVERRIDE the HTTP API's defaultAuthorizer (JWT) via HttpNoneAuthorizer.
    // approve REQUIRES the JWT (a signed-in browser approves the device) — it
    // inherits the default authorizer (no override).
    r('/device/start', [M.POST], deviceFn, 'DeviceStart', new HttpNoneAuthorizer());
    r('/device/poll', [M.POST], deviceFn, 'DevicePoll', new HttpNoneAuthorizer());
    r('/device/approve', [M.POST], deviceFn, 'DeviceApprove');

    // ---- WebSocket API (ingest + live + control) ------------------------------
    const wsAuthorizerFn = makeFn('WsAuthorizerFn', 'ws_authorizer');
    grantRead(wsAuthorizerFn);

    const wsConnectFn = makeFn('WsConnectFn', 'ws_connect');
    const wsDisconnectFn = makeFn('WsDisconnectFn', 'ws_disconnect');
    const wsDefaultFn = makeFn('WsDefaultFn', 'ws_default');
    const wsEventFn = makeFn('WsEventFn', 'ws_event');
    const wsSubscribeFn = makeFn('WsSubscribeFn', 'ws_subscribe');
    const wsControlFn = makeFn('WsControlFn', 'ws_control');
    grantReadWrite(wsConnectFn);
    grantReadWrite(wsDisconnectFn);
    grantReadWrite(wsEventFn);
    grantReadWrite(wsSubscribeFn);
    grantReadWrite(wsControlFn);

    const wsAuthorizer = new WebSocketLambdaAuthorizer('WsConnectAuthorizer', wsAuthorizerFn, {
      authorizerName: 'command-hq-ws',
      identitySource: ['route.request.querystring.token'],
    });

    this.webSocketApi = new apigwv2.WebSocketApi(this, 'HqWebSocketApi', {
      apiName: 'command-hq-ws',
      connectRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsConnectIntegration', wsConnectFn),
        authorizer: wsAuthorizer,
      },
      disconnectRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsDisconnectIntegration', wsDisconnectFn),
      },
      defaultRouteOptions: {
        integration: new WebSocketLambdaIntegration('WsDefaultIntegration', wsDefaultFn),
      },
    });
    this.webSocketApi.addRoute('event', {
      integration: new WebSocketLambdaIntegration('WsEventIntegration', wsEventFn),
    });
    this.webSocketApi.addRoute('subscribe', {
      integration: new WebSocketLambdaIntegration('WsSubscribeIntegration', wsSubscribeFn),
    });
    this.webSocketApi.addRoute('control', {
      integration: new WebSocketLambdaIntegration('WsControlIntegration', wsControlFn),
    });

    const wsStage = new apigwv2.WebSocketStage(this, 'HqWebSocketStage', {
      webSocketApi: this.webSocketApi,
      stageName: 'prod',
      autoDeploy: true,
    });

    for (const fn of [wsEventFn, wsSubscribeFn, wsControlFn, wsDisconnectFn]) {
      this.webSocketApi.grantManageConnections(fn);
      fn.addEnvironment('WS_CALLBACK_URL', wsStage.callbackUrl);
    }

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'HttpApiUrl', { value: this.httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'WebSocketUrl', { value: wsStage.url });
  }
}
