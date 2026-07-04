# Improvement Ideation

**Type:** Self-improvement — step 3

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Turn insights into a **ranked list of concrete improvements** to specific prompt commands
/ skills. This is where "we noticed X" becomes "change skill Y this way."

## Reuses the idea loop

This component **is** the [idea-research ↔ idea-refinement](../groups/idea-refinement.md)
loop pointed at insights instead of product ideas — same generate → refine → converge
shape, same convergence gate. Don't reimplement it; parameterize it.

- **Goal/context fed in:** the insight + its evidence + the target skill's current text.
- **Generation:** candidate edits (reword a guardrail, add a skill, split a command,
  add an example, change a default).
- **Scoring axes** (the [idea-research](../groups/idea-research.md) rubric, specialized):
  Impact (how much it fixes the insight) · Confidence (evidence strength) · Ease (edit
  size/risk) · **Blast radius** (how many agents/users the change touches — higher = more
  caution) · **Reversibility** (can we cleanly roll back).
- **Refinement:** prune edits that are risky, speculative, or off-target; keep the
  smallest change that plausibly resolves the insight.

## Input

- A candidate insight from [insight-extractor](./insight-extractor.md).

## Output

- An ordered list of proposed edits, each: target artifact (which command/skill),
  the concrete change, the hypothesis ("this should reduce correction rate on command Z"),
  and a predicted metric to confirm it.

## Decisions

### Bundling: one edit per insight
**One proposed edit per insight, deployed and evaluated independently** before the next is
queued — bundling collapses attribution (you can't tell which change moved the metric).
Merge two edits into one revision **only** when they have a documented causal dependency
(edit B is meaningless without edit A). Mirrors Google SRE's "one canary at a time" and
TextGrad's node-by-node, reversible updates.

### New skills vs. edit existing — staged
**Start at "edit existing prompts only."** New-skill creation is the high-risk end of
self-modification (ADAS/Gödel-Agent show real instability and reward-hacking when agents
write their own tools), and Voyager shows new skills are net-negative *without* an
execution-verification gate. Progression:

| Stage | Allowed | Gate |
|---|---|---|
| 1 (launch) | edit wording/instructions of existing prompts | automated eval beats baseline |
| 2 | tweak params/thresholds in existing skills | eval + regression suite |
| 3 | propose a new skill | **human approval** + sandboxed test |
| 4 | auto-promote new skills | mature eval harness + immutable safety core |

Don't advance a stage until the eval harness is proven and a non-self-modifiable safety
core exists (see [skill-editor](./skill-editor.md)).

### Fix-a-failure vs. propagate-a-win
**Failure-first, 70/30** at launch (two queues: failure / propagation). Defect-elimination
is higher-ROI and more robust — "propagate a win" edits are the biggest reward-hacking
surface (amplifying a proxy metric without ground truth), so they **never auto-promote
without an independent eval gate**. Rank the failure queue by an FMEA-style
`failure_rate × user_impact × attribution_confidence`. Rebalance toward 50/50 once the
baseline failure rate is low and the eval harness is trusted. (GEPA's hybrid — keep winners
in the pool, but *direct mutations at failures* — beats either pure strategy.)

### Prompt-edit generation — what the literature says
Generation should **pair a numeric score with an LLM-written critique** (neither alone is
as actionable). Feed the meta-prompt a **history of past (edit, score) attempts** (OPRO).
Try **structural reforms first** — add a chain-of-thought section, normalize examples to a
consistent format, enrich few-shots (Anthropic Prompt Improver: +30% on classification) —
**before** semantic rewrites. Treat each prompt section as a node and target the critique
at the specific node (TextGrad), rather than rewriting the whole command.

> Sources: GEPA, OPRO, DSPy/MIPRO, TextGrad, Anthropic Prompt Improver; ADAS, Gödel Agent,
> Voyager (self-modification risk); Lean/FMEA prioritization; in-context reward-hacking (2402.06627).
