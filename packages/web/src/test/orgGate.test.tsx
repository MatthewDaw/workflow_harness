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

/** Sign in via the mock client so the OrgGate (which mounts inside LoginGate) runs. */
async function signIn() {
  await userEvent.type(await screen.findByLabelText('Username'), 'matt');
  await userEvent.type(screen.getByLabelText('Password'), 'pw');
  await userEvent.click(screen.getByRole('button', { name: /sign in/i }));
}

describe('org gate', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('forces onboarding when /me reports no org', async () => {
    // /me returns org: null → the OrgGate must show the setup form, not the app.
    installFetchStub({ me: { org: null } });
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);

    await signIn();

    expect(await screen.findByLabelText('Set up your organization')).toBeInTheDocument();
    expect(screen.queryByTestId('objectives-screen')).not.toBeInTheDocument();
  });

  it('flips to Objectives after creating an org', async () => {
    // /me returns null until the org is created, then a member — so the gate flips
    // once createOrg's 'Me' invalidation refetches /me.
    let created = false;
    installFetchStub({
      me: () => (created ? { org: 'acme' } : { org: null }),
      routes: {
        'POST orgs': () => {
          created = true;
          return { org: 'acme', admin: true };
        },
      },
    });
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);

    await signIn();

    await userEvent.type(await screen.findByLabelText('Organization name'), 'acme');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: /create organization/i }));

    await waitFor(() => expect(screen.getByTestId('objectives-screen')).toBeInTheDocument());
  });

  it('switches the active org from the header menu and reloads under it', async () => {
    // Two memberships; /me reports the active one and flips after the switch POST.
    let switched = false;
    installFetchStub({
      me: () =>
        switched
          ? { org: 'beta', orgs: ['acme', 'beta'] }
          : { org: 'acme', orgs: ['acme', 'beta'] },
      routes: {
        'POST me/org': () => {
          switched = true;
          return { org: 'beta', admin: false };
        },
      },
    });
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);

    await signIn();

    // The header shows the active org ('acme') as the switcher trigger; open it.
    await userEvent.click(await screen.findByRole('button', { name: /^acme/i }));
    await userEvent.click(await screen.findByRole('menuitem', { name: 'beta' }));

    // After switching, the header reflects the new active org.
    await waitFor(() => expect(screen.getByRole('button', { name: /^beta/i })).toBeInTheDocument());
  });

  it('shows a generic error when joining with a bad name/password (403)', async () => {
    installFetchStub({
      me: { org: null },
      routes: {
        'POST orgs/join': { status: 403, body: { error: 'invalid organization name or password' } },
      },
    });
    render(<App store={makeStore()} authClient={createMockClient(null)} router={memoryRouter} />);

    await signIn();

    // Switch to the Join tab, fill it, and submit into the 403.
    await userEvent.click(await screen.findByRole('tab', { name: /join organization/i }));
    await userEvent.type(screen.getByLabelText('Organization name'), 'nope');
    await userEvent.type(screen.getByLabelText('Password'), 'wrong');
    await userEvent.click(screen.getByRole('button', { name: /join organization/i }));

    expect(await screen.findByRole('alert')).toHaveTextContent(
      'Invalid organization name or password.',
    );
  });
});
