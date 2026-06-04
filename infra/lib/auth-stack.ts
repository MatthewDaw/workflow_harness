import * as cdk from 'aws-cdk-lib/core';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import { Construct } from 'constructs';

/**
 * The org every brand-new account lands in. Google sign-in auto-provisions a
 * federated user with no `custom:org`, but the API authorizes every request on
 * that claim — so without a default a new account would sign in and then 401
 * everywhere. The pre-token-generation Lambda below injects this value when the
 * user has no org of their own (existing orgs are preserved).
 */
const DEFAULT_ORG = 'personasearch';

/**
 * Auth foundation for Command HQ (Unit U4 + Google federation): a Cognito user
 * pool for HQ web users, a Hosted UI domain, a Google identity provider (social
 * sign-in, AWS-native — no Auth0), and an app client the SPA authenticates
 * against (SRP for email/password + the OAuth code flow for "Continue with
 * Google"). The wrapper does not use Cognito directly — it authenticates with a
 * self-minted device token (see packages/backend/src/auth). The API/WS
 * authorizers (U5) reference these outputs.
 */
export class AuthStack extends cdk.Stack {
  readonly userPool: cognito.UserPool;
  readonly userPoolClient: cognito.UserPoolClient;
  readonly userPoolDomain: cognito.UserPoolDomain;

  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    this.userPool = new cognito.UserPool(this, 'HqUserPool', {
      userPoolName: 'command-hq',
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      signInCaseSensitive: false,
      autoVerify: { email: true },
      standardAttributes: {
        email: { required: true, mutable: true },
      },
      // Per-user data is scoped by org; carried as a custom claim on the token.
      customAttributes: {
        org: new cognito.StringAttribute({ minLen: 1, maxLen: 256, mutable: true }),
      },
      passwordPolicy: {
        minLength: 12,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
      },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ---- Default-org pre-token-generation trigger -----------------------------
    // Runs on every token issuance. New accounts (notably Google federated users,
    // who are auto-created on first sign-in) carry no `custom:org`; this injects
    // the default into the ID token so the API authorizer sees a valid org. Users
    // who already have an org keep it (we only fill when absent). Inline + zero
    // dependencies, so no bundling step is needed.
    const orgClaimFn = new lambda.Function(this, 'DefaultOrgClaim', {
      runtime: lambda.Runtime.NODEJS_20_X,
      handler: 'index.handler',
      timeout: cdk.Duration.seconds(5),
      code: lambda.Code.fromInline(
        `const DEFAULT_ORG = ${JSON.stringify(DEFAULT_ORG)};\n` +
          'exports.handler = async (event) => {\n' +
          '  const attrs = (event.request && event.request.userAttributes) || {};\n' +
          "  if (!attrs['custom:org']) {\n" +
          '    event.response = {\n' +
          '      claimsOverrideDetails: {\n' +
          "        claimsToAddOrOverride: { 'custom:org': DEFAULT_ORG },\n" +
          '      },\n' +
          '    };\n' +
          '  }\n' +
          '  return event;\n' +
          '};\n',
      ),
    });
    this.userPool.addTrigger(cognito.UserPoolOperation.PRE_TOKEN_GENERATION, orgClaimFn);

    // ---- Hosted UI domain -----------------------------------------------------
    // Required for the social-login redirect flow. The Google IdP's redirect URI
    // is `${domain}/oauth2/idpresponse` (registered in the Google OAuth client).
    this.userPoolDomain = this.userPool.addDomain('HqDomain', {
      cognitoDomain: { domainPrefix: 'command-hq-066756' },
    });

    // ---- Google identity provider (AWS-native social sign-in) -----------------
    // The client ID is public (it appears in OAuth redirects); the secret is read
    // from Secrets Manager at deploy time, never committed.
    const google = new cognito.UserPoolIdentityProviderGoogle(this, 'GoogleIdp', {
      userPool: this.userPool,
      clientId:
        '163836316499-evusmfl30r9qag5jrcjvji5ine94jcgo.apps.googleusercontent.com',
      clientSecretValue: cdk.SecretValue.secretsManager('command-hq/google-oauth', {
        jsonField: 'clientSecret',
      }),
      scopes: ['openid', 'email', 'profile'],
      attributeMapping: {
        email: cognito.ProviderAttribute.GOOGLE_EMAIL,
        givenName: cognito.ProviderAttribute.GOOGLE_GIVEN_NAME,
        familyName: cognito.ProviderAttribute.GOOGLE_FAMILY_NAME,
      },
    });

    // ---- App client -----------------------------------------------------------
    // CloudFront (prod) plus the common Vite dev ports, so "Continue with Google"
    // redirects back successfully whether running deployed or on localhost.
    const appUrls = [
      'https://d13sqkbwzqe38l.cloudfront.net/',
      'http://localhost:5173/',
      'http://localhost:5174/',
      'http://localhost:5175/',
      'http://localhost:5176/',
    ];
    this.userPoolClient = this.userPool.addClient('HqWebClient', {
      userPoolClientName: 'command-hq-web',
      authFlows: { userSrp: true },
      // SPA: public client, no generated secret.
      generateSecret: false,
      preventUserExistenceErrors: true,
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
      supportedIdentityProviders: [
        cognito.UserPoolClientIdentityProvider.COGNITO,
        cognito.UserPoolClientIdentityProvider.GOOGLE,
      ],
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL, cognito.OAuthScope.PROFILE],
        callbackUrls: appUrls,
        logoutUrls: appUrls,
      },
    });
    // The client lists Google as a provider, so it must be created after the IdP.
    this.userPoolClient.node.addDependency(google);

    // ---- Outputs --------------------------------------------------------------
    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', { value: this.userPoolClient.userPoolClientId });
    new cdk.CfnOutput(this, 'CognitoDomain', {
      value: `${this.userPoolDomain.domainName}.auth.${this.region}.amazoncognito.com`,
    });
  }
}
