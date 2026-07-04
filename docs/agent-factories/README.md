# Agent Factories

Working ideas for **agent factories** — systems that decompose into a set of focused
coding/research agents, orchestrated together for specific workflows.

> **Start here: [DEFENSE.md](./DEFENSE.md)** — the presentation-grade statement of the
> v2 architecture (Define → Converge → Compound), why it's shaped that way, and the
> answers to the hard questions. The files below are the detailed designs it composes.
> Slide deck: [defense-slides.html](./defense-slides.html) (open in a browser; ← → to
> navigate, Ctrl+P for PDF).

> Note: these docs intentionally largely ignore the current codebase. They are a
> greenfield exploration of newer ideas. Details are rough and will be fleshed out.

## Core premise

An agent factory mostly decomposes into:

1. A pool of **agent groups** — each group owns one unit of work and may contain
   specialized **sub-agents** for parts of that work.
2. **Orchestration** that wires groups together into end-to-end workflows.

## What every agent *is* (definitional)

Every agent in this system — each group **and** each sub-agent — is definitionally:

> **a prompt command (a slash-command / skill entry point) + a set of skills associated
> with it.**

There is no other kind of agent here. A "group" is a prompt command whose skills include
the ability to dispatch its sub-agents; a "sub-agent" is itself a prompt command + skills.
This is the single unit the whole factory is built from, and it's what the
[self-improvement](./self-improvement/) loop edits and evolves over time.

Practical consequences:
- Defining a new agent = writing a prompt command and listing the skills it can use.
- Composition = one command's skill set includes invoking other commands.
- Everything improvable (prompts, skill sets) is therefore a first-class, editable artifact.

## The agent groups

> Each group's definition file lives in [`groups/`](./groups/).

Each group has its own file. They roughly form a pipeline, with a few cross-cutting
research/refinement groups that other groups call into — and a **supervisor** on top
that picks which groups to chain for a given task.

### Top level

| Group | Role |
|-------|------|
| [Supervisor](./groups/supervisor.md) | Entry point. Takes a task (+ optional workflow & steps), routes it to an orchestration mode (greenfield / ralph ticket-munching / feature / fix / research-only / custom), and drives the chain. |

### Pipeline (highest level → shippable code)

| # | Group | In → Out |
|---|-------|----------|
| 1 | [High-level goal research](./groups/high-level-goal-research.md) | (nothing / a domain) → clear executive statement of what the repo is trying to do |
| 2 | [High-level goal → architecture](./groups/high-level-goal-to-architecture.md) | goal → tooling choices + dependency list |
| 3 | [High-level goal → feature list](./groups/high-level-goal-to-feature-list.md) | goal → specific feature list + wireframe |
| 4 | [Features → specs](./groups/features-to-specs.md) | features → ready-to-code specs / Linear tickets |
| 5 | [Specs → code](./groups/specs-to-code.md) | tickets → implemented code |
| 6 | [Validation](./groups/validation.md) | code → validated, consistent, tested, simplified code |

### Cross-cutting (called by pipeline groups)

| Group | Role |
|-------|------|
| [Code research](./groups/code-research.md) | Answers questions about the codebase with line references |
| [Idea research](./groups/idea-research.md) | Generates an ordered list of best ideas toward a goal |
| [Idea refinement](./groups/idea-refinement.md) | Prunes idea-research output down to what's actually best |

## How they connect (rough)

```
            supervisor  ── picks a mode, then drives one of these chains ──┐
                                                                           │
                                                                           ▼
high-level-goal-research
        │
        ▼
high-level-goal-to-architecture ──▶ (idea-research → idea-refinement) loop
        │
        ▼
high-level-goal-to-feature-list ──▶ wireframe
        │
        ▼
features-to-specs
        │
        ▼
specs-to-code ◀────────────── code-research
        │
        ▼
validation ◀────────────────── code-research
```

`idea-research` may call `code-research` (but never reads the repo directly itself).
`validation` calls `code-research` for consistency and duplication checks.

## Cross-cutting decision: parallel isolation + merge gate

Both [Specs → code](./groups/specs-to-code.md) and [Validation](./groups/validation.md)
run agents in parallel over the same repo. The same rule applies to both, grounded in
research that parallel agents in a *shared* workspace perform **below a single agent**:

- **One git worktree/branch per parallel agent** — physical isolation, mandatory.
- **Sequential merge gate** with tests between merges; the agent that caused a conflict
  resolves it (local context beats a central coordinator).
- **Re-validate the merged state** once at the end.
- Cap concurrency at **~4–6 agents** before integration overhead eats the gains.

## Self-improvement

The factory is not static. A separate [`self-improvement/`](./self-improvement/) system
watches all Claude interactions across the org (the CommandHQ idea), mines insights from
them, and feeds those insights as ideas into a loop that **dynamically improves the prompt
commands + skills** every agent here is made of. It closes the compounding loop: the
factory gets better at building by learning from how it's used.

## Folder layout

```
agent-factories/
  README.md            ← this file
  DEFENSE.md           ← v2 architecture statement (start here)
  defense-slides.html  ← the 5-minute deck
  groups/              ← the agent group + sub-agent definitions
  self-improvement/    ← self-healing: skills learn (content) + registry health
                         (dedup, auto-split, top-k search — structure)
```

## Status

Scaffolding + **all open design questions resolved** with researched, sourced defaults
(per-file "decided" sections). Decisions span: idea-loop convergence, idea scoring
(ICE + fit, geometric mean) & funnel ratios, JTBD customer grounding, discovery research
depth, boring-tech tooling selection + ADR/C4 artifacts, JSON-tree wireframes, MoSCoW +
Walking-Skeleton MVP cuts, Definition-of-Ready specs + vertical slicing, code-research
retrieval registry / citation-confidence / caching, parallel isolation + merge gates,
CPM-vs-FIFO scheduling, and the supervisor's phase model / classifier routing / durable
resume.

Still to flesh out: the concrete sub-agent prompts and exact I/O contracts per group, and
a few low-stakes tuning notes left inline (e.g. CPM thresholds, semantic-cache cutover).
