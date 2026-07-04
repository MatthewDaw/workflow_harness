---
date: 2026-06-11
topic: context-engineering-ideation-and-decisions
scope: essay (Context Engineering), agent-families theory, system design pivots, slow-loop tool vision
---

# Ideation & Decisions — 2026-06-10/11 Session

One-paragraph arc: started as a math-rigor ideation for the agent-families training loop, became a theory program (two LaTeX docs), then an essay ("Context Engineering: What Comes After Gradient Descent") that found its thesis after one false start, and ended with two conceptual pivots to the system design (agents → stages; specialists → the library) and a concrete product vision for the slow loop (universal ingestion into a governed knowledge graph). This document is the durable record of what was decided and why.

---

## 1. The essay — settled positions

**Ground truth: `docs/agent-families/essay-full.html`** (full rendered essay, MathJax, Equations 1–14). `blog_post.md` was deleted 2026-06-11 — do not recreate. Snip figures from `docs/agent-families/essay-math.html`. Escher *Drawing Hands* is the hero image.

- **Thesis (after iterating away from "prompting = gradient descent"):** gradient descent specializes in **capability generation** (the only writer of σᵢxᵢᵀ shards); context engineering specializes in **capability coordination** (the prompt assembles ΔW_C, which can only bias trigger scores xᵢᵀΔW_C q — Equation 8). Evidence anchors: random-label ICL (Min 2022 — selection, not learning), elicitation ceiling (Petrov 2024).
- **Rejected framing:** "a prompt update IS a gradient update." The honest version: same outer-product form always; exact equality only under the labeled-(input,label) construction (von Oswald 2023, Dai 2022). The value vᵢ is target-blind; the error signal eᵢ is label-aware — same shape, different content.
- **The L1–L6 ladder** (chat → CLAUDE.md → skills → tools → knowledge graphs → multi-agent) = one idea: increasingly intelligent policies for constructing ΔW_C. Read as a history of better codes.
- **Two axes of all agentic progress:** decompose in time × retrieve in space. Quality of a system = how well it answers "what is the next small job, and what is the very best context for it?"
- **The closing math (Equations 11–14), arrived at via ce-ideate research round:**
  - Eq 11: the nested objective beside SGD's — `G* = argmin_G { E_q[ min_{C ⊆ K_G(q)} D(q,C) ] + λ·L(G) }`. Same nesting, one level up. The field's canonical objective (survey 2507.13334; REPLUG; IB-RAG; submodular RAG) is the *inner* bracket; the outer bracket is formally open.
  - **No hard window budget.** User decision: |C| ≤ B was deleted. The binding constraint is interference, not space (Equation 10: junk steers); D is U-shaped in loaded context; the minimum is interior, enforced by attention, not a wall. "Million-token windows move the wall, not the problem."
  - Inner min presented as a **Belady-OPT oracle** — never computed, the criterion every ladder level approximates ("a criterion, not a certificate").
  - Eq 12: cost(G) = L(G) + L(traces|G) — storage and access as **two levels of one code** (map equation), measured on real usage traces, code-relative (MDL hedge).
  - Eq 13: partition cost = search-within + route-between (Garicano); both extreme partitions blow up; hierarchy is the theorem. Split condition: divide when context noise outgrows coordination noise (2506.16411).
  - Eq 14: V(s) = min_job min_C [ℓ + V(s′)] — the closing questions as a Bellman recursion, the essay's last equation.
  - **Slow-loop honesty:** the job distribution is unknown and drifting → in practice the slow loop is a self-organizing rule with a splay-tree-style guarantee, which is *why* it is a loop.
- **Convention:** every section introduces its symbols in a table before its first equation (three tables: Eqs 1–4, 5–10, 11–14).
- Rejected for the closing (recorded in `docs/ideation/2026-06-11-essay-closing-math-formulation-ideation.md`): two sibling argmins (the draft that "didn't feel right"), Bayes prior/posterior form, Denning working set, register-allocation liveness, U(q,C) with a "needed capabilities" oracle (smuggles the oracle), standalone policy-π form.

## 2. Theory program (paper-grade, code later)

- `docs/agent-families/theory.tex` — the system as **budgeted zeroth-order stochastic optimization with learned proposals, evaluated by a biased noisy oracle**. Proved here: well-posedness; bisimulation-metric objective (Banach); measurement-limited learning rate (Bretagnolle–Huber); anytime-valid gates (Ville, e-products, e-BH); causal credit (randomized masking, Shapley sampling); conditional submodular-greedy guarantees; drift/occupation convergence to Δ-statistical stationarity; falsifier regret bound on grader exploitability. Impossibility boundary: Rice, NFL, Kolmogorov, LLM-as-oracle. Assumption Ledger maps every constant to telemetry.
- `docs/agent-families/prompting-as-training.tex` — the Tier-B foundation: **Equivalence Principle** (de Finetti: conditioning = parameter update on predictives), dual-form theorem (Irie 2022) as the exact mechanism-level case, simulation defect ε_sim as estimable error, semi-parametric class f_{L,R} with ceiling J*_ctx ≤ J*_wt, the translation dictionary (training ↔ library learning), saturation–jump prediction, regime argument (selection-controlled vs averaging-controlled updates — "not poor man's SGD").
- Earlier math-rigor ideation survivors (record: `docs/ideation/2026-06-10-agent-families-math-rigor-ideation.md`): event-level measurement ledger (do-first enabler), IRT capability backbone + adaptive curriculum, MDL-in/hazard-out library economics, quarantine trials as randomized ablations + e-process gates + offline Shapley, G-theory grader model + conformal triage (MDE = go/no-go), Ralph loops as absorbing Markov chains + Luby restarts, grader-as-loss program (STL robustness margins, falsification floor, bisimulation model-diff, CMDP shadow prices, σ-standardized soft-min with tail-certified judges, pairwise Bradley–Terry + DIF, anytime-valid settlement).
- **Publishable lanes confirmed open by research:** formal memory-organization objective (Eq 12 territory); the two-loop pairing; Garicano uncited in LLM literature; map equation never applied to skill/agent graphs; STL robustness for UI traces; falsification grading of LLM-built apps.

## 3. System design pivots (to land in a DESIGN.md revision before Plans 003–005)

- **Agents → stages.** An agent decomposes into: fresh window + stage tools/permissions + output contract + preset knowledge bias. Under a good fast loop, the *bias* is redundant — per-job retrieval computes it better. What survives: **stages** (plan → assign → work → verify), because they differ by act, permissions, and trust structure (verifier independence cannot be retrieved into existence).
- **Specialists migrate from the org chart into the knowledge graph.** Garicano's hierarchy exists because knowledge is expensive to load into humans; LLM sessions load knowledge per job — so the same economics now shape the *index*, not the staffing. Equation 13's modules are library-side structure (placement, routing, governance units), not runtime personas. This completes DESIGN.md's own "ownership ≠ reachability" concession.
- **Persistent specialist sessions are contingent optimizations, added back only when measured:** (1) KV-cache prefix economics, (2) parallel write ownership (file territories), (3) judgment/persona on boundary work, (4) cold-start before retrieval traffic exists.
- **The library's objective replaces the heuristics:** cap ~50, silhouettes 0.3/0.35, compressibility gate → ΔMDL moves on one objective (Eq 12); retirement → censored survival; routing health → entropy/PMI telemetry.

## 4. The slow-loop tool (the product the user most wants)

**Vision:** one entry point — ingest *anything* (Claude session transcripts, reverse-prompted repo rules, PR review threads) → denoise to "makes code better" → dedup strongly → contradiction self-healing → file into a self-organizing graph; agents/partition derived from the graph + usage traces.

**Market verdict (researched, caveat: fast-moving):** no existing tool does the full loop.
- Closest per axis: **Mem0** (ADD/UPDATE/DELETE/NOOP ops = dedup+contradiction semantics), **Zep/Graphiti** (temporal edge invalidation = contradiction self-healing; entity dedup), **Cognee** (ingest-anything pipeline shape), **Letta** (sleep-time compute = slow loop architecture), **Microsoft GraphRAG** (Leiden community detection over KG = nearest production cousin of the partitioning move), **LightRAG** (incremental graph build), **HippoRAG** (PPR retrieval), **Devin Knowledge / CodeRabbit Learnings** (PR threads → proposed knowledge; closed SaaS).
- **Unclaimed differentiators:** the domain-scoped denoising gate; a principled organization objective (compression + expected access); one add_idea() gauntlet for all sources; agents as derived state.
- **Build path:** the existing agent-families Phase 0 core IS the engine; missing pieces are **ingestion adapters** — session hook (transcripts → candidate ideas), repo miner, PR webhook — each just emitting add_idea() calls. First adapter: the session hook (highest daily value, generates the usage traces the slow-loop math needs).
- Study clones (shallow, outside repo): `C:\Users\mattd\Documents\gauntlet\oss-memory-study\` — mem0, graphiti, cognee, letta, graphrag, LightRAG, HippoRAG. Reading order: mem0 memory ops → graphiti invalidation → graphrag communities.

## 5. Fast/slow loop vocabulary (canonical)

- **Fast loop** — per job, spends structure: choose the context that minimizes job loss (Eq 11 inner). All retrieval mechanisms live here. The inference of context engineering.
- **Slow loop** — across jobs, builds structure: organize the knowledge so the fast loop stays cheap and good (Eq 11 outer / Eq 12). add_idea, dedup, contradiction repair, splits live here. The training of context engineering. Its only value is improving the fast loop.

## 6. Artifacts index (this session)

| artifact | role |
|---|---|
| `docs/agent-families/essay-full.html` | **the essay — ground truth** (Eqs 1–14, symbol tables, Escher) |
| `docs/agent-families/essay-math.html` | snippable figure blocks for Medium (parts 1–3 + legends) |
| `docs/agent-families/slides.html` | presentation deck (research-extension slides incl. proof slides, ↓/⇧-click skip nav) |
| `docs/agent-families/slide22-derivation.html` / `.tex`, `slide22-bridge.html` | line-by-line derivations backing the proof slides |
| `docs/agent-families/theory.tex` | formal theory: optimization + validity proofs + assumption ledger |
| `docs/agent-families/prompting-as-training.tex` | the semi-parametric / equivalence-principle foundation |
| `docs/ideation/2026-06-10-agent-families-math-rigor-ideation.md` | math-rigor survivors + grader-as-loss program |
| `docs/ideation/2026-06-11-essay-closing-math-formulation-ideation.md` | closing-equation research, survivors, rejections |
| `docs/brainstorms/2026-06-11-essay-closing-math-section-requirements.md` | the spec the closing section was written from |
| `C:\Users\mattd\Documents\gauntlet\oss-memory-study\` | 7 OSS reference repos (outside this repo) |

## 7. Open questions / revisit list

- DESIGN.md R3: rewrite §3–6 around stages + library-side partition + the Eq 11/12 objective (the only blocked path is building Plans 003–005 as currently written).
- Does the "General Arch" finite-window paragraph need softening now that the equation drops B? (Current read: it works as foreshadowing — only constraint #2 survives into the math.)
- Verification pass before publishing named claims: "Garicano uncited in LLM lit," "no formal organization objective anywhere," current Mem0/Zep/Cognee/Devin capabilities.
- Essay length check: closing section ~1,000 words vs spec's 1,100–1,300; splay paragraph is the most compressed if expansion wanted.
- OSS comparison fan-out (mem0/graphiti/graphrag vs DESIGN §4–5) — agreed useful, not yet run.
- Empirical hypotheses worth testing once telemetry exists: γ (weak submodularity), p_Δ decay (stationarity diagnosis), saturation–jump across base-model upgrades, Infomap modules vs hand-drawn agent boundaries.

## 8. Agreed next steps (in order)

1. **Commit this session's artifacts** (several logical commits; blog_post.md deletion included).
2. **Ship the essay** — read-through, snip figures, paste to Medium, publish.
3. **DESIGN.md reconciliation (R3)** with the OSS comparison as input — before any Plan 003–005 work.
4. **Build the session-hook ingestion adapter** (transcripts → add_idea) and dogfood the slow loop.
