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

  // Google-federated users get a synthetic Cognito username like
  // "google_104923206893820071904", which is useless to show. Prefer the
  // human-friendly identity claims from the ID token (name → given_name →
  // email local-part), falling back to the raw username only if none exist.
  const friendlyName = (rawUsername: string, claims: Record<string, unknown>): string => {
    const name = typeof claims.name === 'string' ? claims.name.trim() : '';
    if (name) return name;
    const given = typeof claims.given_name === 'string' ? claims.given_name.trim() : '';
    if (given) return given;
    const email = typeof claims.email === 'string' ? claims.email.trim() : '';
    if (email) return email.split('@')[0] ?? email;
    return rawUsername;
  };

  const toUser = async (userId: string, rawUsername: string): Promise<AuthUser> => {
    let claims: Record<string, unknown> = {};
    try {
      const session = await fetchAuthSession();
      claims = session.tokens?.idToken?.payload ?? {};
    } catch {
      // No session/claims available — fall back to the raw username below.
    }
    return {
      userId,
      username: friendlyName(rawUsername, claims),
      org: config.org,
    };
  };

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
