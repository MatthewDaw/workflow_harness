import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import { DynamoEventSource, SqsDlq } from 'aws-cdk-lib/aws-lambda-event-sources';
import { StartingPosition, FilterCriteria, FilterRule } from 'aws-cdk-lib/aws-lambda';
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
      // Device-auth items (DEVAUTH/DEVUC) carry an epoch-SECONDS `ttl` attribute;
      // DynamoDB TimeToLive reaps them automatically so abandoned device-code
      // flows cannot accumulate unbounded.
      timeToLiveAttribute: 'ttl',
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    this.table.addGlobalSecondaryIndex({
      indexName: 'GSI1',
      partitionKey: { name: 'GSI1PK', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'GSI1SK', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });

    // ---- Device-token signing secret (AWS Secrets Manager) --------------------
    // The device-token HMAC secret must NEVER be a literal in the synthesized
    // template (it previously fell back to 'placeholder-dev-secret'). Instead it
    // lives in Secrets Manager and is injected into the lambdas as a CDK dynamic
    // reference — the template carries a `{{resolve:secretsmanager:...}}` token,
    // and CloudFormation resolves the real value at deploy time.
    //
    // An existing secret can be adopted (e.g. one rotated out of band) via the
    // DEVICE_TOKEN_SECRET_ARN env var / `deviceTokenSecretArn` context; otherwise
    // a managed secret with a freshly generated 64-char value is created here.
    const existingSecretArn =
      process.env.DEVICE_TOKEN_SECRET_ARN ??
      (this.node.tryGetContext('deviceTokenSecretArn') as string | undefined);

    const deviceTokenSecret: secretsmanager.ISecret = existingSecretArn
      ? secretsmanager.Secret.fromSecretCompleteArn(this, 'DeviceTokenSecret', existingSecretArn)
      : new secretsmanager.Secret(this, 'DeviceTokenSecret', {
          secretName: 'command-hq/device-token-secret',
          description: 'HMAC signing secret for claude+ device tokens (command-hq).',
          generateSecretString: {
            // A long, opaque signing key. No spaces/quotes/backslashes so it is
            // safe to carry verbatim through env + CloudFormation resolution.
            passwordLength: 64,
            excludePunctuation: true,
            excludeCharacters: ' "\'\\',
            requireEachIncludedType: false,
          },
          removalPolicy: cdk.RemovalPolicy.RETAIN,
        });

    // ---- Lambda scaffolding ---------------------------------------------------
    const commonEnv: Record<string, string> = {
      HARNESS_TABLE: this.table.tableName,
      USER_POOL_ID: props.userPool.userPoolId,
      USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
      // A CDK dynamic reference to the Secrets Manager value: `unsafeUnwrap()`
      // yields the `{{resolve:secretsmanager:...}}` token (NOT the plaintext), so
      // the literal placeholder can never reach a real deploy. CloudFormation
      // substitutes the live secret value when the lambda is created/updated.
      DEVICE_TOKEN_SECRET: deviceTokenSecret.secretValue.unsafeUnwrap(),
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
    // The bundle key mirrors the esbuild entry name (`rest/mcpServers` →
    // `rest_mcpServers`, see infra/scripts/bundle-backend.mjs) — the convention is
    // filename-derived (`/`→`_`), exactly like `rest_skills` ↔ rest/skills.ts.
    const mcpServersFn = makeFn('RestMcpServersFn', 'rest_mcpServers');
    const objectivesFn = makeFn('RestObjectivesFn', 'rest_objectives');
    const weeklyFn = makeFn('RestWeeklyFn', 'rest_weekly');
    const memoriesFn = makeFn('RestMemoriesFn', 'rest_memories');
    const deviceFn = makeFn('RestDeviceFn', 'rest_device');
    const dodFn = makeFn('RestDodFn', 'rest_dod');
    // Membership / org onboarding (GET /me, POST /orgs, POST /orgs/join). Reads
    // + writes the PROFILE + ORG records, so it needs read-write.
    const orgsFn = makeFn('RestOrgsFn', 'rest_orgs');
    grantReadWrite(projectsFn);
    // Sessions handler writes too: shutdown/kill marks a session done in the
    // projection (authoritative terminate, incl. ghost sessions with no daemon).
    grantReadWrite(sessionsFn);
    grantReadWrite(agentsFn);
    grantReadWrite(skillsFn);
    grantReadWrite(mcpServersFn);
    grantReadWrite(objectivesFn);
    grantReadWrite(weeklyFn);
    grantReadWrite(memoriesFn);
    grantReadWrite(deviceFn);
    grantReadWrite(dodFn);
    grantReadWrite(orgsFn);

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

    // Rate-limit the API stage to bound abuse of the public, unauthenticated
    // /device/start + /device/poll routes (which write to DynamoDB) — without a
    // ceiling those endpoints allow unbounded writes. The HttpApi auto-creates
    // its `$default` stage, so we reach through to its L1 CfnStage and set the
    // stage-wide DefaultRouteSettings throttle.
    const defaultStageNode = this.httpApi.defaultStage?.node.defaultChild as
      | apigwv2.CfnStage
      | undefined;
    if (defaultStageNode) {
      defaultStageNode.defaultRouteSettings = {
        throttlingRateLimit: 20,
        throttlingBurstLimit: 40,
      };
    }

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
    r('/projects/{id}', [M.GET, M.DELETE], projectsFn, 'ProjectById');
    r('/projects/{id}/requirements', [M.GET, M.PUT], projectsFn, 'ProjectRequirements');
    r('/projects/{id}/refresh', [M.POST], projectsFn, 'ProjectRefresh');
    r('/projects/{id}/docs', [M.GET], projectsFn, 'ProjectDocs');
    r('/projects/{id}/docs/content', [M.GET], projectsFn, 'ProjectDocContent');
    // Mined learnings (topic-focus logging) live in the project's LEARN#
    // partition; projectsFn already has read on the harness table (grantReadWrite
    // above), so no extra grant is needed.
    r('/projects/{id}/learnings', [M.GET], projectsFn, 'ProjectLearnings');

    // Project opt-in for the org catalog (skills/agents/mcp-servers/bundles).
    // These are dispatched by the projects Lambda's internal path-based router
    // (rest/projects.ts handler) — NOT by the skills/agents/mcp-servers Lambdas —
    // so they all point at projectsFn. On a deployed HTTP API an unregistered
    // path 404s at the gateway BEFORE reaching the Lambda, so each must be a
    // dedicated route here even though one Lambda serves them all.
    //
    // CRITICAL: the opt-in handler reads the project id via
    // `pathParam(event, 'projectId')` (NOT 'id', unlike the /projects/{id}
    // routes above), so the first segment param MUST be `{projectId}` — the
    // gateway route param name has to match what the handler reads exactly.
    r('/projects/{projectId}/skills/{skillName}', [M.POST, M.DELETE], projectsFn, 'ProjectSkillOptIn');
    r('/projects/{projectId}/agents/{agentName}', [M.POST, M.DELETE], projectsFn, 'ProjectAgentOptIn');
    r('/projects/{projectId}/mcp-servers/{name}', [M.POST, M.DELETE], projectsFn, 'ProjectMcpOptIn');
    r('/projects/{projectId}/bundles/{bundleName}', [M.POST, M.DELETE], projectsFn, 'ProjectBundleOptIn');

    r('/sessions', [M.GET], sessionsFn, 'Sessions');
    r('/sessions/{id}', [M.GET], sessionsFn, 'SessionById');
    // Control plane: the REST handler authorizes the caller owns the session,
    // then postToConnection's the ControlAction frame to the owning daemon over
    // the WS management API (see the WS grant + WS_CALLBACK_URL wiring below).
    r('/sessions/{id}/control', [M.POST], sessionsFn, 'SessionControl');

    r('/agents', [M.GET, M.POST], agentsFn, 'Agents');
    r('/agents/{name}', [M.GET, M.PUT, M.DELETE], agentsFn, 'AgentByName');
    r('/agents/{name}/scope', [M.POST], agentsFn, 'AgentScope');

    r('/skills', [M.GET, M.POST], skillsFn, 'Skills');
    r('/skills/{name}', [M.GET, M.PUT, M.DELETE], skillsFn, 'SkillByName');
    r('/skills/{name}/members', [M.POST], skillsFn, 'SkillMembers');
    r('/skills/{name}/members/{member}', [M.DELETE], skillsFn, 'SkillMemberDelete');
    r('/skills/{name}/dissolve', [M.POST], skillsFn, 'SkillDissolve');
    r('/skills/{name}/usage', [M.GET], skillsFn, 'SkillUsage');
    r('/skills/{name}/scope', [M.POST], skillsFn, 'SkillScope');

    // MCP servers mirror the skills catalog routes MINUS the bundle verbs
    // (members/dissolve) and the retired-by-design scope verb — the catalog is a
    // flat, org-only set (no bundles, no per-server tiering). NOTE: the project
    // opt-in route (/projects/{projectId}/mcp-servers/{name}) is NOT served by
    // this mcpServersFn — it is dispatched by the projects Lambda's internal path
    // router and is registered up with the other /projects routes above (against
    // projectsFn, using the {projectId} first-segment param the opt-in handler
    // reads). The routes below are the org-catalog CRUD only.
    r('/mcp-servers', [M.GET, M.POST], mcpServersFn, 'McpServers');
    r('/mcp-servers/{name}', [M.GET, M.PUT, M.DELETE], mcpServersFn, 'McpServerByName');
    r('/mcp-servers/{name}/usage', [M.GET], mcpServersFn, 'McpServerUsage');

    r('/objectives', [M.GET, M.POST, M.PUT], objectivesFn, 'Objectives');
    r('/dod', [M.GET, M.PUT], dodFn, 'Dod');

    // Membership / org onboarding. All inherit the default Cognito JWT authorizer
    // — a brand-new user still has a valid token (their PROFILE just has no org
    // yet), so these are authenticated but org-membership-agnostic. POST /me/org
    // switches the active org among the ones the caller has already joined.
    r('/me', [M.GET], orgsFn, 'Me');
    r('/me/org', [M.POST], orgsFn, 'MeOrgSwitch');
    r('/orgs', [M.POST], orgsFn, 'Orgs');
    r('/orgs/join', [M.POST], orgsFn, 'OrgsJoin');
    r('/objectives/{id}', [M.GET, M.DELETE], objectivesFn, 'ObjectiveById');

    // Weekly routes accept EITHER a Cognito ID token (web) OR the claude+ wrapper
    // device token. The default gateway authorizer only accepts Cognito JWTs and
    // would 403 the device token before the handler runs, so we OVERRIDE it with
    // HttpNoneAuthorizer and let the weekly handler verify the bearer token itself
    // (rest/bearerAuth.ts: device token OR Cognito), still enforcing project
    // ownership. This is what lets `/hq-weekly-update` publish from the PTY.
    r('/projects/{pid}/weekly', [M.GET, M.PUT], weeklyFn, 'Weekly', new HttpNoneAuthorizer());
    r(
      '/projects/{pid}/weekly/{week}',
      [M.GET, M.PUT],
      weeklyFn,
      'WeeklyByWeek',
      new HttpNoneAuthorizer(),
    );
    r(
      '/projects/{pid}/weekly/{week}/publish',
      [M.POST],
      weeklyFn,
      'WeeklyPublish',
      new HttpNoneAuthorizer(),
    );

    // Memories route mirrors weekly: HttpNoneAuthorizer so the claude+ daemon's
    // device token reaches the handler (the default JWT authorizer would 403 it
    // before it runs). The handler verifies the bearer token itself (device OR
    // Cognito) and scopes the reconcile to the caller's own author key, so a
    // collaborator's daemon can sync memories to a project they do not own.
    r('/projects/{pid}/memories', [M.GET, M.PUT], memoriesFn, 'ProjectMemories', new HttpNoneAuthorizer());

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

    // The REST sessions handler also relays ControlAction frames to the owning
    // daemon's WS connection (POST /sessions/{id}/control), so it needs the same
    // manage-connections grant + management endpoint as the WS handlers.
    this.webSocketApi.grantManageConnections(sessionsFn);
    sessionsFn.addEnvironment('WS_CALLBACK_URL', wsStage.callbackUrl);

    // ---- DynamoDB Streams consumer (projection / roll-up backstop) ------------
    // The table's NEW_AND_OLD_IMAGES stream was enabled but unconsumed. This
    // lambda drives the advertised "Streams drive projections/roll-ups": it
    // re-folds event records into the session projection (idempotent backstop for
    // the inline ingestion fold) and recomputes objective roll-ups when a
    // project's stored progress changes. It reads + writes the table.
    const streamConsumerFn = makeFn('StreamConsumerFn', 'ws_streamConsumer');
    grantReadWrite(streamConsumerFn);

    // A DLQ captures records that exhaust retries so a poison batch cannot block
    // the shard indefinitely; bisectBatchOnError isolates the offending record.
    const streamDlq = new sqs.Queue(this, 'StreamConsumerDlq', {
      queueName: 'command-hq-stream-consumer-dlq',
      retentionPeriod: cdk.Duration.days(14),
    });

    streamConsumerFn.addEventSource(
      new DynamoEventSource(this.table, {
        startingPosition: StartingPosition.TRIM_HORIZON,
        batchSize: 25,
        maxBatchingWindow: cdk.Duration.seconds(2),
        bisectBatchOnError: true,
        retryAttempts: 3,
        reportBatchItemFailures: true,
        onFailure: new SqsDlq(streamDlq),
        // Only INSERT/MODIFY carry a recompute trigger; REMOVE (TTL reaps, etc.)
        // is pure noise to the projection/roll-up driver, so filter it out at the
        // source to avoid waking the lambda for nothing.
        filters: [
          FilterCriteria.filter({ eventName: FilterRule.isEqual('INSERT') }),
          FilterCriteria.filter({ eventName: FilterRule.isEqual('MODIFY') }),
        ],
      }),
    );

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'HttpApiUrl', { value: this.httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'WebSocketUrl', { value: wsStage.url });
  }
}
