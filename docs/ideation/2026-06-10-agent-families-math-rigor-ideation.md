---
date: 2026-06-10
topic: agent-families-math-rigor
focus: mathematical/statistical rigor upgrades for the agent-families training loop (DESIGN.md) — difficulty estimation, information theory, formal verification, causal credit, sequential testing — plus a user-requested deep-dive turning the grader into a proper multi-component loss function
mode: repo-grounded
---

# Ideation: Agent Families — Mathematical Rigor & the Grader as a Loss Function

Two rounds. Round 1: system-wide math-rigor ideation (6 frames × 8 ideas = 48 raw → 7 survivors). Round 2 (user-directed refinement: "the grader is the weakest link; I want a solid multi-focus loss function, formal verification included"): grader-focused research + 3 frames × 7-8 ideas = 22 raw → 7 survivors composing one loss architecture.

## Grounding Context

Subject: the training/optimizing loop of the agent-families system (docs/agent-families/DESIGN.md, R2). Implementation state at ideation time: Phase 0 (library core: SQLite + sqlite-vec, add_idea, thresholds.toml) complete; Phase 1 (pipeline skeleton, Ralph loops, judge record/replay seam) essentially complete (Plan 002 U8 done); Phases 2–3 (explorer/grader, learning loop, scale — Plans 003–005) planned but unbuilt, so new math can still shape those plans and schemas are cheap to extend.

House conventions every proposal must compose with: shadow-first (estimators log before they gate); every threshold in thresholds.toml with provenance + tuning metric; min-N gates for data-hungry machinery; Stage A attribution deterministic-by-contract (judge seam raises if called); binary+CoT default-fail judging with panels on disagreement; frozen replay set ≥20 hand-verified pairs persisted with original judge inputs; benchmark σ via replicate episodes; SPC ≥10 points, special-cause only. Prior decisions respected: mSPRT (α=0.05, β=0.20) + inconclusive→rollback already adopted (docs/agent-factories/self-improvement/evaluation.md); "NTSB weighted attribution" previously rejected — Shapley proposals live beside Stage A as offline telemetry, never inside it.

External research (round 1): IRT-for-LLM-eval is crowded (PSN-IRT 2505.15055; Agent Psychometrics 2604.00594; Growing Pains 2604.12843; ATLAS 2511.04689; Actor-Curator 2602.20532) but agentic sparse-matrix curriculum is open; MDL for skill taxonomies is a nearly empty lane (MDL Skills ICLR21; MIDGARD 2405.05189); Shapley-for-RAG growing (In-Run 2406.11011; 2507.04480 co-retrieval confounding; Cluster Shapley); sequential testing mature (2501.03982; CITE 2605.05873; Noisy-but-Valid 2601.20913); judge MSA thin (G-theory 2507.19980; IRT-GRM 2602.00521; Rating Roulette 2510.27106 — ICC3/Krippendorff over kappa; SPC-on-judges unpublished); Markov retry (4/δ bound 2512.02080; Luby restarts open); survival-for-memory nearly empty; conformal judges (2509.18658; PASC 2605.18812). Competing skill-library systems (SkillClaw, SkillNet, EvoSkills) publish NO measurement model. Strongest publishable angle: **"skill-library curation as a measurement system."**

External research (round 2, grader/loss + formal verification): STL robustness semantics (Fainekos/Pappas; Donzé/Maler) with differentiable implementations — STLCG++ 2501.04194 (1000×, JAX/PyTorch), GradSTL 2508.04438 (irregularly-sampled signals → DOM/a11y event traces); **no published application to UI traces**. Falsification (S-TaLiRo/Breach, ARCH-COMP) — robustness-minimizing adversarial search; **open for web apps**. Active automata learning for web apps (LearnLib/ALEX); bisimulation metric = optimal transport, Sinkhorn Value Iteration (2406.04056, NeurIPS24). E-valuator (2512.03109) e-process verifier testing (~90% accuracy at 80% cost). Constrained/Safe RLHF + DRO (2506.13351, 2510.05703) — gate-as-constraint is the published Goodhart-resistant gate-then-bonus. Catastrophic Goodhart (NeurIPS24) — heavy-tailed judged error defeats KL regularization. Over-optimization scaling laws (2210.10760); Projection Optimization (2502.15145) — min/Chebyshev aggregation reaches Pareto points linear weights cannot. Pairwise ≫ pointwise judging (2603.12520: pointwise ties on 2/3 pairs, within-prompt r=0.27, pairwise recovers 61% vs 21%). IRT/GRM judge diagnostics (2602.00521) and cross-task latent invariance (2605.00238). Rubric rewards lineage (hierarchical essential/additional 2605.30244; factual/process 2511.12344; program-as-judge 2506.10403; Prometheus-2 2405.01535; CARMO 2410.21545).

## Topic Axes

Round 1 — A1 Difficulty & capability measurement · A2 Library structure & economics · A3 Credit assignment & promotion statistics · A4 Instrument reliability · A5 Formal guarantees & loop dynamics

Round 2 (grader) — G1 Loss architecture & aggregation · G2 Graded equivalence signals · G3 Scenario generation & coverage · G4 Judge components & calibration · G5 Grading economics

## Ranked Ideas — Round 1: System-Wide Math Rigor

### 1. Schema-first: the event-level measurement ledger
**Description:** Replace per-insight fitness counters `{retrievals, wins, losses, causal_blames}` with an append-only event table — one row per retrieval event carrying the full co-retrieved set, episode, snapshot, ticket, ralph_iteration, judge, outcome, blame — plus anchor-episode flags, a frozen probe-ticket set, and routing-decision logs. Counters become derived projections.
**Axis:** A2 (enabler)
**Basis:** direct: counters are lossy projections of exactly the information ideas 2–6 require; Phases 2–3 are unbuilt so the schema change is cheap now and impossible to retrofit after training data accumulates.
**Rationale:** Six other survivors become queries over this table; it makes the house "defer data-hungry machinery" convention safe instead of fatal.
**Downsides:** Slight write volume; none material.
**Confidence:** 95% · **Complexity:** Low · **Status:** Unexplored

### 2. IRT latent capability backbone + information-optimal curriculum
**Description:** Fit Rasch/2PL over the (snapshot × ticket/scenario) pass/fail matrix in the span store. Capability curve = latent ability with standard errors (replaces raw one-shot rate, which is confounded by design: the curriculum ladder hardens and the planner authors its own test items — fixed by frozen probe tickets + anchor-item linking). Curriculum becomes adaptive: Fisher-information episode scheduling + learning-progress bandit. Glicko/TrueSkill as lighter fallback estimator.
**Axis:** A1
**Basis:** external: Growing Pains 2604.12843 (anchor calibration), Agent Psychometrics 2604.00594, Actor-Curator 2602.20532 (+28.6%); direct: the response matrix already exists in SCEN/ticket rows.
**Rationale:** Separates "the library improved" from "the targets got easier" — the headline metric currently cannot; episode selection is the largest discretionary quota spend and is currently round-robin.
**Downsides:** Rasch needs ~100 obs/item — ships shadow-first behind a min-N gate; raw rate stays on the chart beside it.
**Confidence:** 80% · **Complexity:** Medium-High · **Status:** Unexplored

### 3. Library lifecycle economics: MDL in, hazard out
**Description:** One two-part-code objective L(taxonomy) + L(traces|taxonomy) scores admission, the cap, dedup, splits, and merges as ΔMDL moves — replacing cap ~50, silhouettes 0.3/0.35, the compressibility gate (which *is* the description-cost term, formalized), and cosine 0.92 (refit as a two-component mixture with cost-ratio decision rule). Router entropy + top-1/top-2 margin + co-retrieval PMI + Pianka niche-overlap run as shadow routing-health telemetry (this is §6's deferred contention trigger, defined). Retirement becomes censored survival analysis — exposure = retrieval opportunities, fixing the "low value vs low exposure" identification error — with option-valued dormancy (almost never hard-retire; tune dormancy depth).
**Axis:** A2
**Basis:** external: MDL-for-skill-taxonomies nearly empty lane (MDL Skills ICLR21, MIDGARD 2405.05189); Skill Shadowing gives the interference curve; survival-for-memory nearly empty lane.
**Rationale:** Converts the design's most load-bearing arbitrary constants into measured quantities and gives the early-warning channel for routing collapse the design admits it lacks.
**Downsides:** Highest theory-to-engineering distance; the trace-codelength term needs careful operationalization.
**Confidence:** 70% · **Complexity:** High · **Status:** Unexplored

### 4. Quarantine trials as designed experiments
**Description:** Randomized fractional-factorial insight-subset masks over the already-mandated validation runs (~2–3 extra runs/batch) → per-insight causal effects, not batch verdicts. Promotion as anytime-valid e-process stopping (extends the house mSPRT; judge-TPR/FPR-corrected per Noisy-but-Valid 2601.20913); cross-target recurrence becomes literal e-value multiplication (replaces the unjustified ≥k rule). Offline TMC/Cluster-Shapley over the event ledger fills the sanctioned beside-Stage-A telemetry slot; empirical-Bayes shrinkage stops the cap tournament being a small-N lottery.
**Axis:** A3
**Basis:** direct: §15 batch-merge already runs independent validation + joint confirmation; quarantine trials are randomized ablations in all but name; prior NTSB rejection honored. external: 2507.04480 (co-retrieval confounding, TMC-Shapley 50–100 samples), In-Run Shapley 2406.11011.
**Rationale:** Quota savings land at the component §15 names "the throughput bottleneck"; the tournament stops promoting free-riders.
**Downsides:** ~2–3 extra trial runs per batch; e-process plumbing.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Unexplored

### 5. The grader as a measurement system
**Description:** Crossed G-theory variance-components model (scenario × judge × occasion × build-replicate × target; ICC3/Krippendorff) on the frozen replay set + replicate episodes, wrapped in a GUM-style combined uncertainty budget (adds heal-rate and DOM-drift noise). Derives: SPC control limits, the 95% gate (largest bar a truly-equivalent app passes w.p. ≥ 1−α), and the **minimum detectable batch effect** — the go/no-go question "can this loop detect its own improvements?" answered before Phase 3 spends quota. D-study allocates calibration spend. Conformal triage (2509.18658/PASC) gates Opus-panel escalation with a distribution-free error ceiling.
**Axis:** A4
**Basis:** direct: DESIGN §5/§17 say gate and limits should be "derived from measured grader noise" but specify no model; external: G-theory 2507.19980; SPC-on-judges has no published paper.
**Rationale:** Every downstream statistic inherits this noise model; a one-bucket σ over- or under-powers all of them at once.
**Downsides:** Needs the Phase 2 replay set to exist first (plumbing already specced).
**Confidence:** 90% · **Complexity:** Medium · **Status:** Unexplored

### 6. Ralph loops as absorbing Markov chains + Luby restarts
**Description:** Per-(family, failure-kind) transition/hazard fits from ralph_iteration spans. MAX_ITERS → optimal stopping vs opportunity cost (4/δ bound 2512.02080 as skeleton); Luby universal restart schedule where the hazard plateaus (open lane); mutation-audit-measured verifier confusion matrix gives P(actually done | verdict); a stagnation e-process replaces the step-repetition cosine tripwire with an α-controlled false-kill guarantee. Supplies §17's promised-but-unbuilt "cost model."
**Axis:** A5
**Basis:** direct: §17 "the cost model decides, not the rule of thumb"; external: 2512.02080; Luby 1993.
**Rationale:** The flat cap is the constant most directly multiplied against quota; tripwires catch pathological loops but nothing catches economically doomed ones.
**Downsides:** Per-failure-kind fits need iteration volume — shadow-first behind min-N.
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored

### 7. LTLf deterministic assertion tier *(superseded/extended by G-A below)*
**Description:** Compile metamorphic and outcome clauses into LTLf monitors over the Playwright event/a11y-tree stream — a formal tier between deterministic asserts and the LLM judge; judge-call-rate per scenario becomes an instrument-health metric driven down; target-free, ships to deployment.
**Axis:** A5
**Basis:** direct: §10's assertion ladder orders deterministic > judge and the metamorphic tier is property-shaped; external: LTLf-for-clone-grading open territory.
**Rationale:** Every scenario moved off the judge permanently removes quota cost and a noise source.
**Downsides:** Monitor authoring cost; round 2 found the strictly stronger quantitative form (STL robustness, G-A).
**Confidence:** 65% · **Complexity:** Medium · **Status:** Unexplored

## Ranked Ideas — Round 2: The Grader as a Multi-Component Loss

These compose into one architecture: **Layer 0** graded signals (G-A/B/C) → **Layer 1** calibrated components (G-F + round-1 #5) → **Layer 2** composition law (G-D/E) → **Layer 3** economics (G-G). Headline property: a vector of calibrated, noise-quantified components with one principled scalarization — trackable across epochs (IRT anchors), comparable across apps (bisim anchor + DIF audit), Goodhart-resistant (constraint + soft-min + tail certificates), with a real gradient path (STL margins) for future direct training.

### G-A. STL robustness margins: every assertion returns ρ ∈ ℝ, not a boolean
**Description:** Compile scenario assertions/expected outcomes into Signal Temporal Logic formulas over the timestamped DOM/a11y event trace; score each scenario by signed robustness margin ρ(φ, trace) — passed-by-this-much / violated-by-this-much; per-scenario loss = hinge(τ−ρ). Binary verdict = sign(ρ), so tolerance tiers survive as thresholds. Feeds the IRT backbone graded responses instead of dichotomous ones. Differentiable today via STLCG++ (2501.04194) / GradSTL (2508.04438, irregular sampling fits event streams).
**Axis:** G2
**Basis:** external: Fainekos/Pappas, Donzé/Maler robustness semantics; STLCG++/GradSTL; no published UI-trace application — open lane.
**Rationale:** Kills the variance collapse at the gate (a near-miss and a catastrophe stop being identical zeros); the single cleanest path from rubric score to future-differentiable loss.
**Downsides:** STL authoring discipline for scenario writers; smooth-semantics tuning.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Unexplored

### G-B. Falsification floor: adversarial robustness-minimizing search
**Description:** Treat scenario parameters (input values, step orderings, viewport, timing) as a search space; run S-TaLiRo/Breach-style falsification (annealing/CMA-ES, LLM-proposed mutations) minimizing ρ against the clone with the target as oracle. Loss gains a worst-case min-ρ component beside the mean; counterexamples auto-promote into the scenario corpus (data flywheel; hard IRT items for free). Replay caching keeps per-probe cost near zero after first resolve.
**Axis:** G3
**Basis:** external: S-TaLiRo/Breach, ARCH-COMP practice; open lane for web apps. reasoned: a fixed suite is a static train set the improvement loop will Goodhart; a search-based adversary is the only coverage mechanism that doesn't decay.
**Rationale:** Makes the loss adversarially supported rather than sample-supported — the quantity a training loop should actually minimize is worst-case divergence.
**Downsides:** Search budget; needs G-A first; perturbation space needs design care.
**Confidence:** 75% · **Complexity:** Medium-High · **Status:** Unexplored

### G-C. Bisimulation model-diff distance: the coverage-free global signal
**Description:** Active automata learning (LearnLib/ALEX pattern through the existing Playwright harness) extracts Mealy machines of target and clone; Sinkhorn Value Iteration computes the optimal-transport bisimulation distance between them. One continuous, scenario-independent behavioral distance per episode — the estimand the scenario fraction estimates — with an error model orthogonal to the suite's blind spots (instrument disagreement becomes detectable), comparable across apps/epochs by construction; the OT coupling localizes divergence to state-pairs (credit assignment).
**Axis:** G2
**Basis:** external: 2406.04056 (NeurIPS24); ALEX/LearnLib on web apps; quantitative model-diffing of clones is an open lane.
**Rationale:** Solves the units/comparability problem at the root and audits the scenario sampler.
**Downsides:** Heaviest lift of the round; L* query cost on real apps; state-abstraction choices are research-grade.
**Confidence:** 60% · **Complexity:** High · **Status:** Unexplored

### G-D. Constrained-loss formalization: gate-then-bonus as CMDP with shadow prices
**Description:** Equivalence ≥ τ as a hard feasibility constraint; perf/design/code/a11y bonuses as the objective inside the feasible set; Lagrange multipliers logged per epoch in the span store. λ = the exchange rate between constraint slack and bonus value ("one point of pass-rate is worth 3.2 points of design bonus this epoch") — directly promptable to the text-based learner and the single most informative dashboard curve.
**Axis:** G1
**Basis:** external: Safe RLHF / DRO 2506.13351; provably convergent primal-dual 2510.05703; Catastrophic Goodhart motivates constraint-form over weight-form.
**Rationale:** Same gate semantics as today, expressed as a citable, Goodhart-resistant mathematical object that survives unchanged into a gradient-training future.
**Downsides:** Mostly an aggregation-layer change — minimal; requires bounded bonus terms (clipping).
**Confidence:** 85% · **Complexity:** Low-Medium · **Status:** Unexplored

### G-E. The composition law: σ-standardized soft-min with tail-certified admission
**Description:** Every component enters in z-units standardized by its own G-theory σ (round-1 #5 supplies them). The bonus block aggregates by temperature-controlled soft-min (Chebyshev — reaches Pareto points linear weights cannot, per 2502.15145; resists easiest-proxy over-optimization per 2210.10760). The whole gate/tier/bonus rulebook is re-expressible as asymmetric pinball knots — one printable table of (dimension, knot, slope-below, slope-above), convex per dimension. Judged components are admitted to continuous duty only with a light-tail certificate (Hill/Pickands on frozen-replay error distributions); heavy-tailed components are demoted to binary gate duty or winsorized at a logged quantile (Catastrophic Goodhart).
**Axis:** G1/G4
**Basis:** external: 2502.15145, 2210.10760, Catastrophic Goodhart (NeurIPS24); direct: frozen replay + mutation audits already exist as the certification data source.
**Rationale:** An admission protocol, not a one-off check — every future component and judge-model swap re-certifies; a 2σ gain on the weakest dimension always beats a 10σ gain on the strongest.
**Downsides:** Temperature and slope parameters to tune (with provenance); tail estimation needs adequate replay-set size.
**Confidence:** 70% · **Complexity:** Medium · **Status:** Unexplored

### G-F. Judge measurement layer: pairwise Bradley-Terry + DIF invariance audit
**Description:** Judged dimensions switch from pointwise verdicts to pairwise snapshot-vs-snapshot comparisons (swap-and-averaged, default-fail preserved), fitted by Bradley-Terry onto the IRT scale via anchor comparisons — epoch-over-epoch progress becomes an estimated latent with SEs. A differential-item-functioning audit fits judge severity per (prompt-version × target-domain) and tests invariance; failures become per-app severity offsets at settlement. Within-prompt tie-rate monitored as the surrogate-drift leading indicator.
**Axis:** G4
**Basis:** external: 2603.12520 (pointwise ties on 2/3 pairs, within-prompt r=0.27, pairwise recovers 61% vs 21%); 2602.00521, 2605.00238 (IRT invariance machinery).
**Rationale:** The loss's job is detecting improvement deltas, and pointwise judging is demonstrably near-blind to exactly those; "comparable across target apps" becomes a tested claim.
**Downsides:** Pairwise calls scale with comparison graph size (mitigated by G-G's freed quota); BT linking needs anchor design.
**Confidence:** 80% · **Complexity:** Medium · **Status:** Unexplored

### G-G. Anytime-valid settlement: e-process early stopping over information-ordered scenarios
**Description:** Order scenario execution by Rasch Fisher information (most-discriminating first); run the gate decision as an e-process (E-valuator pattern, 2512.03109); settle the moment the verdict is statistically settled at level α. Every component reports as (estimate, σ, e-value, n-spent) — the anytime-valid CI is the noise quantification. Freed quota funds the falsifier (G-B) and pairwise tiers (G-F). Distinct from round-1 #4: that gates library admission; this is per-episode grading economics.
**Axis:** G5
**Basis:** external: E-valuator 2512.03109 (~90% accuracy at 80% cost); composes with accepted e-process gates and the IRT backbone.
**Rationale:** Compounding economics — the better the measurement system gets, the cheaper each measurement becomes; early-stopped scores remain legitimate longitudinal entries.
**Downsides:** Stopping rule must be audited against the G-theory noise model to avoid optimistic early calls.
**Confidence:** 85% · **Complexity:** Medium · **Status:** Unexplored

## Publishable Framing

Round-1 umbrella: **skill-library curation as a measurement system** (IRT capability backbone + G-theory instrument model + e-process gates + Shapley fitness), positioned against Voyager/SkillClaw/EvoSkills (systems side, no measurement models) and PSN-IRT/CITE (stats side, no agentic system). Round-2 wedge, sharpest novelty: **STL robustness semantics for UI event traces + falsification-based grading of LLM-built web apps + OT-bisimulation model diffing** — no prior art found for any of the three; together with the constrained-loss composition they form "a calibrated loss function for behavioral software cloning."

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Glicko/TrueSkill snapshot ratings | Merged into #2 as fallback estimator (less expressive item parameters) |
| 2 | Bid-price quota control (revenue management) | Premature unifying layer; value-per-token currency survives inside #2/#6 |
| 3 | CRM dose-finding for question-budget anneal | Narrow lever, below leverage floor; revisit when elicitation telemetry exists |
| 4 | Mid-episode surrogate endpoint (Athey-style surrogate index) | Needs #5's noise model first; Goodhart risk; deferred |
| 5 | Stagnation e-process tripwire | Folded into #6 |
| 6 | Pianka niche-overlap index | Folded into #3 routing-health telemetry |
| 7 | Hierarchical partial pooling for recurrence + NNT effect sizes | Folded into #4's evidence framework |
| 8 | Per-agent interference-curve cap estimation | Folded into #3 (the MDL interference term) |
| 9 | Hidden-state verifier-confusion loop variant | Folded into #6 |
| 10 | Frozen probe tickets | Folded into #1/#2 |
| 11 | TLA+/Alloy orchestrator model | Valuable hygiene but not training-loop math; revisit before parallel episodes |
| 12 | Pinball-loss unification (standalone) | Merged into G-E as the knot/slope formalization |
| 13 | DIF audit (standalone) | Merged into G-F |
| 14 | CARMO dynamic per-target rubrics | Registry is already per-target; dynamic criteria threaten cross-epoch comparability |
| 15 | Prometheus-2-style learned surrogate grader | Deferred — needs accumulated (trace, score) pairs + tie-rate drift monitoring; revisit when mid-episode loss density matters |
