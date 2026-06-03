import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, act } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { Provider } from 'react-redux';
import type { ReactNode } from 'react';
import { AuthProvider, useAuth } from '../auth/AuthProvider.js';
import { createMockClient } from '../auth/mockClient.js';
import type { AuthClient, AuthUser } from '../auth/authClient.js';
import { makeStore, type AppStore } from '../app/store.js';
import { baseApi } from '../api/baseApi.js';
import { installFetchStub } from './testUtils.js';

const MATT: AuthUser = { userId: 'user-matt', username: 'matt', org: 'gmail.com' };
const BILL: AuthUser = { userId: 'user-bill', username: 'bill', org: 'cow.com' };

/** Minimal child exposing the auth context so a test can drive sign-out. */
function Harness() {
  const { user, signOut } = useAuth();
  return (
    <button type="button" onClick={() => void signOut()}>
      {user ? `in:${user.userId}` : 'out'}
    </button>
  );
}

function renderWithClient(client: AuthClient, store: AppStore) {
  const wrap = (children: ReactNode) => (
    <Provider store={store}>
      <AuthProvider client={client}>{children}</AuthProvider>
    </Provider>
  );
  return render(wrap(<Harness />));
}

/** How many RTK Query cache entries currently exist. */
const cacheSize = (store: AppStore) => Object.keys(store.getState().api.queries).length;

describe('AuthProvider per-user isolation', () => {
  beforeEach(() => installFetchStub({ projects: [] }));
  afterEach(() => vi.unstubAllGlobals());

  it('wipes the RTK Query cache on sign-out', async () => {
    const store = makeStore();
    renderWithClient(createMockClient(MATT), store);
    await screen.findByText('in:user-matt');

    // Prime the cache with a query result for the signed-in user.
    await store.dispatch(baseApi.endpoints.getProjects.initiate());
    expect(cacheSize(store)).toBeGreaterThan(0);

    await userEvent.click(screen.getByRole('button'));

    await waitFor(() => {
      expect(screen.getByText('out')).toBeInTheDocument();
      expect(cacheSize(store)).toBe(0);
      expect(store.getState().auth.idToken).toBeNull();
    });
  });

  it('wipes the cache and adopts the new token when the account switches (OAuth redirect)', async () => {
    let fireChange: () => void = () => {};
    let current: AuthUser | null = MATT;
    const client: AuthClient = {
      getCurrentUser: async () => current,
      signIn: async () => current as AuthUser,
      signInWithGoogle: async () => {},
      signOut: async () => {
        current = null;
      },
      getIdToken: async () => (current ? `tok-${current.userId}` : null),
      onChange: (cb) => {
        fireChange = cb;
        return () => {};
      },
    };

    const store = makeStore();
    renderWithClient(client, store);
    await screen.findByText('in:user-matt');

    await store.dispatch(baseApi.endpoints.getProjects.initiate());
    expect(cacheSize(store)).toBeGreaterThan(0);

    // The redirect returns a *different* account.
    current = BILL;
    await act(async () => {
      fireChange();
    });

    await waitFor(() => {
      expect(screen.getByText('in:user-bill')).toBeInTheDocument();
      expect(cacheSize(store)).toBe(0);
      expect(store.getState().auth.idToken).toBe('tok-user-bill');
    });
  });
});
