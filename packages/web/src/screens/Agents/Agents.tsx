import type { Agent } from '@harness/shared';
import { SCOPE_TIERS, type ScopeTier } from '@harness/shared';
import { useGetAgentsQuery } from '../../api/baseApi.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const SCOPE_LABEL: Record<ScopeTier, string> = {
  org: '◆ Org · all users',
  user: '● My global · all my repos',
  project: '▪ Project',
};

/** Agents registry (U24): grouped by scope tier (org → user → project). */
export function Agents() {
  const { data, isLoading } = useGetAgentsQuery();
  const agents = data ?? [];

  return (
    <div className="hq-pad" data-testid="agents-screen">
      <ScreenHeader
        title="Agents (scoped library)"
        subtitle="Three scopes, narrowest to broadest: project → my global → org."
      />
      {isLoading && <div className="text-mut">Loading agents…</div>}
      {/* Render broadest → narrowest, matching the wireframe's top-down layout. */}
      {[...SCOPE_TIERS].reverse().map((tier) => {
        const inTier = agents.filter((a) => a.scope.tier === tier);
        if (inTier.length === 0) return null;
        return (
          <section key={tier} className="mb-4">
            <div className="my-1.5 text-xs font-medium text-mut">{SCOPE_LABEL[tier]}</div>
            <div className="grid grid-cols-3 gap-3.5">
              {inTier.map((a) => (
                <AgentCard key={`${tier}-${a.name}`} agent={a} />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function AgentCard({ agent }: { agent: Agent }) {
  return (
    <div className="hq-box bg-paper">
      <div className="flex justify-between">
        <b>{agent.name}</b>
        <Pill>{agent.model}</Pill>
      </div>
      {agent.prompt && <div className="my-1.5 text-xs text-mut">{agent.prompt}</div>}
      <div className="my-1.5">
        {agent.skills.map((s) => (
          <Pill key={s} variant="skill" className="mr-1">
            {s}
          </Pill>
        ))}
      </div>
      <div className="mt-1.5 text-[11px] text-faint">scope: {agent.scope.tier}</div>
    </div>
  );
}
