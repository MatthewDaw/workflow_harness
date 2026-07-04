# Agent Families: A Self-Growing Agent System for Reverse-Coding Applications

**Status:** Design — pre-implementation
**Revision:** 3 — 2026-06-12. Reorganizes the system around two pivots, grounded in a literature + OSS-source study (`2026-06-11-memory-store-simplification-design-note.md`): **(1) agents → stages** — the runtime is a fixed `plan → assign → work → verify` pipeline differing by tools/permissions/contract/trust, never by knowledge; runtime personas and the family router are removed. **(2) specialists → the knowledge graph** — groups (modules/skills) are *derived* from the insight graph by hierarchical Leiden and named lazily, **never authored at ingest** (Ontology B), because LLM-authored taxonomies measurably degrade (Library Drift +0.0pp; Skill Shadowing −21%). §3–§6 and §6a are R3; the heuristics (cap ~50, silhouettes 0.3/0.35) are replaced by one organization objective (§6a). The lifecycle (§5), traceability (§12), grading (§10), and training-loop (§11) machinery carry forward from R2. Revision 2 citations in §18; R3 citations in the design note.

**R2 → R3 superseded:** §3 (agent families → stages), §4 "What an agent is" (persona-lens → derived module), §4 boundary-ticket multi-persona refinement (removed), §4 routing signal's cosine-0.92 dedup (→ key/value + NLI, §5), §6 silhouette splitting (→ Leiden partitioning scored by §6a). Everything else in R2 stands.

## 1. Vision

Build a tool that can reverse-code any application by observing it the way a user would — clicking around, seeing the features — and then rebuilding it in a modern stack. The runtime is a fixed pipeline of **stages** — plan → assign → work → verify (plus a shared read-only retriever) — and the system improves itself through exactly one operation: **adding an idea**. Ideas are atomic records in a self-organizing knowledge graph; the graph's structure (modules, and the human-facing skills exported from them) is **derived** from the ideas and their usage, by community detection — never authored by hand at ingest. Specialization lives in the *index*, not the staffing: every job's context is retrieved per-job from the whole store, conditioned on the job. An automated training loop builds clones of real apps and grades the results, feeding the graph through the one `add_idea` door.

## 2. The core reframe: this is not RL

Nothing in this system updates model weights. There is no gradient, no policy, no reward model to train. It is an **evolving skill library with text-based credit assignment**. The learnable parameters are:

1. The insight texts (grown by adding insights)
2. The index (embeddings over insights — key & full vectors, §4/§5)
3. The derived module hierarchy (Leiden communities over the insight graph, maintained by the §6a objective)

The "reward" is the grading agent's rubric score; the "update operator" is `add_idea()`.

**Foundation verdict (from the research review, §18):** every load-bearing mechanism here has direct, quantitative prior art — the reflector ≈ ACE's Reflector / GEPA's failure diagnosis; the skill library ≈ Voyager/AWM/Dynamic Cheatsheet; plan→work→verify ≈ MetaGPT/ChatDev with MAST's failure taxonomy mapping onto it; iterate-until-verified is validated by Huang et al. (external grounded feedback is *necessary*); behavioral cross-stack rewriting is Mechanical Orchard's and AWS Transform's production method. Three pieces appear **genuinely novel** (no documented prior art found): silhouette-triggered agent splitting, the self-reorganizing routed taxonomy, and explorer-generated scenario manifests closing a training loop.

The research's strongest warning, adopted throughout this revision: **LLM-authored skill libraries are worth approximately nothing without governance** (Library Drift: +0.0pp vs. +16.2pp human-curated; flipped to +0.328 vs +0.002 by cap + retirement + structural schema), and **library size itself degrades routing** (Skill Shadowing: 21% pass-rate drop at 202 skills; selection accuracy 88%→53%). Dedup and pruning matter as much as adding — and *bounding* matters more than both.

**(R3) — this warning is now structural, not just a guardrail.** The R3 study confirmed the failure mode is *authoring groups at ingest*: not one of six surveyed memory/RAG systems (mem0, Graphiti, GraphRAG, LightRAG, Cognee) names groups per-add; all author only nodes+edges and derive named communities in a separate batch pass. R2's design was the lone author-at-ingest outlier (the placement judge minted a named skill per `add_idea`). R3 moves grouping out of ingest entirely (§4 "What a module is," §6): ingest adds an insight node + edges; Leiden derives modules; names are lazy. The cap/silhouette heuristics R2 used to *fight* drift are replaced by an objective that never lets the drift form (§6a).

## 3. Stages (R3 — replaces "Agent families")

The runtime is a **fixed pipeline of stages**, not a router over specialist agents. A stage is a fresh session with a defined act, tool/permission surface, output contract, and trust position. **Stages differ by what they do and what they may touch — never by what they know.** Knowledge is not a property of a stage; it is retrieved per-job from the whole store (§4, §5). There is no family router, no preset domain bias, and no persistent persona; what R2 called an "agent" dissolves (its knowledge → the graph, its identity → a stage). A Ralph loop (§7) reruns a stage until a checker confirms completion.

Why stages survive when agents don't: the *bias* an agent carried (a domain prior baked into a prompt) is redundant under a good fast loop — per-job retrieval computes a better, job-specific bias. What cannot be retrieved into existence is **trust structure**: the verifier must be a *different session* than the producer. That independence is the load-bearing reason the pipeline has seams at all.

### Pipeline stages (the deployed system)

| Stage | Act | Writes | Executes |
|---|---|---|---|
| **Plan** | Turn a request + Q&A into a fleshed-out, ticketed feature list divided into units of work | Tickets only | Nothing |
| **Assign** | Give each ticket its context: per-job retrieval (§5) over the whole store conditioned on the ticket, plus **file-territory** allocation for parallel work (the only "routing" that remains) | Assignments | Nothing |
| **Work** | Take a ticket + its assigned context and code it | Code + tests | Its own unit tests only (scoped — see §8) |
| **Verify** | Check work against codebase conventions, requirements, unit correctness, and integration | Verdicts | Build, integration tests, browser |
| **Retriever** | Answer questions from the codebase, the internet, or (in training) the human simulator. A read-only oracle called by any stage; never routes work itself | — | Read-only research |

The workflow is always **plan → assign → work → verify**. Per-job context conditioning lives in *assign* (it replaces the family router); knowledge specialization lives in the *index* (§4), not in any stage.

### Persistent specialist sessions are contingent optimizations (added back only when measured)

R2's persistent personas are not a runtime primitive in R3. A long-lived specialized session is reintroduced *only* when a measurement justifies it: (1) KV-cache prefix economics, (2) parallel write ownership (file territories — already in *assign*), (3) judgment/persona on genuine boundary work, (4) cold-start before retrieval traffic exists. Each is an optimization over the stage pipeline, never a return to knowledge-partitioned staffing.

### Training-only stage-roles

These remain as defined in R2 (they are training-time roles, not runtime knowledge partitions):

| Role | Function |
|---|---|
| **Explorer (human simulator)** | Looks at a target app through the UI only; maintains the exploration frontier; writes a human-style build prompt; answers clarifying questions in natural language; performs acceptance (UAT) on delivered increments |
| **Grader** | Builds the per-target feature registry; after each episode, compares the built app against the target across every instrument available and produces rubric scores plus improvement-tier scores |
| **Reflector** | Two-stage: deterministic attribution over the traceability store, then LLM root-cause per failure cluster, emitting a quarantined idea batch (§12) |

## 4. Data model

**The single most important structural decision: never dissolve insights into skills.** Insights are atomic, immutable records, forever — the substrate every surveyed memory tool lacks (mem0 fuses fact+context into prose; Graphiti mutates edges in place; Cognee has no atomic record). Immutability is what makes rollback, quarantine, snapshot-keyed parallel episodes, and per-insight causal attribution possible. A **module/skill is a *derived* group — never authored at ingest** (R3 — see "What a module is" below): a community of insights produced by the §6 Leiden pass and named lazily.

```
Insight  { id, precondition, action, expected_outcome, negative_scope, scope_tag,  # R3: schema'd atom + "when NOT to apply" (§5 Op.1)
           key_vector, full_vector,                                 # R3: two embeddings — key=precond+action, full=all (§5/§2b)
           valid_at, invalid_at,                                    # R3: world/version validity, append-only (§5)
           provenance: manual|reflector|researched|seeded|consolidated,  # R3: consolidated = a slow-loop general parent (§6/§5 Op.3)
           source_run_id, created_at,
           status: quarantined | active | dormant | retired,        # dormant = a consolidated parent's preserved children (§5 Op.3)
           fitness: { retrievals, wins, losses, causal_blames, corroborations } }   # R3: corroborations = recurrence votes (§5 Op.2)
InsightEdge { src, dst, weight,
              kind: similarity|corroborates|refines|contradicts|generalizes_from }  # R3: similarity=derived for Leiden; rest=semantic, append-only (§5)
Module   { id, level, parent_module?, name?, description?, member_insight_ids[] }  # R3: DERIVED by Leiden, named lazily
```

`Module` replaces R2's `Skill`/`Agent`/`Family` triple. It is the output of the §6 partition pass, not an ingest-time entity: a *level* of the hierarchical-Leiden partition (coarse level = what R2 called an agent's domain; leaf community = a skill; a leaf's lazy summary = the `SKILL.md` export). `name`/`description` are nullable because they are filled lazily, only when a name is needed for display or routing. Atomic insights are what make everything downstream possible: re-clustering, partitioning, dedup, contradiction repair, per-insight fitness tracking, and **rollback** (disable a batch of insight IDs if validation fails).

*Note on the existing Phase-0 tables:* the shipped `skills`/`agents` tables survive physically (R3 demotes, doesn't drop, to keep the migration reversible — §16/the design note R3 list). `skills` becomes the derived-module table (its per-add `create_skill` writer is removed; a batch writer replaces it); `agents`/`families` lose their runtime/persona semantics and the family router (`library/router.py`) is demoted to dead code behind the *assign* stage.

### What a module is (R3 — replaces "What an agent is")

R2 defined an *agent* as a specialist lens with four facets (persona, owned cluster, routing identity, retrieval policy). R3 removes the persona and the family-scoped retrieval entirely; what remains is a **module: a derived community of atomic insights serving as a placement/routing/governance unit** — structure in the index, not staffing in the org chart.

- **Derived, not authored.** A module is the output of the §6 Leiden pass over the insight graph; it is *discovered*, not chosen at ingest. Hierarchical Leiden yields levels for free, so "module" (coarse) and "skill" (leaf) are levels of one partition, not two designed entities.
- **Named lazily.** A module's `name`/`description` are generated by a brief LLM pass over the cluster's central insights, only when a name is needed (display, the `SKILL.md` export, or coarse routing) — never at ingest. (GraphRAG/Graphiti/Cognee all name groups *after* clustering; GitNexus ships exactly this — Leiden over a codebase graph → per-community `SKILL.md` for Claude Code.)
- **A governance unit, not a retrieval wall.** Modules carry the bookkeeping R2 hung on agents — fitness roll-ups, retirement, lineage, the `SKILL.md` export boundary.

Two invariants, restated for the store:

- **Ownership ≠ reachability — now total.** Retrieval (the *assign* stage / fast loop, §5) reaches the **whole store**, at **insight** granularity, conditioned on the actual job. A module organizes and governs; it never blindfolds retrieval. This fully resolves R2's own "ownership ≠ reachability" concession — there is no family-pool scope left to leak across.
- **Insights, not modules, carry knowledge.** A stage retrieves insights; modules are how the slow loop *organizes* those insights, not a thing a stage reasons "as."

**Why this does not bloat context:** injected context is bounded by the *token budget* and selected per-job by relevance over the whole active set; deriving groups changes *how the store is organized for cheap retrieval* (§6a), not *how many* insights a job loads. Insight-level retrieval (not whole-skill retrieval) is also what the granularity research endorses (the proposition is the retrieval unit).

*(R2's "boundary tickets: multi-persona refinement" is removed in R3. With no runtime personas there is nothing to negotiate between; a cross-cutting ticket is handled by per-job retrieval reaching the whole store plus file-territory allocation in the *assign* stage. The artifact-mediated, verifier-gated discipline survives as the general plan→assign→work→verify contract, not as a special persona-consultation mode.)*

### Structural insight schema (new in R2)

Every insight is authored against a fixed structural template: **precondition / action-pattern / expected-outcome** (plus scope tag). This is the "meta-skill structural prior" from Library Drift, and the R3 study sharpened *why* it matters: the schema admits a **key/value decomposition** — `key = precondition + action` ("when it applies + what to do"), `value = expected_outcome (+ scope_tag)`. That decomposition is what makes dedup and contradiction tractable (§5): same key + same value = duplicate, same key + *opposite* value = contradiction, different key = unrelated — conflicts collide on the key instead of hiding behind cosine (the schema-grounded-memory result: 97% vs 87% F1 *because* the schema makes conflicts collide). The reflector (§12) and any manual `add_idea` caller must emit this shape; registration rejects free-prose insights. **(R3 minor, open):** consider an explicit `rationale` ("because Z") field — the granularity research finds the rationale load-bearing for code-rule reuse; currently implicit in `expected_outcome`. Decide at the schema pass.

### Ratchet governance: the active set is bounded (new in R2)

The library breathes in both directions. **(R3): the cap and the silhouette/cosine heuristics that drove this in R2 become *moves scored against one objective* (§6a), not standalone thresholds.** What survives unchanged is the *shape* of the breathing:

- **Bounding is still a principle** — an unbounded active set degrades retrieval (Skill Shadowing). But the bound is no longer a fixed per-agent cap of ~50; it is whatever the §6a objective (storage codelength + expected access cost on real traces) settles to. Retrieval operates only over **active-status** insights; reachability is the whole store conditioned on the job (§4 "What a module is").
- **Outcome-driven retirement → usage-conditioned survival.** Insights that stop earning retrievals-with-wins are demoted to a **dormant archive** (preserved, searchable, revivable, out of the active index) by a censored-survival rule on usage, not a counter threshold. Admission pressure is resolved by the objective, not a fixed tournament size.
- Fitness pruning remains; the load-bearing defense is now that **groups are never authored at ingest** (§4/§6) — the drift the cap fought in R2 cannot form, because there is no incrementally-grown taxonomy to drift.

### Skills: group is the noun, compile is a patch operator

Three layers, kept distinct. **(R3): membership is *derived* by the §6 Leiden pass, not authored at ingest** — but once a module exists, its rendering/export story is unchanged from R2:

1. **Storage (the definition):** membership — `{level, parent_module?, name?, description?, ordered member_insight_ids[]}`. The membership set is the output of the partition pass (§6), recomputed/rebalanced per derive run, not appended per `add_idea`. (This is the one place the R2→R3 change is a behavioral rewrite, not a schema swap: module *identity* must be tracked across derive runs — see §6 / the design note's "durable identity" caveat.)
2. **Runtime rendering (derived, cached):** concatenation is the v1 renderer — lossless, transparent, debuggable. When a module crosses a size threshold, a **compile step** renders the group into a clean document with per-section provenance annotations back to insight IDs. Compilation is **delta-patching, never full re-render** — untouched sections stay byte-identical (ACE "context collapse" under full rewrites; byte-stability keeps prompt caching alive). The compiler never edits insights; insights stay the source of truth, or rollback is lost. (LightRAG's threshold-gated re-summary is the adopted economics: don't re-render a module until N new members accumulate.)
3. **Export:** a leaf module maps directly onto the Claude Code skill format — `SKILL.md` with `name:`/`description:` frontmatter, body = compiled rendering. The lazy-named leaf community *is* the exportable skill. The whole project can be exported as a set of real skills for real users.

### Index and matching signal (R3 — replaces "Routing signal")

The index operates on **full insight text, not description centroids** (SkillRouter: 31–44pp accuracy loss when bodies are dropped). R3 makes two changes the storage study forced:

- **Three vectors per insight, one task-prefix per master (the prefixes change geometry — §13).** A **`clustering:` key vector** (`precondition + action`) is the dedup/contradiction blocking filter (§5); a **`clustering:` full vector** (whole atom) feeds the §6 partition graph; a separate **`search_document:` retrieval vector** (whole atom, added with the *assign* stage, matched by `search_query:` queries) serves per-job retrieval. Never retrieve on a clustering vector. The two clustering vectors are the v1 organization need; the retrieval vector is additive (Phase 1+).
- **Cosine never renders a verdict.** R2's "cosine > 0.92 short-circuits to merge" is **unsafe and removed**: sentence embeddings are negation-blind — a statement sits *closer* to its negation (~0.97) than to its paraphrase (~0.94), so a cosine gate silently merges contradictions as duplicates. R3 demotes cosine to a *candidate filter* (key-vector neighbors ≥ ~0.80) and lets a **local NLI cross-encoder** make the duplicate/contradiction/unrelated call (§5). Cosine retrieves; NLI (and the §5 temporal layer) decides.

## 5. Idea registration and lifecycle

The one operation: `add_idea(text)` — but an idea has a *lifecycle*, not a placement. **(R3): ingest authors no group.** The placement/taxonomy judge (R2 steps 3–4, which minted a named skill per add) is removed; grouping is the §6 batch pass. Ingest reduces to admit → key-collision → classify → insert node. There is no `append_to_skill`/`new_skill` decision.

### Registration (R3 pipeline)

1. **Schema check:** the idea must arrive in the structural template (§4). Free prose is bounced for restructuring.
2. **Admission gate = extraction + generalization (the original piece — Operation 1).** Messy, hyper-specific raw material (a whole transcript, a PR thread) is rewritten into a clean, *transferable* schema'd atom in three sub-stages: **(a) extract by contrast** — segment, label outcomes, extract the lesson by comparing what worked vs what failed (ExpeL / MACLA); **(b) generalize by typed substitution** — replace instance trivia (file paths, repo names, values) with typed placeholders, keep load-bearing preconditions (AWM beats human-authored workflows +7.6pp doing exactly this); **(c) decontextualize + altitude-audit** — rewrite as a self-contained proposition (Dense X), then audit "one over-general misfire, one over-specific non-application" to tighten the precondition and emit the **`negative_scope`** ("when NOT to apply"). The generalization lint (anti-leak) is sub-stage (b)'s filter: "this app's admin is at /wp-admin" is stripped; "web apps commonly have role-gated admin areas" passes. Proposes a scope tag (`universal`/`stack:<x>`/`domain:<y>`); override rate is reflector-drift telemetry. No surveyed memory tool has a precision admission gate — one of the three things R3 stays original about.
3. **Content-hash fast path + key-collision candidate fetch:** embed the **key vector** (§4); fetch key-near neighbors at cosine ≥ ~0.80 (a *candidate filter*, not a verdict — contradictions live at 0.90+, so a tight gate would discard them).
4. **Classify each candidate with a local NLI cross-encoder** (`cross-encoder/nli-deberta-v3-base`, ~5ms CPU, no quota) into one of four **append-only** moves — *never an in-place merge* (mutating two records into one breaks immutability and loses provenance; the literature is unanimous against it — **Operation 2**): **entailment / near-identical → corroborate** (keep the new record distinct, add a `corroborates` edge, increment the incumbent's corroboration count — duplicates are *votes*, the §11 cross-target-recurrence signal); **same key + nuance → refine** (keep both, `refines` edge); **same key + opposite value → supersede (deferred — the stamp does NOT happen at ingest)**: record the `contradicts` edge only; the new insight is `quarantined`, so it must not touch active state. The `invalid_at` stamp that retires the incumbent is applied at **promotion**, through the single-writer queue (minting a snapshot), *after* the batch passes validation — so an unvalidated or hallucinated idea cannot silently kill a live rule, and registration stays snapshot-free (§5 lifecycle, §15 queue). At promotion the contradiction is re-checked and the winner decided (recency / source authority / evidence count); **different key → unrelated**. (Corroborate and refine are safe at ingest: a corroboration is an append-only `fitness_event`, not an active-set mutation, and a `refines`/`corroborates` edge is metadata — neither flips an incumbent's status.) The LLM judge + the adopted Graphiti `resolve_edge` prompt are the **fallback** for low-confidence NLI calls, not the workhorse — decisive under the one-Max-subscription ceiling. (NLI beats GPT-4 on short pairwise conflict, 90.9 vs 76.4 F1.)
5. **Insert** the insight node `status: quarantined` with both vectors; materialize its typed edges (similarity lazily / semantic from step 4). No module is touched at ingest.

**Operation 3 — slow-loop consolidation (additive, in the §6 derive pass).** When a community accumulates many corroborating/refining specifics, synthesize a **new general parent insight** (`provenance: consolidated`) with `generalizes_from` edges to its children. **Children are never destroyed** — demoted to `dormant` (out of the active index, preserved as evidence), never `retired`. Destroying specifics measurably hurts (TriMem −14.5% answer tokens; GAM −37% F1; rare-but-critical rules vanish by the 3rd compression pass); consolidation helps *only when additive* (TiMem +2.12% at 52% fewer tokens). The parent transfers; the children are the auditable evidence base. Consolidation is scored by §6a — a parent subsuming N now-dormant specifics lowers `cost(G)`, so it is an objective-driven move, not a separate heuristic.

This is the write path; the §6 derive pass turns the accumulating node+edge graph into named modules.

### Lifecycle: quarantine → validate → promote (or auto-revert) (new in R2)

Reflector batches do **not** enter the active library on registration. The default trust polarity is *suspect until proven*:

1. **Quarantine:** the batch registers with `status: quarantined` — retrievable only in designated trial runs, tagged in every trajectory it touches.
2. **Validation:** (a) *optional causal check* — replay the source episode's failed slice (the failing tickets/scenarios only) with the batch active: do these ideas actually address these failures? (b) *required generalization check* — a held-out micro-benchmark must show non-negative delta vs. the incumbent library.
3. **Promote or auto-revert.** Reversion is the default outcome of a failed validation, not a manual rescue. Promoted insights become `active`. **(R3):** admission is governed by the §6a objective (usage-conditioned survival), not R2's fixed-cap tournament; promotion is also where a deferred supersede applies its incumbent's `invalid_at` (§5 step 4) — both run through the single-writer queue, minting the snapshot.

This is TextGrad's validation-based reversion applied to the library, and it is the structural answer to Huang et al.: reflector ideas are self-generated refinements, the exact class of edit that degrades without external grounded validation. It also gives clean per-batch causal attribution — "which batch broke things" is a lookup, not forensics.

### Rollback statistics (new in R2)

Benchmark-driven rollback decisions use **control limits, not raw thresholds** (statistical process control): act on special-cause variation, not run-to-run grader noise. Reacting to common-cause variance as if it were signal provably destabilizes the process (Deming's funnel), and grader noise (§10) is comparable in magnitude to per-batch effects.

## 6. Partitioning (R3 — replaces "Splitting"; the slow loop)

R2 grew structure by *splitting* author-at-ingest groups on geometric triggers (silhouette 0.3/0.35, token/insight counts, k-means). R3 inverts this: structure is **derived** by a batch community-detection pass over the whole insight graph, and every structural move is scored against one objective (§6a) rather than a threshold. There is no skill-split or agent-split trigger; there is a `derive_skills` pass.

### The derive pass

1. **Build the graph** from the active insights' full vectors (§4): a **mutual-kNN / SNN graph at k≈15**, edges re-weighted by the **Tanimoto coefficient** (beats raw cosine and Jaccard for clean communities — CosTaL). At our scale (<50k nodes) the graph is recomputed from sqlite-vec each pass — sub-second; no materialized edge table, no dynamic-Leiden machinery until >100k nodes.
2. **Propose partitions.** Run **hierarchical Leiden** (`graspologic_native.hierarchical_leiden`, Constant Potts Model — modularity's resolution limit swallows small-but-real clusters) across a small **resolution sweep**, producing several candidate *whole* partitions (each call returns the full hierarchy `level` / `parent_cluster` / `is_final_cluster`: coarse levels = modules, leaf communities = skills). Leiden is a **proposal generator, not the objective**: because §6a's objective is map-equation codelength, **Infomap** (which optimizes exactly that) is the matched alternative proposer to A/B against Leiden — both feed the same scorer.
3. **Select by the objective — whole-partition, never per-move.** Score each candidate partition with §6a's `cost(G)` on the traces and **adopt the lowest-cost candidate only if it beats the incumbent partition**. Do **not** cherry-pick individual community boundaries from a proposal — accepting some moves of a partition and rejecting others would yield a partition no proposer generated (the failure this rule exists to prevent). The only **per-move** operations are the incremental deltas — **consolidate** (synthesize a general parent over corroborating specifics, §5 Op.3) and **retire** — each scored in isolation as a `cost(G)` delta against the standing partition. Retirement is usage-conditioned survival (§4 governance), not a counter; consolidation demotes its children to `dormant` (preserved), never `retired`.
4. **Name lazily.** A brief LLM pass over each changed community's central insights fills `name`/`description` (GraphRAG `create_community_reports` / Graphiti `generate_summary_description` shape) — only for communities whose membership changed, only when a name is needed.
5. **Track identity across passes.** Communities are matched to the prior partition (majority-overlap) so a module's fitness roll-ups, lineage, and `SKILL.md` export stay stable run-to-run. This is the load-bearing piece of the R2→R3 migration (durable identity for re-derivable groups); the schema delta is small, the identity-tracking behavior is new.

The derive pass runs as **one promotion-queue operation** (it mutates the active set, so it mints a snapshot — unlike snapshot-free registration). Cadence: after every N inserts, or scheduled; warm-start is unnecessary at current scale (full re-Leiden is sub-second).

The `reflector/agent_split.py` machinery is repurposed as this partition-move engine; the routing-replay validation R2 used at split time is subsumed by the objective (§6a) plus the lazy-naming pass.

## 6a. The organization objective (R3 — new; the one scorer)

Every structural move — split, merge, retire, re-home, the bound on the active set — is scored against a single objective, promoted from the theory program (essay Eq 12) to a system spec:

```
cost(G) = L(G) + L(traces | G)
```

storage codelength `L(G)` (the size of the organized graph) plus expected access cost `L(traces | G)` (how dearly the fast loop pays to retrieve, measured on **real usage traces** — the `fitness_events` log, which already exists). It is code-relative (an MDL hedge), and it is the one place R3 is original about *structure*: Leiden's own objective (graph modularity) is **access-blind**, so the partition primitive is adopted but the function that decides which partition to keep is ours.

**v1 operational definition (this is the part to nail before any code — everything in §4–§6 defers to it):** model the fast loop as a **random walker over a flow graph** whose nodes are insights and whose edge weights are the *flow* between them — in v1 the mutual-kNN similarity weights (§6), and once `fitness_events` accumulate, **co-retrieval frequency** (how often two insights are pulled into the same job; §6a edge-weight seam below).

- **`L(traces | G)` = the map equation** (Rosvall & Bergström) of that walker under partition `G` = between-module **routing** bits + within-module **locate** bits. Per access, concretely: `≈ log2(#modules the trajectory enters) + log2(|module containing the insight|)`. A partition that co-locates co-retrieved insights in small modules pays fewer bits. This is exactly Eq 13's *search-within + route-between* (Garicano), and it is why **Infomap is the matched optimizer** (it minimizes precisely this).
- **`L(G)` = storage bits:** the active insights plus a per-module codebook/description overhead (a module costs bits to name and index). This term is what penalizes *both* extremes — over-splitting (many modules → routing term blows up) and under-splitting (giant modules → locate term blows up) — so the minimum is interior, no arbitrary cap needed.
- **Acceptance semantics (resolves the §6 propose-vs-score gap):** the objective **selects whole partitions** (lowest `cost(G)` among the proposers' candidates, adopted only if it beats the incumbent) and scores **consolidate/retire as isolated per-move deltas**. It never edits a proposed partition move-by-move.

- **Edge weights, v1 → later.** The graph (§6) is built on embedding **similarity** first (available before any traces exist), so the slow loop runs from day one. Once `fitness_events` accumulate, blend in **co-retrieval** ("these insights get pulled into the same job") — which is what the access term actually rewards. This is the seam between the adopted primitive and the original objective.
- **Why it replaces the heuristics.** R2's cap ~50, silhouettes 0.3/0.35, and the compressibility gate were proxies for "is this organized well?" The objective measures that directly. A move that lowers `cost(G)` on the traces is adopted; one that doesn't is rejected — the proxies become consequences, not rules.
- **Honesty (from the theory program):** the job distribution is unknown and drifting, so the slow loop is a self-organizing rule with a splay-tree-style amortized guarantee, not a one-shot optimum — which is *why* it is a loop, run between episodes, never mid-episode (the library stays frozen during an episode for clean attribution, §11).

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

"Machine-checkable" means *checkable by something other than the producer's self-report* — a spectrum: deterministic validator > independent judge > self-report. Each stage's contract sits as far toward deterministic as its artifact allows.

### Per-stage contracts

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

**Planner cooperation:** the plan-checker gains a **file-ownership lint** — two tickets claiming the same files are flagged or serialized — and the dependency DAG (already linted) defines the rehearsal waves. Side effect: the lint pressures the planner stage toward genuinely parallelizable decompositions, itself a step toward one-shot-ability.

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
- **Overfitting detector:** positive fitness on one target, neutral-or-negative elsewhere → memorized a repo → retire automatically. **Cross-target recurrence** (the same lesson independently arising on ≥k targets) is the strongest *promotion* evidence — the evidence-based complement to the generalization lint's prediction. **(R3): this is the same quantity as §5 Operation 2's `corroborations` count — they are one mechanism, not two.** A recurring lesson arrives as a near-duplicate, gets classified `corroborate`, and increments the incumbent's corroboration count (the new record kept as distinct provenance); "cross-target recurrence ≥ k" is just "`corroborations` ≥ k from distinct targets." Build one counter, on `fitness_events`.

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

Attribution output is `{primary, contributing[]}` — a primary owner for routing pressure, with contributing factors (NTSB-style) so multi-causal failures don't starve secondary stages of signal.

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
- **Permanent library (slow loop):** at settlement, the reflector nominates run-memory survivors for generalization through the normal `add_idea` gauntlet — arriving with nonzero evidence (they already won at least once), and subject to cross-target recurrence promotion (§11). **(R3): the reflector is not exempt from the admission gate.** Its `counterfactual_insight` (§12.3) re-enters through `add_idea` and passes §5 Operation 1 (generalize by typed substitution + altitude-audit → `negative_scope`) and Operation 2 (key-collision → NLI → corroborate/refine/supersede). The reflector produces a *candidate* in the structural schema; §5 — the one door — does the generalization and the dup/contradiction classification. Generalization is owned by the gate, not duplicated in the reflector.

### Embedding and vector stack (decided)

- **Embedder: `nomic-embed-text-v1.5`** — Apache 2.0, 137M params, 768-dim (Matryoshka-reducible), CPU-friendly (~20–80ms per 200-token text), with dedicated task prefixes that **genuinely change the embedding geometry** — so a vector built for one job is not optimal for another. **(R3) — three vectors per insight, one prefix per master (decided; supersedes R2's single-prefix story and the deleted k-means/silhouette split machinery):**
  1. **`clustering:` key vector** (`precondition + action`) — dedup/contradiction blocking (§5).
  2. **`clustering:` full vector** (whole atom) — the §6 partition graph.
  3. **`search_document:` retrieval vector** (whole atom) — per-job retrieval, matched by `search_query:`-embedded job queries at the *assign* stage.

  Never retrieve on a clustering vector or partition on a retrieval vector — the prefixes are not interchangeable. The two clustering vectors are the v1 ingestion/organization need; the retrieval vector lands with the *assign* stage (Phase 1+), so v1 stores two and the third is additive. The three-vector cost is trivial at our scale (sqlite-vec brute-force, one extra encode at the cold ingest path; the hot retrieval path touches only vector 3). **Validate the geometry split on the ~50 hand-labeled pair set (§17)** — if clustering-prefixed retrieval turns out adequate, vector 3 can be dropped, but the default is the un-compromised split because the fast loop is the system's hottest, most important path. Runner-up embedder: Qwen3-Embedding-0.6B (heavier, weaker clustering fit); gte/bge-base-en-v1.5 score ~4pts higher on MTEB clustering if community separation needs it.
- **Runtime:** in-process via sentence-transformers (ONNX backend, 1.4–3× CPU speedup) — natural fit with the Python orchestrator (§15); no sidecar service needed.
- **Vector store: `sqlite-vec`, brute-force, in the same SQLite database as everything else.** At 10–50k vectors of dim 768, brute-force KNN is milliseconds; ANN only matters past ~100–200k (revisit then). Keeping vectors in the one snapshot-keyed store preserves the parallelism invariants for free. *Pinecone was considered and rejected:* it is a vector store, not an embedder (the model choice remains either way); it adds a cloud dependency and recurring cost to a system whose only paid AI is the Claude subscription; and it splits library state out of the snapshot-keyed SQLite store that rollback, quarantine, and parallel-episode keying all depend on.
- **Pinning discipline:** `embedding_model` + `embedding_dim` columns on every vector-bearing row; never mix models in one index; a model swap is a deliberate migration — full re-embed + recalibration of all cosine thresholds against the held-out labeled pair set (§17).

## 14. Anti-overfitting: cross-stack strategy

Cross-stack training (e.g., PHP target → React build) does not destroy signal — the signal never touches target code. The explorer's output is a behavioral spec; the grader compares behavior against behavior. Cross-stack is the best anti-overfitting tool available: surviving insights must be about process, not repo quirks.

- **Vary the target stack, fix the output stack** (initially). Worker/verifier stages accumulate on a consistent substrate; planner/retriever stages generalize across diverse inputs. Diversify the output stack later.
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

- **Phase 0 — the heart, standalone:** data model + `add_idea` CLI: embedding index (full-text), cosine prefilter, routing judge (placement + dedup + contradiction + generalization lint + scope tags), structural insight schema, skill rendering with delta-patch compilation, export to SKILL.md, **span/traceability SQLite schema, snapshot-ID keying, single-writer promotion queue**. Testable with hand-written ideas before any pipeline exists. **(R3 reconciliation):** the shipped Phase 0 implements the *author-at-ingest* path (the routing/placement judge that mints a named skill per add). R3 keeps the data-model spine (atomic insights, snapshots, promotion queue, structural schema, render/export) but replaces the ingest brain: the placement judge is removed, dedup/contradiction becomes key-collision + local NLI (§5), and grouping moves to the §6 derive pass. The session-transcript adapter (design note §8) is the shortest path to dogfooding the R3 write path; the §6 Leiden pass + §6a objective land once traces accumulate.
- **Phase 1 — pipeline skeleton:** plan → work → verify on toy tasks; hardcoded generic prompts; headless `claude -p`; harness gate; Ralph loop with typed failures + tripwires; **full trace capture from the first run**.
- **Phase 2 — one target, manual ideas:** registry pre-research + frontier ledger; explorer + grader on **linkding** (target #1 — small enough to hand-check every registry entry and grader verdict); episode/increment structure; **you act as the reflector** — reading traces (human viewing layer) and hand-writing ideas validates the registration machinery and *records the navigation moves the automated reflector's tools should mimic*. Mutation-seeded verifier audits start here (the reward model must be calibrated before it trains anything). Graduate to **Kanboard** (full grading-pattern coverage, legacy PHP) and then the **RealWorld implementation** (grader calibration) per §11's opening sequence.
- **Phase 3 — close the loop:** automated reflector (Stage A + Stage B); quarantine/validation/promotion lifecycle; ratchet governance (cap + retirement); splitting; held-out benchmark + control charts; improvement tier; parallel episodes.

**Where to overinvest:** the **verifier/grader instruments** (the reward model — §10's calibration machinery is how you know it works) and the **idea lifecycle** (structural schema + quarantine gate — what separates a compounding library from the +0.0pp outcome the literature measures for ungoverned ones).

## 17. Resolved questions: initializations, triggers, and decisions

Every formerly-open question now carries either a decision or a default-plus-trigger. Nothing here blocks Phase 0.

**Hyperparameters (tune as we work — the obligation is tunability, not the values):**

- All thresholds (active cap ~50, cosine merge ~0.92, step-repetition similarity, gate pass-rate, …) live in **one config file**, each entry carrying its default's provenance and its tuning metric. Decisions are logged with the threshold value that made them, so changing a value shows what would have flipped. **(R3):** several of these are demoted: the active cap ~50 and silhouettes 0.3/0.35 → moves scored against the §6a objective (no fixed cap); cosine merge ~0.92 → a *candidate filter* at ~0.80, with a local NLI cross-encoder rendering the duplicate/contradiction verdict (§5). New R3 config: three vectors per insight — key & full use `clustering:`, the per-job retrieval vector uses `search_document:`/`search_query:` (§13, one prefix per master); optional 256-dim Matryoshka truncation; the NLI confidence threshold for LLM-judge fallback; and the §6 graph params (k≈15, Tanimoto). The geometry split is validated on the hand-labeled pair set.
- Cosine thresholds are **not portable across embedding models** — recalibrate against ~50 hand-labeled duplicate pairs whenever the embedder changes.
- **Every tripwire ships in shadow mode first** (log, don't kill) — a misfiring kill-switch is worse than a missing one. The step-repetition threshold is set from the logged similarity distribution of productive iterations.
- The gate pass-rate is derived from measured grader noise (frozen replay set), not chosen independently; must-tier failures get panel adjudication rather than counting against a percentage.
- **Model-version and prompt-set changes are instrument events.** The resolved Claude model version is stamped on every span and episode; likewise every harness prompt template (stage templates, pipeline prompts, judge/resolver/reflector/induction prompts) is content-hashed into a versioned **prompt-set manifest** — registered with stored content at orchestrator startup, stamped on every span/episode, and revertible as a first-class operation (activate any prior set). A change to either annotates the SPC charts (limits recompute), triggers fixture re-records, and is reported alongside the curves — score movement across an instrument change is never attributed to the library. (The *learned* prompt material — insights, and the modules/descriptions derived from them — is already versioned and revertible via snapshots and module lineage; the manifest covers the harness side.)

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

### R3 sources (the memory-store refactor — full provenance + adopt/adapt/build table in `2026-06-11-memory-store-simplification-design-note.md`)

**OSS systems studied (build-vs-adopt):** mem0 — live path is ADD-only, the 4-op merge is orphaned (2504.19413); Graphiti/Zep — bi-temporal edges, invalidate-not-delete, `resolve_edge` dedup/contradiction prompt, 94.8% DMR (2501.13956); Microsoft GraphRAG — hierarchical Leiden + community reports (2404.16130); LightRAG — incremental merge, threshold-gated re-summary (2410.05779); Cognee, HippoRAG (PPR). Verdict: keep the SQLite atomic-insight core; adopt leaf algorithms (`graspologic_native` Leiden, Graphiti's prompt, LightRAG re-summary), not frameworks.

**Ontology — derive-from-graph, not author-at-ingest:** no surveyed system names groups per-add; Library Drift +0.0pp vs +16.2pp (2605.19576); Skill Shadowing −21% / 68% from name-collision (2605.24050); TaxoAdapt — corpus-derived beats static LLM taxonomy 26–50% (2506.10737); SkillGraph — derived edges +31.2pts (2605.12039); CommunityKG-RAG — community retrieval +16.45pp (2408.08535); GitNexus — Leiden→per-community SKILL.md, in production.

**Atom granularity & schema:** Dense X Retrieval — propositions +12 Recall@5, self-contained (2312.06648); schema-grounded memory (xmemory) — 97% vs 87% F1, conflicts collide on a key (2604.27906); structural-memory granularity comparison (2412.15266); FActScore atomic facts (2305.14251).

**Storage/index & contradiction:** negation blindness — a negation is cosine-closer than a paraphrase (HEROS 2306.05083; Semantic-Adapter 2504.00584); NLI cross-encoder beats GPT-4 on short pairwise conflict 90.9 vs 76.4 F1 (ECon 2410.04068; deberta-v3 ~92% MNLI); DiffCSE/SNCSE negation-aware embeddings (2204.10298, 2201.05979); SemDeDup threshold fragility (2303.09540); graph-from-embeddings — mutual-kNN + Tanimoto (CosTaL, bbad157); dynamic Leiden 3.9–6.1× (2410.15451); `clustering:` prefix geometry (nomic 2402.01613; intrinsic-dim 2506.01435); Matryoshka (2205.13147).

**Generalization & consolidation (Operation 1/3):** AWM — typed-variable abstraction beats human workflows +7.6pp (2409.07429); MACLA — contrastive precondition tightening, over-abstraction → 51% reusability (2512.18950); WALL-E / Guideline-Learning — instance-stripping, negative scope (2410.07484, 2310.05066); Generative-Agents reflection — additive, originals never deleted (2304.03442); TriMem — fact-only loses 14.5% answer tokens (2605.19952); GAM — removing the specific layer −37% F1 (2604.12285); TiMem — additive hierarchy +2.12% at 52% fewer tokens (2601.02845); ExpeL UPVOTE = corroboration votes (2308.10144); provenance-aware append-only tiering (2602.17913).

**Organization objective (§6a):** the map equation / Infomap — codelength = routing + locate (Rosvall & Bergström, *PNAS* 2008); Garicano — knowledge hierarchy as search-within + route-between (2006). The outer objective `G* = argmin_G E_q[min_C D(q,C)] + λL(G)` is formally open in the RAG literature (survey 2507.13334; REPLUG; IB-RAG) — the inner bracket is the field's canonical objective, the outer is the original lane.
