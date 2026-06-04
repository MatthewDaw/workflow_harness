---
completion: 0
---

# Command HQ + claude+ — Project Requirements

**Product goal:** Keep day-to-day agent work and company strategy in one
continuously-synced system — every session a developer runs is captured, made
visible, and reconciled against the company's objectives automatically, with
completion that is verified from real code rather than self-reported.

This document is the high-level **Project Requirements** for the product: the
outcomes management expects, not how they are built. The detailed breakdown
lives under [`docs/plans/`](./plans/command-hq-overview.md); `st6_prd.md` is the
inspiration this product builds on natively.

## Required outcomes

- **Plan mapping.** Every repo's work ladders up from a company objective to a
  repo-level goal to a detailed requirements breakdown. Progress against those
  requirements is audited from the actual code and surfaced as a live completion
  number — not entered by hand. Completion is verifiable, not self-reported.

- **Weekly plan.** A developer can produce a weekly update that pairs "what
  actually got done" (derived from git history) with "what's next," and have
  next week's commitments checked for alignment against the company's fixed
  objectives — visible to managers, never blocking the developer.

- **Claude Code integration.** A single organization-wide catalog of agents and
  skills, with each project opting in to the ones it wants. Live sessions are
  watchable and steerable as they run, so work in progress is observable across
  the company in real time.

- **Claude CLI wrapper.** `claude+` hosts the real `claude` CLI as a persistent
  per-repo daemon that survives terminal close, captures every session, and
  streams it to the cloud — without polluting the developer's personal Claude
  configuration.

- **AgentForge.** A developer can mark a slice of work, capture it, and distill
  it into a reusable, organization-wide agent, so proven workflows become shared
  capability instead of one-off effort.
