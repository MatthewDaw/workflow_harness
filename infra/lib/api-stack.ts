import * as path from 'path';
import * as cdk from 'aws-cdk-lib/core';
import * as dynamodb from 'aws-cdk-lib/aws-dynamodb';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as apigwv2 from 'aws-cdk-lib/aws-apigatewayv2';
import * as secretsmanager from 'aws-cdk-lib/aws-secretsmanager';
import * as sqs from 'aws-cdk-lib/aws-sqs';
import * as cloudwatch from 'aws-cdk-lib/aws-cloudwatch';
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

    // OpenRouter API key for the skill-idea loop's model calls (chat + embeddings).
    // It is a user-supplied key (not generated), created/populated out-of-band in
    // Secrets Manager; CDK references it by name and injects it into the Lambda env
    // as OPENROUTER_API_KEY via the same dynamic-reference path as the device token.
    const openRouterSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'OpenRouterApiKey',
      'command-hq/openrouter-api-key',
    );

    // The Neon serverless-Postgres connection URL for the strategic-execution
    // domain (objectives + weekly — KTD7/U16). Neon is an external managed
    // Postgres reached over its HTTP driver, so there is NO Aurora cluster, VPC,
    // or security group here — just the connection string, injected as
    // DATABASE_URL via the same dynamic-reference path as the device-token secret.
    // Created/populated out-of-band in Secrets Manager; CDK references it by name.
    const neonDbSecret = secretsmanager.Secret.fromSecretNameV2(
      this,
      'NeonDatabaseUrl',
      'command-hq/neon-database-url',
    );

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
      // The skill-idea loop's model calls (OpenRouter chat + embeddings) read this.
      OPENROUTER_API_KEY: openRouterSecret.secretValue.unsafeUnwrap(),
      // The strategic-execution Postgres (Neon) connection URL — objectives +
      // weekly handlers and the stream consumer read this via `db/pg/client.ts`.
      DATABASE_URL: neonDbSecret.secretValue.unsafeUnwrap(),
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
    const workflowsFn = makeFn('RestWorkflowsFn', 'rest_workflows');
    const skillsFn = makeFn('RestSkillsFn', 'rest_skills');
    // Skill ideas — the candidate-learnings surfacing read path (skill-idea loop, U11).
    const ideasFn = makeFn('RestIdeasFn', 'rest_ideas');
    // The bundle key mirrors the esbuild entry name (`rest/mcpServers` →
    // `rest_mcpServers`, see infra/scripts/bundle-backend.mjs) — the convention is
    // filename-derived (`/`→`_`), exactly like `rest_skills` ↔ rest/skills.ts.
    const mcpServersFn = makeFn('RestMcpServersFn', 'rest_mcpServers');
    const objectivesFn = makeFn('RestObjectivesFn', 'rest_objectives');
    const weeklyFn = makeFn('RestWeeklyFn', 'rest_weekly');
    // The lifecycle transitions (lock / reconcile-start / reconcile-complete) are a
    // separate handler (U4) — reconcile-complete is transactional (KTD3).
    const weeklyTransitionsFn = makeFn('RestWeeklyTransitionsFn', 'rest_weeklyTransitions');
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
    grantReadWrite(workflowsFn);
    grantReadWrite(skillsFn);
    grantReadWrite(ideasFn);
    grantReadWrite(mcpServersFn);
    grantReadWrite(objectivesFn);
    grantReadWrite(weeklyFn);
    grantReadWrite(weeklyTransitionsFn);
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
    // THE noAuth CONTRACT (stated once; routes below just reference it): passing
    // this shared HttpNoneAuthorizer OVERRIDES the HTTP API's default Cognito JWT
    // authorizer, making the route PUBLIC at the gateway. That is required
    // wherever the claude+ wrapper's HS256 device token must reach the Lambda —
    // the gateway JWT authorizer only accepts Cognito tokens and would 403 the
    // device token before the handler runs. Every noAuth handler authenticates
    // IN-HANDLER (Cognito JWT OR device token — resolvePrincipal /
    // resolveOrgCatalogAuth / bearerAuth) and enforces its ownership/admin gate
    // server-side, so opening the gateway does not weaken authorization. Routes
    // without noAuth inherit the default JWT authorizer and stay Cognito-gated.
    const noAuth = new HttpNoneAuthorizer();
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
        ...(authorizer ? { authorizer } : {}),
      });

    r('/projects', [M.GET, M.POST], projectsFn, 'Projects');
    // GET is noAuth (device token; see contract above); DELETE stays Cognito-gated.
    r('/projects/{id}', [M.GET], projectsFn, 'ProjectByIdGet', noAuth);
    r('/projects/{id}', [M.DELETE], projectsFn, 'ProjectByIdDelete');
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
    // noAuth (see contract above); the handler enforces admin-or-owner.
    r('/projects/{projectId}/skills/{skillName}', [M.POST, M.DELETE], projectsFn, 'ProjectSkillOptIn', noAuth);
    r('/projects/{projectId}/agents/{agentName}', [M.POST, M.DELETE], projectsFn, 'ProjectAgentOptIn', noAuth);
    r('/projects/{projectId}/workflows/{workflowName}', [M.POST, M.DELETE], projectsFn, 'ProjectWorkflowOptIn', noAuth);
    r('/projects/{projectId}/mcp-servers/{name}', [M.POST, M.DELETE], projectsFn, 'ProjectMcpOptIn', noAuth);
    r('/projects/{projectId}/bundles/{bundleName}', [M.POST, M.DELETE], projectsFn, 'ProjectBundleOptIn', noAuth);
    r('/projects/{projectId}/agent-bundles/{bundleName}', [M.POST, M.DELETE], projectsFn, 'ProjectAgentBundleOptIn', noAuth);

    r('/sessions', [M.GET], sessionsFn, 'Sessions');
    r('/sessions/{id}', [M.GET], sessionsFn, 'SessionById');
    // Control plane: the REST handler authorizes the caller owns the session,
    // then postToConnection's the ControlAction frame to the owning daemon over
    // the WS management API (see the WS grant + WS_CALLBACK_URL wiring below).
    r('/sessions/{id}/control', [M.POST], sessionsFn, 'SessionControl');

    // All agents routes are noAuth (see contract above); admin-gated server-side.
    r('/agents', [M.GET], agentsFn, 'AgentsGet', noAuth);
    r('/agents', [M.POST], agentsFn, 'AgentsPost', noAuth);
    r('/agents/{name}', [M.GET, M.PUT, M.DELETE], agentsFn, 'AgentByName', noAuth);
    // Agent-bundle catalog verbs mirror the skills bundle verbs below.
    r('/agents/{name}/members', [M.POST], agentsFn, 'AgentMembers', noAuth);
    r('/agents/{name}/members/{member}', [M.DELETE], agentsFn, 'AgentMemberDelete', noAuth);
    r('/agents/{name}/dissolve', [M.POST], agentsFn, 'AgentDissolve', noAuth);

    // Workflows mirror the agents catalog routes MINUS the bundle verbs
    // (members/dissolve) and the scope verb — a workflow is itself the
    // composition unit (no bundling in v1), so the surface is plain CRUD + the
    // kind-generic promote verb. noAuth (see contract above). The project opt-in
    // route (/projects/{projectId}/workflows/{workflowName}) is NOT served here —
    // it is dispatched by the projects Lambda and registered with the other
    // /projects routes above (against projectsFn).
    r('/workflows', [M.GET], workflowsFn, 'WorkflowsGet', noAuth);
    r('/workflows', [M.POST], workflowsFn, 'WorkflowsPost', noAuth);
    r('/workflows/{name}', [M.GET, M.PUT, M.DELETE], workflowsFn, 'WorkflowByName', noAuth);
    r('/workflows/{name}/promote', [M.POST], workflowsFn, 'WorkflowPromote', noAuth);

    // Workflow RUN status (M5) — the live execution surface the Go executor
    // reports to and the web Workflows tab polls. The run/node ids are path-tail
    // segments dispatched inside the workflows Lambda. noAuth (executor device token).
    r('/workflows/{name}/runs', [M.GET, M.POST], workflowsFn, 'WorkflowRuns', noAuth);
    r('/workflows/{name}/runs/{runId}', [M.GET], workflowsFn, 'WorkflowRunById', noAuth);
    r(
      '/workflows/{name}/runs/{runId}/nodes/{nodeId}',
      [M.POST],
      workflowsFn,
      'WorkflowRunNode',
      noAuth,
    );

    // ALL skills routes are noAuth (see contract above) — this is what lets
    // /hq-add-skill author skills directly instead of round-tripping the git seed.
    r('/skills', [M.GET], skillsFn, 'SkillsGet', noAuth);
    r('/skills', [M.POST], skillsFn, 'SkillsPost', noAuth);
    r('/skills/{name}', [M.GET, M.PUT, M.DELETE], skillsFn, 'SkillByName', noAuth);
    r('/skills/{name}/members', [M.POST], skillsFn, 'SkillMembers', noAuth);
    r('/skills/{name}/members/{member}', [M.DELETE], skillsFn, 'SkillMemberDelete', noAuth);
    r('/skills/{name}/dissolve', [M.POST], skillsFn, 'SkillDissolve', noAuth);
    r('/skills/{name}/usage', [M.GET], skillsFn, 'SkillUsage', noAuth);
    // Promote (repoint the org-wide TRUE pointer) + fold an idea into a new
    // revision (skill-idea loop, U16). BOTH are the skills Lambda (the fold reuses
    // `putNewVersion`/the built-in guard there), skill-edit gated server-side.
    r('/skills/{name}/promote', [M.POST], skillsFn, 'SkillPromote', noAuth);
    r('/skills/{name}/ideas/{ideaId}/fold', [M.POST], skillsFn, 'SkillIdeaFold', noAuth);
    // Candidate learnings (skill-idea loop, U11): corroborated-only ideas a working
    // session may surface when the skill loads.
    r('/skills/{name}/candidate-learnings', [M.GET], ideasFn, 'SkillCandidateLearnings', noAuth);
    // All ideas (skill-idea loop, U13): EVERY idea for a skill — corroborated,
    // uncorroborated, and folded history — for the Command HQ dropdown; the
    // handler dispatches on the `/ideas` suffix.
    r('/skills/{name}/ideas', [M.GET], ideasFn, 'SkillIdeas', noAuth);
    // Unassigned bin (skill-idea loop, U15): the org's new-skill backlog — topics
    // the judge rejected from every candidate skill, with frequency. READ is open
    // to any org member; promote-to-skill is admin-gated server-side.
    r('/ideas/unassigned', [M.GET], ideasFn, 'IdeasUnassigned', noAuth);
    r(
      '/ideas/unassigned/{entryId}/promote-to-skill',
      [M.POST],
      ideasFn,
      'IdeasUnassignedPromote',
      noAuth,
    );

    // MCP servers mirror the skills catalog routes MINUS the bundle verbs
    // (members/dissolve) and the retired-by-design scope verb — the catalog is a
    // flat, org-only set (no bundles, no per-server tiering). NOTE: the project
    // opt-in route (/projects/{projectId}/mcp-servers/{name}) is NOT served by
    // this mcpServersFn — it is dispatched by the projects Lambda and registered
    // with the other /projects routes above. Org-catalog CRUD only here; noAuth
    // (see contract above).
    r('/mcp-servers', [M.GET], mcpServersFn, 'McpServersGet', noAuth);
    r('/mcp-servers', [M.POST], mcpServersFn, 'McpServersPost', noAuth);
    r('/mcp-servers/{name}', [M.GET, M.PUT, M.DELETE], mcpServersFn, 'McpServerByName', noAuth);
    r('/mcp-servers/{name}/usage', [M.GET], mcpServersFn, 'McpServerUsage', noAuth);

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

    // Weekly routes are noAuth (see contract above; bearerAuth + project
    // ownership in-handler) — this is what lets `/hq-weekly-update` drive the
    // commit lifecycle from the PTY. The itemized commit CRUD (U3) replaces the
    // legacy prose store/serve; the lifecycle transitions live on
    // `/projects/{pid}/weekly/{week}/...` (U4).
    r('/projects/{pid}/weekly', [M.GET], weeklyFn, 'Weekly', noAuth);
    r('/projects/{pid}/weekly/{week}', [M.GET], weeklyFn, 'WeeklyByWeek', noAuth);
    r(
      '/projects/{pid}/weekly/{week}/commits',
      [M.POST],
      weeklyFn,
      'WeeklyCommits',
      noAuth,
    );
    r(
      '/projects/{pid}/weekly/{week}/commits/{cid}',
      [M.PUT, M.DELETE],
      weeklyFn,
      'WeeklyCommitById',
      noAuth,
    );
    // Lifecycle transitions (U4) — each transition is its own POST; `canTransition`
    // (KTD2) gates legality (409), and reconcile-complete is transactional (KTD3).
    r('/projects/{pid}/weekly/{week}/lock', [M.POST], weeklyTransitionsFn, 'WeeklyLock', noAuth);
    r(
      '/projects/{pid}/weekly/{week}/reconcile/start',
      [M.POST],
      weeklyTransitionsFn,
      'WeeklyReconcileStart',
      noAuth,
    );
    r(
      '/projects/{pid}/weekly/{week}/reconcile/complete',
      [M.POST],
      weeklyTransitionsFn,
      'WeeklyReconcileComplete',
      noAuth,
    );

    // Memories are noAuth (see contract above); the handler scopes the reconcile
    // to the caller's own author key, so a collaborator's daemon can sync
    // memories to a project they do not own.
    r('/projects/{pid}/memories', [M.GET, M.PUT], memoriesFn, 'ProjectMemories', noAuth);

    // ---- Device-auth (claude+ device-code login) ------------------------------
    // start/poll are noAuth: the CLI hits them before it has ANY token. approve
    // requires the Cognito JWT (a signed-in browser approves the device).
    r('/device/start', [M.POST], deviceFn, 'DeviceStart', noAuth);
    r('/device/poll', [M.POST], deviceFn, 'DevicePoll', noAuth);
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

    // Alarm on DLQ DEPTH (U23 observability). A record that exhausts retries
    // lands here — most commonly a skill / topic embed that keeps failing
    // (Bedrock throttle, S3 Vectors outage) or an un-decodable record. Without
    // this, the loop fails to EMPTY-STATE: the skill is silently un-embedded, the
    // topic is silently un-associated, and nothing surfaces an error. The alarm
    // makes a non-empty DLQ visible: ANY message present for one minute (a single
    // poison record is already a problem worth a human look) breaches. The per-
    // record `EmbedOutcome=failure` metric (metrics.ts) is the faster leading
    // indicator; this is the durable backstop for records that fully gave up.
    new cloudwatch.Alarm(this, 'StreamConsumerDlqDepthAlarm', {
      alarmName: 'command-hq-stream-consumer-dlq-depth',
      alarmDescription:
        'Stream-consumer DLQ is non-empty — a skill/topic embed or association ' +
        'record exhausted retries (skill-idea loop fails to empty-state). Inspect ' +
        'the DLQ; correlate with the EmbedOutcome=failure metric.',
      metric: streamDlq.metricApproximateNumberOfMessagesVisible({
        period: cdk.Duration.minutes(1),
        statistic: 'Maximum',
      }),
      threshold: 0,
      comparisonOperator: cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
      evaluationPeriods: 1,
      // A queue with no messages reports no datapoints; do NOT alarm on missing
      // data (that is the healthy empty state), only on an actual depth > 0.
      treatMissingData: cloudwatch.TreatMissingData.NOT_BREACHING,
    });

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'TableName', { value: this.table.tableName });
    new cdk.CfnOutput(this, 'HttpApiUrl', { value: this.httpApi.apiEndpoint });
    new cdk.CfnOutput(this, 'WebSocketUrl', { value: wsStage.url });
  }
}
