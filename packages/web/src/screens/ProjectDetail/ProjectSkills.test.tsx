import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Skill } from '@harness/shared';
import { ProjectSkills } from './ProjectSkills.js';
import { lastMatching, makeProject, makeSkill, renderWithProviders } from '../../test/testUtils.js';

const PROJECT = makeProject({ enabledSkills: ['gh'] });

const SKILLS: Skill[] = [
  makeSkill({
    name: 'gh',
    description: 'GitHub CLI',
    source: 'built-in',
    createdBy: { userId: 'u', name: 'Matt' },
  }),
  makeSkill({ name: 'browse', description: 'Browser', createdBy: { userId: 'u', name: 'Sam' } }),
  // A bundle whose members are reachable only by expanding it in the picker.
  makeSkill({
    name: 'frontend',
    kind: 'bundle',
    description: 'Frontend pack',
    members: ['react-skill', 'css-skill'],
    resolvedMembers: ['react-skill', 'css-skill'],
    createdBy: { userId: 'u', name: 'Sam' },
  }),
  makeSkill({ name: 'react-skill', description: 'React', createdBy: { userId: 'u', name: 'Sam' } }),
  makeSkill({ name: 'css-skill', description: 'CSS', createdBy: { userId: 'u', name: 'Sam' } }),
];

describe('ProjectSkills (project opt-in)', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('lists enabled skills and removes one via disableProjectSkill', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('skill-card-gh');

    await userEvent.click(screen.getByTestId('remove-skill-gh'));
    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'DELETE' && u.includes('projects/weekly-compass/skills/gh')),
      ).toBeDefined(),
    );
  });

  it('pins a per-repo variant via enableProjectSkill carrying { variantId }', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: {
        projects: [PROJECT],
        skills: SKILLS,
        skillVariants: {
          gh: [
            { variantId: 'gh#base', baseName: 'gh', name: 'gh', version: 1, isTrue: true },
            {
              variantId: 'gh#R#weekly#U#matt',
              baseName: 'gh',
              name: 'gh',
              version: 2,
              repoId: 'weekly',
              authorUserId: 'matt',
            },
          ],
        },
      },
    });
    await screen.findByTestId('skill-card-gh');

    // The enabled skill's footer carries a per-repo variant dropdown; picking the
    // fork re-enables the skill with that variantId (the project's pin).
    const select = (await screen.findByTestId('variant-select-gh')) as HTMLSelectElement;
    await waitFor(() => expect(select.options.length).toBe(2));
    await userEvent.selectOptions(select, 'gh#R#weekly#U#matt');

    await waitFor(() => {
      const req = lastMatching(
        (u, m) => m === 'POST' && u.includes('projects/weekly-compass/skills/gh'),
      );
      expect(req).toBeDefined();
      const body = JSON.parse(String(req!.body));
      expect(body.variantId).toBe('gh#R#weekly#U#matt');
    });
  });

  it('adds a standalone skill from the catalog modal via enableProjectSkill', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('skill-card-gh');

    await userEvent.click(screen.getByTestId('enable-skill'));
    await screen.findByTestId('catalog-picker');

    await userEvent.click(screen.getByTestId('catalog-picker-row-skill-browse'));
    await userEvent.click(screen.getByTestId('catalog-picker-apply'));

    await waitFor(() =>
      expect(
        lastMatching((u, m) => m === 'POST' && u.includes('projects/weekly-compass/skills/browse')),
      ).toBeDefined(),
    );
  });

  it('adds a whole bundle via enableProjectBundle', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('skill-card-gh');

    await userEvent.click(screen.getByTestId('enable-skill'));
    await screen.findByTestId('catalog-picker');

    await userEvent.click(screen.getByTestId('catalog-picker-row-bundle-frontend'));
    await userEvent.click(screen.getByTestId('catalog-picker-apply'));

    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/bundles/frontend'),
        ),
      ).toBeDefined(),
    );
  });

  it('expands a bundle and toggling one member enables that skill', async () => {
    renderWithProviders(<ProjectSkills />, {
      route: '/projects/weekly-compass/skills',
      routePath: '/projects/:projectId/skills',
      seed: { projects: [PROJECT], skills: SKILLS },
    });
    await screen.findByTestId('skill-card-gh');

    await userEvent.click(screen.getByTestId('enable-skill'));
    await screen.findByTestId('catalog-picker');

    await userEvent.click(screen.getByTestId('catalog-picker-expand-frontend'));
    await userEvent.click(await screen.findByTestId('catalog-picker-member-react-skill'));
    await userEvent.click(screen.getByTestId('catalog-picker-apply'));

    await waitFor(() =>
      expect(
        lastMatching(
          (u, m) => m === 'POST' && u.includes('projects/weekly-compass/skills/react-skill'),
        ),
      ).toBeDefined(),
    );
    // The whole-bundle endpoint must NOT have been hit for a single member.
    expect(
      lastMatching((u, m) => m === 'POST' && u.includes('projects/weekly-compass/bundles/')),
    ).toBeUndefined();
  });
});
