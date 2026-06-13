---
title: 'feat: Agent Families Phase 3b — Training at Scale'
type: feat
status: active
date: 2026-06-10
origin: docs/agent-families/DESIGN.md
---

# feat: Agent Families Phase 3b — Training at Scale

## Summary

Make the closed loop trustworthy and scalable: the held-out benchmark suite with epochs and target rotation, the rehearsal pass with the one-shot metric (the system's headline capability curve), improvement-tier grading (equivalence-gated quality bonuses), the family router with agent splitting, parallel episodes with batch merging, enforcement activation (tripwire kill-mode, suspect-verdict consumption), and the RealWorld calibration target. Done means multi-epoch training runs across a rotated target pool with parallel episodes, scores on a control-charted held-out suite, and a one-shot-rate curve — the system DESIGN.md describes, fully operational.

## Problem Frame

Plan 4 closes the loop on one target; nothing yet distinguishes learning from memorizing, measures autonomy, rewards exceeding the target, or runs more than one episode at a time. This plan activates the machinery the design already specifies and earlier plans seamed: every §17 trigger gets its consumer, every shadow-mode detector gets its enforcement decision, and the §15 parallel invariants get their scheduler.

---

## Requirements

**Benchmark suite, epochs, rotation**

- R1. The held-out benchmark suite: 3–5 targets (one per archetype, never trained on), each onboarded via the target-generic harness with a frozen scenario slice (Plan 4's Kanboard micro-benchmark pattern, generalized); full-suite runs every N episodes, scores control-charted per target and aggregate.
- R2. Epochs: the `epoch` column populates; the training pool rotates per the §11 curriculum — linkding plus pool candidates passing the docker-boot check. Kanboard stays held-out (Plan 4's micro-benchmark); it may promote to the training pool **only after** a replacement held-out target is fully onboarded and Kanboard's frozen slice + accumulated SPC history are formally retired (a documented migration, not a config flip). Under persistent clones, revisit scores conflate accumulated code with library improvement — so the **generalization curve is measured by rebuild-probe episodes** (R6), with ordinary revisit scores reported as the engagement-progress curve alongside it.
- R3. The transfer-gap probe (one episode in a second output stack, ~once per epoch, late phase) ships as a runnable mode with its score-delta report — the §17 output-stack diversification trigger gets its instrument.
- R4. RealWorld/Conduit onboards as the **calibration target**: grader Gauge-R&R against the published spec (the only target whose ground truth isn't our own inference).

**Rehearsal pass and the one-shot metric**

- R5. Increment execution gains the rehearsal pass per §11: after convergence, re-execute all tickets as a fan-out — each in its own worktree from the **orchestrator-minted increment-base ref** (Plan 4's contract), in DAG waves (topological order, parallel within a wave), one shot each, no iteration, **consuming the episode's accumulated run-memory workflows** (that consumption is what makes one-shot rates a learning curve rather than a raw-model baseline) — merge in wave order, harness gate, one integration verify; all green → adopt the rehearsal artifact; any failure → fall back to the converged artifact and record the *passed-alone-broke-together* failure class for the reflector (its first integration-lesson source).
- R6. The one-shot metric: ticket one-shot rate (rehearsal tickets passing with zero iterations), increment one-shot, episode one-shot — tracked per (target, epoch, snapshot) alongside rubric scores. Under persistent clones, **episode one-shot is measured by periodic rebuild-probe episodes** (`mode=rebuild_probe`: fresh workspace, full-app opening prompt, one delivery pass) — ordinary episodes structurally cannot measure it. **Probe promotion (the clone-rot remedy):** when a probe's settlement score ≥ the engagement clone's latest revisit score on the must tier, the probe's artifact is promoted to become the engagement clone (workspace-pointer swap, recorded as an engagement event; the old workspace is retained for traces) — detection and remediation are the same instrument, and a declining code-structure score on the persistent clone is the corroborating rot signal.
- R7. Rehearsal economics per §17: rehearse every increment for the first episodes (metric baseline), then sample so rehearsal spend ≤ ~15–20% of episode budget; the converge-first → fan-out-first graduation is a computed cost-model crossover surfaced in reports, flipped by config.
- R8. The file-ownership lint promotes from warn to enforcement (serialize conflicting tickets into separate waves) — its Phase 1 consumer finally exists.

**Improvement-tier grading**

- R9. Lexicographic scoring per §10: behavioral pass rate is a hard gate (threshold derived from measured grader noise, must-tier panel-adjudicated); below it the bonus is zero, never offset.
- R10. Capped bonuses above the gate: (a) performance — k6/autocannon p95 latency ratio vs target under an identical load profile (credit capped at 2×) + Core Web Vitals (LCP, INP) as median-of-5 against absolute budgets; (b) design — MLLM pairwise screenshot judgment (CoT + rubric: hierarchy/readability/layout/typography), swap-and-average, invoked only for gross differences, uncertain = neutral, scored as absolute quality not target-similarity; (c) code structure — criterion-separated LLM rubric + lockfile/lint health (explicitly not MI/cyclomatic); (d) automated UX — axe-core, console-error absence, viewport checks.
- R11. Anti-Goodhart guards: per-dimension cap (~30% of the bonus pool), total bonus ≤ 15–20% of base, dimension-weight rotation across grading runs, human spot-audits sampled on high-bonus episodes.

**Router and agent splitting**

> **SUPERSEDED BY R3 — see plan 2026-06-12-010 (Phase C) R9.** The entire
> "Router and agent splitting" cluster below — **R12** (family router), **R13**
> (routing replay + agent split), **R14** (agent splitting / explorer-family
> decision), and the **R14b/R14c** family-pool retrieval + boundary-ticket
> multi-persona refinement — is **superseded by R3** and replaced by plan
> 2026-06-12-009 §6's Leiden *derive* pass (whole-store insight-level retrieval +
> derived module partition). There is no per-request family router and no
> persona/boundary path in the R3 runtime; the demoted `router.route` /
> `run_boundary_ticket` code is kept only for reversibility (plan-009 R13/R14,
> plan-010 R6/R7). The rest of plan 005 (R1–R11 benchmark suite, rehearsal /
> one-shot, improvement tier, **R8 file-ownership enforcement — now the assign
> stage's territory source**, parallel episodes, enforcement modes) still stands.

- R12. The family router activates: per request, an LLM routing call over the family's agent descriptions selects the specialist; **every routing decision is logged** (request, candidates, choice) — the data agent splitting and the §6 routing replay require.
- R13. Agent split per §6, gated on `min_routing_decisions`: silhouette over skill descriptions + minimum cluster sizes + the ≤25-word compressibility gate; contrastive sibling descriptions generated jointly; routing replay ≥90% agreement against logged decisions; transactional lineage (parent retired, children with `parent_id`, revertible until replay + one benchmark run pass); base-prompt residue check before commit.
- R14. The explorer-family decision executes: if Plan 4's instrument-health records show idea-shaped volume (explorer prompt/answer lessons recurring), seed explorer (and grader) families through the normal taxonomy; otherwise the sink remains. A documented decision point, not an automatic.
- R14b. **Family-pool retrieval with own-skills prior** activates with the router (DESIGN §4 "What an agent is"): once agents split, a worker retrieves over its family's whole active pool, own active skills weighted up and budget-first, siblings' insights as a relevance-gated fallback — ownership stays a governance boundary, not a retrieval wall. Budget cap (Plan 4 R3) bounds injected context regardless of pool size; the own-skills share is a config dial.
- R14c. **Boundary-ticket multi-persona refinement** (DESIGN §4 "Boundary tickets"): a ticket is flagged cross-cutting only when routing is ambiguous *or* its retrieved insights span ≥2 agent clusters (rare). Such a ticket runs as Ralph iterations over the shared artifact — the primary persona owns and drafts it; **at most one or two** additional personas (the other side of the boundary) each read the committed artifact and refine it; the verifier gates. Artifact-mediated only (no context relay); hard terminator = the 1–2 cross-persona cap + verifier acceptance as arbiter + the §7 no-progress tripwire; optional final reconciler pass. Composes with R14b retrieval; not a task split (one persona owns the ticket).

**Parallel episodes and batch merging**

- R15. Parallel episodes per §15: N episodes on N targets against one snapshot, **at most one in-flight episode per target** (frontier ledger, mention-coverage audit, persistent clone, and target container state are per-target serial); per-episode environment isolation (compose stack + clone port namespace per episode — the Phase 1 R15 seam activates); library read-only during episodes; all writes through the single-writer promotion queue; expect 2–3 concurrent before token-bound.
- R16. Batch merging per §15 (decided policy): validate each batch independently against the shared snapshot; merge all validated batches through registration (cosine prefilter/judge/ratchet absorb overlap); **one joint confirmation run on the union** before promotion; revert per-batch; joint-confirmation failure rate is the interaction-effect telemetry.
- R17. Validation runs (trials, benchmarks, replay re-judging, mutation audits) parallelize freely — read-only against library variants; the validation gate is the throughput bottleneck and parallelizes first.

**Enforcement activation**

- R18. Tripwires flip from shadow to kill: thresholds set from Phase 1–3's logged similarity distributions (per §17's shadow-first discipline); a fired tripwire ends the Ralph loop with the typed escalation it has logged all along.
- R19. Suspect-verdict consumption: tickets closed by a verifier flagged in a mutation audit are re-verified before their fitness events count; `instrument_suspect` episode scores (episode-level flags written by Plan 4's harness, already excluded from Plan 4's SPC) are additionally excluded from curriculum decisions here.
- R20. Question-budget annealing and persona rotation activate on their §17/§9 triggers (budget tightens across epochs toward 3–5; personas only if planner scores plateau), both as config changes consuming logged telemetry — no new machinery.

---

## Key Technical Decisions

- **Suite slices reuse the Plan 4 micro-benchmark pattern** (frozen must-tier slice + fixed frontier/seed + benchmark mode) per target — one mechanism, N instances; full registries can grow later without touching frozen slices.
- **Rehearsal failure is signal, never a loop** (§11): fall back to the converged artifact; the rehearsal exists for the metric and the integration-failure class.
- **Improvement bonuses ride the deterministic instruments wherever possible** (k6, CWV, axe, lockfile audits); LLM judgment only where research says it's reliable (gross pairwise design differences), neutral on uncertainty.
- **The router is also the routing-decision logger** — splitting was deliberately starved until this data exists; the `min_routing_decisions` gate (config, ~low hundreds) is the §6 small-N noise guard.
- **Parallelism is a scheduler change, not a redesign** — the §15 invariants (read-only library, single-writer queue, snapshot keying, env isolation) were enforced from Phase 0 precisely so this plan only adds the scheduler and the compose namespacing.
- **Enforcement flips are config promotions of logged detectors** — every kill/exclusion mode activated here has been logging in shadow since Phase 1; no detector is invented and enforced in the same plan.

---

## Implementation Units

### U1. Benchmark suite and epochs

- **Goal:** The generalization instrument: N held-out targets, rotation, control charts.
- **Requirements:** R1, R2
- **Dependencies:** Plan 4 U4 (pattern), U7 (SPC)
- **Files:** `agent-families/targets/<suite-targets>/`, `agent-families/src/agent_families/grading/{benchmark.py,suite.py}`, `agent-families/src/agent_families/pipeline/curriculum.py`, `agent-families/tests/{test_suite.py,test_curriculum.py}`
- **Approach:** Onboard 3–5 archetype targets through the generic harness (docker-boot qualification per DESIGN §11); generalize the frozen-slice mechanism; suite runner (every N episodes, parallelizable per R17); epoch bookkeeping + rotation order; per-target and aggregate control charts on the Plan 4 SPC machinery.
- **Test scenarios:** suite run produces per-target + aggregate scores keyed (target, epoch, snapshot, mode=benchmark); rotation order deterministic per epoch seed; revisit curve query (same target across epochs) returns the generalization series; a training-pool target is rejected as a suite target (held-out constraint enforced); control-chart limits recompute only on suite runs.
- **Verification:** two fixture epochs produce a coherent generalization curve.

### U2. Rehearsal pass and one-shot metric

- **Goal:** The autonomy instrument and the integration-failure signal.
- **Requirements:** R5–R8
- **Dependencies:** Phase 1 U4/U6 (state machine, gate), Phase 2 U6 (episodes), Plan 4 U3 (run memory — one-shot sessions consume it)
- **Files:** `agent-families/src/agent_families/pipeline/rehearsal.py`, `agent-families/tests/test_rehearsal.py`; touches `pipeline/{episode.py,planning.py}` (wave scheduling, lint promotion)
- **Approach:** Post-convergence fan-out: worktrees from the increment-base ref, **provisioned as template copies with the increment-base tree checked out over them** (a bare git worktree has no `node_modules` — Phase 1's copy-installed-template pattern applies per worktree; a shared read-only junction-linked `node_modules` is the tested Windows alternative; per-worktree setup time/disk feeds R7's crossover cost model); DAG waves with file-ownership serialization (lint promoted to enforcement); one-shot sessions (no Ralph loop) receiving the episode's workflow ledger under the standard injection budget; wave-order merge, gate + single integration verify; adopt-or-fallback; *passed-alone-broke-together* typed failure records; metric computation per R6; sampling + cost-model crossover per R7.
- **Test scenarios (fake-driven):** diamond DAG rehearses in two waves with the conflicting-files pair serialized; all-green rehearsal adopts the fan-out artifact (workspace head moves); one failed ticket falls back to converged artifact and writes the integration-failure record; one-shot rates computed correctly across fixtures; sampling honors the budget share; crossover report compares both regimes' measured costs.
- **Verification:** metric appears in settlement reports; fallback path leaves the episode equivalent to a no-rehearsal run.

### U3. Improvement-tier grading

- **Goal:** Grade "better," not just "same," without corrupting the reward.
- **Requirements:** R9–R11
- **Dependencies:** Phase 2 U7 (settlement), Plan 4 U7 (gate-threshold derivation)
- **Files:** `agent-families/src/agent_families/grading/improvement.py`, `agent-families/tests/test_improvement.py`
- **Approach:** Gate from measured noise; **perf/CWV measurements run against a production artifact of the clone** — `vite build` + `vite preview` (and Hono in production mode) as a settlement-time step, never the dev server (dev-mode overhead would systematically zero the dimension); build failure is a typed zero-bonus outcome; k6 profile harness (identical load on both apps; p95 ratio capped), CWV median-of-5 vs absolute budgets; pairwise design judge (swap-and-average, gross-difference gating, neutral-on-uncertainty) via the judge seam; code-structure rubric + lockfile/lint audits; axe-core/console/viewport checks; composition per R11 (caps, rotation, spot-audit sampling); all bonus components itemized in the settlement report.
- **Test scenarios:** gate-fail zeroes the bonus regardless of stellar components; per-dimension cap and total cap enforced; p95 credit caps at 2×; uncertain design judgment scores neutral (fixture); weight rotation changes composition between runs deterministically by seed; spot-audit sampling flags the configured fraction; folklore metrics (MI/cyclomatic) provably absent from the rubric inputs.
- **Verification:** decision table for gate × caps × rotation has 1:1 tests; one live dual-app bonus run documented.

### U4. Family router and agent splitting

- **Goal:** The taxonomy self-reorganizes — the design's most novel mechanism, finally fed.
- **Requirements:** R12–R14, R14b (family-pool retrieval with own-skills prior), R14c (boundary-ticket multi-persona refinement)
- **Dependencies:** Plan 4 U8 (maintenance pass, lineage seam), Plan 4 U2 (retrieval — extends its scope per R14b), Phase 0 U2 (store)
- **Files:** `agent-families/src/agent_families/library/router.py`, `agent-families/src/agent_families/reflector/agent_split.py`, `agent-families/src/agent_families/library/retrieval.py` (extend Plan 4's retrieval to family-pool + prior + fallback), `agent-families/tests/{test_router.py,test_agent_split.py,test_boundary_ticket.py}`
- **Approach:** Router = judge-seam call over family agent descriptions, decision logged (request hash, candidates, choice, confidence); agent-split candidacy in the maintenance pass behind `min_routing_decisions`; §6 mechanics end-to-end (silhouette + sizes + compressibility gate; joint contrastive descriptions; routing replay against the log; lineage transaction revertible until replay + benchmark pass; base-prompt residue check); explorer-family decision point documented with its evidence query.
- **Test scenarios (router/split):** routing decisions logged with full candidates; split candidacy refuses below the decision-count gate; fixture agent with two planted skill clusters splits with ≥90% replay agreement; replay below threshold rejects and preserves parent; reverted split restores routing identically; residue in a base prompt blocks the split with the conversion warning; children inherit correct skill partitions including quarantined members.
- **Required acceptance tests for R14b/R14c (these exact behavioral assertions MUST exist and pass — do not substitute weaker ones; extend the `## Conformance` note mapping each invariant → its test name):**
  - `test_boundary_retrieval_crosses_clusters` — fixture: two split agents, disjoint clusters A/B. A boundary query (embeds near both) → retrieved set contains ≥1 insight from A **and** ≥1 from B, and injected tokens ≤ R3 budget. *(ownership ≠ reachability, with no bloat — the core API-contract case)*
  - `test_indomain_retrieval_stays_in_cluster` — same fixture; an in-domain-A query → **zero** cluster-B insights (fallback dormant). *(specialist stays sharp — the "don't collapse to generalist" invariant)*
  - `test_prior_dial_monotone` — raising the own-skills budget share strictly reduces the count of fallback (cross-cluster) insights retrieved for a fixed boundary query. *(the dial is real and tightenable)*
  - `test_boundary_trigger_is_gated` — a non-boundary ticket runs exactly one persona pass; only a ticket whose retrieved insights span ≥2 clusters (or ambiguous routing) enters the multi-persona path. *(rare by construction)*
  - `test_boundary_pass_cap` — count persona-switch events: the multi-persona loop performs **≤2** cross-persona passes, never more, under any fixture. *(hard terminator)*
  - `test_boundary_oscillation_halts` — an oscillation fixture (persona A and B making opposing edits) terminates via the cap + §7 no-progress tripwire within bounded passes; it does not loop. *(anti-thrash)*
  - `test_boundary_is_artifact_mediated` — assert the second persona's input contains the **committed** artifact state from the first persona's pass (read from the work product), and that no persona receives another's hidden reasoning/context. *(artifact-mediation, not context-relay — the MAST/Cognition failure this design rejects)*
- **Verification:** all seven named R14b/R14c tests pass; the `## Conformance` mapping is complete; a fixture split + boundary ticket runs end-to-end producing a negotiated artifact within the pass cap.
- **Verification:** one fixture split survives the full transaction including a benchmark-pass gate.

### U5. Parallel episodes and batch merging

- **Goal:** Wall-clock scale within quota reality.
- **Requirements:** R15–R17
- **Dependencies:** Plan 4 U7 (validation), Phase 2 U2/U6 (target env, episodes)
- **Files:** `agent-families/src/agent_families/pipeline/scheduler.py`, `agent-families/tests/test_scheduler.py`; touches `grading/target_env.py` (per-episode namespacing)
- **Approach:** Episode scheduler (N concurrent, config; quota-aware admission); per-episode compose project names + port namespaces (the Phase 1/2 seams activate); batch merging per R16 (independent validation against the shared snapshot → merge-all through registration → joint confirmation run → promote; per-batch revert); parallel validation runners per R17.
- **Test scenarios (fake-driven):** two concurrent fixture episodes never contend on the library (read-only verified) and serialize at the queue; port/compose namespaces disjoint; two batches validated against the same snapshot merge with the prefilter absorbing a planted near-duplicate; joint-confirmation failure (planted interaction) blocks promotion and records the telemetry; per-batch revert after a merged promotion removes exactly one batch's insights; validation runs execute concurrently.
- **Verification:** determinism under a **canonical merge order** — validated batches sort by a stable key (episode ID) before registration; the parallel run's post-merge state is byte-identical to a sequential run using the same shared snapshot and the same canonical order. (True order-independence is unachievable: judge-mediated dedup makes the surviving canonical row a function of registration order — the canonical sort is the fix, not a stronger claim.)

### U6. Enforcement activation and annealing

- **Goal:** Every shadow detector gets its consumer; every trigger its config.
- **Requirements:** R18–R20
- **Dependencies:** Phase 1 U7 (tripwire data), Phase 2 U8 (audit flags), Plan 4 U7
- **Files:** `agent-families/src/agent_families/pipeline/enforcement.py`, `agent-families/tests/test_enforcement.py`
- **Approach:** Tripwire thresholds derived from logged distributions (documented derivation, config-pinned with provenance per §17); kill-mode wiring to the existing typed escalations; suspect-verdict re-verification hook before fitness counting; `instrument_suspect` exclusion from SPC/curriculum; annealing schedule + persona-rotation trigger as config consuming logged telemetry.
- **Test scenarios:** tripwire fires only above the derived threshold (fixture distributions); killed loop escalates with the existing typed record (no new failure shape); suspect verifier's ticket re-verified before fitness lands; suspect episode scores excluded from chart recompute; annealing schedule steps the budget per epoch; rotation trigger fires only on the plateau condition.
- **Verification:** shadow-vs-enforce A/B on fixture data shows identical detection, differing only in action.

### U7. RealWorld calibration and scale e2e

- **Goal:** Calibrate the grader against published ground truth; prove the whole system.
- **Requirements:** R4, end-to-end over R1–R20
- **Dependencies:** U1–U6
- **Files:** `agent-families/targets/realworld/`, `agent-families/tests/test_e2e_scale.py`, `agent-families/README.md`
- **Approach:** Onboard one RealWorld implementation, pinned by commit/digest like every target; author its slice from the published spec — then **pre-screen**: execute the slice once against the implementation, hand-adjudicate every spec/implementation divergence (real-world implementations deviate from the spec routinely), exclude-and-record deviating behaviors from the Gauge-R&R denominator so target error never lands in the grader's calibration number; Gauge-R&R the grader against the screened spec-derived verdicts; scale e2e: a fixture multi-epoch run (2 epochs × 2 parallel episodes × rotation) through suite scoring, rehearsal metrics, merged batches, and one agent split; README gains the training-operations runbook (start/suspend/resume a training campaign, read the curves, respond to instrument alarms).
- **Test scenarios:** grader verdicts vs spec-derived expectations quantify agreement (the calibration number); fixture campaign produces: generalization curve, one-shot curve, merged-batch lineage, a completed split — all queryable; runbook commands execute against the fixture state.
- **Verification:** offline campaign e2e green; the documented live campaign procedure is the project's operating manual.

---

## Scope Boundaries

**Deferred beyond this plan (the design's own §17 future triggers):** output-stack diversification (transfer-gap probe ships, the decision waits on its data); routing-contention split trigger (needs the routing telemetry this plan starts collecting); GEPA-style library-lineage tournaments (explicitly an experiment mode, not the merge policy); deployment packaging (CLI wrapper — post-training product work), **including the context-retriever family's deployment implementation** (codebase/internet research backing the question channel when no simulator exists — the family is seeded from Phase 0 but its research capability ships with the wrapper); multi-account/API scaling (excluded by standing decision).

**Non-goals:** no enforcement mode ships without its shadow-mode data trail; no new detector is invented and enforced in the same plan; no API-key usage.

---

## Risks & Dependencies

- **Token-bound parallelism:** 2–3 concurrent episodes is the expected ceiling on one Max subscription; the scheduler's quota-aware admission is the guard, and the Agent SDK credit (post-June-15) re-measurement from Plans 1–4 sets the real number.
- **Suite onboarding is the labor item** (3–5 targets × harness + slice + hand-verified verdicts) — amortized by the target-generic harness, bounded by slice-first registry scoping; suite targets can onboard incrementally (suite of 2 is valid early).
- **Design-judge reliability stays coarse** (gross differences only) — accepted; the improvement tier's deterministic components carry the weight.
- **Agent splitting on early data:** the `min_routing_decisions` gate plus the replay/benchmark transaction are the guards; the first split should be expected to revert at least once.
- All Plans 0–4 risks carry forward; five unimplemented plans deep, the schema migrations remain the integration checkpoints, and implementation order is strictly Plan 0 → 5.

---

## Sources & Research

- DESIGN.md §10 (improvement tier, anti-Goodhart, calibration), §11 (rehearsal, one-shot metric, suite composition, curriculum), §6 (agent split mechanics), §15 (parallelism invariants, batch-merge policy, quota), §16–§17 (phase definition, triggers, enforcement discipline)
- Plans 0–4: consumed contracts; the §17 trigger list is this plan's requirements source
- Session research: improvement-grading research (SWE-Perf gate-then-measure, constrained-RLHF gating, Lighthouse median-of-5, pairwise-design reliability limits, MI/cyclomatic criticisms, reward-hacking guards) — implemented in U3; skill-library and judge research underpinning the merge policy and enforcement posture
