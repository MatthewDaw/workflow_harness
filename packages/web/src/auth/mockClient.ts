import type { AuthClient, AuthUser } from './authClient.js';

/**
 * In-memory AuthClient used for local dev and tests. Starts signed-out; any
 * non-empty credentials sign in as `@matt` (the wireframe's user). Lets the
 * whole login-gate flow run with no Cognito user pool.
 */
export function createMockClient(initialUser: AuthUser | null = null): AuthClient {
  let current: AuthUser | null = initialUser;

  return {
    async getCurrentUser() {
      return current;
    },
    async signIn(username, password) {
      if (!username || !password) {
        throw new Error('username and password are required');
      }
      current = { userId: `user-${username}`, username, org: 'acme' };
      return current;
    },
    async signInWithGoogle() {
      // No real IdP in the mock; sign in as the wireframe user.
      current = { userId: 'user-google', username: 'matt', org: 'acme' };
    },
    async signOut() {
      current = null;
    },
    async getIdToken() {
      return current ? `mock-token-${current.userId}` : null;
    },
    onChange() {
      return () => {};
    },
  };
}
