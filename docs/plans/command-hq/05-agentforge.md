---
status: active
type: feature
created: 2026-06-02
completion: 93
feature: agentforge
ground-truth: this doc (folds in the former docs/brainstorms/2026-06-02-forge-commands-requirements.md)
origin: ../../ideation/2026-06-02-forge-agent-distillation.md
---

# Feature 5 — AgentForge (capture a unit of work → distill a reusable agent)

Two bundled claude+ commands bracket a slice of work and distill it into a
reusable agent — a short, focused prompt plus the minimal skills it needs —
registered to Command HQ. `/startforge` opens the boundary (lands current work to
main, then branches a worktree); `/endforge` closes it, distills the captured diff
+ session into an agent, and registers it **at the author's scope**. A minimal
Command HQ control then lets a person **promote** the agent org-wide.

## Problem frame

Good work done in one claude+ session is trapped there — the patterns, ordering,
and skill choices that made a phase go well aren't reusable, so the next person
solving a similar phase problem starts from scratch. claude+ already captures
sessions and has an org-wide agent/skill registry (`wrapper/internal/config`), plus
a read-side Forge concept that mines fuzzy history (`packages/backend/src/forge`).
What's missing is a precise way to say "*this* slice is worth keeping," capture it
exactly, and turn it into a phase-specialist agent others can use. Today the cost
is silent: hard-won approaches are re-derived across 175+ users instead of
compounding.

## Key decisions

- **Distiller-first v1.** v1 ships the **capture → distill → register** path
  end-to-end. The Claude Code optimize/refine loop (running candidate prompts in a
  worktree and scoring them) is **deferred to the next increment** so the capture +
  registration plumbing ships and proves out first.
- **`/startforge` lands current work to main, then branches.** It commits + pushes
  the current branch's work to main for a clean baseline, then creates a fresh
  worktree + branch off main. If the work isn't cleanly landable, it **aborts with a
  warning** rather than forcing a messy land.
- **The quality gate is the HQ "promote" step, not the CLI.** `/endforge` registers
  at the **author's own scope** only. Going live for all users is a deliberate human
  action in Command HQ — this keeps a weak forge from hitting the whole org
  automatically (research: unverified auto-published agents can degrade outcomes).
- **`/endforge` classifies but never silently blocks.** It judges whether the diff
  is a coherent feature and warns (with reasons) when it looks incoherent, but the
  author can override and forge anyway.
- **Mint only the skills this agent needs.** Reference existing skills where they
  match what was actually used; create the minimal new skills the agent requires and
  register those alongside it. No broad skill synthesis.
- **Optimize the combined prompt, split afterward** *(applies once the optimizer
  lands)*. The distilled prompt may contain project-specifics; a post-step splits it
  into a reusable, project-agnostic agent prompt and a project-specific provenance
  doc.

## Actors

- **Forging user** — runs `/startforge` and `/endforge`; reviews and confirms the
  forged agent before registration.
- **claude+ daemon / capture** — records the session (transcript, skills used) and
  the diff between the start marker and `/endforge`; the evidence the distiller uses.
- **Command HQ** — the registry the agent + skills publish to, and the web UI where
  agents are viewed and promoted.
- **Promoter** — a person (possibly the forging user) who flips a forged agent
  org-wide in HQ.
- **Consuming users** — org members who receive a promoted agent via the existing
  config-sync drift/pull.

## Flows

- **F1 — Start a forge.** `/startforge [what you're working on]` → land current work
  to main (commit + push); if not cleanly landable, **abort with a warning and
  stop**. Otherwise resolve a name (provided, or generated + user-confirmed), create
  a worktree + branch off main, and record the start marker (base commit).
- **F2 — Do the work.** The user works in the forge branch; claude+ captures the
  session as it already does. No new user action.
- **F3 — End a forge.** `/endforge` → ask whether to generate a reusable agent (no →
  forge closes, no agent). If yes: classify coherence + warn if not (override
  allowed); distill a short focused prompt from the diff + session; curate the
  minimal skill set (reference existing, create needed-but-missing); show the drafted
  agent; on confirm, register the agent + any new skills to Command HQ at the author's scope.
- **F4 — Promote org-wide.** A promoter opens the forged agent in Command HQ,
  views it (prompt + skills), and flips it to org scope. Config-sync drift then makes
  it pullable by all consuming users.

## Requirements

**Capture & boundary**
- **R1** `/startforge` accepts an optional description; when absent, it asks what the user is working on.
- **R2** `/startforge` names the forge: user-provided if given, else a generated name the user can accept or change.
- **R3** `/startforge` lands the current branch's work to main (commit + push) before creating the forge branch.
- **R4** If the work can't be landed cleanly (conflicts / unrelated WIP), `/startforge` aborts with an explanatory warning and makes no changes.
- **R5** `/startforge` creates a fresh worktree + branch off main and records the start marker for the end diff.
- **R6** The evidence for distillation is the branch diff (start marker → `/endforge`) plus the session claude+ already records; no new capture mechanism.

**Distillation & split**
- **R7** `/endforge` first asks whether to generate a reusable agent; "no" closes the forge without producing one.
- **R8** When generating, `/endforge` classifies whether the diff is a coherent feature and warns with reasons when it is not, but allows override.
- **R9** `/endforge` distills a short, focused prompt aimed at the phase problem; conciseness is for clarity, not a token-spend target.
- **R10** The prompt encodes good working principles — foundation first, correct ordering, avoid known dead-ends — drawn partly from standing guidance and partly from the order actually followed in the captured session.
- **R11** v1 registers the distilled prompt **as-is** (it may contain project-specifics), labeled "not yet generalized" in the registry. The project-agnostic split is **deferred** (see Deferred).

**Skills**
- **R12** The agent's skill set is curated, not searched: anchored on the skills actually used, kept minimal.
- **R13** Where a needed capability matches an existing skill, the agent references it rather than duplicating it.
- **R14** Where the agent needs a capability with no existing skill, `/endforge` creates only that skill and registers it alongside the agent.
- **R15** The registered agent references its skills as pointers; agent and skills are distinct registry records.

**Registration & HQ promotion**
- **R16** `/endforge` shows the drafted agent (prompt + curated skills) and registers only on the user's confirmation.
- **R17** `/endforge` registers the agent (and any new skills) to Command HQ at the **author's own scope**; it does not publish org-wide.
- **R18** Registration completes such that the agent's skill pointers resolve (skills present before the agent is consumable).
- **R19** Command HQ provides a control to view a forged agent and **promote it to org scope**.
- **R20** A promoted agent becomes available to all org users through the existing config-sync drift/pull.

## Acceptance examples

- **AE1 (R3, R4)** — uncommitted, conflicting WIP → `/startforge` aborts with a warning and creates no branch/worktree.
- **AE2 (R7)** — answering "no" at `/endforge` registers no agent or skill.
- **AE3 (R8)** — a diff of several unrelated changes → `/endforge` warns it looks incoherent (with reasons) and still proceeds on override.
- **AE4 (R13, R14)** — a session using one existing skill + one capability with no matching skill → the agent references the existing skill and one new skill is created and registered.
- **AE5 (R17, R19, R20)** — a freshly forged agent is visible/usable only at the author's scope until a promoter flips it org-wide in HQ, after which all org users can pull it.

## Builds on existing code

- **Forge backend** `packages/backend/src/forge/` — `embed.ts`, `propose.ts`
  (`aggregateFrequencies`/`splitConfidence`), `search.ts` — reused for the
  draft/curation machinery. The trigger changes from a fuzzy query to a precise
  start/end checkpoint; the pipeline mostly stays.
- **Registry + sync** `wrapper/internal/config/` — scoped (`org | user#uid |
  proj#pid`), content-hashed items, push/pull drift — carries registration and the
  promote-and-pull distribution.
- **Capture** `wrapper/internal/capture/` — already records sessions + skills used,
  so the evidence is observable for free.

## Deferred (later increments)

- **The optimize/refine loop** — run candidate prompts in a worktree, score for
  outcome + process quality, iterate (~3–5 times on the user's Claude subscription),
  plus the **split pass** (reusable prompt vs. project-specific provenance doc). The
  next increment after this v1; rationale + 2026 research in the
  [ideation doc](../../ideation/2026-06-02-forge-agent-distillation.md).
- The "try it out" propagation / trainset loop; agent/skill versioning, hash-pinning,
  CI-on-skill-change, `AGENTS.md` export.
- **At v1, hide/disable** the read-side Forge tab (`wrapper/internal/tui/tab_forge.go`)
  so only `/startforge`…`/endforge` is user-visible; full code removal is a fast-follow.

**Outside this v1's identity:** token-spend / prompt-length optimization; search-based
skill selection or broad skill synthesis beyond what the forged agent needs.

## Open questions

- **Resolved (2026-06-03):** v1 runs with a **single admin user = the promoter**;
  permissions/roles and a transfer-validation gate are out of scope for now.
- **During planning:** exact start-marker shape + how `/endforge` computes the diff;
  where the provenance doc is stored and how it links to the agent record; name/dedup
  when a forged agent collides with an existing one; abandoned-forge cleanup
  (worktree/branch left when `/endforge` is never run).
- **[Deferred — 2026-06-03 review]** Redact secrets (key/token patterns) from the
  diff + transcript before Claude Code distills them, so secrets can't land in a registered prompt.
- **Interaction states to define (design phase):** `/startforge` abort output (what
  the warning lists; partial-land handling) and the HQ promote control (what the
  admin sees before confirming).

## Status

- **Built:** read-side `forge/` backend (embed/propose/search); capture; the
  config-sync registry + org-wide drift/pull.
- **Not built:** `/startforge` (land-to-main + branch + start marker); `/endforge`
  (classify → distill → curate skills → register at author scope); the HQ **promote**
  control. The optimize/refine loop is deferred to the next increment.
