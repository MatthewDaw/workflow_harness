/**
 * Auth client abstraction (U19). The default implementation is a thin Cognito
 * wrapper around Amplify Auth, but the app depends only on this interface so
 * tests (and the login gate) can inject a mock without a real user pool.
 */

export interface AuthUser {
  userId: string;
  username: string;
  org: string;
}

export interface AuthClient {
  /** Resolves the current user, or null if unauthenticated. */
  getCurrentUser(): Promise<AuthUser | null>;
  /** Sign in with username/password; resolves the authenticated user. */
  signIn(username: string, password: string): Promise<AuthUser>;
  signOut(): Promise<void>;
  /** The current id/access token for authorizing REST + WS calls, if signed in. */
  getIdToken(): Promise<string | null>;
}

/** Configuration for the Cognito-backed client, sourced from Vite env vars. */
export interface CognitoConfig {
  userPoolId: string;
  userPoolClientId: string;
  org: string;
}

export function readCognitoConfig(): CognitoConfig | null {
  const env = import.meta.env;
  const userPoolId = env.VITE_COGNITO_USER_POOL_ID;
  const userPoolClientId = env.VITE_COGNITO_USER_POOL_CLIENT_ID;
  if (!userPoolId || !userPoolClientId) return null;
  return {
    userPoolId,
    userPoolClientId,
    org: env.VITE_ORG ?? 'acme',
  };
}
