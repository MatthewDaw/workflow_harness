# Insight Extractor

**Type:** Self-improvement — step 2

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Mine the interaction stream from [interaction-watcher](./interaction-watcher.md) into a
small set of **candidate insights** — recurring, evidence-backed observations about where
the agents (prompt commands + skills) help, hurt, or could be sharper.

## Input

- The normalized, tagged interaction records.
- The current set of prompt commands + skills (so insights can be attributed to a target).

## Output

- A ranked list of **insights**, each: a claim + the evidence (linked interaction
  records) + the agent/skill it implicates + a frequency/severity estimate.
- Insights are **evidence, not edits** — they describe a pattern, not a fix.

## Sub-agents (to flesh out)

- **Pattern miner** — clusters interactions to surface recurring failure/friction modes.
- **Win miner** — finds what's working so it can be reinforced/propagated, not just fixes.
- **Attributor** — ties each insight to the specific prompt command + skill version.
- **Deduper** — merges insights already known / already logged as learnings.

## Insight taxonomy (first cut)

- **Capability gap** — the skill can't do a thing users repeatedly need.
- **Prompt defect** — instructions ambiguous / missing a guardrail → repeated mistakes.
- **Missing skill** — agent lacks a tool/skill it keeps needing.
- **Convention drift** — agent repeatedly violates a project norm.
- **Reinforcement** — a pattern that works and should be made the default / propagated.

## Decisions

### Significance threshold
Score each candidate `frequency_in_window × severity_weight`, and require it to cross in
**both a short (7-day) and a long (28-day) window** — single-window crossing is noise
(SRE multi-window burn-rate logic). Concrete promote rule:

> **≥ 3 distinct affected sessions AND failure rate ≥ 2× the skill's baseline rate,
> sustained across the 7-day *and* 28-day window** → promote to the "act on this" queue.
> A single **severity-critical** event (user corrected the agent, task aborted, safety
> issue) lowers the session floor to **2 — never 1.**

Tag each interaction against a failure taxonomy (incomplete / constraint-violation /
wrong-result / tool-error / hallucination / off-topic / convention-drift) so you compute a
**rate per category**, not one undifferentiated blob.

### Memory / dedupe
**Separate, curated insight ledger** — not the raw markdown/JSONL store ("without curation,
semantic memory becomes a junk drawer"). Keep `docs/solutions/` / `ac-learn` as the raw
**episodic record**; the ledger is a distilled layer on top. Per insight: `text`,
`embedding`, `importance` (ExpeL-style, **starts at 2**). On each mining run, embed the
candidate and nearest-neighbor search the ledger:
- cosine **≥ 0.90** → NOOP (already known);
- **0.85–0.90** → UPVOTE/UPDATE the existing entry instead of inserting;
- below 0.85 → ADD.
DOWNVOTE on contradiction; **importance hits 0 → drop.** (SQLite + vector column suffices.)

### LLM-mined *and* metric-triggered — run both
Metrics are the **tripwire**, LLM transcript analysis is the **diagnostician**. Run cheap
metric triggers continuously (failure/correction-rate per command, eval-score trend); when
one fires (or weekly), run the **LLM miner on the flagged low-scoring traces** to surface
*why*. Both write to the same ledger, tagged `source: metric_trigger | transcript_analysis`.
Metrics alone = alarms without explanations; LLM alone = expensive and anecdotal.

### Anecdote guard
The promote rule above *is* the guard: **≥3 distinct sessions** (count sessions, **not
events** — one bad session emits hundreds), **≥2× base rate**, **two-window persistence**,
and the **importance counter** forcing multi-run confirmation before an insight carries
weight. Never act on a single session no matter how dramatic.

> Sources: Google SRE multi-window burn rate; Sentry issue-grouping/anomaly detection;
> ExpeL importance counters; Mem0 dedupe thresholds; LLM-agent failure taxonomies (MAST).
