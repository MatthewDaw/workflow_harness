import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { App } from '../app/App.js';
import { makeStore } from '../app/store.js';
import { createMockClient } from '../auth/mockClient.js';
import { installFetchStub } from './testUtils.js';

const memoryRouter = (children: ReactNode) => <MemoryRouter>{children}</MemoryRouter>;

describe('login gate', () => {
  beforeEach(() => installFetchStub({}));
  afterEach(() => vi.unstubAllGlobals());

  it('shows the login form when unauthenticated and gates the app', async () => {
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);
    expect(await screen.findByLabelText('Sign in to Command HQ')).toBeInTheDocument();
    expect(screen.queryByTestId('objectives-screen')).not.toBeInTheDocument();
  });

  it('renders Objectives (the first nav item) after signing in', async () => {
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);

    await userEvent.type(await screen.findByLabelText('Username'), 'matt');
    await userEvent.type(screen.getByLabelText('Password'), 'pw');
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }));

    await waitFor(() => expect(screen.getByTestId('objectives-screen')).toBeInTheDocument());
  });
});
