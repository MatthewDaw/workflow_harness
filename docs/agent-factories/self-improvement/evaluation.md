# Evaluation

**Type:** Self-improvement — step 5 (closes the loop)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Decide whether an applied edit **actually improved outcomes**. Keep the wins, roll back
the regressions. This is what makes the loop a ratchet instead of a random walk.

## Input

- A live edit from [skill-editor](./skill-editor.md): the change + its hypothesis + its
  predicted metric + the version stamp.
- Post-edit interaction records from [interaction-watcher](./interaction-watcher.md).

## Output

- A verdict per edit — **keep / roll back / inconclusive (extend window)** — plus the
  measured effect.
- Kept edits become the **new baseline** the next round of insight extraction learns on
  top of (the compounding step).

## Behavior

- **Hypothesis-driven** — every edit declared a predicted metric in
  [improvement-ideation](./improvement-ideation.md); evaluation checks *that* metric, not
  vibes.
- **Before/after or A/B** — compare the edited artifact's outcomes against the prior
  version's baseline (canary cohort A/B where rollout allows it; otherwise before/after on
  the same metric).
- **Metric set (first cut):** correction rate, failure/error rate, turns-to-done,
  one-shot success rate, latency/cost, explicit user feedback.
- **Roll back on regression** — if the metric doesn't move (or moves the wrong way) past
  a confidence threshold, revert via the skill-editor's version history.

## Sub-agents (to flesh out)

- **Measurer** — computes the metric delta from post-edit interaction records.
- **Significance judge** — decides if the delta is real or noise (sample size, threshold).
- **Verdict / rollback** — keeps or reverts; updates the baseline.

## Decisions

### Sample size & window
Use **always-valid sequential testing** (confidence sequences / mSPRT, α=0.05, β=0.20) so
you can monitor continuously **without p-value peeking inflation**. Verdict floor: **≥ 200
events per arm AND ≥ 7 calendar days** (captures day-of-week + novelty), regardless of how
fast significance appears. Cap at ~2,000–5,000 per arm (or MDE-driven); if no boundary is
crossed by the cap → **inconclusive → roll back** (default-safe). Proportion sizing:
`n = (z_α/2+z_β)²·[p1(1-p1)+p2(1-p2)]/(p1-p2)²` (≈600/arm for 20%→25% at standard power).

### Multiple-edit attribution
Assign each prompt **parameter domain** (system prompt / few-shot / tool descriptions) to
its own **mutually-exclusive experiment layer** (Google/Optimizely overlapping-experiment
design). Within a layer, **one active experiment at a time** — queue the rest; promote each
edit to default before launching the next (sequential rollout = the simplest correct
attribution at limited traffic). Keep a **5% global holdout** that sees no experiments to
audit cumulative "launch debt" monthly.

### LLM-as-judge — guardrail, not decider
Hard metrics drive the keep/rollback decision; the **LLM judge is a guardrail that can
*block* a keep** when quality drops even though hard metrics improved. Use a **3-level
rubric (Bad/OK/Good)** pointwise for monitoring and **double-pass pairwise** (both orderings,
averaged — kills position bias) for keep/rollback. Calibrate against **~100 human-labeled
examples** and only trust it past **Cohen's κ > 0.4**. A hard-metric win with a >10pp
relative judge-quality drop → block pending human review.

### Metric-gaming / Goodhart guard
Never evaluate on a single metric. For each **primary** metric define **one counter** metric
(its specific gaming risk — e.g. primary one-shot-success → counter response-length to catch
truncation; primary turns-to-done → counter completion-accuracy) plus **two guardrails**
(correction rate + user-satisfaction/judge quality). **A keep requires: primary improves
AND no guardrail degrades** (one-sided, α=0.10 for guardrail sensitivity) — any guardrail
breach forces rollback regardless of the primary. Require the gain to **persist across two
cohort windows** (novelty decay), and alert when the primary improves while any guardrail
moves adversely >1σ. Keep the metric set small (3–5 total; more guardrails → more false
rollbacks).

> Sources: always-valid inference / confidence sequences (Johari et al.); SPRT for low
> traffic (Patronus); Google overlapping-experiment layers; LLM-as-judge bias/calibration
> (2504.14716, LangChain); Goodhart guardrail/counter-metric practice (PostHog/Eppo).
