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
  // Subscribers to auth-state changes. The real Cognito client emits an Amplify
  // Hub event when an OAuth (Google) redirect completes; AuthProvider listens for
  // that to flip the login gate. The mock emits the same way so the Google path
  // updates the UI without a real IdP.
  const listeners = new Set<() => void>();
  const emit = () => listeners.forEach((cb) => cb());

  return {
    async getCurrentUser() {
      return current;
    },
    async signIn(username, password) {
      if (!username || !password) {
        throw new Error('username and password are required');
      }
      current = { userId: `user-${username}`, username, org: deriveOrg(username) };
      emit();
      return current;
    },
    async signInWithGoogle() {
      // No real IdP in the mock; sign in as the wireframe user. Unlike Cognito
      // (which navigates away and resolves the user on redirect return),
      // AuthProvider's Google path does not call setUser itself — it waits for
      // onChange. So we must notify listeners here or the gate never flips.
      current = { userId: 'user-google', username: 'matt', org: deriveOrg('matt') };
      emit();
    },
    async signOut() {
      current = null;
      emit();
    },
    async getIdToken() {
      return current ? `mock-token-${current.userId}` : null;
    },
    onChange(cb) {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
  };
}
