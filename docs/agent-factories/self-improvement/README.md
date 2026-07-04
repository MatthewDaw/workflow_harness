# Self-Improvement

The loop that makes the factory get better at building over time. It watches how every
agent is actually used, mines insights from that, turns insights into proposed
improvements, and edits the **prompt commands + skills** that every agent in
[`../groups/`](../groups/) is made of — then measures whether the edit actually helped.

This is the CommandHQ idea, formalized: **watch all Claude interactions → learn insights
→ use them as ideas to dynamically improve the skills.**

> Like everything else here, each component below is itself a *prompt command + a set of
> skills* (see [What every agent *is*](../README.md#what-every-agent-is-definitional)).
> This is the **meta-group**: the one whose work product is edits to the other agents.

## The loop

```
        ┌──────────────────────────────────────────────────────────────┐
        │                                                              │
        ▼                                                              │
[interaction-watcher]  watch ALL Claude interactions (CommandHQ telemetry)
        │                                                              │
        ▼                                                              │
[insight-extractor]    mine the stream → candidate insights/learnings   │
        │                                                              │
        ▼                                                              │
[improvement-ideation] insights → ranked improvement ideas              │
        │              (REUSES idea-research ↔ idea-refinement loop)     │
        ▼                                                              │
[skill-editor]         idea → actual edit to a prompt command / skill    │
        │              (gated + run through the validation group)        │
        ▼                                                              │
[evaluation]           did it improve outcomes? keep wins / roll back ───┘
                       (kept change = new baseline; loop continues)
```

The thing being edited (a prompt command + its skills) is a first-class, versioned
artifact — which is exactly what makes this loop possible.

## Two halves of self-healing

- **Content heals** — the skills themselves learn from interactions (the loop above).
- **Structure heals** — the *catalog* of agents and skills stays deduplicated,
  indexable, right-sized, and searchable as it grows:
  [registry health](./registry.md). Skills never repeat; oversized skills/agents
  auto-split; everything resolves via top-k vector search so context stays O(k) no
  matter how many agents the org registers.

## Components

| Component | Role |
|-----------|------|
| [interaction-watcher](./interaction-watcher.md) | Capture every Claude interaction across the org — the CommandHQ observation substrate. |
| [insight-extractor](./insight-extractor.md) | Mine the captured stream into candidate insights (failures, friction, winning patterns, corrections). |
| [improvement-ideation](./improvement-ideation.md) | Turn insights into a ranked list of concrete skill/command improvements. Reuses the [idea loop](../groups/idea-research.md). |
| [skill-editor](./skill-editor.md) | Apply an approved improvement as a real edit to a prompt command / skill, gated + validated. |
| [evaluation](./evaluation.md) | Measure whether a change actually improved outcomes; keep wins, roll back regressions. |
| [registry](./registry.md) | Structural healing: skill dedup, indexing/ingestion targets, auto-split, top-k search, agent disambiguation. |

## How it connects to the rest of the factory

- **Edits the groups, doesn't replace them** — its output is diffs to the prompt commands
  + skills in [`../groups/`](../groups/) (and to the self-improvement components
  themselves — it can improve its own loop).
- **Reuses primitives** — improvement-ideation *is* the
  [idea-research ↔ idea-refinement](../groups/idea-refinement.md) loop pointed at insights;
  skill-editor runs its edits through the [validation](../groups/validation.md) group; and
  evaluation borrows the metric-driven keep/rollback discipline.
- **Triggered by the [supervisor](../groups/supervisor.md)** — runs as a background /
  scheduled mode, not on the critical path of any single task.

## Design stance

- **Insights are evidence, not edits.** Nothing changes a skill until an insight survives
  ideation, refinement, validation, and (for risky edits) a human gate.
- **Measure or revert.** Every applied change carries a hypothesis and a metric; if it
  doesn't move the metric, it's rolled back. No unmeasured "improvements."
- **Compounding.** Each kept change becomes the new baseline the next round learns on top
  of — the factory's skills ratchet upward instead of drifting.

## Status

Loop shape, component boundaries, **and all open design questions resolved** with
researched, sourced defaults (per-file "decided" sections). Decisions span: telemetry
consent/redaction (OTel-Collector gateway) + tail-sampling + version attribution;
two-window significance thresholds + a curated insight ledger (ExpeL importance counters) +
metric-tripwire/LLM-diagnostician split; one-edit-per-insight + staged self-modification +
failure-first 70/30 + prompt-optimization tactics (OPRO/TextGrad/GEPA/Anthropic improver);
canary rollout + immutable-core self-edit guardrails + git⇄registry versioning; and
sequential-test sample floors + mutually-exclusive experiment layers + LLM-judge-as-
guardrail + Goodhart counter/guardrail metrics.

Still to flesh out: concrete sub-agent prompts and exact I/O contracts per component.
