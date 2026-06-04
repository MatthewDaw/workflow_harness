---
status: active
type: overview
created: 2026-06-02
completion: 90
---

> **completion: 90%** — this top-of-`docs/plans/` file represents the requirements
> for the **whole project**. The website reads this number and displays it on both
> the Command HQ high-level **Project Requirements** bar and the **Detailed
> Requirements** root. Every doc in this folder carries its own `completion:` at the
> top; `/update-progress` maintains them.

# Command HQ + claude+ — Feature Overview

**This is the single top-level map of what we're building, and the canonical,
self-contained spec for the project.** Each feature has a detail file under
[`command-hq/`](./command-hq/); the durable technical decisions (KTD1–12, data
model, event schema) live in [06-platform-architecture.md](./command-hq/06-platform-architecture.md).
Read this file first for direction.

## What this product is (one paragraph)

`claude+` is a terminal wrapper that hosts the real `claude` CLI as a persistent
per-repo daemon and streams every session to the cloud. **Command HQ** is the web
app that turns that stream into a live, strategy-aligned view of all work:
company objectives, per-repo requirements, agents/skills, live sessions,
and weekly reconciliation. The thesis: **day-to-day agent work and company
strategy are the same system, kept in sync automatically.**

**Live deployment (prod).** Command HQ is deployed and serving:
API `https://l5edwucexb.execute-api.us-east-1.amazonaws.com`, WebSocket
`wss://fgxq7ezbl1.execute-api.us-east-1.amazonaws.com/prod`, site
`https://d13sqkbwzqe38l.cloudfront.net`, Cognito pool `us-east-1_HqxqXElfd`,
default org `personasearch`. New accounts come from **Google federated sign-in**
(email/password self-signup is disabled); a Cognito pre-token-generation Lambda
defaults a missing `custom:org` to `personasearch` so a fresh account is
authorized everywhere (`infra/lib/auth-stack.ts`).

## Relationship to `st6_prd.md`

`st6_prd.md` is the **inspiration**, not a literal spec. It describes a weekly
planning tool that forces every weekly commitment to ladder up to strategy
(RCDO), with a reconciliation of *planned vs. actual* and manager visibility.
Command HQ **builds that idea in natively** (the Weekly Update + Objectives
roll-up) and **extends it with the harness**: the reconciliation's "actual" is
real captured sessions/commits, completion is *verified* (not self-reported), and
agents that do the work can be distilled and reused. We are not bound by st6's
React/Spring/Module-Federation stack.

## The five features

| # | Feature | One-liner | Detail |
|---|---------|-----------|--------|
| 1 | **Plan mapping** | Company goal → repo goal → detailed breakdown, with a tool that auto-audits progress and verifies completion | [01-plan-mapping.md](./command-hq/01-plan-mapping.md) |
| 2 | **Weekly plan** | A `/weekly-update` skill that interviews you on next-week goals, then auto-writes "what got done" (from git) vs "what's next" | [02-weekly-update.md](./command-hq/02-weekly-update.md) |
| 3 | **Claude Code integration** | A single org catalog of agents/skills in HQ, per-project opt-in (`enabledSkills`/`enabledAgents`), and live session watch + steer | [03-claude-code-integration.md](./command-hq/03-claude-code-integration.md) |
| 4 | **Claude CLI wrapper** | `claude+` wraps the real `claude` CLI as a persistent, capturable daemon (survives terminal close; single-laptop) | [04-claude-cli-wrapper.md](./command-hq/04-claude-cli-wrapper.md) |
| 5 | **AgentForge** | Mark a slice of work, capture it, and distill it into a reusable org-wide agent via prompt gradient-descent | [05-agentforge.md](./command-hq/05-agentforge.md) |

Plus the substrate everything rides on: **Platform & Architecture** —
[06-platform-architecture.md](./command-hq/06-platform-architecture.md).

## How the pieces fit together

```
        claude+ (wrapper, feature 4)
        runs sessions in a repo, captures events
                       │  outbound WS
                       ▼
        Command HQ (serverless platform substrate)
                       │
   ┌───────────────────────────────────┬───────────────────────┐
   ▼                                   ▼
 Sessions (watch+steer, feature 3)   Detailed Requirements
 captured by claude+                 = repo docs/plans/ on GitHub
                                     (feature 1, HQ renders read-only)
                                          │ /update-progress (client-side) computes
                                          │ + pushes completion: % to GitHub
                                          │ (HQ reads it; HQ never writes)
                                          ▼
                                     Project Requirements
                                     = single HQ-owned .md (feature 1, high level)
                                     progress bar = top docs/plans completion: %
                                                            │ rolls up
                                                            ▼
                                                   Company Objectives (RCDO)
                                                            ▲
   /weekly-update (feature 2): git history since last report = "actual",
   reconciled against the plan; conformity-checked against these objectives.

   Source of truth: Project Requirements → Command HQ · Detailed Requirements + progress (completion:) → GitHub. HQ reads, never writes.
   Agents/Skills registry (feature 3) supplies the sessions;
   AgentForge (feature 5) mints new registry agents from captured sessions.
```

## Build status at a glance

The **new-model migration**
([2026-06-03 plan](./2026-06-03-001-feat-command-hq-new-model-migration-plan.md),
units U1–U22) is **landed**: tickets are removed end-to-end, GitHub `completion:`
frontmatter is the progress source of truth, the two-tier requirements UI ships,
the client skills exist under `.claude/skills/`, and the infra footguns
(SearchStack, device-token secret, Go CI, CORS) are fixed. The table below
reflects current reality.

| Feature | Backend | Frontend | Skill/CLI |
|---|---|---|---|
| 1 Plan mapping | roll-up re-pointed off tickets onto stored `progressPct`; GitHub `completion:` frontmatter read/store (`github/history.ts`, `rest/projects.ts`); docs-tree REST; Definition-of-Done REST (`rest/dod.ts`, advisory). **Prod-E2E gate still deferred.** | objectives tree + bars; two-tier requirements UI built (`ProjectRequirements.tsx`, `DetailedRequirements.tsx`, `MarkdownView`); editable Project Requirements | `/update-progress` skill **built** |
| 2 Weekly plan | weekly = store/serve a client-posted report (`rest/weekly.ts`); ticket-based `assembleWeekly`/`align.ts` removed; `conformityScore` round-trips | Weekly screen renders posted report (`ProjectWeekly.tsx`) | `/weekly-update` skill **built** |
| 3 CC integration | org catalog (skills/agents org-only + `createdBy`) + per-project opt-in (`enabledSkills`/`enabledAgents`, agent-enable skill union) + control gateway (`ws/control.ts`); project opt-in REST wired; scope-change endpoint retired | agents/skills (flat org catalog, author filter)/sessions/live-watch screens; per-project Skills + Agents opt-in toggles wired | config sync (remote half + drift) materializes the linked project's enabled set |
| 4 CLI wrapper | — | desktop (Wails) app exists; xterm UX (visible cursor, scrollback, double-click rename) | daemon/attach/capture; LLM auto-titles (`internal/title/`); hook receiver; **isolated `~/.claude+` config root** |
| 5 AgentForge | fuzzy `forge/` backend **removed**; SearchStack dropped from synth | distilled agents land in the org catalog; an admin enables them per project | `/startforge`/`/endforge` skills **built** (distiller-first) |

> Legend: "built" = code present + tested in repo; "deferred" = explicitly parked
> for a later increment. Detail files carry per-feature status sections.

### Isolated config root + bundled-skill seed (cross-cutting, built)

Two subsystems underpin the skills story and weren't in the original map:

- **Isolated `~/.claude+` config root.** claude+ launches its inner Claude with
  `CLAUDE_CONFIG_DIR=~/.claude+` — a **stable** directory seeded once from
  `~/.claude` (auth/settings/MCP) — so product-bundled skills and claude+ session
  history never pollute the user's personal `~/.claude`. The capture tailer follows
  the same root, and the transcript directory slug replaces **every**
  non-alphanumeric char with `-` to match Claude Code's project-hash naming.
  *Code:* `wrapper/internal/config/overlay.go`, `pty/session.go`,
  `capture/parse.go` (`projectHash`/`slugifyPath`).
- **HQ org-catalog skill bundle seed.** The repo's `.claude/skills/` set is seeded
  into HQ at **org scope** (`personasearch`), grouped as the **`command-hq-starter`**
  bundle, with `createdBy: { userId: 'system', name: 'system' }` stamped on each
  record. The Skills tab shows them on a fresh deploy with no device connected; this
  is the source of truth for "skills visible in HQ out of the box." A project then
  **opts in** to the ones it wants (or enables an agent that brings them) via its
  `enabledSkills`/`enabledAgents` — the seed populates the catalog, not any single
  project. *Code:* `packages/backend/src/seed/skills.ts`,
  `infra/scripts/seed-skills.mjs`, `.github/workflows/seed-skills.yml` + the deploy
  seed step.

### In progress / next (NOT yet on this branch — do not treat as done)

Being built in parallel; the docs anticipate them but they are not merged here:

- Live **per-session activity feed** in the watch view.
- **Session name + first-prompt columns** in the Sessions tab.
- A **`UserPromptSubmit`** hook that auto-renames a session on the first prompt and
  pushes the rename to the attached CLI tab.
- A **daemon protocol-version bump** so rebuilds auto-replace a stale daemon.
- **Heartbeat + ~60s freshness window** so power-loss/killed daemons drop off HQ's
  live list.
- **Overview tab removed** (project default tab → Project Requirements).
- **Skills tab overhaul** (searchable picker with author filter, a per-project Skills
  subtab for opt-in, hide bundle members by default with a toggle, a new
  `create-hq-skill` skill).
- **Delete agents/skills**, with the `command-hq-starter` bundle protected
  server-side.
