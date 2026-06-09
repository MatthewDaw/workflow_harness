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
    // One shared HttpNoneAuthorizer reused across every public route (the
    // canonical CDK pattern — it is a stateless no-op binding). Passing it on a
    // route OVERRIDES the HTTP API's default JWT authorizer, making that route
    // PUBLIC at the gateway so the claude+ HS256 device token reaches the Lambda.
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
        // When omitted, the HTTP API's defaultAuthorizer (JWT) applies. Pass an
        // HttpNoneAuthorizer to OVERRIDE the default and make a route PUBLIC.
        ...(authorizer ? { authorizer } : {}),
      });

    r('/projects', [M.GET, M.POST], projectsFn, 'Projects');
    // GET is PUBLIC at the gateway (HttpNoneAuthorizer) so the claude+ wrapper's
    // device token reaches the handler, which verifies it in-handler via
    // resolvePrincipal (device token OR Cognito). DELETE stays Cognito-gated.
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
    // PUBLIC at the gateway (HttpNoneAuthorizer) so the claude+ device token
    // reaches projectsFn; the opt-in handler (projectForOptIn) authenticates the
    // caller (Cognito JWT OR device token) and enforces the admin-or-owner gate
    // server-side, so a developer can enable/disable catalog items on their own
    // project straight from claude+.
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

    // PUBLIC at the gateway (device token reaches the Lambda); the agents Lambda
    // authenticates + admin-gates server-side, exactly like skills above.
    r('/agents', [M.GET], agentsFn, 'AgentsGet', noAuth);
    r('/agents', [M.POST], agentsFn, 'AgentsPost', noAuth);
    r('/agents/{name}', [M.GET, M.PUT, M.DELETE], agentsFn, 'AgentByName', noAuth);
    r('/agents/{name}/scope', [M.POST], agentsFn, 'AgentScope', noAuth);
    // Agent-bundle catalog verbs mirror the skills bundle verbs below.
    r('/agents/{name}/members', [M.POST], agentsFn, 'AgentMembers', noAuth);
    r('/agents/{name}/members/{member}', [M.DELETE], agentsFn, 'AgentMemberDelete', noAuth);
    r('/agents/{name}/dissolve', [M.POST], agentsFn, 'AgentDissolve', noAuth);

    // Workflows mirror the agents catalog routes MINUS the bundle verbs
    // (members/dissolve) and the scope verb — a workflow is itself the
    // composition unit (no bundling in v1), so the surface is plain CRUD + the
    // kind-generic promote verb. PUBLIC at the gateway (HttpNoneAuthorizer) so the
    // claude+ device token reaches the Lambda; the workflows Lambda authenticates
    // + admin-gates server-side, exactly like agents above. The project opt-in
    // route (/projects/{projectId}/workflows/{workflowName}) is NOT served here —
    // it is dispatched by the projects Lambda and registered with the other
    // /projects routes above (against projectsFn).
    r('/workflows', [M.GET], workflowsFn, 'WorkflowsGet', noAuth);
    r('/workflows', [M.POST], workflowsFn, 'WorkflowsPost', noAuth);
    r('/workflows/{name}', [M.GET, M.PUT, M.DELETE], workflowsFn, 'WorkflowByName', noAuth);
    r('/workflows/{name}/promote', [M.POST], workflowsFn, 'WorkflowPromote', noAuth);

    // Workflow RUN status (M5) — the live execution surface the Go executor
    // reports to and the web Workflows tab polls. The run/node ids are path-tail
    // segments dispatched inside the workflows Lambda (the agents promote/members
    // precedent). Same HttpNoneAuthorizer + in-handler auth as the routes above so
    // the executor's device token reaches the Lambda.
    r('/workflows/{name}/runs', [M.GET, M.POST], workflowsFn, 'WorkflowRuns', noAuth);
    r('/workflows/{name}/runs/{runId}', [M.GET], workflowsFn, 'WorkflowRunById', noAuth);
    r(
      '/workflows/{name}/runs/{runId}/nodes/{nodeId}',
      [M.POST],
      workflowsFn,
      'WorkflowRunNode',
      noAuth,
    );

    // ALL skills routes are PUBLIC at the gateway (HttpNoneAuthorizer) so the
    // claude+ wrapper's HS256 device token reaches the Lambda — the gateway JWT
    // authorizer would reject HS256 outright. The skills Lambda authenticates
    // in-handler (Cognito JWT OR device token via resolveOrgCatalogAuth) and
    // enforces the admin gate SERVER-SIDE from the profile, so opening the gateway
    // does not weaken catalog-write authorization. This is what lets /hq-add-skill
    // author skills directly instead of round-tripping through the git seed.
    r('/skills', [M.GET], skillsFn, 'SkillsGet', noAuth);
    r('/skills', [M.POST], skillsFn, 'SkillsPost', noAuth);
    r('/skills/{name}', [M.GET, M.PUT, M.DELETE], skillsFn, 'SkillByName', noAuth);
    r('/skills/{name}/members', [M.POST], skillsFn, 'SkillMembers', noAuth);
    r('/skills/{name}/members/{member}', [M.DELETE], skillsFn, 'SkillMemberDelete', noAuth);
    r('/skills/{name}/dissolve', [M.POST], skillsFn, 'SkillDissolve', noAuth);
    r('/skills/{name}/usage', [M.GET], skillsFn, 'SkillUsage', noAuth);
    r('/skills/{name}/scope', [M.POST], skillsFn, 'SkillScope', noAuth);
    // Promote (repoint the org-wide TRUE pointer) + fold an idea into a new
    // revision (skill-idea loop, U16). BOTH are the skills Lambda (the fold reuses
    // `putNewVersion`/the built-in guard there) and BOTH are skill-edit gated
    // server-side; `noAuth` so the claude+ device token reaches the handler.
    r('/skills/{name}/promote', [M.POST], skillsFn, 'SkillPromote', noAuth);
    r('/skills/{name}/ideas/{ideaId}/fold', [M.POST], skillsFn, 'SkillIdeaFold', noAuth);
    // Candidate learnings (skill-idea loop, U11): corroborated-only ideas a working
    // session may surface when the skill loads. `noAuth` so the claude+ device token
    // reaches the handler, which gates server-side (the gate is a security boundary).
    r('/skills/{name}/candidate-learnings', [M.GET], ideasFn, 'SkillCandidateLearnings', noAuth);
    // All ideas (skill-idea loop, U13): EVERY idea for a skill — corroborated,
    // uncorroborated, and folded history — for the Command HQ dropdown. Reuses the
    // same `ideasFn`/bundle and `noAuth` device-token contract; the handler
    // dispatches on the `/ideas` suffix.
    r('/skills/{name}/ideas', [M.GET], ideasFn, 'SkillIdeas', noAuth);
    // Unassigned bin (skill-idea loop, U15): the org's new-skill backlog — topics
    // the judge rejected from every candidate skill, with frequency. Reuses the
    // same `ideasFn`/bundle and `noAuth` device-token contract. READ is open to
    // any org member; the promote-to-skill action is admin-gated server-side.
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
    // this mcpServersFn — it is dispatched by the projects Lambda's internal path
    // router and is registered up with the other /projects routes above (against
    // projectsFn, using the {projectId} first-segment param the opt-in handler
    // reads). The routes below are the org-catalog CRUD only.
    // PUBLIC at the gateway (device token reaches the Lambda); the mcp-servers
    // Lambda authenticates + admin-gates server-side, exactly like skills above.
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

    // Weekly routes accept EITHER a Cognito ID token (web) OR the claude+ wrapper
    // device token. The default gateway authorizer only accepts Cognito JWTs and
    // would 403 the device token before the handler runs, so we OVERRIDE it with
    // HttpNoneAuthorizer and let the weekly handler verify the bearer token itself
    // (rest/bearerAuth.ts: device token OR Cognito), still enforcing project
    // ownership. This is what lets `/hq-weekly-update` publish from the PTY.
    r('/projects/{pid}/weekly', [M.GET, M.PUT], weeklyFn, 'Weekly', noAuth);
    r(
      '/projects/{pid}/weekly/{week}',
      [M.GET, M.PUT],
      weeklyFn,
      'WeeklyByWeek',
      noAuth,
    );
    r(
      '/projects/{pid}/weekly/{week}/publish',
      [M.POST],
      weeklyFn,
      'WeeklyPublish',
      noAuth,
    );

    // Memories route mirrors weekly: HttpNoneAuthorizer so the claude+ daemon's
    // device token reaches the handler (the default JWT authorizer would 403 it
    // before it runs). The handler verifies the bearer token itself (device OR
    // Cognito) and scopes the reconcile to the caller's own author key, so a
    // collaborator's daemon can sync memories to a project they do not own.
    r('/projects/{pid}/memories', [M.GET, M.PUT], memoriesFn, 'ProjectMemories', noAuth);

    // ---- Device-auth (claude+ device-code login) ------------------------------
    // start/poll are PUBLIC: the CLI hits them before it has any token. They must
    // OVERRIDE the HTTP API's defaultAuthorizer (JWT) via HttpNoneAuthorizer.
    // approve REQUIRES the JWT (a signed-in browser approves the device) — it
    // inherits the default authorizer (no override).
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
