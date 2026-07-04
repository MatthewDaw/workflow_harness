# Skill Editor

**Type:** Self-improvement — step 4 (the actuator)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Apply an approved improvement as a **real edit** to a prompt command / skill artifact —
the only component that actually changes the factory. Treats the edit like any other code
change: scoped, isolated, validated, gated, reversible.

## Input

- A chosen improvement from [improvement-ideation](./improvement-ideation.md): target
  artifact + concrete change + hypothesis + predicted metric.

## Output

- A committed, versioned edit to the target prompt command / skill (or a new one), behind
  whatever gate its blast radius demands — ready for [evaluation](./evaluation.md) to
  measure.

## Behavior

- **Isolated** — edits land on a branch/worktree (same isolation + merge-gate rule as the
  [groups](../README.md#cross-cutting-decision-parallel-isolation--merge-gate)).
- **Validated** — runs through the [validation](../groups/validation.md) group (the prompt
  command + skill is an artifact; lint it, test it against fixtures, simplify, un-slop).
- **Gated by blast radius** — low-risk/reversible edits to a single skill can auto-apply;
  high-blast-radius or low-reversibility edits require a human gate before rollout.
- **Versioned + attributable** — every edit records the insight + evidence that motivated
  it, so [evaluation](./evaluation.md) can attribute outcome changes and roll back cleanly.

## Sub-agents (to flesh out)

- **Editor** — makes the actual change to the command/skill text or skill set.
- **Validator hook** — invokes the validation group on the edited artifact.
- **Gatekeeper** — decides auto-apply vs. human-approval from blast radius + reversibility.
- **Rollout** — stages the change (canary cohort → full) and records the version.

## Decisions

### Rollout: canary, even with fast rollback
Prompt regressions are **silent** (quality degrades without moving error/latency — see the
ChatGPT-sycophancy full-rollout incident), so limit blast radius *before* detection rather
than relying on rollback *after*. Default ladder:
- **Canary 5% for 1–2h**, session-sticky, gated by an LLM-as-judge quality check + auto-
  rollback triggers (p99 latency ↑>40%, refusal/correction rate ↑>5%, cost out of budget),
  then ramp `5 → 20 → 50 → 100`.
- **Shadow mode** (run new prompt on real traffic, serve old, compare) only for *structural*
  changes (restructured prompt, new skill file, tool-schema change).
- Trivial additive edits (one extra few-shot example) → 5%-for-1h with auto-promote.

### Self-edit safety — immutable core
When the loop can edit its own components, the threats are **meta-level reward hacking**
(edit the evaluator to return high scores) and **runaway drift**. Guardrails:
- **Immutable kernel, enforced at the filesystem/IAM layer (not by instruction):** the loop
  **may** edit skill files, prompt commands, few-shot examples, tool descriptions; it
  **may not** edit the evaluator, scoring rubric, approval gate, or rollback mechanism.
- **Risk-tiered gates:** prompt/skill edit → auto-eval + async human review; new skill →
  synchronous human approval; edit to watcher/extractor → dual sign-off; edit to
  evaluator/editor → **architecturally blocked**.
- **Sandboxed staging:** improvement runs execute against held-out eval tasks with no
  production write credentials; only the human gate promotes.
- **Eval independence:** evaluation runs on a held-out set the editing agent cannot write
  to; alert when live scores diverge sharply from the staging scores that justified promotion
  (a gamed-eval signal).

### Versioning & storage
**Git as source of truth, bidirectionally synced to a registry** (self-hosted Langfuse):
templates live in `prompts/commands/*.md` + `prompts/skills/*.md`; CI pushes to the registry
on merge. **Content-addressable version hashes** (sha256 of normalized text + config) —
immutable, dedupable, reproducible — stamped into every trace. **Label-pointer promotion**
(`production`/`staging`/`canary` labels point at version hashes; rollback = re-point the
label, no redeploy, <60s; protect `production` with RBAC). Every promoted version carries a
`promoted_by_eval: <eval_run_id>` foreign key, and a **full-artifact snapshot is taken before
any edit batch** so rollback is guaranteed. Rule: *if a change can't be reviewed, rolled
back, or attributed, it doesn't run.*

> Sources: canary/shadow + ChatGPT-sycophancy rollback case; self-modifying-agent safety
> (immutable core, sandboxing, dual gates); Langfuse/Braintrust prompt versioning + git sync.
