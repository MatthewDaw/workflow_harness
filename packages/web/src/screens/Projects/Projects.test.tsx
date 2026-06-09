import { describe, it, expect, afterEach, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { Project } from '@harness/shared';
import { Projects, normalizeRepoSlug, projectIdFor } from './Projects.js';
import { renderWithProviders } from '../../test/testUtils.js';

const PROJECT: Project = {
  id: 'weekly-compass',
  name: 'weekly-compass',
  repo: 'gh/acme/weekly-compass',
  ownerUserId: 'user-matt',
  progressPct: 62,
  liveSessionCount: 0,
  enabledSkills: [],
  enabledAgents: [],
  enabledWorkflows: [],
  enabledAgentBundles: [],
  enabledBundles: [],
  enabledMcpServers: [],
};

function renderProjects() {
  return renderWithProviders(<Projects />, {
    route: '/projects',
    seed: { projects: [PROJECT] },
  });
}

/** The StubRequest the test fetch records carries the method + JSON body. */
type RecordedRequest = { url: string; method: string; body: unknown };
function postsTo(pathSuffix: string): RecordedRequest[] {
  const fetchMock = globalThis.fetch as unknown as { mock: { calls: [RecordedRequest][] } };
  return fetchMock.mock.calls
    .map((c) => c[0])
    .filter((r) => r.method === 'POST' && r.url.endsWith(pathSuffix));
}

describe('Projects — connect repo', () => {
  afterEach(() => vi.unstubAllGlobals());

  it('normalizes repo slugs and derives a stable id', () => {
    expect(normalizeRepoSlug('  https://github.com/Acme/Weekly-Compass.git ')).toBe(
      'Acme/Weekly-Compass',
    );
    expect(normalizeRepoSlug('gh/acme/weekly-compass')).toBe('acme/weekly-compass');
    expect(projectIdFor('Acme/Weekly-Compass')).toBe('acme-weekly-compass');
  });

  it('reveals the connect form when the button is clicked', async () => {
    renderProjects();
    await screen.findByText('1 projects');
    expect(screen.queryByTestId('connect-repo-form')).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId('connect-repo-toggle'));
    expect(screen.getByTestId('connect-repo-form')).toBeInTheDocument();
  });

  it('keeps Connect disabled until a valid owner/repo slug is entered', async () => {
    renderProjects();
    await userEvent.click(screen.getByTestId('connect-repo-toggle'));
    const submit = screen.getByTestId('connect-repo-submit');
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByTestId('connect-repo-input'), 'just-a-name');
    expect(submit).toBeDisabled();
    await userEvent.type(screen.getByTestId('connect-repo-input'), '{Backspace>11/}acme/atlas');
    expect(submit).toBeEnabled();
  });

  it('POSTs the connected repo with a derived id and gh/-prefixed repo', async () => {
    renderProjects();
    await userEvent.click(screen.getByTestId('connect-repo-toggle'));
    await userEvent.type(screen.getByTestId('connect-repo-input'), 'acme/atlas-billing');
    await userEvent.click(screen.getByTestId('connect-repo-submit'));

    await waitFor(() => expect(postsTo('/projects').length).toBe(1));
    const body = JSON.parse(String(postsTo('/projects')[0]!.body));
    expect(body).toMatchObject({
      id: 'acme-atlas-billing',
      name: 'atlas-billing',
      repo: 'gh/acme/atlas-billing',
    });

    // Connecting then fires a best-effort GitHub framing refresh.
    await waitFor(() => expect(postsTo('/projects/acme-atlas-billing/refresh').length).toBe(1));
    // Form closes on success.
    await waitFor(() => expect(screen.queryByTestId('connect-repo-form')).not.toBeInTheDocument());
  });
});
