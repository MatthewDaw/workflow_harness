# Idea Refinement

**Type:** Cross-cutting refinement group

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Refine a set of ideas down to the ones that are actually best for the project.

## Behavior

- Runs **only after** [Idea Research](./idea-research.md).
- Prunes — keeps only the ideas that genuinely fit the project.

## Input

- The ordered idea list from Idea Research.
- Project context / constraints to judge against.

## Output

- A refined (smaller, sharper) ordered list of ideas worth pursuing.

## Sub-agents (to flesh out)

- **Critic / pruner** — kills weak or off-fit ideas with reasons.
- **Consolidator** — merges overlapping ideas.
- **Fit checker** — validates against project goal and constraints.

## Loop

Idea Research ↔ Idea Refinement is an **iterative loop**, not a single pass:

```
idea-research ──▶ idea-refinement ──▶ (keep going?) ──▶ idea-research ──▶ ...
                                          │
                                          ▼ (converged)
                                    refined idea list
```

Each round: research generates/expands, refinement prunes and sharpens, and gaps
exposed by pruning feed the next research round. Loop until it converges (ideas stop
improving) or a round budget is hit.

## Convergence rule (decided)

Stop the loop on a combined gate. Grounded in iterative-refinement research
(Self-Refine caps at 4 rounds; Reflexion stops on no-improvement; FunSearch/CALM track
a no-breakthrough counter; diversity research warns of mode collapse within 2–3 rounds).

**Stop when ANY of:**
1. **Hard cap** — round `N_max = 6`. Non-negotiable circuit breaker.
2. **No new keepers** — zero new ideas entered the keeper set for **2 consecutive
   rounds**. (Primary convergence signal; robust even when the set is small.)
3. **Set stability** — Jaccard overlap of the top-N keeper set between consecutive
   rounds **> 0.85** (≤15% turnover).

**Guards:**
- **Minimum floor** — always run **≥ 2 rounds** before any early stop (round 1 captures
  ~50% of the gain, round 2 ~25% more).
- **Anti-premature-convergence** — before honoring signal 2 or 3, check the keeper set
  is actually *diverse* (mean pairwise semantic similarity **< 0.80**). If it collapsed
  to near-duplicates, inject a diversity-forcing prompt ("generate ideas meaningfully
  different from {keepers}") and run one more round instead of stopping.

```
min_rounds=2; max_rounds=6; no_new_threshold=2; jaccard_stop=0.85; diverse_below=0.80
for round in 1..max_rounds:
    research(); refine()
    if round < min_rounds: continue
    converged = (no_new_keepers_for >= no_new_threshold) or (jaccard(R, R-1) > jaccard_stop)
    if converged:
        if mean_pairwise_sim(keepers) < diverse_below: break      # converged AND diverse
        else: inject_diversity_prompt(); reset(no_new_keepers_for)  # collapse → push once
```

Cheap/fast variant: `max_rounds=4, no_new_threshold=1`. High-quality variant:
`max_rounds=8` plus an LLM-as-judge score-delta gate (stop if Δ < 1/100).

> Failure modes to avoid: **premature convergence** (keeper set collapses to near-dupes —
> caught by the diversity guard) and **infinite churn** (pruned ideas keep re-entering —
> caught by the hard cap + the 2-round no-new signal).

## Generic refinement loop (decided)

**Yes — this is a reusable primitive, not idea-specific.** The generate → critique/prune
→ converge loop is well-established across decision types (Self-Refine tested it across 7
task types; AWS's evaluator/reflect-refine pattern generalizes it to code, plans, and
"agents that reason through trade-offs"; agentic-ADR systems run the same loop for
architecture decisions). It applies whenever (1) the output space is too large for a good
one-shot, (2) a critic can assess quality against a rubric, and (3) refinement has a
defined action space (prune / merge / rephrase / replace).

So [goal → architecture](./high-level-goal-to-architecture.md) (and design-option /
approach-selection steps) reuse this same loop and the convergence gate above. One rule
that holds everywhere: **the critic should be a distinct role/model from the generator**
to avoid confirmation bias.
