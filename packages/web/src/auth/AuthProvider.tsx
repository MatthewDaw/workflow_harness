import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { useDispatch } from 'react-redux';
import type { AuthClient, AuthUser } from './authClient.js';
import { readCognitoConfig } from './authClient.js';
import { createMockClient } from './mockClient.js';
import { setIdToken } from '../app/authSlice.js';
import { baseApi } from '../api/baseApi.js';

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
  // The last identity we adopted, so we can detect an account switch (e.g. an
  // OAuth redirect that returns a *different* user) and wipe the previous user's
  // cached data before adopting the new one.
  const lastUserId = useRef<string | null>(null);

  // Drop every cached query/subscription when the identity changes away from a
  // previously-known user, so one user can never render another's data in the
  // same browser. A first sign-in (no prior identity) starts from an empty cache
  // and needs no reset.
  const resetCacheOnIdentityChange = useCallback(
    (nextUserId: string | null) => {
      if (lastUserId.current && lastUserId.current !== nextUserId) {
        dispatch(baseApi.util.resetApiState());
      }
      lastUserId.current = nextUserId;
    },
    [dispatch],
  );

  useEffect(() => {
    let active = true;
    (async () => {
      const c = client ?? (await buildDefaultClient());
      if (!active) return;
      setResolvedClient(c);
      const current = await c.getCurrentUser();
      if (!active) return;
      resetCacheOnIdentityChange(current?.userId ?? null);
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
  }, [client, dispatch, resetCacheOnIdentityChange]);

  // After a Hosted UI (Google) redirect returns, Amplify exchanges the code
  // asynchronously and fires a Hub event. Re-fetch the user + token then, so the
  // gate flips to the app without a manual reload. If the redirect returns a
  // *different* account, the cache is wiped first (account switch).
  useEffect(() => {
    if (!resolvedClient) return;
    const unsubscribe = resolvedClient.onChange(async () => {
      const current = await resolvedClient.getCurrentUser();
      resetCacheOnIdentityChange(current?.userId ?? null);
      setUser(current);
      dispatch(setIdToken(current ? await resolvedClient.getIdToken() : null));
    });
    return unsubscribe;
  }, [resolvedClient, dispatch, resetCacheOnIdentityChange]);

  const value = useMemo<AuthState>(
    () => ({
      user,
      loading,
      async signIn(username, password) {
        if (!resolvedClient) throw new Error('auth client not ready');
        const u = await resolvedClient.signIn(username, password);
        // Wipe any residual cache if this is a different identity than before.
        resetCacheOnIdentityChange(u.userId);
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
        try {
          await resolvedClient.signOut();
        } finally {
          // Sign-out always clears local state AND the data cache, even if the
          // client's signOut rejected — never strand a half-signed-out session
          // showing the previous user's data.
          setUser(null);
          lastUserId.current = null;
          dispatch(setIdToken(null));
          dispatch(baseApi.util.resetApiState());
        }
      },
    }),
    [user, loading, resolvedClient, dispatch, resetCacheOnIdentityChange],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider');
  return ctx;
}
