import { Amplify } from 'aws-amplify';
import {
  signIn as amplifySignIn,
  signOut as amplifySignOut,
  getCurrentUser as amplifyGetCurrentUser,
  fetchAuthSession,
} from 'aws-amplify/auth';
import type { AuthClient, AuthUser, CognitoConfig } from './authClient.js';

/**
 * Cognito-backed AuthClient using Amplify Auth (U19, KTD5). Instantiated only
 * when Cognito env config is present; otherwise the app falls back to the mock
 * client so it remains runnable/testable without a deployed user pool.
 */
export function createCognitoClient(config: CognitoConfig): AuthClient {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.userPoolClientId,
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
  };
}
