import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import { Provider } from 'react-redux';
import { AppShell } from './AppShell.js';
import { AuthProvider } from '../auth/AuthProvider.js';
import { createMockClient } from '../auth/mockClient.js';
import type { AuthUser } from '../auth/authClient.js';
import { makeStore } from '../app/store.js';
import { installFetchStub } from '../test/testUtils.js';
import { wsEvent } from '../ws/liveActions.js';

const MATT: AuthUser = { userId: 'user-matt', username: 'matt', org: 'gmail.com' };

describe('AppShell sign-out control', () => {
  beforeEach(() => installFetchStub({ sessions: [] }));
  afterEach(() => vi.unstubAllGlobals());

  it('renders an explicit, labelled Sign out button that signs the user out', async () => {
    const client = createMockClient(MATT);
    const signOutSpy = vi.spyOn(client, 'signOut');

    render(
      <Provider store={makeStore()}>
        <AuthProvider client={client}>
          <MemoryRouter initialEntries={['/']}>
            <Routes>
              <Route path="/" element={<AppShell />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </Provider>,
    );

    const button = await screen.findByRole('button', { name: 'Sign out' });
    expect(button).toBeInTheDocument();

    await userEvent.click(button);
    expect(signOutSpy).toHaveBeenCalledTimes(1);
  });

  it('exposes a Get started nav item (install + link device)', async () => {
    render(
      <Provider store={makeStore()}>
        <AuthProvider client={createMockClient(MATT)}>
          <MemoryRouter initialEntries={['/']}>
            <Routes>
              <Route path="/" element={<AppShell />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </Provider>,
    );

    expect(await screen.findByRole('link', { name: 'Get started' })).toBeInTheDocument();
  });
});

describe('AppShell live counter', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('counts a session that goes live via WS even when it was not in the initial list', async () => {
    installFetchStub({ sessions: [] }); // no live sessions at fetch time
    const store = makeStore();

    render(
      <Provider store={store}>
        <AuthProvider client={createMockClient(MATT)}>
          <MemoryRouter initialEntries={['/']}>
            <Routes>
              <Route path="/" element={<AppShell />} />
            </Routes>
          </MemoryRouter>
        </AuthProvider>
      </Provider>,
    );

    // Initially 0 live.
    expect(await screen.findByText(/0 live/)).toBeInTheDocument();

    // A brand-new session announces itself live over the socket.
    store.dispatch(
      wsEvent({
        v: 1,
        instanceId: 'inst-9',
        host: 'matt@mbp',
        ts: 5000,
        seq: 0,
        event: {
          kind: 'session.start',
          sessionId: 'newlive',
          projectId: 'proj-x',
          host: 'matt@mbp',
          name: 'fresh session',
        },
      }),
    );

    // The header now reflects the newly-live session (was the "0 live" undercount bug).
    expect(await screen.findByText(/1 live/)).toBeInTheDocument();
  });
});
