---
status: active
type: overview
created: 2026-06-02
completion: 58
---

> **completion: 58%** — this top-of-`docs/plans/` file represents the requirements
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
| 3 | **Claude Code integration** | Registered agents/skills in HQ, promotion across repo/user/team scopes, and live session watch + steer | [03-claude-code-integration.md](./command-hq/03-claude-code-integration.md) |
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

| Feature | Backend | Frontend | Skill/CLI |
|---|---|---|---|
| 1 Plan mapping | roll-up + GitHub parse exist; **/update-progress + prod-E2E gate + editable requirements: not built** | objectives tree + progress bars exist; **two-tier requirements UI, edit, full-screen, checks: not built (wireframed)** | `/update-progress` skill: **not built** |
| 2 Weekly plan | agent logic + draft/publish endpoints exist | weekly screen is display-only/stubbed | `/weekly-update` skill: **not built** |
| 3 CC integration | scope model + registry + control gateway exist | agents/skills/sessions screens exist | config sync exists |
| 4 CLI wrapper | — | desktop (Wails) app exists | daemon/attach/capture exist |
| 5 AgentForge | `forge/` embed+propose+search exist | — | `/startforge`/`/endforge`: **not built** |

> Legend: "exists" = code present in repo; "not built" = designed/wireframed only.
> Detail files carry per-feature status sections.
