import { describe, it, expect } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import type { WorkflowNode } from '@harness/shared';
import { WorkflowGraph, layerNodes } from '../components/WorkflowGraph.js';

/**
 * M2b — WorkflowGraph DAG view. Covers the topological layering (Kahn's
 * algorithm via `layerNodes`), the cycle/empty edge cases, and the rendered
 * card/loop-badge/run-state overlay.
 */

function node(over: Partial<WorkflowNode> & { id: string }): WorkflowNode {
  return {
    agent: 'builder',
    label: '',
    prompt: '',
    dependsOn: [],
    ...over,
  };
}

/** ids per layer, in the order `layerNodes` grouped them. */
function layerIds(nodes: WorkflowNode[]): string[][] {
  return layerNodes(nodes).levels.map((row) => row.map((n) => n.id));
}

describe('layerNodes — topological layering', () => {
  it('groups a diamond DAG into longest-path layers', () => {
    // a → b, a → c, (b,c) → d. `d` must sit below BOTH its parents.
    const nodes = [
      node({ id: 'a' }),
      node({ id: 'b', dependsOn: ['a'] }),
      node({ id: 'c', dependsOn: ['a'] }),
      node({ id: 'd', dependsOn: ['b', 'c'] }),
    ];
    expect(layerIds(nodes)).toEqual([['a'], ['b', 'c'], ['d']]);
    expect(layerNodes(nodes).unsorted).toEqual([]);
  });

  it('places a node below its DEEPEST parent, not its shallowest', () => {
    // a → b → c, and a → c directly. c depends on both a (layer 0) and b
    // (layer 1) so it must land on layer 2, not layer 1.
    const nodes = [
      node({ id: 'a' }),
      node({ id: 'b', dependsOn: ['a'] }),
      node({ id: 'c', dependsOn: ['a', 'b'] }),
    ];
    expect(layerIds(nodes)).toEqual([['a'], ['b'], ['c']]);
  });

  it('puts every independent root on the first layer', () => {
    const nodes = [node({ id: 'x' }), node({ id: 'y' }), node({ id: 'z' })];
    expect(layerIds(nodes)).toEqual([['x', 'y', 'z']]);
  });

  it('handles the empty graph', () => {
    const out = layerNodes([]);
    expect(out.levels).toEqual([]);
    expect(out.unsorted).toEqual([]);
  });

  it('ignores dangling dependsOn refs rather than crashing', () => {
    const nodes = [node({ id: 'a', dependsOn: ['ghost'] }), node({ id: 'b', dependsOn: ['a'] })];
    expect(layerIds(nodes)).toEqual([['a'], ['b']]);
  });

  it('routes cycle-trapped nodes into the unsorted band instead of dropping them', () => {
    // p ↔ q form a 2-cycle (no zero-indegree node), r is a clean root.
    const nodes = [
      node({ id: 'r' }),
      node({ id: 'p', dependsOn: ['q'] }),
      node({ id: 'q', dependsOn: ['p'] }),
    ];
    const out = layerNodes(nodes);
    expect(out.levels.flat().map((n) => n.id)).toEqual(['r']);
    expect(out.unsorted.map((n) => n.id).sort()).toEqual(['p', 'q']);
  });
});

describe('WorkflowGraph — render', () => {
  it('renders one row per layer with each node card', () => {
    const nodes = [
      node({ id: 'a', label: 'Gather' }),
      node({ id: 'b', agent: 'reviewer', dependsOn: ['a'] }),
    ];
    render(<WorkflowGraph nodes={nodes} />);

    // Row 0 holds the root; row 1 holds its dependent.
    const l0 = screen.getByTestId('workflow-level-0');
    const l1 = screen.getByTestId('workflow-level-1');
    expect(within(l0).getByTestId('workflow-node-a')).toHaveTextContent('Gather');
    expect(within(l1).getByTestId('workflow-node-b')).toHaveTextContent('reviewer');
  });

  it('shows the empty state for a graph with no nodes', () => {
    render(<WorkflowGraph nodes={[]} />);
    expect(screen.getByTestId('workflow-graph-empty')).toBeInTheDocument();
  });

  it('badges rerun nodes with the criteria tooltip and overlays the run count + state', () => {
    const nodes = [
      node({
        id: 'gen',
        rerun: { mode: 'self', endCriteria: 'tests pass', maxRuns: 5 },
      }),
    ];
    render(
      <WorkflowGraph nodes={nodes} runState={{ gen: { state: 'looping', runs: 3 } }} />,
    );

    const badge = screen.getByTestId('workflow-node-rerun-gen');
    expect(badge).toHaveTextContent('3');
    expect(badge).toHaveAttribute('title', expect.stringContaining('tests pass'));

    const card = screen.getByTestId('workflow-node-gen');
    expect(card).toHaveAttribute('data-state', 'looping');
    expect(card).toHaveTextContent('looping');
  });
});
