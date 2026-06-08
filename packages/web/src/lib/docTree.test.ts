import { describe, it, expect } from 'vitest';
import { buildDocTree, type TreeNode } from './docTree.js';

/**
 * `buildDocTree` groups repo doc paths into a flattened, render-ready tree.
 * It is pure, so these cases are deterministic.
 */

function labels(nodes: TreeNode[]): string[] {
  return nodes.map((n) => (n.kind === 'folder' ? `[${n.label} ${n.completion}%]` : n.doc.title));
}

describe('buildDocTree', () => {
  it('keeps files directly under docs/plans/ at the root', () => {
    const tree = buildDocTree([
      { path: 'docs/plans/overview.md', title: 'Overview', completion: 80 },
      { path: 'docs/plans/2026-plan.md', title: 'Plan', completion: 40 },
    ]);
    expect(tree.every((n) => n.depth === 0)).toBe(true);
    expect(labels(tree)).toEqual(['Overview', 'Plan']);
  });

  it('nests subfolder docs under a folder header with mean completion', () => {
    const tree = buildDocTree([
      { path: 'docs/plans/overview.md', title: 'Overview', completion: 100 },
      { path: 'docs/plans/command-hq/01-map.md', title: 'Map', completion: 80 },
      { path: 'docs/plans/command-hq/02-week.md', title: 'Week', completion: 40 },
    ]);
    expect(labels(tree)).toEqual(['Overview', '[command-hq/ 60%]', 'Map', 'Week']);
    const folder = tree.find((n) => n.kind === 'folder');
    expect(folder?.depth).toBe(0);
    // Docs in the folder are indented one level.
    const docNodes = tree.filter((n) => n.kind === 'doc');
    expect(docNodes.find((n) => n.kind === 'doc' && n.doc.title === 'Map')?.depth).toBe(1);
  });

  it('handles arbitrary nesting depth', () => {
    const tree = buildDocTree([{ path: 'docs/plans/a/b/deep.md', title: 'Deep', completion: 50 }]);
    expect(labels(tree)).toEqual(['[a/ 50%]', '[b/ 50%]', 'Deep']);
    const deep = tree.find((n) => n.kind === 'doc');
    expect(deep?.depth).toBe(2);
  });

  it('emits each folder header once and preserves doc order within it', () => {
    const tree = buildDocTree([
      { path: 'docs/plans/x/one.md', title: 'One', completion: 10 },
      { path: 'docs/plans/x/two.md', title: 'Two', completion: 30 },
    ]);
    expect(tree.filter((n) => n.kind === 'folder')).toHaveLength(1);
    expect(labels(tree)).toEqual(['[x/ 20%]', 'One', 'Two']);
  });
});
