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
the local registry in sync (`wrapper/internal/config/`): each agent or skill is a
content-hashed `Item`; drift is push/pull/reconciled. **As built, the wrapper reads
the union of `~/.claude` (the user's own) and the isolated `~/.claude+`
(product-bundled) when computing drift, but only ever materializes pulled items
into `~/.claude+`** — see [04](./04-claude-cli-wrapper.md) (isolated config root).
HQ shows the product-bundled skills out of the box via the org-scope
`command-hq-starter` seed (`packages/backend/src/seed/skills.ts`,
`infra/scripts/seed-skills.mjs`), independent of any connected device.

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
- **Steer:** from HQ you can **inject a message / pause / interrupt / shut down /
  kill** a live session. Because the daemon holds a persistent *outbound*
  WebSocket, HQ routes a control frame down to it — no inbound ports needed (single
  laptop; the daemon dials out). Every control frame is authorized (the requester
  must own the target session).
  *Code:* `ws/control.ts`, `rest/sessions.ts` `sendControl`; on the daemon the
  inbound control `payload` is decoded as an **object** (`transport/wsclient.go` →
  `msg.Payload.Text`) and applied by `transport/control.go`'s `Receiver`, which
  supports `inject`/`pause`/`interrupt`/`shutdown` (SIGTERM + force fallback)/`kill`.
  The **control gateway** is described in [platform](./06-platform-architecture.md).

## Open questions

- Bundle assignment granularity (whole bundle vs. individual member to an agent).
- Whether promotion needs review/approval at org scope (supply-chain surface for
  175+ users) — see Forge governance note.

## Status

- **Built:** scope model + resolution; agents/skills/bundles registry + REST;
  config sync drift (over the `~/.claude` ∪ `~/.claude+` union); sessions list +
  live watch + control gateway (inject/pause/interrupt/shutdown/kill); steer +
  scope/bundle controls wired in the web UI; org-scope bundled-skill seed.
- **In progress (not on this branch):** session name + first-prompt columns on the
  Sessions tab; live per-session activity feed in the watch view; a Skills-tab
  overhaul (searchable picker, hide bundle members by default, a `create-hq-skill`
  skill); delete agents/skills with the `command-hq-starter` bundle protected
  server-side. See the overview's "In progress / next".
- **Polish/open:** org-publish governance (review/approval at org scope).
