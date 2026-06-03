import { useState } from 'react';
import { Link } from 'react-router-dom';
import type { Agent, ScopeRef, ScopeTier } from '@harness/shared';
import { SCOPE_TIERS, SCOPE_PRECEDENCE } from '@harness/shared';
import { useGetAgentsQuery, useChangeAgentScopeMutation } from '../../api/baseApi.js';
import { useAuth } from '../../auth/AuthProvider.js';
import { Pill, ScreenHeader } from '../../components/primitives.js';

const SCOPE_LABEL: Record<ScopeTier, string> = {
  org: '◆ Org · all users',
  user: '● My global · all my repos',
  project: '▪ Project',
};

/**
 * Build the destination scope for an elevate/demote. Org targets the viewer's
 * org id, user targets the viewer's user id; project keeps the agent's existing
 * project id (an agent at a project tier already carries it).
 */
function targetScope(tier: ScopeTier, current: ScopeRef, ctx: { org: string; userId: string }): ScopeRef {
  if (tier === 'org') return { tier: 'org', id: ctx.org };
  if (tier === 'user') return { tier: 'user', id: ctx.userId };
  return { tier: 'project', id: current.tier === 'project' ? current.id : ctx.userId };
}

/** Agents registry (U17/U24): grouped by scope tier with elevate/demote + promote. */
export function Agents() {
  const { data, isLoading } = useGetAgentsQuery();
  const { user } = useAuth();
  const agents = data ?? [];
  // v1 single-admin model: no role system — the current user is treated as the
  // admin, so the org-promote affordance is always exposed when signed in.
  const ctx = { org: user?.org ?? '', userId: user?.userId ?? '' };
  const isAdmin = Boolean(user);

  return (
    <div className="hq-pad" data-testid="agents-screen">
      <div className="flex items-start justify-between">
        <ScreenHeader
          title="Agents (scoped library)"
          subtitle="Three scopes, narrowest to broadest: project → my global → org."
        />
        <Link to="/agents/new" className="hq-btn hq-btn-pri no-underline" data-testid="new-agent">
          + New agent
        </Link>
      </div>
      {isLoading && <div className="text-mut">Loading agents…</div>}
      {/* Render broadest → narrowest, matching the wireframe's top-down layout. */}
      {[...SCOPE_TIERS].reverse().map((tier) => {
        const inTier = agents.filter((a) => a.scope.tier === tier);
        if (inTier.length === 0) return null;
        return (
          <section key={tier} className="mb-4" data-testid={`scope-group-${tier}`}>
            <div className="my-1.5 text-xs font-medium text-mut">{SCOPE_LABEL[tier]}</div>
            <div className="grid grid-cols-3 gap-3.5">
              {inTier.map((a) => (
                <AgentCard
                  key={`${tier}-${a.name}`}
                  agent={a}
                  ctx={ctx}
                  isAdmin={isAdmin}
                />
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function AgentCard({
  agent,
  ctx,
  isAdmin,
}: {
  agent: Agent;
  ctx: { org: string; userId: string };
  isAdmin: boolean;
}) {
  const [changeScope, { isLoading }] = useChangeAgentScopeMutation();
  const tier = agent.scope.tier;

  const move = (to: ScopeTier) => {
    if (to === tier) return;
    changeScope({ name: agent.name, from: agent.scope, to: targetScope(to, agent.scope, ctx) });
  };

  return (
    <div className="hq-box bg-paper" data-testid={`agent-card-${agent.name}`}>
      <div className="flex justify-between">
        <Link to={`/agents/${encodeURIComponent(agent.name)}/edit`} className="text-ink no-underline">
          <b>{agent.name}</b>
        </Link>
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
      <div className="mt-1.5 flex items-center justify-between">
        <span className="text-[11px] text-faint">scope: {tier}</span>
        <span className="flex items-center gap-1">
          <label className="sr-only" htmlFor={`scope-${agent.name}`}>
            Scope for {agent.name}
          </label>
          <select
            id={`scope-${agent.name}`}
            className="hq-btn"
            data-testid={`scope-picker-${agent.name}`}
            value={tier}
            disabled={isLoading}
            onChange={(e) => move(e.target.value as ScopeTier)}
          >
            {SCOPE_TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          {/* Single-admin promote: elevate straight to org scope (U15). Gated to
              the admin and hidden once already at org. */}
          {isAdmin && SCOPE_PRECEDENCE[tier] > SCOPE_PRECEDENCE.org && (
            <button
              type="button"
              className="hq-btn hq-btn-pri"
              data-testid={`promote-${agent.name}`}
              disabled={isLoading}
              onClick={() => move('org')}
            >
              ↑ elevate to org
            </button>
          )}
        </span>
      </div>
    </div>
  );
}
