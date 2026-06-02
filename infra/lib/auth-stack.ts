import * as cdk from 'aws-cdk-lib/core';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

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
    const appUrls = ['https://d13sqkbwzqe38l.cloudfront.net/', 'http://localhost:5173/'];
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
