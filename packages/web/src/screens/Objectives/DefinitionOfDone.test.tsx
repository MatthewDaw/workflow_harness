import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { DefinitionOfDone } from '@harness/shared';
import { Objectives } from './Objectives.js';
import { renderWithProviders } from '../../test/testUtils.js';

/**
 * Plan-mapping feature 1: the org-wide Definition of Done surfaced on the
 * Objectives screen. Read-only display of the current DoD near the top, plus an
 * admin editor (two checkboxes + notes) that saves via PUT /dod. Advisory.
 */

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

/** Find the last request to /dod matching a method (reads the stubbed Request). */
function lastDodCall(method: string): { url: string; body: unknown } | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (req.method === method && /\/dod$/.test(req.url)) {
      return { url: req.url, body: req.body ? JSON.parse(String(req.body)) : undefined };
    }
  }
  return undefined;
}

const TIGHTENED: DefinitionOfDone = {
  requiresUnitTests: true,
  requiresProdE2E: true,
  notes: 'Run npm run e2e:prod against staging.',
};

describe('Definition of Done card', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('renders a read-only summary of the served DoD near the top', async () => {
    renderWithProviders(<Objectives />, { route: '/objectives', seed: { dod: TIGHTENED } });
    const summary = await screen.findByTestId('dod-summary');
    expect(summary).toHaveTextContent('unit tests passing + prod-E2E verified');
    expect(screen.getByTestId('dod-display')).toHaveTextContent(
      'Run npm run e2e:prod against staging.',
    );
    // The card is labelled advisory (never blocks).
    expect(screen.getByText(/never blocks a progress push/i)).toBeInTheDocument();
  });

  it('shows the floor default when no DoD is configured', async () => {
    renderWithProviders(<Objectives />, { route: '/objectives' });
    const summary = await screen.findByTestId('dod-summary');
    expect(summary).toHaveTextContent('unit tests passing');
    expect(summary).not.toHaveTextContent('prod-E2E');
  });

  it('admin edits the DoD and saves via PUT /dod', async () => {
    renderWithProviders(<Objectives />, {
      route: '/objectives',
      seed: { dod: { requiresUnitTests: true, requiresProdE2E: false } },
    });
    // Editor hydrates from the served DoD.
    await waitFor(() =>
      expect(screen.getByTestId('dod-prod-e2e')).not.toBeChecked(),
    );

    await userEvent.click(screen.getByTestId('dod-prod-e2e'));
    await userEvent.type(screen.getByTestId('dod-notes'), 'verify against prod');
    await userEvent.click(screen.getByTestId('dod-save'));

    await waitFor(() => expect(lastDodCall('PUT')).toBeDefined());
    const put = lastDodCall('PUT')!;
    expect(put.url).toMatch(/\/dod$/);
    expect(put.body).toMatchObject({
      requiresUnitTests: true,
      requiresProdE2E: true,
      notes: 'verify against prod',
    });
  });
});
