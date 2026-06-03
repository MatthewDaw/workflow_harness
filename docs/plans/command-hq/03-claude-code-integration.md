---
status: active
type: feature
created: 2026-06-02
completion: 93
feature: claude-code-integration
---

# Feature 3 — Claude Code Integration (registry, scopes, live sessions)

How Command HQ shows the agents/skills a developer has registered, how those get
promoted between **repo → user → team** levels, and how active sessions are shown
and steered.

## Registered agents & skills in HQ

Agents and skills the developer uses are registered and visible in HQ as a
browsable catalog, including **bundles** (a named set of skills; bundles can
nest).

*Code today:* `rest/agents.ts`, `rest/skills.ts`; UI `screens/Agents/Agents.tsx`,
`screens/Skills/Skills.tsx`, `screens/Skills/SkillBundle.tsx`. The wrapper keeps
the local `~/.claude` registry in sync (`wrapper/internal/config/`): each agent or
skill is a content-hashed `Item`; drift is push/pull/reconciled.

## Promotion across the three levels

Scope tiers (your "repo / user / team" = code's `project / user / org`):

| Your term | Code tier | Meaning |
|-----------|-----------|---------|
| repo | `project` (`proj#<pid>`) | visible only inside that repo |
| user | `user` (`user#<uid>`) | visible to that user across their repos |
| team | `org` (`org#<id>`) | visible to the whole company |

- **Resolution:** the effective set composes org + user + project; on a name
  collision the **narrowest scope wins** (a repo skill overrides an org skill of
  the same name). *Code:* `packages/shared/src/scope.ts` (`resolveScoped`,
  `SCOPE_PRECEDENCE`).
- **Promote / demote** ("elevate to org", "demote to project") simply **rewrites
  the scope key** of the item. *Code:* `rest/scopeauth.ts`, the
  `changeAgentScope` / `changeSkillScope` mutations in `web/src/api/baseApi.ts`.
- AgentForge ([feature 5](./05-agentforge.md)) registers its distilled agents at
  **org** scope so the whole team gets them.

## Active sessions — show and steer

- **Show:** a live list (live pinned, then newest), per project and globally.
  *Code:* `rest/sessions.ts`, `screens/Sessions/*`, `screens/LiveWatch/LiveWatch.tsx`;
  live data arrives over a WebSocket middleware (`web/src/ws/liveMiddleware.ts`).
- **Steer:** from HQ you can **inject a message / pause / interrupt** a live
  session. Because the daemon holds a persistent *outbound* WebSocket, HQ routes a
  control frame down to it — no inbound ports needed (single laptop; the daemon dials out). Every
  control frame is authorized (the requester must own the target session).
  *Code:* `ws/control.ts`, `rest/sessions.ts` `sendControl`; the **control
  gateway** is described in [platform](./06-platform-architecture.md).

## Open questions

- Bundle assignment granularity (whole bundle vs. individual member to an agent).
- Whether promotion needs review/approval at org scope (supply-chain surface for
  175+ users) — see Forge governance note.

## Status

- **Built:** scope model + resolution; agents/skills/bundles registry + REST;
  config sync drift; sessions list + live watch + control gateway.
- **Polish/open:** promotion UX affordances; org-publish governance.
