import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { useDispatch } from 'react-redux';
import type { AuthClient, AuthUser } from './authClient.js';
import { readCognitoConfig } from './authClient.js';
import { createMockClient } from './mockClient.js';
import { setIdToken } from '../app/authSlice.js';

interface AuthState {
  user: AuthUser | null;
  loading: boolean;
  signIn: (username: string, password: string) => Promise<void>;
  signInWithGoogle: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthState | null>(null);

/**
 * Lazily build the default AuthClient: Cognito when env config is present,
 * otherwise the in-memory mock so the app runs without a deployed user pool.
 * Kept async-imported so the Amplify dependency is only pulled when configured.
 */
async function buildDefaultClient(): Promise<AuthClient> {
  const config = readCognitoConfig();
  if (config) {
    const { createCognitoClient } = await import('./cognitoClient.js');
    return createCognitoClient(config);
  }
  return createMockClient();
}

export function AuthProvider({
  children,
  client,
}: {
  children: ReactNode;
  /** Injectable client for tests; defaults to Cognito-or-mock. */
  client?: AuthClient;
}) {
  const dispatch = useDispatch();
  const [resolvedClient, setResolvedClient] = useState<AuthClient | null>(client ?? null);
  const [user, setUser] = useState<AuthUser | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let active = true;
    (async () => {
      const c = client ?? (await buildDefaultClient());
      if (!active) return;
      setResolvedClient(c);
      const current = await c.getCurrentUser();
      if (!active) return;
      setUser(current);
      // Restore the bearer token into the store so API calls are authorized
      // across reloads, not just immediately after an interactive sign-in.
      if (current) dispatch(setIdToken(await c.getIdToken()));
      if (!active) return;
      setLoading(false);
    })();
    return () => {
      active = false;
    };
  }, [client, dispatch]);

  // After a Hosted UI (Google) redirect returns, Amplify exchanges the code
  // asynchronously and fires a Hub event. Re-fetch the user + token then, so the
  // gate flips to the app without a manual reload.
  useEffect(() => {
    if (!resolvedClient) return;
    const unsubscribe = resolvedClient.onChange(async () => {
      const current = await resolvedClient.getCurrentUser();
      setUser(current);
      dispatch(setIdToken(current ? await resolvedClient.getIdToken() : null));
    });
    return unsubscribe;
  }, [resolvedClient, dispatch]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      async signIn(username, password) {
        if (!resolvedClient) throw new Error('auth client not ready');
        const u = await resolvedClient.signIn(username, password);
        setUser(u);
        // Capture the bearer token so RTK Query's prepareHeaders can attach it.
        dispatch(setIdToken(await resolvedClient.getIdToken()));
      },
      async signInWithGoogle() {
        if (!resolvedClient) throw new Error('auth client not ready');
        await resolvedClient.signInWithGoogle();
      },
      async signOut() {
        if (!resolvedClient) return;
        await resolvedClient.signOut();
        setUser(null);
        dispatch(setIdToken(null));
      },
    }),
    [user, loading, resolvedClient, dispatch],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
