---
date: 2026-06-10
topic: agent-families-design-research
focus: research-grounded improvement of docs/agent-families/DESIGN.md — verified foundation + meaningful improvement
mode: repo-grounded
---

# Ideation: Agent Families Design — Research-Grounded Improvements

All eight survivors were accepted and folded into `docs/agent-families/DESIGN.md` (Revision 2) the same day; this record is provenance for that revision. Full citations live in DESIGN.md §18.

## Grounding Context

Subject: the agent-families self-improving reverse-coding system design. Grounding: full design-doc scan; no institutional learnings existed (`docs/solutions/` absent); heavy external research across 8 areas (skill libraries, gradient-free optimization, multi-agent SE failure taxonomies, iterate-until-verified, LLM-judge reliability, behavioral cloning/differential testing, clustering-driven taxonomy growth, simulated users) plus a supplemental round on improvement-oriented grading.

**Foundation verdict:** every load-bearing mechanism has direct quantitative prior art (ACE/GEPA reflector lineage, Voyager/AWM/Dynamic Cheatsheet libraries, MetaGPT/ChatDev pipelines + MAST failure data, Huang et al. on external feedback necessity, Mechanical Orchard/AWS Transform behavior-first rewrites). Genuinely novel, no prior art found: silhouette-triggered agent splitting, the self-reorganizing routed taxonomy, explorer-generated scenario manifests closing a training loop.

## Topic Axes

A1 Skill library & memory governance · A2 Verification & grading reliability · A3 Pipeline roles & failure containment · A4 Training loop & credit assignment · A5 Spec generation & the explorer

## Ranked Ideas

### 1. Ratchet governance: active-skill cap + outcome-driven retirement
**Axis:** A1 · **Basis:** external: Library Drift (+0.0pp LLM-authored vs +16.2pp curated; cap+retirement+schema → +0.328 vs +0.002); Skill Shadowing (21% drop at 202 skills) · **Status:** Explored — DESIGN §4

### 2. Routing & rendering corrections: full-text routing, cosine merge, delta-patch compilation
**Axis:** A1 · **Basis:** external: SkillRouter (31–44pp body-signal effect; cosine>0.92 merge); ACE (context collapse; delta-patches +10.6%) · **Status:** Explored — DESIGN §4–5

### 3. Reward-signal integrity: debiased judging + instrument calibration
**Axis:** A2 · **Basis:** external: MAST (incorrect verification 9.1%, worse than none); judge literature (binary+CoT; agreeableness bias; style ≫ position bias); mutation testing · **Status:** Explored — DESIGN §10

### 4. Staged insight admission: quarantine → validate → promote, SPC-gated rollback
**Axis:** A4 · **Basis:** external: TextGrad validation-reversion; Huang et al. self-bias amplification; Deming funnel · **Status:** Explored — DESIGN §5

### 5. Two-tier memory: run-scoped workflow induction + cross-target recurrence promotion
**Axis:** A4 · **Basis:** external: AWM (~7 workflows/site → 51.1% rel. improvement; online > offline) · **Status:** Explored — DESIGN §13

### 6. Explorer subsystem: feature registry + exploration frontier + verified oracle
**Axis:** A5 · **Basis:** direct: user requirements (frontier memory, pre-registered inventory); external: Lost in Simulation (goal drift), DuetSim (generator+verifier) · **Status:** Explored — DESIGN §9–10

### 7. Equivalence-gated improvement tier
**Axis:** A2 · **Basis:** external: SWE-Perf (gate-then-measure), constrained-RLHF (gating not blending), Lighthouse median-of-5, MLLM-UI-judge (pairwise reliable on gross differences only), MI/cyclomatic validity criticisms · **Status:** Explored — DESIGN §10

### 8. Ralph loop hardening: typed failures + no-progress tripwires
**Axis:** A3 · **Basis:** external: MAST (step repetition 15.7% + termination-unaware 12.4%); Feedback Friction (typed > prose) · **Status:** Explored — DESIGN §7

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | NTSB weighted attribution | Downgraded to `primary + contributing[]` field in the reflector schema; standalone version conflicted with single-attribution simplicity |
| 2 | Target-side evidence caching | Tactical grader-implementation detail, below the step-function floor |
| 3 | Routing-contention split trigger | Premature — needs routing telemetry that exists only post-Phase 3 |
| 4 | Zero-question curriculum + assumption ledgers | Narrower training-curriculum variant (assumption ledger partially survived as the planner `assumptions[]` field) |
| 5 | Gauge R&R, N-version verdicts, jidoka halt, thymic selection, Pareto archive, fresh-context explorer, success-channel reflector | Merged into survivors 3/4/5/6/8 |
| 6 | ~30 cross-frame near-duplicates | Five of six frames independently converged on survivors 1–4; merged (convergence = corroboration) |
