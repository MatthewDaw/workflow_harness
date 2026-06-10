import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ObjectiveNode } from '@harness/shared';
import { Objectives } from './Objectives.js';
import { lastMatching, renderWithProviders } from '../../test/testUtils.js';

const OBJECTIVES: ObjectiveNode[] = [
  { id: 'rc1', org: 'acme', level: 'rally_cry', title: 'Win the quarter', pct: 40 },
  {
    id: 'do1',
    org: 'acme',
    level: 'defining_objective',
    title: 'Ship Command HQ',
    parentId: 'rc1',
    pct: 55,
  },
];

/** Find the last request to /objectives matching a method (reads the stubbed Request). */
function lastObjectivesCall(method: string): { url: string; body: unknown } | undefined {
  const req = lastMatching((u, m) => m === method && /\/objectives(\/|$)/.test(u));
  if (!req) return undefined;
  return { url: req.url, body: req.body ? JSON.parse(String(req.body)) : undefined };
}

describe('Objectives editor (HQ-owned RCDO)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('still renders the tree of objectives', async () => {
    renderWithProviders(<Objectives />, { route: '/objectives', seed: { objectives: OBJECTIVES } });
    expect(await screen.findByText('Win the quarter')).toBeInTheDocument();
    expect(screen.getByText('Ship Command HQ')).toBeInTheDocument();
  });

  it('creates a node via createObjective with the chosen level + title + parent', async () => {
    renderWithProviders(<Objectives />, { route: '/objectives', seed: { objectives: OBJECTIVES } });
    await screen.findByText('Win the quarter');

    await userEvent.selectOptions(screen.getByTestId('objective-level'), 'outcome');
    await userEvent.type(screen.getByTestId('objective-title'), 'Reach 80% completion');
    await userEvent.selectOptions(screen.getByTestId('objective-parent'), 'do1');
    await userEvent.click(screen.getByTestId('objective-add'));

    await waitFor(() => expect(lastObjectivesCall('POST')).toBeDefined());
    const post = lastObjectivesCall('POST')!;
    expect(post.url).toMatch(/\/objectives$/);
    expect(post.body).toMatchObject({
      level: 'outcome',
      title: 'Reach 80% completion',
      parentId: 'do1',
    });
  });

  it('deletes a node via deleteObjective', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithProviders(<Objectives />, { route: '/objectives', seed: { objectives: OBJECTIVES } });
    await screen.findByText('Win the quarter');

    await userEvent.click(screen.getByTestId('objective-delete-do1'));

    await waitFor(() => expect(lastObjectivesCall('DELETE')).toBeDefined());
    expect(lastObjectivesCall('DELETE')!.url).toMatch(/\/objectives\/do1$/);
  });

  it('edits a node title via createObjective with the existing id', async () => {
    vi.spyOn(window, 'prompt').mockReturnValue('Win the year');
    renderWithProviders(<Objectives />, { route: '/objectives', seed: { objectives: OBJECTIVES } });
    await screen.findByText('Win the quarter');

    await userEvent.click(screen.getByTestId('objective-edit-rc1'));

    await waitFor(() => expect(lastObjectivesCall('POST')).toBeDefined());
    expect(lastObjectivesCall('POST')!.body).toMatchObject({
      id: 'rc1',
      level: 'rally_cry',
      title: 'Win the year',
    });
  });
});
