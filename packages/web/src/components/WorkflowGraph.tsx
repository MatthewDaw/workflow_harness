import { useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { WorkflowNode } from '@harness/shared';

/**
 * The per-node run state overlaid on the DAG while a workflow run is active
 * (M5). `state` colors the card; `runs` is shown on rerun nodes.
 */
export type WorkflowNodeRunState = {
  state: 'pending' | 'running' | 'looping' | 'done' | 'failed';
  runs?: number;
};

/**
 * The lightweight custom layered DAG view. It topologically layers the nodes by
 * their `dependsOn` edges (Kahn's algorithm), renders each layer as a horizontal
 * row of node cards, and overlays an absolutely-positioned SVG that draws the
 * `dependsOn` connectors plus the distinct dashed `declared-by` control edges.
 * Pass `runState` (M5) to color cards by run state and show per-node run counts.
 *
 * This is the SINGLE rendering of a workflow graph — the global Workflows
 * catalog modal, the editor live preview, and a project's enabled-workflow
 * cards all render through it, so they read identically.
 */
export function WorkflowGraph({
  nodes,
  runState,
  onNodeClick,
}: {
  nodes: WorkflowNode[];
  runState?: Record<string, WorkflowNodeRunState>;
  onNodeClick?: (id: string) => void;
}) {
  // Layer the DAG. `levels` is an ordered list of rows (each a list of nodes);
  // any node left unsorted by a stray cycle lands in a final "unsorted" band so
  // we render rather than crash.
  const { levels, unsorted } = useMemo(() => layerNodes(nodes), [nodes]);

  // Connector overlay: we measure card positions after layout and draw SVG lines
  // from each parent card's bottom edge to its child card's top edge. Refs are
  // keyed by node id; a layout effect recomputes the line geometry whenever the
  // node set or container size changes.
  const containerRef = useRef<HTMLDivElement | null>(null);
  const cardRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const [edges, setEdges] = useState<DrawnEdge[]>([]);

  // Index of every node id → its node, so connectors can resolve `dependsOn` and
  // `declaredBy` references regardless of which layer they landed in.
  const byId = useMemo(() => {
    const m = new Map<string, WorkflowNode>();
    for (const n of nodes) m.set(n.id, n);
    return m;
  }, [nodes]);

  useLayoutEffect(() => {
    function measure() {
      const container = containerRef.current;
      if (!container) {
        setEdges([]);
        return;
      }
      const base = container.getBoundingClientRect();
      const drawn: DrawnEdge[] = [];
      const center = (r: DOMRect) => ({
        x: r.left - base.left + r.width / 2,
        top: r.top - base.top,
        bottom: r.top - base.top + r.height,
      });
      for (const node of nodes) {
        const child = cardRefs.current.get(node.id);
        if (!child) continue;
        const c = center(child.getBoundingClientRect());
        // Solid DAG edges: a parent (dependsOn) feeds this node from above.
        for (const dep of node.dependsOn) {
          if (!byId.has(dep)) continue;
          const parent = cardRefs.current.get(dep);
          if (!parent) continue;
          const p = center(parent.getBoundingClientRect());
          drawn.push({
            key: `dep:${dep}->${node.id}`,
            x1: p.x,
            y1: p.bottom,
            x2: c.x,
            y2: c.top,
            kind: 'dependsOn',
          });
        }
        // Distinct dashed control edge: the checker node declares this one done.
        if (node.rerun?.mode === 'declared-by' && node.rerun.declaredBy) {
          const checker = cardRefs.current.get(node.rerun.declaredBy);
          if (checker && byId.has(node.rerun.declaredBy)) {
            const ch = center(checker.getBoundingClientRect());
            drawn.push({
              key: `dcl:${node.rerun.declaredBy}->${node.id}`,
              x1: ch.x,
              y1: ch.top,
              x2: c.x,
              y2: c.top,
              kind: 'declaredBy',
            });
          }
        }
      }
      setEdges(drawn);
    }

    measure();
    // Recompute on resize so connectors track reflow (no heavy deps).
    const ro =
      typeof ResizeObserver !== 'undefined'
        ? new ResizeObserver(() => measure())
        : null;
    if (ro && containerRef.current) ro.observe(containerRef.current);
    window.addEventListener('resize', measure);
    return () => {
      ro?.disconnect();
      window.removeEventListener('resize', measure);
    };
  }, [nodes, byId, levels, unsorted]);

  const allRows = unsorted.length > 0 ? [...levels, unsorted] : levels;

  if (nodes.length === 0) {
    return (
      <div className="hq-box text-mut" data-testid="workflow-graph-empty">
        No nodes yet — add an agent node to start the DAG.
      </div>
    );
  }

  return (
    <div ref={containerRef} className="relative" data-testid="workflow-graph">
      {/* Connector overlay sits behind the cards and ignores pointer events so
          the cards stay clickable. */}
      <svg
        className="pointer-events-none absolute inset-0 h-full w-full"
        data-testid="workflow-graph-edges"
        aria-hidden="true"
      >
        {edges.map((e) => (
          <line
            key={e.key}
            x1={e.x1}
            y1={e.y1}
            x2={e.x2}
            y2={e.y2}
            stroke={e.kind === 'declaredBy' ? '#b5402f' : '#b6a47a'}
            strokeWidth={e.kind === 'declaredBy' ? 1.5 : 1}
            strokeDasharray={e.kind === 'declaredBy' ? '4 3' : undefined}
            data-testid={`workflow-edge-${e.kind}`}
          />
        ))}
      </svg>

      <div className="relative flex flex-col gap-6">
        {allRows.map((row, i) => {
          const isUnsorted = unsorted.length > 0 && i === allRows.length - 1;
          return (
            <div
              key={isUnsorted ? 'unsorted' : `level-${i}`}
              className="flex flex-wrap justify-center gap-3.5"
              data-testid={isUnsorted ? 'workflow-band-unsorted' : `workflow-level-${i}`}
            >
              {row.map((node) => (
                <NodeCard
                  key={node.id}
                  node={node}
                  run={runState?.[node.id]}
                  onClick={onNodeClick}
                  registerRef={(el) => {
                    if (el) cardRefs.current.set(node.id, el);
                    else cardRefs.current.delete(node.id);
                  }}
                />
              ))}
            </div>
          );
        })}
      </div>
    </div>
  );
}

/** A single node card; colored by run state when a run is overlaid. */
function NodeCard({
  node,
  run,
  onClick,
  registerRef,
}: {
  node: WorkflowNode;
  run?: WorkflowNodeRunState;
  onClick?: (id: string) => void;
  registerRef: (el: HTMLDivElement | null) => void;
}) {
  const title = node.label || node.agent;
  const rerunTitle = node.rerun ? rerunSummary(node.rerun) : undefined;
  const tone = run ? STATE_TONE[run.state] : undefined;

  return (
    <div
      ref={registerRef}
      className={`hq-box relative z-10 min-w-[150px] max-w-[220px] ${
        onClick ? 'cursor-pointer' : ''
      }`}
      style={tone ? { borderColor: tone.border, background: tone.bg } : undefined}
      data-testid={`workflow-node-${node.id}`}
      data-state={run?.state}
      onClick={onClick ? () => onClick(node.id) : undefined}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-[13px] font-semibold text-ink" title={title}>
            {title}
          </div>
          <div className="truncate font-mono text-[11px] text-mut" title={node.agent}>
            {node.agent}
          </div>
        </div>
        {node.rerun && (
          <span
            className="hq-pill hq-pill-skill shrink-0"
            title={rerunTitle}
            data-testid={`workflow-node-rerun-${node.id}`}
          >
            ↻{run?.runs != null ? ` ${run.runs}` : ''}
          </span>
        )}
      </div>
      {run && (
        <div className="mt-1.5 text-[11px] uppercase tracking-wide text-mut">
          {run.state}
        </div>
      )}
    </div>
  );
}

/** Per-state card tones (token hexes from the brass/parchment palette). */
const STATE_TONE: Record<
  WorkflowNodeRunState['state'],
  { border: string; bg: string }
> = {
  pending: { border: '#b6a47a', bg: '#efe4c8' },
  running: { border: '#b1842f', bg: '#f1e3c2' },
  looping: { border: '#8f6921', bg: '#f1e3c2' },
  done: { border: '#5b6235', bg: '#e6e8d2' },
  failed: { border: '#b5402f', bg: '#f3ddd6' },
};

/** Human-readable rerun tooltip: end-criteria (self) or checker (declared-by). */
function rerunSummary(rerun: NonNullable<WorkflowNode['rerun']>): string {
  if (rerun.mode === 'declared-by') {
    return `Reruns until '${rerun.declaredBy ?? '?'}' declares it done (max ${rerun.maxRuns})`;
  }
  const crit = rerun.endCriteria ? `: ${rerun.endCriteria}` : '';
  return `Reruns until end-criteria met${crit} (max ${rerun.maxRuns})`;
}

/** A measured connector to draw in the SVG overlay. */
type DrawnEdge = {
  key: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  kind: 'dependsOn' | 'declaredBy';
};

/**
 * Topologically layer the nodes by their `dependsOn` edges using Kahn's
 * algorithm, computing each node's layer as the LONGEST path from a root so a
 * node always renders below its deepest parent. Returns the ordered layers plus
 * any nodes a stray cycle left unsorted (rendered in a final band rather than
 * dropped). Handles the empty graph and self/dangling edges gracefully.
 */
export function layerNodes(nodes: WorkflowNode[]): {
  levels: WorkflowNode[][];
  unsorted: WorkflowNode[];
} {
  const byId = new Map<string, WorkflowNode>();
  for (const n of nodes) byId.set(n.id, n);

  const indegree = new Map<string, number>();
  const adj = new Map<string, string[]>();
  for (const n of nodes) {
    indegree.set(n.id, 0);
    adj.set(n.id, []);
  }
  for (const n of nodes) {
    for (const dep of n.dependsOn) {
      // Only count real edges (dangling refs/self-loops to absent ids ignored).
      if (!byId.has(dep) || dep === n.id) continue;
      adj.get(dep)!.push(n.id);
      indegree.set(n.id, (indegree.get(n.id) ?? 0) + 1);
    }
  }

  // Kahn's algorithm, tracking each node's layer as 1 + max(parent layer).
  const layer = new Map<string, number>();
  let queue: string[] = [];
  for (const [id, deg] of indegree) {
    if (deg === 0) {
      layer.set(id, 0);
      queue.push(id);
    }
  }
  let processed = 0;
  while (queue.length > 0) {
    const next: string[] = [];
    for (const id of queue) {
      processed += 1;
      const l = layer.get(id) ?? 0;
      for (const child of adj.get(id) ?? []) {
        layer.set(child, Math.max(layer.get(child) ?? 0, l + 1));
        const deg = (indegree.get(child) ?? 0) - 1;
        indegree.set(child, deg);
        if (deg === 0) next.push(child);
      }
    }
    queue = next;
  }

  // Group the layered nodes into rows, preserving input order within a row.
  const maxLayer = layer.size === 0 ? -1 : Math.max(...layer.values());
  const levels: WorkflowNode[][] = [];
  for (let i = 0; i <= maxLayer; i++) levels.push([]);
  const unsorted: WorkflowNode[] = [];
  for (const n of nodes) {
    const l = layer.get(n.id);
    if (l === undefined) unsorted.push(n); // left over by a cycle
    else levels[l]!.push(n);
  }

  // Drop any empty trailing layers (defensive; shouldn't happen).
  return { levels: levels.filter((row) => row.length > 0), unsorted };
}
