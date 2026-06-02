import { BrowserRouter } from 'react-router-dom';
import { Provider } from 'react-redux';
import { useMemo, type ReactNode } from 'react';
import { AuthProvider } from '../auth/AuthProvider.js';
import { LoginGate } from '../auth/LoginGate.js';
import { makeStore, type AppStore } from './store.js';
import { AppRoutes } from './router.js';
import type { AuthClient } from '../auth/authClient.js';

/**
 * Root composition (U19): Redux store → auth provider → login gate → router.
 * Accepts injectable `store`/`authClient`/router for tests so screens can be
 * mounted in isolation with seeded data and a mock auth client.
 */
export function App({
  store,
  authClient,
  router,
}: {
  store?: AppStore;
  authClient?: AuthClient;
  /** Test seam to swap BrowserRouter for MemoryRouter. */
  router?: (children: ReactNode) => ReactNode;
}) {
  const appStore = useMemo(() => store ?? makeStore(), [store]);
  const wrapRouter = router ?? ((children: ReactNode) => <BrowserRouter>{children}</BrowserRouter>);

  return (
    <Provider store={appStore}>
      <AuthProvider client={authClient}>
        <LoginGate>{wrapRouter(<AppRoutes />)}</LoginGate>
      </AuthProvider>
    </Provider>
  );
}
