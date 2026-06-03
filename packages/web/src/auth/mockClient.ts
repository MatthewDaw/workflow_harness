import type { AuthClient, AuthUser } from './authClient.js';

/**
 * Derive a tenant `org` from the login identity so distinct logins are distinct
 * profiles in local/demo use (the real Cognito client uses the token's org
 * claim instead). An email maps to its domain (`mattdaw7@gmail.com` → `gmail.com`,
 * `bill@cow.com` → `cow.com`), so two different accounts land in two different
 * orgs and never see each other's data; a bare username falls back to
 * `org-<username>`.
 */
export function deriveOrg(username: string): string {
  const at = username.indexOf('@');
  if (at > 0) return username.slice(at + 1).toLowerCase();
  return `org-${username.toLowerCase()}`;
}

/**
 * In-memory AuthClient used for local dev and tests. Starts signed-out; any
 * non-empty credentials sign the user in, deriving their tenant `org` from the
 * email/username (see {@link deriveOrg}) so separate logins are separate
 * profiles. Lets the whole login-gate flow run with no Cognito user pool.
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
      current = { userId: `user-${username}`, username, org: deriveOrg(username) };
      return current;
    },
    async signInWithGoogle() {
      // No real IdP in the mock; sign in as the wireframe user.
      current = { userId: 'user-google', username: 'matt', org: deriveOrg('matt') };
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
