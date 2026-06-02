import * as cdk from 'aws-cdk-lib/core';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';

/**
 * Auth foundation for Command HQ (Unit U4): a Cognito user pool for HQ web
 * users plus an app client the SPA authenticates against. The wrapper does not
 * use Cognito directly — it authenticates with a self-minted device token
 * (see packages/backend/src/auth) — so this stack only provisions the web-user
 * identity surface. The API/WS authorizers (U5) reference these outputs.
 */
export class AuthStack extends cdk.Stack {
  readonly userPool: cognito.UserPool;
  readonly userPoolClient: cognito.UserPoolClient;

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

    this.userPoolClient = this.userPool.addClient('HqWebClient', {
      userPoolClientName: 'command-hq-web',
      authFlows: {
        userSrp: true,
      },
      // SPA: public client, no generated secret.
      generateSecret: false,
      preventUserExistenceErrors: true,
      accessTokenValidity: cdk.Duration.hours(1),
      idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.days(30),
    });

    new cdk.CfnOutput(this, 'UserPoolId', { value: this.userPool.userPoolId });
    new cdk.CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
  }
}
