import { useMemo } from 'react';
import type { ObjectiveNode } from '@harness/shared';
import { useGetObjectivesQuery } from '../../api/baseApi.js';
import { Bar, Pill, ScreenHeader } from '../../components/primitives.js';

/** Group nodes by parent so the RCDO tree can render recursively. */
function childrenOf(nodes: ObjectiveNode[], parentId: string | undefined): ObjectiveNode[] {
  return nodes.filter((n) => n.parentId === parentId);
}

function Node({ node, all }: { node: ObjectiveNode; all: ObjectiveNode[] }) {
  const kids = childrenOf(all, node.id);
  const label =
    node.level === 'rally_cry'
      ? '◆ Rally Cry'
      : node.level === 'defining_objective'
        ? 'Defining Objective'
        : node.level === 'outcome'
          ? 'Outcome'
          : 'Supporting Outcome';
  return (
    <div className="my-2.5">
      <div className="flex items-center justify-between">
        <span>
          <Pill className="mr-2">{label}</Pill>
          <span className="text-sm">{node.title}</span>
        </span>
        <span className="text-xs text-faint">{node.pct ?? 0}%</span>
      </div>
      <div className="my-2">
        <Bar pct={node.pct ?? 0} />
      </div>
      {kids.length > 0 && (
        <div className="ml-3.5 border-l border-line2 pl-3.5">
          {kids.map((k) => (
            <Node key={k.id} node={k} all={all} />
          ))}
        </div>
      )}
    </div>
  );
}

export function Objectives() {
  const { data, isLoading, isError } = useGetObjectivesQuery();
  const roots = useMemo(() => childrenOf(data ?? [], undefined), [data]);

  return (
    <div className="hq-pad" data-testid="objectives-screen">
      <ScreenHeader
        title="Company Objectives (RCDO)"
        subtitle="Rally Cries → Defining Objectives → Outcomes → Supporting Outcomes. Roll-ups are computed bottom-up from linked work."
      />
      {isLoading && <div className="text-mut">Loading objectives…</div>}
      {isError && (
        <div className="hq-note" role="alert">
          Could not load objectives. Showing an empty tree.
        </div>
      )}
      {!isLoading && roots.length === 0 && (
        <div className="hq-box text-mut">No objectives yet. Add a Rally Cry to get started.</div>
      )}
      {roots.map((r) => (
        <div key={r.id} className="hq-box mb-3 border-l-[3px] border-l-[#8a6fb5] bg-paper">
          <Node node={r} all={data ?? []} />
        </div>
      ))}
    </div>
  );
}
