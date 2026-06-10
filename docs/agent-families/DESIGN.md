# Agent Families: A Self-Growing Agent System for Reverse-Coding Applications

**Status:** Design — pre-implementation
**Revision:** 2 — 2026-06-10. Incorporates the external-research review (skill-library governance, judge reliability, multi-agent failure taxonomies, improvement-oriented grading), the reflector specification, the episode-based training-loop execution model, and parallel-experiment readiness. Revision 1 sections amended in place; research citations in §18.

## 1. Vision

Build a tool that can reverse-code any application by observing it the way a user would — clicking around, seeing the features — and then rebuilding it in a modern stack. The system is organized as **agent families** (plan, work, verify, plus a shared context retriever), and it improves itself through exactly one operation: **adding an idea**. Ideas accumulate into skills, skills accumulate into agents, and agents split into specialists as their skill sets grow — so the system's capability taxonomy grows automatically from a stream of insights, driven by an automated training loop that builds clones of real apps and grades the results.

## 2. The core reframe: this is not RL

Nothing in this system updates model weights. There is no gradient, no policy, no reward model to train. It is an **evolving skill library with text-based credit assignment**. The learnable parameters are:

1. The skill texts (grown by adding insights)
2. The routing index (embeddings over insights)
3. The agent taxonomy (which splits over time)

The "reward" is the grading agent's rubric score; the "update operator" is `add_idea()`.

**Foundation verdict (from the research review, §18):** every load-bearing mechanism here has direct, quantitative prior art — the reflector ≈ ACE's Reflector / GEPA's failure diagnosis; the skill library ≈ Voyager/AWM/Dynamic Cheatsheet; plan→work→verify ≈ MetaGPT/ChatDev with MAST's failure taxonomy mapping onto it; iterate-until-verified is validated by Huang et al. (external grounded feedback is *necessary*); behavioral cross-stack rewriting is Mechanical Orchard's and AWS Transform's production method. Three pieces appear **genuinely novel** (no documented prior art found): silhouette-triggered agent splitting, the self-reorganizing routed taxonomy, and explorer-generated scenario manifests closing a training loop.

The research's strongest warning, adopted throughout this revision: **LLM-authored skill libraries are worth approximately nothing without governance** (Library Drift: +0.0pp vs. +16.2pp human-curated; flipped to +0.328 vs +0.002 by cap + retirement + structural schema), and **library size itself degrades routing** (Skill Shadowing: 21% pass-rate drop at 202 skills; selection accuracy 88%→53%). Dedup and pruning matter as much as adding — and *bounding* matters more than both.

## 3. Agent families

An **agent family** is a router plus N specialist agents. The router receives a request and fans it out; specialists complete work and return a unified response; a Ralph loop (§7) reruns work until a checker confirms completion. Each family starts with one generic specialist and no skills, and grows via idea registration and splitting.

### Pipeline families (the deployed system)

| Family | Role | Writes | Executes |
|---|---|---|---|
| **Planner** | Turn a request + Q&A into a fleshed-out, ticketed feature list divided into units of work | Tickets only | Nothing |
| **Worker** | Take a ticket + retrieved context and code it | Code + tests | Its own unit tests only (scoped — see §8) |
| **Verifier** | Check work against codebase conventions, requirements, unit correctness, and integration | Verdicts | Build, integration tests, browser |
| **Context retriever** | Answer questions from the codebase, the internet, or (in training) the human simulator. Called by plan/work/verify; never routes work itself | — | Read-only research |

The workflow is always **plan → work → verify**.

### Training-only families

| Family | Role |
|---|---|
| **Explorer (human simulator)** | Looks at a target app through the UI only; maintains the exploration frontier; writes a human-style build prompt; answers clarifying questions in natural language; performs acceptance (UAT) on delivered increments |
| **Grader** | Builds the per-target feature registry; after each episode, compares the built app against the target across every instrument available and produces rubric scores plus improvement-tier scores |
| **Reflector** | Two-stage: deterministic attribution over the traceability store, then LLM root-cause per failure cluster, emitting a quarantined idea batch (§12) |

## 4. Data model

**The single most important structural decision: never dissolve insights into skills.** Insights are atomic records, forever. A skill is a *view* — a named, ordered group over its member insights.

```
Insight  { id, text, embedding, scope_tag, source_run_id, created_at,
           status: quarantined | active | dormant | retired,
           fitness: { retrievals, wins, losses, causal_blames } }
Skill    { id, agent_id, name, description, insight_ids[], token_count }
Agent    { id, family_id, parent_id?, description, base_prompt_specialty,
           permissions, skill_ids[], active_cap }
Family   { id, charter, router_prompt, agent_ids[] }
```

Atomic insights are what make everything downstream possible: re-clustering, splitting, dedup, contradiction repair, per-insight fitness tracking, and **rollback** (disable a batch of insight IDs if validation fails).

### What an agent is (R3 — evolved definition)

Earlier shorthand treated an agent as "a directional prompt with privately registered skills" — a capability *container*. That fused three things better kept separate: identity, what it owns, and what it can reach. The current definition: **an agent is a specialist *lens* over its family's shared knowledge**, with four decoupled facets:

- **Persona** — a thin base prompt (frozen family template + generated specialty section, §6) that fixes its role, domain, and *judgment*. Specialization lives primarily here: two agents handed identical retrieved insights reason differently because of it.
- **Owned skill cluster** — the coherent insight group it owns (`skill_ids[]`). Ownership is for *bookkeeping*: the active cap, fitness accounting, retirement, split lineage, and the clean `SKILL.md` export. The cluster is also the agent's **retrieval prior**.
- **Routing identity** — its `description` and taxonomy position, which the family router selects on.
- **Retrieval policy** — own-skills-first (weighted, claims most of the token budget), with a **relevance-gated, budget-capped fallback to the rest of the family's active pool** for boundary tickets.

Three invariants keep it a specialist, not a generalist:

- **Ownership ≠ reachability.** An insight is owned by exactly one agent (for governance) but retrievable by any sibling in its family by relevance. The partition organizes; it does not blindfold. This corrects the earlier "retrieve only the agent's own active set" wording, which stranded jointly-needed knowledge at agent boundaries — e.g., an API-contract ticket needs both backend-limitation and frontend-UX insights, which live in different specialists.
- **Specialization is a dial, not a wall** — the own-skills share of the retrieval budget. Tighten it and the specialist stays sharp; the fallback only fires when a cross-domain insight out-scores the in-domain margin (a genuine seam), so it is invisible on ordinary tickets.
- **The agent stays thin.** Knowledge lives in retrieved insights, not the prompt; the persona carries role and judgment, not facts.

**Why this does not bloat context:** injected context is bounded by the *token budget*, independent of pool size — a wider pool changes *which* insights are eligible (better candidates at a boundary), not *how many* are injected. Relevance gating means a normal specialist ticket retrieves the same focused own-domain set it always would. ANN over the whole family pool is the same cost as over a partition at this scale; the scope is the agent's *family* pool, never planner/verifier insights.

### Boundary tickets: multi-persona refinement (R3)

Some tickets are inherently cross-cutting — an API contract, a shared schema, an auth boundary — and are *negotiations between two legitimate lenses* (backend wants minimal coupling/load; frontend wants ergonomic responses), not just facts to merge in one head. Retrieval-with-fallback gets the *knowledge* to one agent, but a single persona may blend the perspectives into a bland compromise rather than a sharp negotiated result. The sanctioned answer is **rare, artifact-mediated, verifier-gated**:

- **Trigger (rare by design):** a ticket is flagged cross-cutting only when routing is ambiguous *or* its retrieved insights span ≥2 agent clusters. The default path stays single-persona; this fires only at genuine seams.
- **Mechanism = Ralph iterations over the shared artifact, never context-relay.** The primary persona owns the ticket, drafts/edits the artifact, and commits; at most **one or two** additional personas (those on the other side of the boundary) each read the *committed artifact* and refine it, committing in turn; the verifier gates. Agents communicate only through the durable work product. The failure mode MAST/Cognition condemn is *relayed context* ("let me explain what I was thinking"), not iterative refinement of a legible artifact — the same artifact-mediated principle as the plan→work→verify pipeline itself.
- **Hard terminator (anti-thrash):** at most 1–2 cross-persona passes; the verifier's acceptance criteria arbitrate; the §7 no-progress tripwire catches oscillation (A simplifies, B re-complicates). An optional final reconciler pass converges a contested artifact.
- **Composes with retrieval:** each pass uses prior+fallback retrieval to be competent in its lens; the multi-pass adds the second lens. This is *not* splitting a task across agents — one persona owns the ticket; the others are bounded consultations on the shared artifact.

Phase 5 machinery (needs the router + the cross-cutting trigger); Plans 0–4 run single-persona.

### Structural insight schema (new in R2)

Every insight is authored against a fixed structural template: **precondition / action-pattern / expected-outcome** (plus scope tag). This is the "meta-skill structural prior" from Library Drift — under it, explicit dedup machinery becomes largely unnecessary, because structurally homogeneous insights collide visibly. The reflector (§12) and any manual `add_idea` caller must emit this shape; the registration judge rejects free-prose insights.

### Ratchet governance: the active set is bounded (new in R2)

The library breathes in both directions:

- **Active cap:** each agent's *active* skill set is bounded (start ~50 skills; a tunable, not a principle — but the existence of a cap is a principle). Routing and retrieval operate only over **active-status** insights (active vs. dormant/quarantined). Note on *scope*: retrieval reaches the whole **family** active pool with an own-skills prior (§4 "What an agent is"), not just the agent's own partition — ownership is a governance/cap boundary, not a retrieval wall.
- **Outcome-driven retirement:** insights/skills that stop earning retrievals-with-wins are demoted to a **dormant archive** — preserved, searchable, revivable, but out of the routed index. Admission of a new skill beyond the cap displaces the weakest incumbent (tournament admission).
- Fitness pruning remains, but it is no longer the load-bearing defense; the cap is. Library *size*, not registration quality, is the empirically dominant failure mode.

### Skills: group is the noun, compile is a patch operator

Three layers, kept distinct:

1. **Storage (the definition):** pure membership — `{name, description, ordered insight_ids[]}`.
2. **Runtime rendering (derived, cached):** concatenation is the v1 renderer — lossless, transparent, debuggable. When a skill crosses a size threshold, a **compile step** renders the group into a clean document with per-section provenance annotations back to insight IDs. **(Amended in R2):** compilation is **delta-patching, never full re-render** — a new insight produces a localized section edit; untouched sections stay byte-identical. Full re-render happens only at skill splits or explicit maintenance. Rationale: ACE documents "context collapse" under iterative full rewrites (detail silently erodes), and full rebuilds defeat the byte-stability that keeps prompt caching alive — exactly when the library churns most. The compiler never edits insights; insights stay the source of truth, or rollback is lost.
3. **Export:** a skill group maps directly onto the Claude Code skill format — `SKILL.md` with `name:`/`description:` frontmatter, body = compiled rendering. The whole project can be exported as a set of real skills for real users.

### Routing signal (amended in R2)

Routing — for idea placement, runtime retrieval, and split replay — operates on **full insight/skill text, not description centroids**. SkillRouter measured a 31–44pp routing-accuracy loss when skill bodies are dropped from the signal. Descriptions remain the human-facing layer and the agent-split compressibility probe, but they are no longer the retrieval substrate. Additionally, a deterministic prefilter runs before any LLM judging: **cosine > 0.92 between a new idea and an existing insight short-circuits to merge review**.

## 5. Idea registration and lifecycle

The one operation: `add_idea(text)` — but an idea now has a *lifecycle*, not just a placement.

### Registration

1. **Schema check:** the idea must arrive in the structural template (§4). Free prose is bounced to its author for restructuring.
2. **Cosine prefilter:** > 0.92 against an existing insight → auto-propose merge; the LLM judge only adjudicates ambiguous placements.
3. **Embed; ANN-search the insight index** (full text). Top ~10 hits yield candidate (family, agent, skill) triples.
4. **LLM judge decides:** append to skill X / create new skill under agent Y / duplicate of Z — merge or discard / contradicts Z — replace or flag.
5. **Generalization lint** (anti-leak): reject or rewrite ideas that name target internals. "Web apps commonly have role-gated admin areas; ask about them during elicitation" passes; "this app's admin is at /wp-admin" is target trivia. The reflector *proposes* a scope tag (`universal` / `stack:<x>` / `domain:<y>`) with justification; the judge — which holds the library context — confirms or overrides. The judge-override rate on scope tags is tracked as reflector-drift telemetry.

### Lifecycle: quarantine → validate → promote (or auto-revert) (new in R2)

Reflector batches do **not** enter the active library on registration. The default trust polarity is *suspect until proven*:

1. **Quarantine:** the batch registers with `status: quarantined` — retrievable only in designated trial runs, tagged in every trajectory it touches.
2. **Validation:** (a) *optional causal check* — replay the source episode's failed slice (the failing tickets/scenarios only) with the batch active: do these ideas actually address these failures? (b) *required generalization check* — a held-out micro-benchmark must show non-negative delta vs. the incumbent library.
3. **Promote or auto-revert.** Reversion is the default outcome of a failed validation, not a manual rescue. Promoted insights become `active` (subject to the cap's tournament admission).

This is TextGrad's validation-based reversion applied to the library, and it is the structural answer to Huang et al.: reflector ideas are self-generated refinements, the exact class of edit that degrades without external grounded validation. It also gives clean per-batch causal attribution — "which batch broke things" is a lookup, not forensics.

### Rollback statistics (new in R2)

Benchmark-driven rollback decisions use **control limits, not raw thresholds** (statistical process control): act on special-cause variation, not run-to-run grader noise. Reacting to common-cause variance as if it were signal provably destabilizes the process (Deming's funnel), and grader noise (§10) is comparable in magnitude to per-batch effects.

## 6. Splitting

### Skill split (deterministic trigger)

Split when `token_count > ~2,000` OR `insight_count > ~25`. Mechanism: k-means (k=2) on member insight embeddings; accept if silhouette > ~0.3, else LLM thematic split. LLM generates the two new names/descriptions. **(R2 note:)** do not evaluate split candidacy until the skill has accumulated a minimum observation count; clustering metrics on small N are noise, and the cosine merge prefilter (§5) should prevent most premature growth in the first place.

### Agent split

Periodically embed the agent's *skill descriptions*; compute silhouette for k=2..4. Trigger when:

- (a) best silhouette > ~0.35, **and**
- (b) each cluster would have ≥ 3 skills, **and**
- (c) an LLM gate confirms: ask an LLM to write a ≤25-word description covering all the agent's skills, then ask a judge whether it actually covers them. Failure to compress = split signal.

Hard cap regardless: if (base prompt + skill index) exceeds the context budget, force a split. **(R2 note:)** once routing telemetry exists (Phase 3+), evaluate **routing contention** — two skills repeatedly competing for the same incoming ideas — as a complementary split/merge trigger; shadowing is fundamentally a routing failure, and a routing-behavior-derived signal targets it more directly than static geometry. Deferred until the telemetry exists.

### Rewriting descriptions and prompts after a split

- **Derive bottom-up, never edit the parent's text.** Generate each child's description fresh from its cluster's contents (the ≤25-word compressibility test *is* the generator, run per cluster).
- **Write the siblings jointly and contrastively**, in one LLM call, with the rest of the family's descriptions in context. "Not responsible for X — that's handled by [sibling]" clauses are encouraged.
- **Validate with a routing replay before committing.** Re-run the router (over full text, per §4) against historical routing decisions and cluster membership; require ~90% agreement. Below that, regenerate; if it won't converge, the clusters weren't separable — reject the split.
- **Base prompts are three parts**, so a split only regenerates the thin middle layer:
  1. *Family template* (shared, frozen): role, workflow position, permissions, output contract, Ralph completion contract.
  2. *Specialty section* (generated): the description expanded to a paragraph, plus: out-of-scope requests are returned to the router, never attempted.
  3. *Skill index* (derived from the cluster's skills).
- **Keep base prompts thin.** Anything substantive hand-patched into a base prompt doesn't travel through a split — knowledge belongs in insights. At split time, diff the parent prompt against the family template; flag non-template residue for conversion into insights before the split commits.
- **Lineage:** retire the parent (frozen, with history); create children with `parent_id`. The split is a transaction — revertible until the new pair passes the routing replay *and* one benchmark run.

The family router updates for free: it is just a prompt listing agent descriptions.

## 7. Ralph loops

The Ralph loop is not a trained component. It is a while-loop where a checker is the stopping condition:

```python
feedback = None
for i in range(MAX_ITERS):                       # cap it; escalate on exhaustion
    result  = run_agent(task, context, feedback) # fresh process each iteration
    verdict = run_checker(result)                # produces evidence
    if verdict.passed: break
    feedback = verdict.failures                  # TYPED records, never prose (R2)
```

Rules:

1. The checker produces **machine-checkable evidence** — the evidence requirement lands on the **checker**, not the producer. The producer's self-report is explicitly untrusted.
2. The completion contract is a structured output schema.
3. Each iteration is a **fresh context** reading a persistent progress file. **(R2:)** progress files are **append-only structured ledgers** (typed entries with unit-id and evidence pointers), never rewritten wholesale — the ACE context-collapse result applies to any LLM-maintained document, and the progress file is the loop's only memory.
4. Hard iteration cap with escalation — **plus tripwires that fire before the cap (R2):**

### Typed failure feedback and tripwires (new in R2)

- `verdict.failures` is a **typed schema** whose failure-kind enum draws from the MAST taxonomy (step repetition, reasoning-action mismatch, termination-unaware, incorrect verification, …) plus `{location, expected, observed, repro_command}`. Models incorporate structured feedback measurably better than prose (Feedback Friction), and the same typed labels flow into the reflector's evidence and per-insight fitness.
- **Step-repetition tripwire:** the orchestrator embeds each iteration's diff/approach summary; iteration N ≈ N−1 (cosine above threshold) → kill the loop *now* with a typed `REPEATED_ATTEMPT` escalation instead of burning to MAX_ITERS. Step repetition (15.7%) and termination-unawareness (12.4%) are the top two failure modes in 1600+ annotated multi-agent traces, and wasted iterations are this system's scarcest resource (§15).
- **No-progress detector:** identical failure set across two consecutive iterations, or diff churn with no verdict movement → early escalation.

"Machine-checkable" means *checkable by something other than the producer's self-report* — a spectrum: deterministic validator > independent judge > self-report. Each family's contract sits as far toward deterministic as its artifact allows.

### Per-family contracts

**Worker → Verifier (code):** worker emits diff + "done" claim; verifier executes build/tests/browser and returns an evidence-backed typed verdict; failures become next-iteration feedback. A **deterministic harness gate** (orchestrator-run compile/typecheck/lint, optionally the unit suite) bounces trivial failures straight back to the worker without waking the verifier.

**Planner → Plan-checker (plans):**

- *Deterministic plan lints:* every requirement (REQ) maps to ≥1 ticket (coverage matrix over IDs — see §12's traceability schema); every ticket has non-empty acceptance criteria linked to REQs; dependency graph is a DAG; no ticket exceeds the unit-of-work size budget; all mandatory traceability links present and well-formed; **file-ownership partitioning** — tickets in the same increment claiming the same files are flagged or serialized (load-bearing for §11's rehearsal fan-out).
- *Judged checks:* each ticket independently implementable? Acceptance criteria *testable*? Adversarial pass for missing edge cases and ambiguities that should have become questions.
- *Assumption verification:* extract the factual claims each ticket depends on, phrase as questions, send through the retriever → human simulator. Wrong plans embed false beliefs; this converts "is the plan right?" into checkable propositions.

**Verifier meta-contract:** every verdict attaches evidence for every claim — non-empty output, exit codes, screenshots, **and a stored repro command per check** (load-bearing for §12 step 7). Deterministically checkable.

**Explorer contracts:** every inventory/registry claim carries a screenshot or interaction trace; **(R2:)** every Q&A *answer* is also checked (§9) — the explorer was previously the only producer in the system without a checker.

## 8. Permission model

Roles are pinned by **effects**: planner writes tickets, worker writes code, verifier executes-and-verdicts. Reads were never the restricted resource — the context retriever exists as a shared read-only oracle.

**The invariant worth defending is the verdict monopoly, not the execution monopoly.** A worker whose code gets executed by any test suite has arbitrary execution *indirectly*, so "worker can't execute" was never containment; its value is role clarity and credit assignment — which survive scoped test execution intact.

- **Worker:** may execute **its own unit tests only**, scoped via Claude Code command-pattern permissions (e.g. `--allowedTools "Bash(npm test*)"`). Red→green iteration inside one context is where most code-quality gain lives.
- **Verifier:** sole executor of integration/browser/build checks; only its evidence-backed verdict closes a unit of work.
- **Harness gate:** compile/lint/typecheck run mechanically at handoff by the orchestrator.
- Fallback: if worker-run tests muddy attribution in practice, dropping to "harness runs the unit suite, worker executes nothing" is a one-line config change.

Credit-assignment property preserved: every escaped bug is exactly one of "worker wrote it" or "verifier passed it" (with §12's contributing-factor refinement).

## 9. Information boundaries and the explorer subsystem (training)

The wall protects the **build pipeline**, not the grader.

| Component | Sees target? | Emits to pipeline |
|---|---|---|
| Explorer (human sim) | **UI only** — no devtools, no network tab, no source. May also use the **clone's** UI (customers use the product) | Text only: prompt + answers + acceptance feedback |
| Plan / work / verify | Never | — |
| Grader / reflector | **Everything** — both apps, target source, traffic, curl, DB | Nothing during the episode; quarantined ideas afterward |

### The explorer as oracle

- **Perfect but passive.** The explorer answers any question about the target's behavior *correctly*, in plain language — no fallibility knob. You can only train against recoverable signal; an unreliable oracle adds grading variance, blurs credit assignment, and breaks reproducibility. (Real-world fallible users are handled later via hand-written insights through the same `add_idea` door.)
- **Incomplete up front.** The initial prompt undersells the app — the gap is the training signal for elicitation.
- **Prompt-style policy (decided):** one fixed, realistic style through Phase 2–3 — style variation only produces signal once elicitation insights are accumulating; before that it's grading variance. If planner scores plateau later: rotate 3–4 named personas (terse PM, rambling founder, detail-fixated operator), deterministically seeded by episode ID so replays reproduce, tagged on the episode record so attribution can condition on persona. Hard constraint: vary *style only* — the perfect-oracle and incomplete-up-front policies stay fixed, or grading fairness drifts with the costume. No difficulty progression; question-budget tightening (below) is the curriculum lever.
- **Behavioral level only.** Describes *what* the app does — never schema, architecture, or endpoints.
- **Question channel:** planner (and worker/verifier, via the retriever) may ask clarifying questions; the Q&A transcript joins the prompt as the pipeline's full requirements statement. **Question budget (decided): a hard cap per increment, mechanically enforced** — question N+1 simply bounces. Start generous (~10/increment, plus a small separate budget for mid-work questions, which should be rare — a plan needing mid-work questions is itself a plan-quality signal), and anneal across epochs toward deployment parity (~3–5, what a real user tolerates). The annealing *is* the elicitation curriculum. Rubric-costing questions was rejected: it pollutes the grade (mixes app quality with planner chattiness) and the feedback is too indirect to train against. Track elicitation efficiency (features recovered per question, measured against the registry). **Deployment parity:** the same channel exists in production, backed by the real user.

### Exploration frontier ledger (new in R2)

The explorer maintains persistent per-target state: a **frontier ledger** of features — `unexplored / partially-explored / explored / newly-discovered`. Each exploration session starts by attacking the least-investigated frontier entries; features discovered mid-exploration enqueue. The ledger:

- drives the **delivery loop** (§11): the explorer requests the next increment from the frontier, not from memory — and because the frontier is seeded from the full registry, **complete feature coverage is structural, not elicited**: the explorer volunteers every feature eventually (episode terminal = frontier exhausted), and planner elicitation only ever sharpens details of features the explorer already raised — it was never the discovery channel;
- carries a **mention-coverage guarantee**: at every settlement, a deterministic audit joins registry FEATs against all MSG `mentions` for the target; any FEAT never mentioned in any prompt/Q&A is force-scheduled into the next episode's opening slice ahead of normal ordering, so coverage converges by mechanism even across budget-terminated episodes;
- makes "missed feature" attribution checkable — *never-visited* vs. *visited-and-omitted* are different failures with different owners;
- is grader-side state, cached per target across episodes.

### Verified oracle (new in R2)

The "perfect oracle" is enforced, not assumed — simulated users measurably drift over long interactions, and the explorer was the one producer exempt from the design's own untrusted-producer rule:

- **Answer grounding:** every Q&A answer must cite a fresh observation (the explorer may re-open the app and click to verify before answering).
- **Answer checking:** a grader-side checker (DuetSim's generator+verifier pattern) validates each answer against the feature registry / runtime evidence *before* it crosses the wall. Answers still exit as plain text; the boundary holds.
- **Fresh-context answering:** no long-lived simulator session. Each question (or small batch) is answered by a fresh-context explorer instance reading the persistent registry + Q&A log — the same fresh-context-plus-ledger pattern Ralph loops already mandate.

One bridge, one modality, no leaks: any agent's question about target behavior terminates at the explorer in training and at the user in deployment, always in words.

## 10. Grading architecture

### Pre-research phase: the feature registry (new in R2)

Before any episode on a target, a grader-side **registration phase** systematically enumerates the target's features and functions — from source code, route tables, network traffic, and UI traversal — and **confirms each on the running app** with captured evidence. The output is the canonical **feature registry** (`FEAT-*` entries): the anchor for rubric coverage (the grader knows where every check will land), for the explorer's frontier ledger, and for the traceability joins in §12. *Source proposes, runtime confirms* applies entry-by-entry: dead code, disabled flags, and half-built branches never enter the registry.

### Fidelity target: functional equivalence, then improvement

The pipeline is graded on what fits through the channel. Pixel fidelity and code structure never survive translation into a stakeholder's sentences — grading similarity on them scores against unrecoverable signal and rewards copying the target's quirks. What replaces "100% rebuild": a behavioral equivalence **gate**, then explicitly-encouraged **improvement** (below).

### Instruments vs. contract

- **Instruments** (grader-side, anything goes): read target source, run curl against the target, record traffic, query its DB, click both apps. Instruments *propose* rubric entries; they never *become* rubric entries.
- **The contract**: behavioral scenarios, stack-agnostic, in vocabulary a user could have used.

### The rubric: executable behavioral scenarios

Each registry entry yields scenarios — setup, steps, expected outcome — executed **on both apps**, pass/fail with evidence. Score = fraction matching. Differential testing with the target as live oracle. Tolerance tiers per scenario: **must match** (data correctness, flow outcomes, validation, error semantics), **should match** (information architecture), **free** (styling, typography, copy — ungraded for *similarity*; graded for *quality* in the improvement tier). Scenarios are self-contained (each creates its own state through the app's UI). Backend behavior is graded through observable consequences: persistence (create → reload), cross-session authorization (two browser contexts), derived correctness (totals match inputs), harness-level observables (mail catcher, downloads). Direct HTTP probes of the build test *properties* from a fixed target-independent baseline checklist (server-side validation, authz-on-direct-access, no stack traces) against the build's own endpoints — never request-equivalence with the target.

### Judge protocol (new in R2): debiased by construction

The grader's judged comparisons are the system's reward signal; documented judge-bias magnitudes exceed the per-batch effects the training loop must detect. Protocol:

- **Binary rubric + chain-of-thought per scenario** — the one configuration the judge literature consistently finds reliable. Never holistic "how similar are these apps" scores.
- **Default-fail framing:** "find the behavioral difference," not "do these match?" — counters documented agreeableness bias (judges over-approve).
- **Style-bias controls:** the dominant measured bias is *style* (0.76–0.92 magnitude), not position (≤0.04) — rubrics must explicitly separate content criteria from presentation, and naive order-swapping is **not** applied blindly (it measurably hurts on some benchmarks). Swap-and-average is reserved for the pairwise improvement-tier judgments below.
- **Panels only on low-confidence or disagreeing verdicts** — cheap single-judge for clear calls.

### Instrument calibration (new in R2): measure the measurer

- **Mutation-seeded verifier audits:** the orchestrator periodically injects known-bad builds (off-by-one, dropped authz check, swapped success/error paths) into the verify queue. Ground truth is known by construction; the verifier's false-pass rate is therefore *measurable*. A verifier that passes a seeded mutant is flagged and its recent verdicts downgraded to suspect. Incorrect verification is empirically *more harmful than no verification* — this is the only fully deterministic check on the component the design says to overinvest in.
- **Frozen replay set (grader Gauge R&R):** periodically re-judge a frozen set of scenario/app pairs to quantify the grader's repeatability and drift; gate the reward signal on the instrument staying inside tolerance. Without a noise estimate, §5's control limits have nothing to compute from.

### Improvement tier (new in R2): grade "better," not just "same"

The system should *want* to exceed the target — faster APIs, nicer design, better structure. The scoring architecture is **lexicographic with capped bonuses** (the one shape the literature agrees on — SPEC/SWE-Perf's gate-then-measure; constrained-RLHF formalizes why optimizing a secondary reward past a threshold destroys the primary):

1. **Hard gate:** behavioral scenario pass rate ≥ threshold (e.g. 95%). Below it, the bonus tier is **zeroed** — quality can never offset correctness, or the system learns to trade equivalence for prettier CSS.
2. **Capped multi-dimensional bonuses** above the gate:
   - **Performance (deterministic):** k6/autocannon p95 latency ratio vs. target under an identical load profile, capped at 2× credit; Core Web Vitals (LCP, INP) as median-of-5 runs in a controlled environment — never single-run, never the composite Lighthouse score.
   - **Visual design (pairwise, coarse):** MLLM pairwise screenshot judgment with CoT + explicit rubric (visual hierarchy, readability, layout, typography), swap-and-average, invoked only for *gross* quality differences — pairwise UI judging is ~90% accurate when designs clearly differ and coin-flip when close, so an uncertain judge scores neutral, never signal. Graded as *absolute quality*, not similarity to the target: a build that improves an ugly target scores higher.
   - **Code structure (rubric, soft):** criterion-separated LLM rubric (modularity, naming, dead-code absence, dependency health from lockfile audit). Explicitly **not** Maintainability Index or cyclomatic complexity as gates — both are documented folklore metrics that teams have satisfied while degrading real maintainability.
   - **Automated UX checks:** axe-core accessibility, console-error absence, mobile viewport.
3. **Anti-Goodhart guards:** each dimension capped (~30% of the bonus pool); total bonus ≤ 15–20% of the base score; dimension weights rotated across grading runs; human spot-audits on a sample of high-bonus episodes. Documented gaming patterns (verbosity bias, metric hill-climbing, agents editing tests to pass) motivate every one of these.

### Target-free metamorphic tier (new in R2): the deployment story

A class of correctness properties needs no target at all: metamorphic relations (create-then-list shows the item; edit-then-revert is identity; refresh is idempotent; totals equal sum of parts) plus the baseline property checklist. Grade this tier alongside differential scenarios in training.

**Deployment-mode verification (decided):** everything ships except the differential oracle — which is more than it sounds like. The deployed verifier runs: (a) **AC-derived behavioral scenarios** — in deployment the requirements artifact (prompt + Q&A) and the planner's acceptance criteria still exist, and the plan-checker already enforces that ACs are testable, so the verifier executes AC-derived scenarios with exactly the machinery the grader uses on registry-derived ones (training pressure on AC testability directly funds deployment verification quality); (b) the **metamorphic tier** and **baseline property checklist**; (c) the improvement tier's **deterministic instruments with absolute budgets** instead of vs.-target comparisons — CWV medians, latency SLOs, axe-core, dependency/lint health, configured per project. The only training-exclusive machinery is the differential comparison itself.

### Scenario-execution harness (decided): resolve once, cache, replay deterministically

How a behavioral scenario becomes a runnable check on two different DOMs, at training-loop cost:

1. **Authoring:** scenarios are JSON — NL steps + expected outcome — derived from registry entries. (WebTestBench's oracle experiment: supplying gold checklists nearly doubled defect-detection F1 — authoring quality bounds grading quality, which our registry discipline already targets.)
2. **Resolution (LLM, amortized):** a *constrained* resolver (Playwright Python + headless Claude; one NL step → one `{action, selector, args}` struct, structured output, no autonomous multi-step loop — 5–10× cheaper than full agent browsing) resolves each step **separately per app**: cache key = SHA256(step text + URL pattern + accessibility-tree fingerprint), scoped to (scenario, app). The target and the clone *must* resolve independently — same behavior, different DOMs.
3. **Replay (free):** cached structs execute as plain Playwright — zero LLM calls. Evidence captured per step: accessibility-tree snapshot + screenshot.
4. **Assertion, three tiers:** (a) deterministic first — URL changes, `aria-invalid`/error roles, navigation blocked — no LLM; (b) the LLM judge runs **once per scenario comparison, not per step**, on accessibility-tree diffs (10–50× cheaper than screenshot inference; behavioral facts like "inline error appeared" live in the tree), under §10's debiased protocol; (c) screenshots archived as evidence, not used as judge input.
5. **Self-healing:** a replay failure re-resolves *that step only* and updates the cache; every healed step must pair with a deterministic post-assertion (guards against silent substitution — healing onto the wrong but similar element); heal-rate per app is logged (a rising rate = DOM drift signal).

Expected economics: the **target's** cache accumulates hits forever (pairs with the frozen target-side evidence cache); the **clone's** cache misses ~100% at each episode start — correct, its DOM is new — so per-episode resolution cost for the clone is a budgeted line item. Tooling: Playwright (Python) as executor; browser-use (MIT, Python-native) as an optional resolver harness; WebArena's evaluator-function pattern for the deterministic tier. Runner-up: Stagehand (TS) has the best native observe/act caching but is TypeScript-first with a cloud dependency.

### Containment valves

The leak channel to guard is the **reflector's pen**, not the grader's eyes: (1) registry/rubric entries must be runtime-confirmed and behaviorally phrased; (2) reflector ideas must be generalized before registration — "write the lesson, not the fact" — enforced by the registration lint, with fitness pruning and the overfitting detector as backstops.

## 11. The training loop: episodes, increments, epochs (rewritten in R2)

### Vocabulary

- **Ralph loop** — iterates within one unit of work.
- **Increment** — one delivery cycle within an episode (a frontier slice: plan → work → verify → acceptance).
- **Episode** — one engagement window with one target: **one target × one library snapshot × one grade.** The clone codebase **persists across a target's episodes** — each episode continues building the same product, like a real engagement; a fresh workspace exists only at a target's first episode and in deliberate **rebuild-probe episodes** (the instrument for episode-level one-shot measurement). Rebuild probes are also the clone-rot remedy: when a probe's settlement score matches or beats the engagement clone's latest revisit score on the must tier, the probe's artifact is **promoted to become the engagement clone** — the cleaner one-shot rebuild replaces the accumulation, so rising one-shot capability continuously refreshes the codebase. The atomic unit of training.
- **Epoch** — one rotation through the target curriculum.

### Episode anatomy

1. **Setup:** clone workspace created at the target's first episode, loaded thereafter; library snapshotted and **frozen** (read-only) for the duration; target reset to seed; grader-side apparatus loaded (registry + frontier ledger + scenario manifests — built on first visit, cached thereafter). **The opening prompt is written by a fresh explorer exploration session on the target UI**, instructed to describe features beyond the already-built set (known from its own accepted-delivery history; differential clone-vs-target browsing is legal — both are UI), frontier-ordered with mention-coverage force-scheduled items first.
2. **Delivery loop:** explorer opens with a prompt covering a frontier slice; per increment: plan → work → verify build *on top of the standing clone codebase* (code persists across increments — that is what makes integration skills trainable); the explorer performs **acceptance (UAT)** on the clone's UI and gives text feedback ("the totals look wrong") — deployment-parity behavior, since customers use the product; accepted → the explorer pulls the next least-investigated frontier entries; rejected → bug tickets in the next cycle.
3. **Termination:** frontier exhausted or episode budget spent.
4. **Settlement:** full registry-anchored rubric + improvement tier; reflector (§12) emits **one quarantined idea batch**; score recorded against (target, epoch, snapshot-id).

### Increment execution: convergence pass, then rehearsal pass (new in R2.1)

The system's headline goal is to approach **one-shotting an entire codebase**. Each increment is therefore executed in two passes:

1. **Convergence pass (sequential, learning-heavy).** One ticket at a time, full Ralph loop until the verifier passes it. One ticket in flight keeps failure attribution clean. The learning that transfers between tickets is the **run-scoped working memory** (§13): each converged ticket distills its discoveries (build conventions, fixture mechanics, interface decisions) into workflows available to subsequent work. The library stays frozen; run memory is the legal within-episode channel.
2. **Rehearsal pass (parallel, one-shot).** Re-execute all tickets as a fan-out — each in its own worktree from the increment-base commit, in **DAG waves** (topological order, parallel within a wave), **one shot each, no iteration** — consuming the run memory the convergence pass produced. Merge in wave order, run the harness gate, verifier performs one integration pass. All green → adopt the rehearsal artifact and accept the increment.

**Rehearsal failure is signal, not a loop.** The rehearsal is never iterated. The convergence pass already produced a working integrated artifact — that is the **fallback**, so the episode always advances. Rehearsal failures form a distinct, valuable failure class for the reflector: *passed alone, broke together* — integration knowledge that sequential building masks. Unit-level lessons come from the convergence pass; integration-level lessons come from the rehearsal.

**Planner cooperation:** the plan-checker gains a **file-ownership lint** — two tickets claiming the same files are flagged or serialized — and the dependency DAG (already linted) defines the rehearsal waves. Side effect: the lint pressures the planner family toward genuinely parallelizable decompositions, itself a step toward one-shot-ability.

**Cost control:** the rehearsal is additive (~N single-shot executions), so (a) **sample it** — every Kth increment is enough once the rate is being tracked; (b) **graduate adaptively** — once one-shot rates are high, invert the order: fan-out first, and drop into sequential convergence only for tickets that fail. Early training: converge-then-rehearse. Late training: one-shot-first, repair the residue. The graduation point is decided by the metric below.

**The one-shot metric (the system's capability curve):** *ticket one-shot rate* — fraction of rehearsal tickets passing verification with zero iterations; *increment one-shot* — the whole fan-out passes integration on the first try; eventually *episode one-shot*. Tracked per (target, epoch, snapshot-id) alongside rubric scores: rubric measures correctness, one-shot rate measures autonomy.

Lifetimes:

| Thing | Within an episode | Across episodes |
|---|---|---|
| Clone codebase | persists across increments | **persists across the target's episodes**; reset only by rebuild probes or target retirement |
| Run-scoped working memory (§13) | accumulates | **dies** (survivors exit via `add_idea`) |
| Library | frozen, read-only | updated between episodes via the quarantine gate |
| Grader-side target apparatus | used | **cached per target** |
| Traces, scores, idea batch | produced | permanent record |

### No loop-until-good (the replay policy)

The system does **not** rerun the same target until its score is good. Reasons, each independently sufficient: it is training-set memorization with extra steps (the overfitting pressure the lint exists for); reflector-on-the-same-failures repeatedly is the self-bias amplification Huang et al. measure; it Goodharts one target's rubric including its noise; it burns the scarcest resource on the flattest part of the curve.

Instead: **rotate targets each episode; revisit each target in later epochs.** Same-target replay is used exactly once per batch, as the optional **failed-slice causal check** in §5's validation — a controlled experiment ("same failures, library ± batch"), not a convergence loop. Progress is measured on (a) the held-out benchmark and (b) the per-target score curve across epochs — improvement on a revisit, with a library shaped by *other* targets in between, is generalization; improvement on an immediate rerun is memorization.

### Attribution and required infrastructure

The reflector's attribution is specified in §12. Supporting infrastructure:

- **Fixed held-out benchmark suite:** 3–5 targets never trained on; full suite every N episodes; **control-charted** (§5) so only special-cause regressions trigger rollback. **Composition (decided):** qualification rule — boots via docker-compose with seed data in under ~2 minutes (*re*-boot time; first-boot setup amortizes into the per-target cache), feature registry lands at 15–60 entries, license permits local use. **Opening sequence (decided):** (1) **linkding** (github.com/sissbruecker/linkding — Django/SQLite bookmarks; ~20–25-feature registry, holdable in one head) as the machinery-shakedown target; (2) **Kanboard** (github.com/kanboard/kanboard — PHP/SQLite kanban) as the first full-coverage target: adds multi-user roles/permissions (unlocking cross-session authz scenarios and IDOR-class baseline probes), derived state (boards, dashboards), and the legacy-PHP cross-stack box; (3) a **RealWorld implementation** (spec: github.com/gothinkster/realworld; implementations: codebase.show/projects/realworld) for grader calibration against the published spec. **Rotation pool candidates** (admit after the docker-boot check): PrivateBin (github.com/PrivateBin/PrivateBin), Shaarli (github.com/shaarli/Shaarli), DokuWiki (github.com/dokuwiki/dokuwiki), Mealie (github.com/mealie-recipes/mealie), Tandoor (github.com/TandoorRecipes/recipes), Monica (github.com/monicahq/monica), InvoiceShelf (github.com/InvoiceShelf/InvoiceShelf — the maintained Crater fork; a stalled upstream is fine for oracles, but InvoiceShelf has cleaner Docker tooling). Held-out set: one per major archetype, same archetypes as training but *different instances* (measures generalization, not novelty shock). Special pick: a **RealWorld/Conduit implementation** — a published fixed app spec with dozens of cross-stack implementations, giving (a) free cross-stack targets with known-identical behavior and (b) the grader-calibration target: Gauge-R&R the grader against a published spec rather than our own inference of the app's behavior. **Flagship target: OpenEMR** (github.com/openemr/openemr) — a genuinely legacy, economically real PHP codebase with official Docker images and demo data. Far too large to be a whole target (registry would land in the hundreds), it is instead partitioned into **module-scoped virtual targets** that individually pass the qualification rule — "build a patient-scheduling tool" (calendar module), "patient registration," "prescription tracking" — each a 15–60-feature slice with the full app as behavioral oracle, the frontier ledger scoped to the module, and `domain:healthcare` tags in play. Graduated ladder: small standalone apps (Phase 2–3, machinery validation) → OpenEMR module episodes (mid-training, real legacy complexity at bounded scope) → periodic multi-module OpenEMR episodes as the **north-star eval** for the one-shot capability curve. (Qualification-rule note: the 2-minute boot is read as *re*-boot time — first-boot setup cost is paid once into the per-target grader cache.)
- **Per-insight fitness:** retrievals, wins/losses, and **causal blames** (§12's `implicated_existing_insights` gives causally-grounded losses, not just co-occurrence).
- **Overfitting detector:** positive fitness on one target, neutral-or-negative elsewhere → memorized a repo → retire automatically. **Cross-target recurrence** (the same lesson independently arising on ≥k targets) is the strongest *promotion* evidence — the evidence-based complement to the generalization lint's prediction.

## 12. The reflector (new in R2 — resolves the R1 open question)

The reflector is not one agent. It is **a mechanical attribution pass + a per-cluster counterfactual LLM call + a batch gate** — thin LLM judgments inside thick mechanical contracts, like everything else here.

### 12.1 Traceability schema (the precondition)

Deterministic diagnosis is a *discipline imposed at artifact-creation time*, not an algorithm run afterward. Every stage's output contract mandates typed IDs and links (enforced by the existing lints); the attribution tree then compiles to joins over a relational store (the same SQLite database as the trace index, §13):

| Artifact | ID | Created by | Mandatory links |
|---|---|---|---|
| Feature registry entry | `FEAT-n` | Grader pre-research | runtime evidence ref |
| Prompt paragraph / Q&A exchange | `MSG-n` | Explorer | `mentions: [FEAT-*]` |
| Requirement unit | `REQ-n` | Planner (extraction step) | `source: MSG-*` |
| Ticket | `TKT-n` | Planner | `covers: [REQ-*]`, `increment: INC-*` |
| Acceptance criterion | `AC-n` | Planner | `ticket: TKT-*`, `req: REQ-*` |
| Worker diff/span | `SPAN-n` | Orchestrator | `ticket: TKT-*`, files touched |
| Verifier check | `CHK-n` | Verifier | `ac: AC-*`, result, **stored repro command**, evidence |
| Rubric scenario | `SCEN-n` | Grader | `feat: FEAT-*`, result, evidence |

The would-be-fuzzy join (`MSG ↔ FEAT`) is deterministic **by construction**: the explorer writes prompts and answers *from* the registry/frontier, tagging `mentions` as it writes. The planner's mandated first step (requirement extraction) makes `REQ → MSG` provenance free.

### 12.2 Stage A: the decision procedure (deterministic)

Per failed scenario `SCEN-x` testing `FEAT-y`:

```
1. COMMUNICATED?  FEAT-y ∈ mentions(prompt ∪ Q&A)?
     NO → micro-judgment #1 (elicitable?) → PLANNER(elicitation) or EXPLORER(prompt)
2. EXTRACTED?     ∃ REQ-r sourced from a MSG mentioning FEAT-y?
     NO → PLANNER (requirement extraction)
3. COVERED?       ∃ TKT-t with REQ-r ∈ covers?
     NO → PLANNER (coverage) — and flag the plan lint that should have caught it
4. SPECIFIED?     ∃ AC-a with req = REQ-r?
     NO → PLANNER (acceptance criteria)
5. IMPLEMENTED?   TKT-t has a non-empty diff and closed normally?
     NO → WORKER (known-failure class: escalated/incomplete)
6. VERIFIED?      ∃ CHK-c on AC-a with result PASS?
     NO → VERIFIER (incomplete verification — meta-contract breach)
7. DISCRIMINATE:  re-execute CHK-c's stored repro command NOW.
     still passes, scenario fails → AC satisfied-as-written but wrong/too weak
                                   → PLANNER (AC quality)
     now fails → regression after verification → find the breaking ticket
                 mechanically (coverage trace of the failing scenario names the
                 files; join vs SPAN.files_touched + increment tags)
                 → that WORKER, or VERIFIER (integration pass on that increment)
8. ANSWER CHECK   for Q&A exchanges mentioning FEAT-y:
     micro-judgment #2 (answer contradicts registry evidence?) → EXPLORER (answering)
     — a lookup if the verified-oracle check (§9) already ran at answer time
```

Steps 1–6 are lookups. Step 7's discriminator is **mechanical re-execution** (this is why CHK stores a repro command). Exactly **two micro-judgments** remain, both narrow classifications: *elicitability* (mostly table-driven — maintain a fixed probe-question taxonomy, tag each FEAT with its probe category at registration; the LLM fires only for out-of-taxonomy features) and *answer contradiction* (binary, both artifacts in hand).

Attribution output is `{primary, contributing[]}` — a primary owner for routing pressure, with contributing factors (NTSB-style) so multi-causal failures don't starve secondary families of signal.

### 12.3 Stage B: counterfactual reflection (LLM)

- **Cluster first:** group failed items by ticket/feature/error signature — one root cause fails many scenarios, and per-failure reflection manufactures near-duplicates.
- **Fresh context per cluster**, seeded with the case file Stage A assembled (the implicated REQ/TKT/AC/SPAN/CHK rows + evidence) — *plus query tools over the full trace store* (§13). Mechanical attribution is the seed hypothesis, not the boundary of visible evidence: the output schema includes `attribution_override` for when the investigation contradicts Stage A, and recurring overrides on one link type are telemetry that the corresponding output contract needs tightening.
- **The core prompt move is the counterfactual:** *"write the insight that, had it been in this agent's context at this moment, would have changed the outcome"* — forcing actionability and making every idea a testable claim (the failed-slice replay tests it).
- **Stingy by instruction:** 0–1 ideas per cluster, with explicit permission to emit none ("if the failure does not support a generalizable lesson, say why"); a per-episode budget (~5–8 ideas) forces ranking. Every quantitative result on memory curation says value comes from selectivity.
- **Typed output:**

```
{ rubric_item(s), attribution: {primary, contributing[]}, evidence_refs[],
  root_cause_hypothesis, counterfactual_insight (structural schema §4),
  scope_tag_proposal + justification, implicated_existing_insights[],
  attribution_override?, confidence }
```

`implicated_existing_insights` lets the reflector blame the *library itself* — "insight X was retrieved and was wrong/misleading" — producing causal fitness losses and triggering contradiction repair at registration.

- **Success channel:** the reflector also nominates run-memory workflows (§13) from *passing* trajectories for generalization through `add_idea` — failure-only learning converges on a library of prohibitions; the largest quantified gain in adjacent work (AWM, 51.1%) came from distilled positive procedures.
- **Batch, never stream:** one batch per episode, registered after settlement, quarantined per §5. The library stays frozen mid-episode (clean attribution); within-run learning is the run-scoped memory's job, not the library's.

## 13. Trace and memory infrastructure (new in R2)

### Capture everything, navigate on demand

- **Capture layer (non-negotiable, cheap):** every `claude -p` invocation already emits a complete transcript (`--output-format stream-json` / session JSONL). The orchestrator registers each as a **span**: `{run_id, episode, increment, family, agent, ticket_id, ralph_iteration, parent_span, artifact_refs}`. A SQLite span index over JSONL files is the entire v1 implementation. Nothing is summarized *away*; summaries are additions.
- **Navigation layer:** the reflector (and any debugging human) gets query tools — list spans, search by string/file/ticket, expand, walk parent/child, diff two Ralph iterations. Full trace available; attention spent selectively. This resolves the slicing-vs-full-trace tension without an irreversible commitment: if slices prove too narrow, widen the tools; if navigation proves slow, add a cheap-model summary tier.
- **Human viewing layer:** LangSmith/Langfuse (or any OTel GenAI sink) over the same spans — valuable in Phase 2 when *you* are the reflector. Note the stack is mostly outside LangChain (headless subprocesses), so any vendor tracing is manual span reporting either way; the span schema is the load-bearing part, not the vendor.

### Two-tier memory

- **Run-scoped working memory (fast loop):** during an episode, successful trajectories are distilled online into reusable workflows ("how auth-gated CRUD works in this build") available to later tickets *in the same episode*. Allowed to be target-specific — it never passes registration and **dies at episode end**. This gives the within-run gains the generalization lint otherwise forbids, and resolves the R1 tension where the most immediately useful knowledge had nowhere legal to live.
- **Permanent library (slow loop):** at settlement, the reflector nominates run-memory survivors for generalization through the normal `add_idea` gauntlet — arriving with nonzero evidence (they already won at least once), and subject to cross-target recurrence promotion (§11).

### Embedding and vector stack (decided)

- **Embedder: `nomic-embed-text-v1.5`** — Apache 2.0, 137M params, 768-dim (Matryoshka-reducible), CPU-friendly (~20–80ms per 200-token text), and the only model in its tier with a dedicated `clustering:` task prefix — directly relevant since k-means/silhouette on insight embeddings drives the splitting machinery. Use `search_document:` at index time, `search_query:` at routing time, `clustering:` for split evaluation. Runner-up: Qwen3-Embedding-0.6B (higher retrieval ceiling, heavier, weaker fit for clustering).
- **Runtime:** in-process via sentence-transformers (ONNX backend, 1.4–3× CPU speedup) — natural fit with the Python orchestrator (§15); no sidecar service needed.
- **Vector store: `sqlite-vec`, brute-force, in the same SQLite database as everything else.** At 10–50k vectors of dim 768, brute-force KNN is milliseconds; ANN only matters past ~100–200k (revisit then). Keeping vectors in the one snapshot-keyed store preserves the parallelism invariants for free. *Pinecone was considered and rejected:* it is a vector store, not an embedder (the model choice remains either way); it adds a cloud dependency and recurring cost to a system whose only paid AI is the Claude subscription; and it splits library state out of the snapshot-keyed SQLite store that rollback, quarantine, and parallel-episode keying all depend on.
- **Pinning discipline:** `embedding_model` + `embedding_dim` columns on every vector-bearing row; never mix models in one index; a model swap is a deliberate migration — full re-embed + recalibration of all cosine thresholds against the held-out labeled pair set (§17).

## 14. Anti-overfitting: cross-stack strategy

Cross-stack training (e.g., PHP target → React build) does not destroy signal — the signal never touches target code. The explorer's output is a behavioral spec; the grader compares behavior against behavior. Cross-stack is the best anti-overfitting tool available: surviving insights must be about process, not repo quirks.

- **Vary the target stack, fix the output stack** (initially). Worker/verifier families accumulate on a consistent substrate; planner/retriever families generalize across diverse inputs. Diversify the output stack later.
- **The output stack (decided): React + Vite + TypeScript (strict) + Hono (Express as fallback) + Drizzle + SQLite + Tailwind + shadcn/ui, tested with Vitest + Playwright.** Evidence: the major agent app builders (v0, Lovable, Bolt) independently converged on React + Vite/Next + Tailwind + shadcn — teams optimizing for exactly our metric (LLM generation reliability); WebGen-Bench's best-performing agent generates React + Vite + TS; Tailwind/shadcn minimize hallucination surface (atomic utility tokens, components inlined as editable JSX rather than opaque APIs); Drizzle publishes `llms-full.txt` and keeps schema + queries in one language. **Plain SPA + API rather than Next.js**: the App Router's churn (async `params` breaking change in 15+, RSC `"use client"` boundaries) is a documented "model knows the old API" failure class — a smaller, stabler surface beats marginally higher training coverage when one-shot rate is the goal. Exact versions live in a pinned template repo; version bumps are deliberate migrations (model priors lag framework churn — the pin is a harness responsibility).
- **Scope tags** keep stack-specific insights in stack-named skills.
- Cross-stack, the grader's source-reading is for **discovery and ambiguity resolution**, never code diffing.
- **Recurrence promotion** (§11) is the evidence-based generality gate; the lint is the fast filter.

## 15. Orchestration and parallel experiments

### Subscription constraint (verified)

Claude Code headless mode (`claude -p "..." --output-format json`) uses the logged-in credential — a Pro/Max subscription works — and the Claude Agent SDK honors the same resolution. The orchestrator shells out to `claude -p`; direct API calls are not used. **Quota policy (decided): one Max subscription is the ceiling — no second account, no API spillover.** The training budget is the weekly cap; everything in §7 (tripwires), §11 (rehearsal economics), and the model-tier table below exists to spend it well. Max plans have 5-hour rolling windows plus weekly caps: the orchestrator must be **checkpointable and resumable**, and wasted iterations are the binding cost.

**Orchestrator (decided): Python**, plain (no LangGraph initially), with the SQLite store; adopt a graph framework only if resume/branching pain materializes. Python also gets sentence-transformers in-process (§13) and Playwright-Python (§10) for free. Per-agent permissions via `--allowedTools` / per-directory settings.

**Model tier per role (defaults — per-role config, revisit on performance data):**

| Role | Default | Rationale |
|---|---|---|
| Planner | Opus | Low call volume, highest leverage per token |
| Plan-checker | Sonnet | Judged checks; deterministic lints are free anyway |
| Worker | Sonnet | The token-volume role; escalate stubborn tickets per-case later |
| Verifier | Sonnet | Execution + scoped judgment; **panel escalations → Opus** |
| Context retriever | Sonnet | Drop to Haiku for simple lookups once query mix is known |
| Explorer | Sonnet | Answers are checker-verified anyway (§9) |
| Grader scenario judge | Sonnet | Binary rubric calls; **disagreement panels → Opus** |
| Reflector (Stage B) | Opus | Lowest frequency, highest blast radius — its output becomes the library |
| Registration judge | Sonnet | Placement/dedup with cosine prefilter doing the bulk |
| Span summaries, mechanical checks | Haiku | Pure summarization/classification |

Principle: volume on Sonnet, low-frequency/high-leverage judgment on Opus, mechanical work on Haiku — and every escalation path (panels, overrides) steps up one tier.

**Security (decided non-goal):** this is experimental, local, throwaway-adjacent code; no sandboxing posture beyond the episode containers that exist for isolation/parallelism reasons. Recorded so nobody adds hardening machinery later out of reflex.

**Deployment packaging (deliberately deferred):** what the product wrapper *is* (CLI vs. service vs. point-at-a-repo tool) is a post-training-loop question that blocks nothing — the pipeline is already CLI-shaped, so the working assumption is a CLI, and the deployment-mode verifier (§10) is wrapper-agnostic. Decide when there's something worth wrapping.

### Parallel-experiment readiness (new in R2 — a day-one requirement)

The system is built to run **N episodes in parallel** even if v1 runs them sequentially. The invariants that make this a scheduler change rather than a redesign are mandatory from Phase 0:

1. **Library is read-only during episodes** — N readers of one frozen snapshot, zero contention.
2. **All library writes flow through a single-writer promotion queue** at episode boundaries (SQLite single-writer is sufficient).
3. **Every artifact is keyed by snapshot ID** — (target, episode, epoch, library-snapshot-id) on every score, trace, and batch, so "which library state produced this" is always a lookup and parallel batches are comparable.
4. **Environment isolation per episode:** one container stack per episode (target app instance, clone dev server, mail catcher, browser), port-namespaced. Docker Compose per target.
5. **Validation is embarrassingly parallel:** quarantine trials, held-out benchmarks, mutation audits, and frozen-replay calibration are all read-only against library variants — parallelize these first, since the validation gate is the throughput bottleneck.

**Batch-merge policy (decided):** when N parallel episodes produce N batches against the same snapshot — **validate independently, then merge all.** (1) Each batch is validated independently against the shared snapshot (preserves per-batch causal attribution). (2) All batches that pass merge through the normal registration machinery, which handles overlap by construction: cosine prefilter merges near-duplicates, the judge flags textual contradictions, and *competing non-contradictory alternatives* (two plausible lessons for the same problem) are the ratchet's job — both enter, fitness decides, the loser retires. (3) One **joint confirmation run** on the union before promotion catches effect-level interactions that text-level machinery cannot see (ideas individually fine, jointly harmful). (4) Per-batch provenance keeps reversion surgical. Batches from different targets are mostly complements, not substitutes — so winner-takes-all selection (GEPA-style tournament over library states) would discard valid orthogonal lessons; it remains only as a possible future *experiment mode* (maintaining diverse library lineages), not the merge policy. Telemetry: the joint-confirmation failure rate (all parts passed alone, union failed) — if ever non-rare, interaction effects deserve real machinery.

Quota reality: parallel sessions share one account-level pool — parallelism compresses wall-clock, it does not create tokens. The free win is overlapping non-LLM time (builds, browser automation, scenario execution) across episodes; expect 2–3 parallel episodes to extract most of the benefit before becoming token-bound. Parallel work *within* an increment exists in exactly one sanctioned form: the **rehearsal pass** (§11) — one-shot ticket executions in DAG waves with worktree isolation and a single integration verify. Open-ended parallel *iteration* on multiple tickets at once (concurrent Ralph loops on a shared codebase) remains deferred: it multiplies token spend and adds the inter-agent failure-mode menu without the rehearsal's clean one-shot semantics.

## 16. Build order

- **Phase 0 — the heart, standalone:** data model + `add_idea` CLI: embedding index (full-text), cosine prefilter, routing judge (placement + dedup + contradiction + generalization lint + scope tags), structural insight schema, skill rendering with delta-patch compilation, export to SKILL.md, **span/traceability SQLite schema, snapshot-ID keying, single-writer promotion queue**. Testable with hand-written ideas before any pipeline exists.
- **Phase 1 — pipeline skeleton:** plan → work → verify on toy tasks; hardcoded generic prompts; headless `claude -p`; harness gate; Ralph loop with typed failures + tripwires; **full trace capture from the first run**.
- **Phase 2 — one target, manual ideas:** registry pre-research + frontier ledger; explorer + grader on **linkding** (target #1 — small enough to hand-check every registry entry and grader verdict); episode/increment structure; **you act as the reflector** — reading traces (human viewing layer) and hand-writing ideas validates the registration machinery and *records the navigation moves the automated reflector's tools should mimic*. Mutation-seeded verifier audits start here (the reward model must be calibrated before it trains anything). Graduate to **Kanboard** (full grading-pattern coverage, legacy PHP) and then the **RealWorld implementation** (grader calibration) per §11's opening sequence.
- **Phase 3 — close the loop:** automated reflector (Stage A + Stage B); quarantine/validation/promotion lifecycle; ratchet governance (cap + retirement); splitting; held-out benchmark + control charts; improvement tier; parallel episodes.

**Where to overinvest:** the **verifier/grader instruments** (the reward model — §10's calibration machinery is how you know it works) and the **idea lifecycle** (structural schema + quarantine gate — what separates a compounding library from the +0.0pp outcome the literature measures for ungoverned ones).

## 17. Resolved questions: initializations, triggers, and decisions

Every formerly-open question now carries either a decision or a default-plus-trigger. Nothing here blocks Phase 0.

**Hyperparameters (tune as we work — the obligation is tunability, not the values):**

- All thresholds (active cap ~50, cosine merge ~0.92, step-repetition similarity, gate pass-rate, …) live in **one config file**, each entry carrying its default's provenance and its tuning metric. Decisions are logged with the threshold value that made them, so changing a value shows what would have flipped.
- Cosine thresholds are **not portable across embedding models** — recalibrate against ~50 hand-labeled duplicate pairs whenever the embedder changes.
- **Every tripwire ships in shadow mode first** (log, don't kill) — a misfiring kill-switch is worse than a missing one. The step-repetition threshold is set from the logged similarity distribution of productive iterations.
- The gate pass-rate is derived from measured grader noise (frozen replay set), not chosen independently; must-tier failures get panel adjudication rather than counting against a percentage.
- **Model-version and prompt-set changes are instrument events.** The resolved Claude model version is stamped on every span and episode; likewise every harness prompt template (family templates, pipeline prompts, judge/resolver/reflector/induction prompts) is content-hashed into a versioned **prompt-set manifest** — registered with stored content at orchestrator startup, stamped on every span/episode, and revertible as a first-class operation (activate any prior set). A change to either annotates the SPC charts (limits recompute), triggers fixture re-records, and is reported alongside the curves — score movement across an instrument change is never attributed to the library. (The *learned* prompt material — skills, descriptions, specialty sections — is already versioned and revertible via snapshots and split lineage; the manifest covers the harness side.)

**Decided:**

- **Explorer prompt style** → fixed realistic style through Phase 2–3; persona rotation only if planner scores plateau (policy in §9).
- **Question budget** → hard cap per increment, annealed across epochs; rubric-costing rejected (policy in §9).
- **Benchmark suite composition** → qualification rule + archetype×stack pool + RealWorld calibration target (in §11).
- **Deployment-mode verifier** → ship everything except the differential oracle; AC-derived scenarios + metamorphic + baseline + absolute budgets (in §10).
- **Probe-question taxonomy** → seed from clustered feature-registry entries after the first 3–4 targets (data-driven, matched to actual training apps); grow by exception — each out-of-taxonomy elicitability judgment proposes a category, human-reviewed in Phase 2–3, auto-admitted later; cap ~25 categories (checklists rot like libraries).
- **Output stack** → React + Vite + TS + Hono + Drizzle + SQLite + Tailwind/shadcn, pinned template repo (§14).
- **Embedding stack** → nomic-embed-text-v1.5 via sentence-transformers, sqlite-vec brute-force in the main SQLite store; Pinecone rejected (§13).
- **Orchestrator** → Python, plain, SQLite-backed (§15).
- **Scenario-execution harness** → resolve-once/cache/replay with three-tier assertion and self-healing (§10).
- **Model tiers** → defaults table in §15; volume on Sonnet, leverage on Opus, mechanical on Haiku.
- **Quota** → one Max subscription is the ceiling; no scaling strategy needed (§15).
- **Security** → explicit non-goal (§15).
- **Deployment packaging** → deliberately deferred; working assumption is a CLI (§15).

**Triggers (telemetry defined now; the data decides later):**

- **Batch merging** → decided, not graduated (see §15): validate each batch independently against the shared snapshot, merge all validated batches through the registration machinery (dedup/contradiction/ratchet handle overlap), one joint confirmation on the union, promote; revert per-batch. Telemetry to watch: joint-confirmation failure rate — interactions that pass individually but fail jointly. Winner-takes-all tournament over library states is demoted to a future experiment mode, since parallel batches are mostly complements, not substitutes.
- **Output-stack diversification** → run a **transfer-gap probe** (~once per epoch, late Phase 3+): one episode in a second stack; the score drop vs. the fixed stack measures how worker-folklore-heavy the library still is. Diversify when the gap is small and a product reason exists; budget seeding episodes for the new stack's empty `stack:` namespace.
- **Rehearsal economics** → rehearse every increment for the first episodes (metric baseline + the reflector's only integration-lesson source), then anneal sampling so rehearsal spend ≤ ~15–20% of episode budget; flip to fan-out-first-with-sequential-repair when the measured cost curves cross (expected near ticket one-shot rate ~70–80%, but the cost model decides, not the rule of thumb).

## 18. Research grounding (key sources)

**Skill libraries & memory:** Voyager (arXiv 2305.16291); ACE — context collapse, delta-patches +10.6% AppWorld (2510.04618); AWM — ~7 workflows/site → 51.1% rel. improvement, online > offline (2409.07429); ExpeL (2308.10144); Dynamic Cheatsheet — curated 50% vs naive 26.7% (2504.07952); Library Drift — LLM-authored +0.0pp vs human +16.2pp; Ratchet Recipe (cap + retirement + structural prior) +0.328 vs +0.002 (2605.19576); Skill Shadowing — 21% drop at 202 skills, selection 88%→53% (2605.24050); SkillRouter — body is the routing signal, 31–44pp; cosine>0.92 merge (2603.22455).

**Optimization without gradients:** GEPA — Pareto-diverse reflective evolution, +13% over MIPROv2 at 35× fewer rollouts (2507.19457); TextGrad — validation-based reversion; OPRO/PromptBreeder.

**Multi-agent & loops:** MAST — 14 failure modes over 1600+ traces; step repetition 15.7%, reasoning-action mismatch 13.2%, termination-unaware 12.4%, incorrect verification 9.1% and worse-than-none (2503.13657); MetaGPT (2308.00352); ChatDev (2307.07924); Huang et al. — no intrinsic self-correction; self-bias amplifies monotonically (2310.01798); Feedback Friction — typed > prose feedback (2506.11930); Reflexion; CRITIC; Ralph Wiggum loop (Huntley).

**Judging & grading:** binary rubric + CoT as the reliable config; agreeableness bias; style bias dominant (0.76–0.92) vs position (≤0.04), naive order-swap can hurt (2604.23178); MLLM-as-UI-judge — pairwise ~90% on gross differences, ~50% on small (2510.08783); VisJudge-Bench (2510.22373); Design2Code — LLM output beat references 64% of the time on human pairwise (2403.03163); LLM-judge for SE — 0.4–0.7 human correlation (2510.24367); MI/cyclomatic validity criticisms (Shepperd; Sourcery).

**Improvement grading & anti-gaming:** SWE-Perf — correctness gate → Mann-Whitney performance delta; agents 2.26% vs experts 10.85% (2507.12415); constrained RLHF — over-optimizing a secondary reward destroys the primary (2310.04373); Lighthouse CI — median-of-5, CWV sub-metrics not composite; k6 percentile SLO gating; reward hacking taxonomy (Weng 2024); Reward Models are Metrics in a Trench Coat (2510.03231).

**Behavioral cloning & testing:** Mechanical Orchard — "the running system is the better spec," I/O equivalence capture; AWS Transform — functional equivalence as first-class artifact; metamorphic testing (Chen 1998+); WebArena.

**Simulated users:** Lost in Simulation — goal distortion over long interactions (2601.17087); DuetSim — generator+verifier fidelity pattern.
