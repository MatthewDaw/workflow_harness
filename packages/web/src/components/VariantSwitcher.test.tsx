import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { VariantSwitcher } from './VariantSwitcher.js';
import { renderWithProviders } from '../test/testUtils.js';

const VARIANTS = {
  gh: [
    { variantId: 'gh#base', baseName: 'gh', name: 'gh', version: 1, isTrue: true, authorName: 'system' },
    {
      variantId: 'gh#R#weekly#U#matt',
      baseName: 'gh',
      name: 'gh',
      version: 2,
      repoId: 'weekly',
      authorUserId: 'u-matt',
      authorName: 'Matt',
    },
  ],
};

interface StubReq {
  url: string;
  method: string;
  body: unknown;
}

function lastMatching(pred: (u: string, m: string) => boolean): StubReq | undefined {
  const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
  for (let i = calls.length - 1; i >= 0; i--) {
    const req = calls[i]![0] as StubReq;
    if (pred(req.url, req.method)) return req;
  }
  return undefined;
}

describe('VariantSwitcher (catalog versioning)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists a name’s variants TRUE-first and flags the true one', async () => {
    renderWithProviders(<VariantSwitcher name="gh" allowPromote />, {
      seed: { skillVariants: VARIANTS },
    });
    const select = (await screen.findByTestId('variant-select-gh')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    const options = Array.from(select.options).map((o) => o.textContent);
    // TRUE base variant first (flagged), then the fork.
    expect(options[0]).toMatch(/true/i);
    expect(options.join(' ')).toMatch(/Matt@weekly/);
  });

  it('promotes the selected (non-true) variant via the promote mutation', async () => {
    renderWithProviders(<VariantSwitcher name="gh" allowPromote />, {
      seed: { skillVariants: VARIANTS },
    });
    const select = (await screen.findByTestId('variant-select-gh')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));

    // Select the fork, then promote it to TRUE.
    await userEvent.selectOptions(select, 'gh#R#weekly#U#matt');
    await userEvent.click(screen.getByTestId('promote-variant-gh'));

    await waitFor(() => {
      const req = lastMatching((u, m) => m === 'POST' && u.includes('skills/gh/promote'));
      expect(req).toBeDefined();
      const body = JSON.parse(String(req!.body));
      expect(body.variantId).toBe('gh#R#weekly#U#matt');
      expect(body.rev).toBe(2);
    });
  });

  it('disables promote when the selected variant is already true', async () => {
    renderWithProviders(<VariantSwitcher name="gh" allowPromote />, {
      seed: { skillVariants: VARIANTS },
    });
    const select = (await screen.findByTestId('variant-select-gh')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    // Default selection is the TRUE base variant → promote is a no-op, disabled.
    expect(screen.getByTestId('promote-variant-gh')).toBeDisabled();
  });

  it('renders nothing for a single-variant skill when promote is off', async () => {
    renderWithProviders(<VariantSwitcher name="solo" />, {
      seed: {
        skillVariants: {
          solo: [{ variantId: 'solo#base', baseName: 'solo', name: 'solo', version: 1, isTrue: true }],
        },
      },
    });
    // No variants to switch between and no promote affordance → nothing rendered.
    await waitFor(() => expect(screen.queryByTestId('variant-switcher-solo')).not.toBeInTheDocument());
  });
});
