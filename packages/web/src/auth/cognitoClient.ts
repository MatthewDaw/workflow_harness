import { Amplify } from 'aws-amplify';
import {
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  signInWithRedirect,
  getCurrentUser as amplifyGetCurrentUser,
  fetchAuthSession,
} from 'aws-amplify/auth';
import { Hub } from 'aws-amplify/utils';
import type { AuthClient, AuthUser, CognitoConfig } from './authClient.js';

/**
 * Cognito-backed AuthClient using Amplify Auth (U19, KTD5). Instantiated only
 * when Cognito env config is present; otherwise the app falls back to the mock
 * client so it remains runnable/testable without a deployed user pool.
 *
 * When a Hosted UI `domain` is configured, the OAuth code flow is enabled so
 * "Continue with Google" (social federation, AWS-native) works.
 */
export function createCognitoClient(config: CognitoConfig): AuthClient {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.userPoolClientId,
        ...(config.domain
          ? {
              loginWith: {
                oauth: {
                  domain: config.domain,
                  scopes: ['openid', 'email', 'profile'],
                  redirectSignIn: [config.redirectUrl],
                  redirectSignOut: [config.redirectUrl],
                  responseType: 'code' as const,
                },
              },
            }
          : {}),
      },
    },
  });

  const toUser = (userId: string, username: string): AuthUser => ({
    userId,
    username,
    org: config.org,
  });

  return {
    async getCurrentUser() {
      try {
        const u = await amplifyGetCurrentUser();
        return toUser(u.userId, u.username);
      } catch {
        return null;
      }
    },
    async signIn(username, password) {
      await amplifySignIn({ username, password });
      const u = await amplifyGetCurrentUser();
      return toUser(u.userId, u.username);
    },
    async signInWithGoogle() {
      await signInWithRedirect({ provider: 'Google' });
    },
    async signOut() {
      await amplifySignOut();
    },
    async getIdToken() {
      try {
        const session = await fetchAuthSession();
        return session.tokens?.idToken?.toString() ?? null;
      } catch {
        return null;
      }
    },
    onChange(cb) {
      // Fires after the Hosted UI redirect completes (signedIn), on sign-out, and
      // if the OAuth code exchange fails — so the provider can refresh the token.
      return Hub.listen('auth', ({ payload }) => {
        if (
          payload.event === 'signedIn' ||
          payload.event === 'signedOut' ||
          payload.event === 'signInWithRedirect' ||
          payload.event === 'signInWithRedirect_failure'
        ) {
          cb();
        }
      });
    },
  };
}
