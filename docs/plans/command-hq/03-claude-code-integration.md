---
status: active
type: feature
created: 2026-06-02
completion: 93
feature: claude-code-integration
---

# Feature 3 — Claude Code Integration (org catalog, per-project opt-in, live sessions)

How Command HQ shows the agents/skills available to a developer as a single
**org catalog**, how each project **opts in** to the skills/agents it wants, and
how active sessions are shown and steered.

> **Model change (2026-06-03):** the old 3-tier scope (repo / user / team) with
> narrowest-wins resolution and scope promotion/demotion is **retired** for skills
> and agents. Skills and agents are now a single **org-scoped catalog**; projects
> turn individual items on via `enabledSkills` / `enabledAgents`. See the full
> [org-catalog design spec](../../superpowers/specs/2026-06-03-org-catalog-scope-collapse-design.md).

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

## Org catalog + per-project opt-in

There are no scope tiers for skills and agents anymore. Every skill and agent is
**org-scoped** (`scope: { tier: "org", id: "<org>" }`) and lives in one shared
**org catalog**. A project then chooses which of those items it wants:

- **One catalog.** `GET /skills` and `GET /agents` return the whole org catalog —
  no `?project` param, no `resolveScoped`. *Code:* `rest/skills.ts`, `rest/agents.ts`;
  keys forced to `SCOPE#org#<org>` via `orgScope(org)` (`db/keys.ts`).
- **Admin-gated writes.** `POST/PUT/DELETE /skills` and `/agents` are org-only and
  admin-gated; the server forces `scope = orgScope(principal.org)` and stamps
  `createdBy: { userId, name }` from the authenticated principal on create.
- **Per-project opt-in.** A `Project` carries `enabledSkills: string[]` and
  `enabledAgents: string[]` (skill/agent names; default `[]`). A project owner or
  org admin toggles them:
  - `POST|DELETE /projects/:id/skills/:name` — add/remove a skill directly (no agent
    needed).
  - `POST|DELETE /projects/:id/agents/:name` — enable/disable an agent. **Enabling an
    agent UNIONS that agent's declared `skills` into `enabledSkills`** (de-duped;
    bundles among them flattened transitively to leaf member skills), so the agent's
    dependencies come along automatically. Disabling an agent removes it from
    `enabledAgents` but does **not** prune `enabledSkills` (a skill may also be
    enabled directly or brought by another agent).
- **No promotion / demotion.** The scope-change endpoint
  (`changeAgentScope`/`changeSkillScope`) is **retired** (returns `410 Gone` / route
  removed); there is no tier to elevate or demote between.
- AgentForge ([feature 5](./05-agentforge.md)) registers its distilled agents into
  the **org catalog**; an admin then enables them per project.

A connected repo materializes **only the linked project's** enabled sets into
`~/.claude+`: the wrapper fetches the full org catalog, then keeps just the items
named in that project's `enabledSkills` / `enabledAgents` — see
[04](./04-claude-cli-wrapper.md) (isolated config root). Because an agent's skills were union-added into
`enabledSkills` when it was enabled, no extra expansion is needed at sync time.

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
