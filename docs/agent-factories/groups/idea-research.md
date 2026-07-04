# Idea Research

**Type:** Cross-cutting research group

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Generate grounded ideas toward a goal. Similar in spirit to `/ac-ideate`.

## Behavior

- May call [Code Research](./code-research.md), but **does not directly read the repo
  itself**.
- **Must** take in context about what goal is trying to be achieved.

## Input

- The goal / context being worked toward.
- (Optional) answers from Code Research about what already exists.

## Output

- An **ordered list of the best ideas** (best first).

## Sub-agents (to flesh out)

- **Generator(s)** — diverse idea generation, possibly multiple angles/personas.
- **Grounding agent** — pulls real constraints via Code Research before ranking.
- **Ranker** — orders ideas by fit to the goal.

## Scoring & funnel (decided)

### Scoring axes — "best" means
Use **ICE + Strategic Fit**, four axes scored 1–10 (ICE is the most agent-friendly
framework — all axes estimable from text alone, no product analytics needed, unlike
RICE's Reach):

| Axis | Measures |
|---|---|
| **Impact** | How much it moves the goal metric |
| **Confidence** | Evidence behind the estimate (10 = high) |
| **Ease** | Inverse effort (10 = trivial) |
| **Strategic Fit** | Alignment with the stated goal/context |
| **Customer Desirability** | Does it address the real JTBD? (see below) — mandatory |

Composite = **geometric mean** of the axes, not arithmetic. Geometric mean punishes any
single near-zero axis (a high-impact / zero-confidence idea should rank low, not average
out to "medium").

### Funnel — how many to generate vs. keep
Grounded in the innovation-funnel literature (<10% of ideas advance per gate; Double
Diamond converges to 2–3 concepts):
- **Generate ~20** candidates per cycle.
- **Prune to ~5** (≈25% retention) — this is what hands to [Idea Refinement](./idea-refinement.md).
- After 2–3 loop rounds, **advance the top 1–3** finalists.

### Customer grounding (relationship to goal research)
Ideas are generated **against a JTBD brief**, never in a vacuum. The
[high-level-goal-research](./high-level-goal-research.md) output supplies the customer
context; every generation prompt carries a required block:
```
When [situation], I want to [motivation], so I can [outcome].
Underserved outcome: Importance=X, Satisfaction=Y → Opportunity Score = Importance + max(Importance − Satisfaction, 0)
```
Target jobs with Opportunity Score in the **8–10 "underserved" band** (avoid >10
overserved). This forces every idea to trace to a specific customer job, and feeds the
**Customer Desirability** scoring axis above.

> Sources: ICE/RICE/Kano comparisons; Ulwick Opportunity Scoring / JTBD; Stevens & Burley
> innovation-funnel benchmark; Double Diamond.

## Loop with Idea Refinement

Runs as an **iterative loop** with [Idea Refinement](./idea-refinement.md): research →
refine → research again on the gaps, until converged. See that file for the loop shape.
