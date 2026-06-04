import { describe, it, expect, afterEach, beforeEach, vi } from 'vitest';
import { act, screen } from '@testing-library/react';
import { DetailedRequirements } from './DetailedRequirements.js';
import { renderWithProviders } from '../../test/testUtils.js';

/**
 * jsdom has no IntersectionObserver. Stub it so the scroll-spy effect can wire
 * up without throwing; we also capture instances if a test wants to fire an
 * intersection manually.
 */
class FakeIntersectionObserver {
  static instances: FakeIntersectionObserver[] = [];
  cb: IntersectionObserverCallback;
  elements: Element[] = [];
  constructor(cb: IntersectionObserverCallback) {
    this.cb = cb;
    FakeIntersectionObserver.instances.push(this);
  }
  observe(el: Element) {
    this.elements.push(el);
  }
  unobserve() {}
  disconnect() {}
}

const seed = {
  docs: {
    'weekly-compass': [
      { path: 'docs/plans/overview.md', title: 'Overview', completion: 80 },
      { path: 'docs/plans/command-hq/01-mapping.md', title: 'Plan Mapping', completion: 90 },
      { path: 'docs/plans/command-hq/02-weekly.md', title: 'Weekly Update', completion: 30 },
    ],
  },
  docContent: {
    'weekly-compass::docs/plans/overview.md': '# Overview\n\nThe overview doc.',
    'weekly-compass::docs/plans/command-hq/01-mapping.md': '# Mapping\n\nThe mapping doc.',
    'weekly-compass::docs/plans/command-hq/02-weekly.md': '# Weekly\n\nThe weekly doc.',
  },
};

function renderDetailed() {
  return renderWithProviders(<DetailedRequirements />, {
    route: '/projects/weekly-compass/detailed-requirements',
    routePath: '/projects/:projectId/detailed-requirements',
    seed,
  });
}

describe('DetailedRequirements (U11)', () => {
  beforeEach(() => {
    FakeIntersectionObserver.instances = [];
    vi.stubGlobal('IntersectionObserver', FakeIntersectionObserver);
    Element.prototype.scrollIntoView = vi.fn();
  });
  afterEach(() => vi.unstubAllGlobals());

  it('lists docs with per-doc completion badges grouped into a folder tree', async () => {
    renderDetailed();
    await screen.findByRole('button', { name: /Overview/ });
    const sidebar = screen.getByRole('complementary', { name: 'Requirement documents' });
    // Root doc + per-doc badges.
    expect(sidebar.textContent).toContain('Overview');
    expect(sidebar.textContent).toContain('80%');
    expect(sidebar.textContent).toContain('Plan Mapping');
    expect(sidebar.textContent).toContain('90%');
    // Folder header with aggregate (mean of 90 and 30 = 60%).
    expect(sidebar.textContent).toContain('command-hq/');
    expect(sidebar.textContent).toContain('60%');
  });

  it('renders every doc in a continuous scroll', async () => {
    renderDetailed();
    expect(await screen.findByText('The overview doc.')).toBeInTheDocument();
    expect(await screen.findByText('The mapping doc.')).toBeInTheDocument();
    expect(await screen.findByText('The weekly doc.')).toBeInTheDocument();
  });

  it('starts the bar on the first doc and updates on scroll-spy intersection', async () => {
    renderDetailed();
    await screen.findByText('The overview doc.');
    // Initial active = first doc (80%).
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '80');
    expect(screen.getByText(/Active doc · Overview · 80%/)).toBeInTheDocument();

    // Fire an intersection making the "Weekly Update" section the top-most.
    const observer = lastObserver();
    const weekly = observer.elements.find(
      (el) => (el as HTMLElement).dataset.path === 'docs/plans/command-hq/02-weekly.md',
    )!;
    act(() => {
      observer.cb(
        [
          {
            target: weekly,
            isIntersecting: true,
            boundingClientRect: { top: 0 } as DOMRectReadOnly,
          } as IntersectionObserverEntry,
        ],
        observer as unknown as IntersectionObserver,
      );
    });

    expect(await screen.findByText(/Active doc · Weekly Update · 30%/)).toBeInTheDocument();
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '30');
  });
});

/** Grab the most recently constructed fake observer. */
function lastObserver(): FakeIntersectionObserver {
  const list = FakeIntersectionObserver.instances;
  return list[list.length - 1] as FakeIntersectionObserver;
}
