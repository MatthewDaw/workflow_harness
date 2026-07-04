---
date: 2026-06-11
topic: memory-store simplification + stage-runtime refinement (R3 input)
status: design note — pressure-tested against 5 cloned OSS memory systems
governing principle: reuse established techniques everywhere; be original only where originality is load-bearing
inputs: 2026-06-11-ideation-and-decisions.md (§3 pivots, §4 slow-loop vision); DESIGN.md R2;
        oss-memory-study/{mem0,graphiti,cognee,graphrag,LightRAG,HippoRAG}; agent-families/src (Phase 0–007 core)
---

# The Memory Store, Simplified — and What the Stage Runtime Needs From It

## 0. TL;DR (the boring-technology answer)

**Do not adopt a memory framework. Keep the Phase 0 SQLite atomic-insight core. Adopt three leaf-level techniques as library calls, and reduce the original surface to three things.**

The headline finding from reading the source (not the marketing): **our Phase 0 core is already a better fit for our own requirements than mem0, Graphiti, or Cognee are.** Every one of those systems *dissolves or mutates facts* (mem0 fuses fact+context into prose blobs and forbids atomicity; Graphiti overwrites edge timestamps in place; Cognee stores everything). We need the opposite — **atomic, immutable, individually-addressable insight records** — because rollback, quarantine, and snapshot-keyed parallel episodes all depend on it, and none of those tools has any of those three. Adopting one of them as the engine means fighting its core storage philosophy (plus inheriting a graph-DB or FastAPI dependency) just to climb back to where `store.py` already sits.

So the build-vs-adopt answer inverts the usual one. The *framework* is build-it-yourself (and mostly already built). The *originality* is much narrower than the org chart of our design implies — but the three original pieces survive contact with the source code.

**What we adopt (leaf libraries / prompts, not frameworks):**
- `graspologic_native.hierarchical_leiden` — the partition primitive (replaces hand-rolled k-means + silhouette thresholds).
- Graphiti's `resolve_edge` dedupe/contradiction prompt + its bi-temporal field idea — to harden our contradiction handling.
- LightRAG's threshold-gated merge-then-summarize — the "don't re-render until N accumulate" economics.
- (Later) HippoRAG's PPR reset-vector retrieval pattern — once the store is genuinely a graph.

**What stays original (and why each is load-bearing — §4):** the domain-scoped admission/denoising gate; the organization *objective* (codelength + expected access on real traces) that scores every structural move; the one governed `add_idea()` gauntlet with quarantine→validate→promote + snapshot rollback.

---

## 1. What the source actually showed (correcting our priors)

Three things changed my read versus the decision record's §4 "market verdict":

**(a) mem0's famous 4-op merge is gone from its live path.** The clone is upstream v2.0.5; `Memory.add()` now runs a **V3 ADD-only** pipeline (`mem0/memory/main.py:679-988`). The `DEFAULT_UPDATE_MEMORY_PROMPT` with ADD/UPDATE/DELETE/NONE (`configs/prompts.py:176-324`) is **orphaned** — no caller. Contradictions are *linked, not resolved*: a new contradictory fact is ADDed alongside the stale one. So the thing we cited mem0 for ("dedup + contradiction semantics") is no longer mem0's live behavior. **We already have a working version of what mem0 deleted** — our `judge.OUTCOMES` enum (`judge.py:57-66`) is a richer 8-way merge decision: `append_to_skill`/`new_skill` (ADD-with-placement), `merge_discard` (NOOP/dedup), `contradiction_flag`/`contradiction_supersede` (UPDATE/DELETE), `lint_reject`/`rewrite_proposed` (admission gate), `no_placement` (NOOP).

**(b) No tool has an admission gate.** mem0's extraction prompt is explicitly recall-biased ("When in doubt, extract" — `prompts.py:578`); Cognee does *zero* relevance filtering (stores everything the LLM extracts); Graphiti/GraphRAG have no admission concept. Our `lint_reject` + generalization lint ("write the lesson, not the fact") is genuinely absent from the field. **Prior confirmed.**

**(c) The partitioner is a 26-line wrapper, not a framework.** GraphRAG's entire Leiden surface is `graphs/hierarchical_leiden.py:1-26` calling `graspologic_native.hierarchical_leiden(edges, max_cluster_size, seed)` and getting `{node, cluster, level, parent_cluster, is_final_cluster}` — full hierarchy from one call. Everything else in GraphRAG is pandas/IO plumbing. **We can depend on the Rust wheel directly and skip the framework.** Crucially, GraphRAG's Leiden optimizes *graph modularity* and re-clusters delta data into *disjoint new communities* without re-balancing (`update/communities.py:47-73`) — which is exactly the gap our Eq 12 objective fills. The primitive is adoptable; the *objective that drives it* is ours.

---

## 2. The simplified architecture

One entry point, one substrate, a fast loop and a slow loop over it.

```
                         add_idea(text, source)         ← session transcripts | repo miner | PR threads
                                  │
   ┌──────────────────────────────┴───────────────────────────────┐
   │  THE GAUNTLET  (slow-loop write path — one governed pipeline)  │
   │                                                                │
   │  1. extract     raw → atomic candidate(s) in structural schema │  (build; cognee-shaped, trivial)
   │  2. admit       denoise: "only what makes code better" +       │  ★ ORIGINAL gate
   │                 generalization lint (no target trivia)         │
   │  3. dedup       cosine>0.92 prefilter → short-circuit merge    │  (have; SkillRouter threshold)
   │  4. reconcile   placement + contradiction decision (8-way)     │  ADAPT Graphiti resolve_edge prompt
   │  5. place       append-to-module | new-module; quarantined     │  (have; judge + lifecycle)
   │  6. validate    held-out delta ≥ 0 → promote, else auto-revert │  ★ ORIGINAL governed lifecycle
   └────────────────────────────────┬───────────────────────────────┘
                                    │  writes flow through the single-writer
                                    │  promotion queue → mints one snapshot
                                    ▼
        ┌───────────────────────────────────────────────────────────┐
        │  SUBSTRATE: atomic immutable insights in ONE SQLite db      │
        │  insights (never dissolved) · skills/modules = membership   │
        │  views · sqlite-vec brute-force KNN · status_transitions ·  │
        │  snapshots · fitness_events (= the usage traces)            │
        └───────────────────────────────────────────────────────────┘
                                    ▲
   ┌────────────────────────────────┴───────────────────────────────┐
   │  REORGANIZE (slow loop, across jobs): graph partition moves      │
   │  scored against ONE objective — codelength L(G) + expected       │  ADOPT graspologic_native Leiden
   │  access cost on fitness_events traces (Eq 12). Retirement =       │  as primitive; ★ ORIGINAL objective
   │  usage-conditioned survival, not counters.                       │  as the scorer of every move
   └─────────────────────────────────────────────────────────────────┘
                                    ▲
   ┌────────────────────────────────┴───────────────────────────────┐
   │  RETRIEVE (fast loop, per job): min_C D(q,C) over the WHOLE      │
   │  store conditioned on the actual job. ANN now; PPR over the      │  (have ANN; ADAPT HippoRAG PPR later)
   │  module graph later. No preset agent/domain bias.                │
   └─────────────────────────────────────────────────────────────────┘
```

The key structural simplification from the refactors: **"agents/families" disappear as a runtime concept.** The `skills` table is already "ordered membership view over insights"; a *module* is the same thing one level up — a community of insights that the slow loop maintains as a placement/routing/governance unit. The fast loop reaches the **whole store** conditioned on the job; ownership is a governance boundary, never a retrieval wall (DESIGN's own "ownership ≠ reachability," now total).

---

## 2a. The ontology decision (researched + decided 2026-06-12): derive-from-graph

This was the open question blocking R3's data model, and it turned out to be the same question as "how do we stream ideas in." Resolved by a literature + OSS-source research pass (3 agents; full citations in the session record). **Decision: Ontology B — derive groups from the graph; author nothing at ingest.**

**The two ontologies:**
- **A — author-at-ingest (what Phase 0 built):** every `add_idea` runs the placement judge, which mints a *named skill* in the same transaction (`create_skill` inside `add_idea`). The taxonomy is LLM-authored incrementally.
- **B — derive-from-graph (decided):** ingest adds only the *insight node + KNN similarity edges*. Named groups are **derived by a batch community-detection pass and named lazily** after clustering.

**Why B, decisively (evidence):**
- **Every production graph-RAG system is B.** GraphRAG (hierarchical Leiden → `create_community_reports`), Graphiti (`build_communities`, default `update_communities=False`, summary after), Cognee (optional memify bucketing + summary) all author only nodes+edges at ingest and derive+name groups in a separate pass. LightRAG rejects community detection entirely; mem0 has no grouping. **Phase 0 is the lone author-at-ingest outlier among all six systems studied.**
- **Author-at-ingest has a measured ceiling:** LLM-authored skill libraries +0.0pp vs no-skills baseline, human-curated +16.2pp (Library Drift, arXiv:2605.19576); 21% pass-rate drop at ~200 skills, 68% from skill-shadowing/name-collision (arXiv:2605.24050); static LLM taxonomies misalign with evolving corpora by 26–50% vs corpus-derived (TaxoAdapt, arXiv:2506.10737).
- **Derive-from-graph wins on the same benchmarks:** graph-derived retrieval +31.2pts over flat categorization on sequential tasks (SkillGraph, arXiv:2605.12039); community-level retrieval +16.45pp over semantic-only (CommunityKG-RAG); dynamic Leiden re-derives at 3.9–6.1× full-recompute with <0.002 modularity loss (arXiv:2410.15451) — lazy periodic re-clustering is affordable.
- **Direct precedent, in production, in our exact domain:** GitNexus (Apr 2026) runs Leiden over a codebase graph and emits a per-community `SKILL.md` for Claude Code — our planned architecture, already deployed.

**This collapses the "one level or two" question.** Hierarchical Leiden returns `level` / `parent_cluster` / `is_final_cluster` from one call, so **one mechanism** yields the whole hierarchy: a *module* is a coarse level, a *skill* is a leaf community, both derived, both named lazily, and a leaf community's lazy summary **is** the `SKILL.md` export. No second authored level exists.

**The atom is validated (keep it):** the schema-enforced self-contained proposition is the best-performing retrieval+memory unit (Dense X Retrieval +5.9–7.8 EM over passages at fixed budget, arXiv:2312.06648; xmemory 97% vs 87% F1 on state/contradiction queries *because* the schema makes conflicts collide, arXiv:2604.27906). Our `precondition / action / expected_outcome + scope_tag` is exactly this. *Minor refinement to weigh:* the literature stresses the "**because Z**" rationale for code-rule reuse — confirm it's carried (currently implicit in `expected_outcome`); consider an explicit `rationale` field.

**What streaming becomes:** `add_idea` reduces to *structural-validate → content-hash → embed → dedup/contradiction → insert insight node (quarantined) + materialize KNN edges*. No taxonomy decision at ingest. The grouping work moves to a scheduled batch `derive_skills` pass (KNN graph → `graspologic_native.hierarchical_leiden` → lazy LLM naming), run as **one snapshot-minting promotion-queue operation** (clustering mutates the active set, unlike today's snapshot-free registration).

**The cost, honestly:** the schema migration is small (insights / `skill_members` / snapshots already model what B needs); the real cost is a **behavioral rewrite** of the consumers that assume durable skill identity — retrieval budgeting, per-skill fitness roll-ups, agent-split lineage, cap-tournament. **But R3's agents→stages refactor already deletes most of them** (agent-split lineage gone; cap-tournament becomes an objective move; persona retrieval prior gone), and retrieval moves from skill-level to **insight-level** scoring — which the granularity research says is the correct direction anyway. A→B and agents→stages are complementary: do them together. Per-insight `fitness_events` (keyed on `insight_id`) survive untouched.

## 2b. Smart storage & the index (researched + decided 2026-06-12): key/value decomposition + retrieve-then-classify

The substrate question — "store ideas so similar ones are close, clusterable, and dedup/contradiction-checkable." Resolved by a second research pass (2 agents, full citations in the session record). It surfaced a **latent bug** in the current design and a single idea that fixes the whole substrate.

**The bug: a cosine-only dedup gate is unsafe.** Sentence embeddings are *negation-blind* — a statement and its negation sit at cosine ~0.97 while a paraphrase sits at ~0.94 (HEROS; e5 measurements): the **contradiction is closer than the duplicate**. So Phase 0's cosine>0.92 prefilter, used to *decide* duplicate-vs-not, will systematically misfile "never use X" as a duplicate of "always use X" and silently merge them. Cosine cannot separate duplicate from contradiction — architectural, not tunable. This must change before the store ingests real contradictory rules.

**The fix (one idea, three payoffs): key/value decomposition of the schema'd atom.**
- **Key** = `precondition + action` (when it applies + what to do), embedded with the `clustering:` prefix (reserved in `embedding.py`, currently unused).
- **Value** = `expected_outcome (+ scope_tag)`.
- Same key + same value = **duplicate**; same key + opposite value = **contradiction**; different key = **unrelated**. Contradictions collide deterministically instead of hiding behind cosine (xmemory 97% vs 87% F1 *because* schema makes conflicts collide, arXiv:2604.27906; Graphiti only compares edges between the *same entity pair* — O(N²)→O(k), arXiv:2501.13956). The **key vector** is the dedup/contradiction blocking filter (ASDC: 98% fewer comparisons, recall 92%→99%); the **full vector** feeds the Leiden graph.

**The verdict on dup-vs-contradiction: retrieve-then-classify with a LOCAL NLI cross-encoder.** Since cosine can't decide, retrieve key-near candidates by embedding, then classify each pair with `cross-encoder/nli-deberta-v3-base` (~5ms CPU, ~90–92% MNLI): entailment→duplicate, contradiction→supersede, neutral→keep. NLI **beats GPT-4 on short pairwise conflict, 90.9 vs 76.4 F1** (ECon, arXiv:2410.04068), costs zero quota, and is deterministic for record/replay — decisive under the one-Max-subscription ceiling. The LLM judge + the adopted Graphiti `resolve_edge` prompt become the **fallback** for low-confidence NLI calls, not the workhorse.

**The graph spec for the `derive_skills` Leiden pass (evidence-backed defaults):** mutual-kNN / SNN graph at **k≈15** over full-vector embeddings; edges re-weighted by **Tanimoto** (beats raw cosine and Jaccard — CosTaL, *Brief. Bioinform.* bbad157); **Leiden with CPM**, not modularity (avoids the resolution limit swallowing small-but-real clusters). At our scale (<50k nodes) **recompute from sqlite-vec each batch** — sub-second, no materialized edge table, no dynamic-Leiden machinery yet (warm-start only matters >100k nodes).

**Embedding tweaks (adopt):** switch stored embeddings to the **`clustering:` prefix** (better intra-cluster geometry; intrinsic dim of clustering embeddings is ~10–26); consider **256-dim Matryoshka** truncation (−1.2 MTEB pts, −67% storage). Embedder stays nomic-embed-text-v1.5; *optional later A/B:* gte/bge-base-en-v1.5 score ~4 pts higher on MTEB clustering if absolute community separation needs it.

**The assembled write path (replaces Phase 0's cosine-gate dedup):**
```
add_idea(insight):
  1. KEY-COLLISION (deterministic): key-vector neighbors ≥ ~0.80 → candidate set
                                    (0.80, NOT 0.92 — contradictions live at cosine 0.90+)
  2. NLI CLASSIFY (local cross-encoder) per candidate:
       entailment    → corroborate → keep new + CORROBORATES edge + ++incumbent.corroborations
       same key+nuance→ refine      → keep both + REFINES edge
       contradiction → supersede   → record CONTRADICTS edge ONLY; invalid_at stamped at
                                      PROMOTION through the queue (NOT here — new node is quarantined)
       neutral       → keep both
     (LLM judge / Graphiti resolve_edge prompt = fallback when NLI confidence is low)
  3. INSERT node (quarantined) + store key & full CLUSTERING vectors. No group authored (§2a, Ontology B).
     (retrieval vector — search_document: — added with the assign stage; never retrieve on a clustering vector)
derive_skills (batch): full-vector → mutual-kNN(k≈15)+Tanimoto → Leiden/Infomap propose →
                       SELECT whole partition by §6a map-equation cost → lazy LLM names.
```
Supersede is deferred: an unvalidated quarantined idea records the contradiction at ingest but only the promotion queue (post-validation) stamps `invalid_at` and retires the incumbent — registration stays snapshot-free.

**Decisions locked (2026-06-12):** (A) **key/value decomposition** as the storage primitive, realized as **three vectors per insight, one task-prefix per master** (the prefixes change geometry — §13): `clustering:` key (precond+action, blocking), `clustering:` full (whole atom, partition graph), `search_document:` retrieval (whole atom, per-job retrieval matched by `search_query:` — added with the *assign* stage). Two clustering vectors are the v1 need; the retrieval vector is additive. Validate the split on the hand-labeled pair set. (B) **retrieve-then-classify with a local NLI cross-encoder**, and **drop the cosine-only dedup verdict** (cosine demoted to a ~0.80 candidate filter; NLI/temporal make the call); (C) **supersede is deferred** — `contradicts` edge at ingest, `invalid_at` at promotion through the queue. `cross-encoder/nli-deberta-v3-base` is a new local dependency (~400 MB, CPU, no API) — record/replay-friendly. *Minor open:* an explicit `rationale` field (the "because Z" the granularity research flagged) — defer to the schema pass.

## 2c. Summarization / rewriting & the lifecycle of similar ideas (researched + decided 2026-06-12)

The store ingests *messy, hyper-specific* raw material (whole conversation threads, PR debates, debugging war stories). Two questions: how to **rewrite** that into clean, generalized, transferable insights; and what to do when a **similar** idea arrives. Resolved by a research pass (2 agents, full citations in the session record). The governing constraint is the immutability principle (§4): **insights are never dissolved or mutated** — so "merge by rewriting two records into one" is *forbidden*, and every operation below is append-only.

### Operation 1 — Ingest-time extraction & generalization (the admission gate, fully specified)

Raw → transferable schema'd atom in three sub-stages (this is the "write the lesson, not the fact" gate, one of the three original pieces):

1. **Extract by contrast.** Segment the transcript, label outcomes, extract the lesson by comparing what *worked* vs what *failed* (ExpeL insight extraction; MACLA contrastive extract). The contrast surfaces the causal rule, not the narrative.
2. **Generalize by typed substitution.** Replace every instance-specific token (file path, repo name, exact value) with a typed placeholder (`{config-file}`, `{package-name}`); keep the load-bearing precondition. AWM does exactly this and **beats human-engineered workflows +7.6pp on WebArena (+51% relative)**. Altitude is *data-driven* — over-abstraction misfires (MACLA over-abstracted SQL → 51% reusability vs 76–78%), under-abstraction won't transfer.
3. **Decontextualize + altitude-audit.** Rewrite as a self-contained proposition (coreferences resolved, context inlined — Dense X, +12 Recall@5). Then an explicit audit: *"give one case where this misfires from over-generality, and one where it's too specific to ever apply."* The audit tightens the precondition and emits a **new `negative_scope` field** ("when NOT to apply" — MACLA ΔΨ⁻, WALL-E exclusions).

Output: the key/value schema'd atom (§2b) + `scope_tag` + `negative_scope`. *Example:* a 3-hour Vite debugging thread → `{precondition: "Vite monorepo, workspace pkgs imported by bare specifier", action: "set resolve.alias per {package-name} → {source-root}", outcome: "prevents build-time module-resolution failure", negative_scope: "not when pkgs are published to npm / resolved via node_modules", scope_tag: stack:vite}`.

### Operation 2 — Near-duplicate handling: corroborate / refine / supersede (NOT mutate-merge)

When key-collision + NLI (§2b/§5) fires on an incoming insight, the verdict resolves to one of four append-only moves — **never** an in-place merge (mem0's UPDATE and A-MEM's evolution both overwrite and lose the pre-state; the literature is unanimous against destroying records):

| NLI verdict vs candidate | Move | Mechanism (all append-only) |
|---|---|---|
| Entailment / near-identical (same key+value) | **Corroborate** | Keep the new record distinct; add a `CORROBORATES` edge; **increment the incumbent's corroboration count**. Duplicates are *votes*, not waste (ExpeL UPVOTE, TEMPR confidence, HMO recall-frequency) — this *is* the §11 cross-target-recurrence promotion signal, and it rides `fitness_events`. |
| Same key, adds a nuance/condition | **Refine** | Keep both; `REFINES` edge. |
| Same key, opposite value | **Supersede (deferred)** | At ingest record the `CONTRADICTS` edge **only** — the new insight is quarantined, so it must not mutate active state. The `invalid_at` stamp + supersede is applied at **promotion**, through the single-writer queue (mints a snapshot), after validation; an unvalidated/hallucinated idea cannot kill a live rule. |
| Different key | **Unrelated** | Keep; no edge beyond similarity. |

So the answer to "should we merge?" is **no mutate-merge** — corroborate (vote), link, or supersede. The typed edges (`corroborates / refines / contradicts / generalizes_from`) enrich the §4 `InsightEdge` model that Leiden already clusters on.

### Operation 3 — Slow-loop consolidation: general parents, preserved children

Where transfer-learning compression actually happens. When a Leiden community accumulates many corroborating/refining specifics, the derive pass (§6) synthesizes a **new, general parent insight** (an immutable record, `provenance: consolidated`) with `GENERALIZES_FROM` edges to its children — additive reflection (Generative Agents, TiMem 5-level tree, TriMem 3-tier, GAM, Zep community summaries all do this). **Children are never destroyed** — demoted to `dormant` (out of the active index, preserved as evidence/provenance), never `retired`.

Keeping the children is **not optional** — the evidence that destroying them hurts is sharp: TriMem loses **14.5%** of answer tokens from fact-only storage; GAM drops **37% F1** when the specific layer is removed; the canonical survey failure is a rare-but-critical rule ("never call the production DB directly") *vanishing by the third compression pass*. Consolidation helps **only when additive** (TiMem: +2.12% accuracy at 52% fewer retrieved tokens). The parent transfers; the children are the evidence base that keeps it trustworthy and auditable.

Triggers + scoring: consolidation fires on a corroboration-count threshold or during the §6 derive pass, and is scored by the §6a objective — a parent that subsumes N specifics (which then go dormant) **lowers `cost(G)`**, so consolidation is a natural objective-driven move, not a separate heuristic.

**Schema deltas implied:** insight gains `negative_scope` (text) and `provenance: consolidated`; `InsightEdge` gains a `kind` (`similarity` [derived, for Leiden] | `corroborates` | `refines` | `contradicts` | `generalizes_from`); corroboration counts live in `fitness_events` (new kind) or as `corroborates`-edge counts; `dormant` status (already in the enum) becomes the consolidation children's resting state. All append-only; immutability preserved throughout.

## 3. Adopt / Adapt / Build — per component

| Pipeline stage | Strongest existing technique | Source (repo · module) | Verdict | Why |
|---|---|---|---|---|
| **Ingestion / extract** | Streaming generator-stage pipeline; "facts" from conversation | Cognee `pipelines/tasks/task.py`, `run_tasks_base.py`; mem0 `ADDITIVE_EXTRACTION_PROMPT` | **BUILD** (steal prompt shape) | The "DAG" is a ~200-line linear list welded to global engine singletons — not liftable. Reuse the *prompt shape* for raw→atomic; our structural schema (precond/action/outcome) is the contract neither has. |
| **Denoise / admission gate** | — (nobody has one) | — | **★ BUILD (original)** | mem0 over-extracts by instruction; Cognee filters nothing; Graphiti/GraphRAG have no admission step. Our `lint_reject`/generalization lint is the precision gate the field lacks. |
| **Dedup (prefilter)** | Cosine>0.92 short-circuit; MinHash+LSH name dedup | SkillRouter (cited); Graphiti `dedup_helpers.py:220-279` | **HAVE / ADAPT** | We already have cosine>0.92 (`judge`/registration). Graphiti's MinHash+LSH+entropy-gate is a nice cheap pre-LLM narrower if name-level dedup ever matters; not needed at v1 volume. |
| **Contradiction / reconcile** | Combined duplicate-vs-contradiction LLM call; bi-temporal validity | Graphiti `prompts/dedupe_edges.py:43-100` + `edge_operations.py:538-573` | **ADAPT (port prompt + idea)** | ~150 lines of pure Python + 2 prompts, zero graph dependency. The prompt's "same relationship, updated title = contradiction NOT duplicate" example directly hardens our `contradiction_supersede`. Keep **append-only**: emit a *new* insight that stamps the prior's `invalid_at`, never mutate in place (Graphiti mutates; we must not). **Decided (§7): adopt `valid_at`/`invalid_at` — `invalid_at` set mechanically on supersede, `valid_at` NULL unless the insight is inherently version-scoped; world/version validity only, since snapshot-time is already covered by `status_transitions`.** |
| **Placement** | LLM places into skill/agent; ANN top-k candidates | our `judge.OUTCOMES`; mem0 `top_k=10` candidate retrieval | **HAVE** | Our placement judge is already a superset of mem0's live path. Nothing to adopt. |
| **Reorganize / partition** | Hierarchical Leiden over the KG | GraphRAG `graphs/hierarchical_leiden.py:1-26` → `graspologic_native` | **ADOPT primitive · ★ BUILD objective** | Depend on the `graspologic-native` Rust wheel directly (no torch, no framework). Feed it `list[(src,dst,weight)]`; get the full hierarchy. **The move-scoring objective (Eq 12) is ours** — Leiden optimizes modularity, which is access-blind. |
| **Incremental merge** | Read→accumulate→vote-type→sum-weight→threshold-gated re-summary | LightRAG `operate.py:2000,2329,265`; `force_llm_summary_on_merge=8` | **ADAPT** | Maps onto our delta-patch compile: don't re-render/re-summarize a module until N new members accumulate. Cheap economics win; port the algorithm, not the package. |
| **Retrieval (fast loop)** | ANN now; PPR reset-vector over graph later | sqlite-vec (have); HippoRAG `HippoRAG.py:1407-1519` | **HAVE / ADAPT later** | ANN over the whole store is fine at 10–50k vectors. HippoRAG's pattern (seed query-relevant nodes → PPR-diffuse → read scores at target type) is the upgrade *if/when* the module graph makes multi-hop relevance matter. `igraph.personalized_pagerank` is a one-liner. |
| **Storage substrate** | Embedded vector + SQL, no server | mem0 (faiss/chroma + SQLite); ours (sqlite-vec) | **HAVE** | We already have the one-database, snapshot-keyed, single-writer store that none of them has. Keep it. |
| **Governed lifecycle** | — (nobody has quarantine→validate→promote + snapshot rollback) | — | **★ HAVE (original)** | `lifecycle.py` + promotion queue + `status_transitions` is the validation-based reversion (TextGrad applied to a library) that no streaming memory tool implements. |

---

## 4. Where we choose to be original (each with one sentence on why no tool suffices)

After reading source, the original surface is **smaller and sharper** than the decision record implied — three things, plus one load-bearing non-novel divergence:

1. **The domain-scoped admission/denoising gate** — *every* surveyed tool stores at recall (mem0 "when in doubt, extract"; Cognee stores all; Graphiti/GraphRAG have no gate), so a precision gate that admits "only what makes code better" and strips target trivia is genuinely unoccupied ground.
2. **The organization *objective*** (storage codelength + expected access cost on real `fitness_events` traces, Eq 12) — GraphRAG's Leiden optimizes graph modularity, which is blind to how the fast loop actually pays for access, so the primitive is adoptable but the function that decides *which* partition moves to keep is ours.
3. **The one governed gauntlet** — `add_idea()` with quarantine→validate→promote, snapshot-keyed rollback, and per-batch causal attribution is validation-based reversion that no streaming memory system (all of which write irreversibly on ingest) provides.
4. **Atomic immutable insight records as the substrate** *(not novel research, but a deliberate, load-bearing divergence)* — mem0 dissolves facts into prose, Graphiti mutates edges in place, Cognee has no atomic record; we keep immutability because rollback, quarantine, and snapshot-keyed parallel episodes are impossible without it.

Everything else — contradiction detection, partitioning, merge economics, retrieval, ingest plumbing, the vector substrate — we **adopt or adapt**. If a reviewer asks "what did you reinvent that you shouldn't have," the honest answer is: the contradiction prompt (now adopting Graphiti's) and the heuristic partitioner (now adopting Leiden).

---

## 5. Could v1 literally be one of these tools + our gate? (the question, answered)

**No — and the reason is concrete, not aesthetic.** Pick the best candidate, mem0 (embedded, Anthropic-first, SQLite history):
- **Lose atomic immutability** — mem0's extraction *forbids* atomicity (`prompts.py:606-621`), so quarantine/rollback/causal-attribution have nothing to attach to.
- **Lose snapshot keying + single-writer promotion queue** — mem0 is streaming per-session top-k; there is no "library snapshot S" for N parallel episodes to read, which our parallel-experiment invariant (DESIGN §15) requires from day one.
- **Lose the governed lifecycle** — no validation-based reversion exists; we'd bolt ours on outside mem0 anyway.
- **Inherit friction** — `openai` + `qdrant-client` as hard core deps, PostHog telemetry, soft structured-output on Anthropic (`response_format` ignored — `llms/anthropic.py`).

You'd end up running mem0 with `infer=False` (raw store-by-id) and putting our admission gate, our placement, our lifecycle, and our atomic schema on top — i.e., using ~30% of mem0 (the storage shell we already have in sqlite-vec) and rebuilding the brain. **Net: adopting a framework costs more than it saves.** Graphiti is worse (Neo4j/FalkorDB, or deprecated embedded Kuzu); Cognee worse still (FastAPI + fastapi-users + litellm + graph store). The Phase 0 core *is* the engine; the OSS value is leaf algorithms.

---

## 6. Part B — what the stage runtime needs from the store (and what changes)

Under refactors 1–4 (agents→stages; specialists→graph; per-job retrieval; persistent sessions = contingent optimizations), the runtime is a **fixed pipeline of stages** — plan → assign → work → verify — differing by tools, permissions, output contract, and **trust structure** (the verifier must be a different session than the producer; that independence cannot be retrieved into existence). Stages do **not** differ by knowledge.

What that runtime asks of the store, and whether it changes the store's design:

| Runtime need | Mechanism in the store | New work? |
|---|---|---|
| **Per-job retrieval conditioning** (fast loop) | `retrieve(job_query, snapshot)` over the **whole** active set, no preset bias | **Simplifies** `library/retrieval.py` — delete the own-skills-prior + family-pool-scope logic; retrieval scope becomes the whole store conditioned on the job. |
| **Assign stage = the routing that remains** | the same `retrieve()` call, plus **file-territory** assignment for parallel work | File-ownership lives in the **plan-checker lint** (DESIGN §11, already specified), not the store. No store change. |
| **Trace logging for the slow loop** | `spans` + `fitness_events` (append-only, snapshot-keyed) | **Already built** (`_SCHEMA_V4`). These *are* the usage traces Eq 12 integrates over — the slow-loop math's substrate already exists. |
| **Handoff context between stages** | artifact-mediated (typed verdicts, append-only ledger) | Already the design (§7). No store change; no persona-relay reintroduced. |
| **Module = governance/placement unit** | `skills`/`agents` tables, repurposed as graph modules | **Demote, don't delete** (see §7). |

**The important conclusion: the store's *design* barely changes** — what changes is that several things we built (persona base prompts, family router, own-skills retrieval prior, silhouette splitting) become **dead or demoted**, and one thing we deferred (the objective) becomes the load-bearing scorer.

---

## 7. DESIGN.md R3 — minimal change list

Smallest diff that lands the pivots without touching the substrate code:

- **§3 Agent families → Stages.** Rewrite: the runtime is a fixed `plan → assign → work → verify` pipeline; stages differ by tools/permissions/output-contract/trust (verifier-independence is the load-bearing invariant), never by knowledge. Delete the router-plus-N-specialists framing.
- **§4 "What an agent is" → "What a module is."** An agent-as-lens/persona is removed. Per §2a (decided), a **module/skill is a *derived* community of atomic insights** (a level of the hierarchical-Leiden partition), named lazily — never authored at ingest. It serves as a placement/routing/governance unit. *Ownership ≠ reachability* becomes total (retrieval reaches the whole store conditioned on the job, at **insight** granularity); "specialization is a dial" and "stays thin" no longer apply to runtime personas. `add_idea` no longer authors named groups (§2a): the placement/taxonomy judge is removed; ingest = insert insight node + KNN edges + dedup/contradiction only.
- **§4 Boundary tickets / multi-persona refinement → delete.** No personas to negotiate; a cross-cutting job is a retrieval + file-assignment concern, not a persona consultation. (Removes the most speculative Phase-5 machinery.)
- **§5 Registration → keep, harden one stage.** Replace the home-grown contradiction step with Graphiti's `resolve_edge` prompt (duplicate-vs-contradiction in one call) and add bi-temporal `valid_at`/`invalid_at` to insights — **append-only** (new record stamps the prior's `invalid_at`; never mutate). Keep cosine>0.92, keep the quarantine→validate→promote lifecycle verbatim.

  **Decision (2026-06-11): adopt the bi-temporal columns, populated cheaply.** Rationale and the population rule, so we don't reinvent what snapshots already give us:
  - *Why they earn their keep:* transaction-time ("was this insight active in the library as of snapshot S") is **already** answered for free by `status_transitions` keyed by `snapshot_id` (`store.status_at()`). The new columns must serve only the *other, genuinely-missing axis* — **world/version validity** ("this build command was true until the repo moved to Vite"), which the snapshot chain cannot express. They are not a second copy of snapshot time.
  - *`invalid_at` — mechanical, no LLM, and applied at PROMOTION not ingest.* A contradiction detected at ingest records only a `contradicts` edge (the new insight is quarantined). The `invalid_at` stamp on the prior insight is applied at **promotion**, through the single-writer queue (minting a snapshot), after the batch passes validation — so an unvalidated/hallucinated idea cannot invalidate a live incumbent, and registration stays snapshot-free. The prior row is never mutated otherwise and never deleted. This is the append-only port of Graphiti's `resolve_edge_contradictions` (`edge_operations.py:538-573`) minus its in-place mutation, with quarantine-gating added on top.
  - *`valid_at` — NULL by default.* Most code rules are timeless-until-superseded, so `valid_at ≈ created_at` and adds nothing — leave it NULL. Populate it **only** when an insight is inherently version/time-scoped, and **only** from the reconcile judge that is already running (no dedicated `_extract_edge_timestamps` call — that is Graphiti tokens/noise we explicitly reject).
  - *Net cost:* one `ALTER TABLE insights ADD COLUMN valid_at/invalid_at` (nullable, backfill-safe, matches the existing migration discipline), a few lines in the supersede branch of `lifecycle`/registration, and one optional field on the reconcile judge schema. No new LLM call on the common path.
- **§6 Splitting → Partitioning.** Replace `token_count`/`insight_count`/silhouette-0.3/0.35 triggers and k-means with `graspologic_native.hierarchical_leiden` over the insight co-occurrence/similarity graph; every split/merge/retire is a **move scored against Eq 12** (codelength + expected access on `fitness_events`), not a threshold. Retirement → usage-conditioned survival (censored), not counter-based. `reflector/agent_split.py` is repurposed as the partition-move engine.
- **New §: The organization objective.** Promote Eq 12 from the essay to a system spec: `cost(G) = L(G) + L(traces|G)`, evaluated on real usage traces, code-relative; the single function the slow loop's moves are scored against; the one place we are original about *structure*.
- **§17 thresholds.** `cap ~50`, `silhouette 0.3/0.35` → demoted to *objective moves* (keep cosine 0.92 as the cheap dedup prefilter; it stays a threshold). Update the provenance lines accordingly.
- **Tables (no migration needed yet):** `families`/`agents` survive physically but lose semantic load — `agents` becomes the module-governance row (drop reliance on `base_prompt_specialty`/`router_prompt` at runtime; leave columns for back-compat). `library/router.py` is demoted to dead code behind the assign stage.
- **Plans 003–005:** the only *blocked* path is building them as written around the family/agent runtime. Re-scope: Plan for the stage pipeline + the partition-objective slow loop, not the router/specialist/silhouette machinery.

---

## 8. Shortest path to a dogfoodable v1 (session-transcript adapter first)

The adapter is small because the hard parts already exist (`store.py`, `lifecycle.py`, the placement judge, sessions transcript capture). v1 needs **no graph and no Leiden** — the existing skills-as-views partition is fine until there's enough volume to make partitioning pay; ship the gauntlet first, the objective second.

1. **`af ingest-session <transcript.jsonl>`** — read the JSONL `claude -p` transcript we already capture (`sessions.py`), run an **extract** LLM pass (raw turns → candidate insights in the structural schema). *(build; ~1 file)*
2. **Admission gate** — the denoise + generalization lint as a judge call with a fixed schema (`admit | rewrite | reject`), reusing `judge.run_judge`. *(★ the original piece; ~1 prompt + wiring)*
3. **add_idea** — feed survivors through the existing cosine prefilter → placement/contradiction judge → quarantine. Harden the contradiction branch with Graphiti's prompt. *(have + adapt)*
4. **Dogfood loop** — point it at this repo's own session transcripts; you become the reflector, reading what got admitted/merged/flagged. This **generates the `fitness_events` usage traces** the slow-loop objective needs before any partitioning code is written.
5. **Only then** — once traces exist and the active set is large enough that retrieval/placement degrades, add the Leiden partition move scored by Eq 12. The objective has data to be measured against, exactly as the two-loop story requires.

Sequencing rationale: the gauntlet + admission gate are the daily-value, original, low-risk piece and they manufacture the data the (harder, original) objective consumes. Adopt the boring primitive (Leiden) last, when the trace data can tell you whether your objective beats plain modularity.

---

## 9. One-paragraph verdict

Keep the SQLite atomic-insight core — it is already the substrate every OSS memory tool *lacks*. Adopt `graspologic_native.hierarchical_leiden` as the partition primitive, port Graphiti's `resolve_edge` contradiction prompt (append-only), and steal LightRAG's threshold-gated re-summary economics. Be original in exactly three places — the admission gate, the organization objective that scores partition moves on real access traces, and the governed quarantine→promote→rollback gauntlet — because those are the three things no surveyed system does. Ship the session-transcript adapter through the existing gauntlet first; it is the shortest path to dogfooding and it produces the traces the objective needs. The answer the principle asked for is: **"our governed SQLite core + Leiden + Graphiti's prompt + our admission gate + our objective" — and most of that is adopt, not build.**
