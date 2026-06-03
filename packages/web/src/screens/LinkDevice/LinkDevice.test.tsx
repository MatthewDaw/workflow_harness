import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { LinkDevice } from './LinkDevice.js';
import { renderWithProviders } from '../../test/testUtils.js';

describe('LinkDevice (device-auth approval)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders the user-code form', async () => {
    renderWithProviders(<LinkDevice />, { route: '/link-device' });
    expect(await screen.findByTestId('link-device')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('WDJB-MJXT')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve device' })).toBeInTheDocument();
  });

  it('shows the claude+ install commands above the device form', async () => {
    renderWithProviders(<LinkDevice />, { route: '/link-device' });
    await screen.findByTestId('link-device');

    // curl one-liner + npm command, each with a Copy button, plus a releases link.
    expect(screen.getByText(/curl -fsSL .*install\.sh \| sh/)).toBeInTheDocument();
    expect(screen.getByText('npm i -g claude-plus')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Copy' }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('link', { name: /claude-plus\/releases/ })).toBeInTheDocument();
  });

  it('shows the success message after approving a code', async () => {
    renderWithProviders(<LinkDevice />, { route: '/link-device' });
    await screen.findByTestId('link-device');

    await userEvent.type(screen.getByPlaceholderText('WDJB-MJXT'), 'WDJB-MJXT');
    await userEvent.click(screen.getByRole('button', { name: 'Approve device' }));

    expect(
      await screen.findByText('Device approved — return to your terminal.'),
    ).toBeInTheDocument();
  });
});
