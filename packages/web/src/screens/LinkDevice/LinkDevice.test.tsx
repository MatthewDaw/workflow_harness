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

  it('explains how to run, detach, reattach, and quit claude+', async () => {
    renderWithProviders(<LinkDevice />, { route: '/link-device' });
    await screen.findByTestId('link-device');

    expect(screen.getByText('Using claude+ in a project')).toBeInTheDocument();
    // The Ctrl-G chords for the day-to-day flow.
    expect(screen.getByText('Ctrl-G d')).toBeInTheDocument();
    expect(screen.getByText('Ctrl-G q')).toBeInTheDocument();
    expect(screen.getByText(/Detach/)).toBeInTheDocument();
    expect(screen.getByText(/Reattach/)).toBeInTheDocument();
    expect(screen.getByText(/Quit/)).toBeInTheDocument();
  });

  it('lists the terminal commands including login and reset', async () => {
    renderWithProviders(<LinkDevice />, { route: '/link-device' });
    await screen.findByTestId('link-device');

    expect(screen.getByText('Terminal commands')).toBeInTheDocument();
    expect(screen.getByText('claude+ reset')).toBeInTheDocument();
    expect(screen.getByText('claude+ sync')).toBeInTheDocument();
    expect(screen.getByText('claude+ stop=N')).toBeInTheDocument();
    // These appear both as a command row and as an inline reference / flow step.
    expect(screen.getAllByText('claude+ ls').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('claude+ login').length).toBeGreaterThanOrEqual(1);
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
