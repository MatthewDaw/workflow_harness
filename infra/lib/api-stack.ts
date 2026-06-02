import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { WebSocketLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import { HttpJwtAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { WebSocketLambdaAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { Construct } from 'constructs';

/**
 * U5 — API stack: the serverless backend the Lambda handlers attach to.
 *
 * Provisions:
 *  - a DynamoDB single-table (`harness`) with overloaded PK/SK, a GSI1 for the
 *    cross-cutting queries (live sessions, user→projects), and Streams enabled
 *    so projections/roll-ups (U6/U10) can react to writes;
 *  - an HTTP API (apigatewayv2) fronting the REST handlers, guarded by a Cognito
 *    JWT authorizer bound to the AuthStack user pool;
 *  - a WebSocket API carrying event ingestion + the live/control path, with the
 *    standard `$connect`/`$disconnect`/`$default` routes plus the custom
 *    `event`/`subscribe`/`control` routes, and a Lambda authorizer on `$connect`
 *    that runs the device/JWT verifier (packages/backend/src/auth);
 *  - the Lambda functions wired to those routes with least-privilege IAM to the
 *    table.
 *
 * The handler source lands in later units (U6/U7/U8…). To keep `cdk synth`
 * self-contained here — no bundler, no Docker, no dependency on files that do
 * not yet exist — each function is provisioned with an inline placeholder whose
 * `handler` points at the eventual backend module path. Swapping the inline
 * `Code` for the bundled backend asset in U29 is a one-line change per function;
 * the routes, authorizers, IAM, and env wiring are final.
 */

export interface ApiStackProps extends cdk.StackProps {
  /** Cognito user pool the HTTP API's JWT authorizer trusts (from AuthStack). */
  readonly userPool: cognito.IUserPool;
  /** App client id the JWT authorizer accepts as an audience (from AuthStack). */
  readonly userPoolClient: cognito.IUserPoolClient;
}

export class ApiStack extends cdk.Stack {
  readonly table: dynamodb.Table;
  readonly httpApi: apigwv2.HttpApi;
  readonly webSocketApi: apigwv2.WebSocketApi;

  constructor(scope: Construct, id: string, props: ApiStackProps) {
    super(scope, id, props);

    // ---- DynamoDB single-table ------------------------------------------------
    // PK/SK overloaded across every entity (see packages/backend/src/db/keys.ts).
    // GSI1 carries the live-session list and the user→projects query. Streams
    // feed the projection/roll-up Lambdas added in later units.
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
    // Common runtime + env shared by every handler. The handler string is the
    // real backend module path so wiring matches once bundling is enabled (U29).
    const commonEnv: Record<string, string> = {
      HARNESS_TABLE: this.table.tableName,
      USER_POOL_ID: props.userPool.userPoolId,
      USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
      // Resolved from Secrets Manager / SSM in deploy units; present so the
      // device-token verifier has a binding at runtime.
      DEVICE_TOKEN_SECRET: process.env.DEVICE_TOKEN_SECRET ?? 'placeholder-dev-secret',
    };

    // Placeholder code keeps synth bundler-free; replaced by the bundled backend
    // asset in U29. The `handler` already names the eventual module export.
    const placeholder = lambda.Code.fromInline(
      'exports.handler = async () => ({ statusCode: 501, body: "not implemented" });',
    );

    const makeFn = (id: string, handler: string): lambda.Function => {
      const fn = new lambda.Function(this, id, {
        runtime: lambda.Runtime.NODEJS_20_X,
        architecture: lambda.Architecture.ARM_64,
        handler,
        code: placeholder,
        timeout: cdk.Duration.seconds(15),
        memorySize: 256,
        environment: commonEnv,
      });
      return fn;
    };

    // Read-only vs read-write split keeps IAM least-privilege per handler.
    const grantRead = (fn: lambda.Function) => this.table.grantReadData(fn);
    const grantReadWrite = (fn: lambda.Function) => this.table.grantReadWriteData(fn);

    // ---- HTTP API (REST) ------------------------------------------------------
    // Handlers read/write the table scoped to the caller's uid. A single
    // Cognito JWT authorizer guards the API; the issuer is the user pool.
    const restProjectsFn = makeFn('RestProjectsFn', 'rest/projects.handler');
    const restSessionsFn = makeFn('RestSessionsFn', 'rest/sessions.handler');
    const restAgentsFn = makeFn('RestAgentsFn', 'rest/agents.handler');
    const restObjectivesFn = makeFn('RestObjectivesFn', 'rest/objectives.handler');
    grantReadWrite(restProjectsFn);
    grantRead(restSessionsFn);
    grantReadWrite(restAgentsFn);
    grantReadWrite(restObjectivesFn);

    const region = cdk.Stack.of(this).region;
    const jwtIssuer = `https://cognito-idp.${region}.amazonaws.com/${props.userPool.userPoolId}`;
    const jwtAuthorizer = new HttpJwtAuthorizer('HqJwtAuthorizer', jwtIssuer, {
      authorizerName: 'command-hq-jwt',
      jwtAudience: [props.userPoolClient.userPoolClientId],
    });

    this.httpApi = new apigwv2.HttpApi(this, 'HqHttpApi', {
      apiName: 'command-hq-http',
      defaultAuthorizer: jwtAuthorizer,
    });

    const route = (
      path: string,
      methods: apigwv2.HttpMethod[],
      fn: lambda.Function,
      integrationId: string,
    ) =>
      this.httpApi.addRoutes({
        path,
        methods,
        integration: new HttpLambdaIntegration(integrationId, fn),
      });

    route(
      '/projects',
      [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      restProjectsFn,
      'ProjectsIntegration',
    );
    route('/projects/{id}', [apigwv2.HttpMethod.GET], restProjectsFn, 'ProjectByIdIntegration');
    route('/sessions', [apigwv2.HttpMethod.GET], restSessionsFn, 'SessionsIntegration');
    route('/sessions/{id}', [apigwv2.HttpMethod.GET], restSessionsFn, 'SessionByIdIntegration');
    route(
      '/agents',
      [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      restAgentsFn,
      'AgentsIntegration',
    );
    route(
      '/skills',
      [apigwv2.HttpMethod.GET, apigwv2.HttpMethod.POST],
      restAgentsFn,
      'SkillsIntegration',
    );
    route('/objectives', [apigwv2.HttpMethod.GET], restObjectivesFn, 'ObjectivesIntegration');

    // ---- WebSocket API (ingest + live + control) ------------------------------
    // $connect runs a Lambda authorizer (device token for daemons, Cognito JWT
    // for web). Ingestion (`event`), fan-out subscription (`subscribe`), and the
    // steer path (`control`) are the three custom routes the daemons/web use.
    const wsAuthorizerFn = makeFn('WsAuthorizerFn', 'ws/authorizer.handler');
    grantRead(wsAuthorizerFn);

    const wsConnectFn = makeFn('WsConnectFn', 'ws/connect.handler');
    const wsDisconnectFn = makeFn('WsDisconnectFn', 'ws/disconnect.handler');
    const wsDefaultFn = makeFn('WsDefaultFn', 'ws/default.handler');
    const wsEventFn = makeFn('WsEventFn', 'ws/event.handler');
    const wsSubscribeFn = makeFn('WsSubscribeFn', 'ws/subscribe.handler');
    const wsControlFn = makeFn('WsControlFn', 'ws/control.handler');
    grantReadWrite(wsConnectFn);
    grantReadWrite(wsDisconnectFn);
    grantReadWrite(wsEventFn);
    grantReadWrite(wsSubscribeFn);
    grantReadWrite(wsControlFn);

    const wsAuthorizer = new WebSocketLambdaAuthorizer('WsConnectAuthorizer', wsAuthorizerFn, {
      authorizerName: 'command-hq-ws',
      // Daemons present the device token on the connect query string (no headers
      // available in browsers' WS handshake either); web passes its JWT the same way.
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

    // The handlers that push frames back out (fan-out + control routing) need
    // execute-api:ManageConnections on this API's connections.
    for (const fn of [wsEventFn, wsSubscribeFn, wsControlFn, wsDisconnectFn]) {
      this.webSocketApi.grantManageConnections(fn);
      // Expose the callback endpoint so handlers can construct the management client.
      fn.addEnvironment('WS_CALLBACK_URL', wsStage.callbackUrl);
    }

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'HttpApiUrl', { value: this.httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'WebSocketUrl', { value: wsStage.url });
  }
}
