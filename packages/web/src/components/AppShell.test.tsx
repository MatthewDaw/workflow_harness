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
