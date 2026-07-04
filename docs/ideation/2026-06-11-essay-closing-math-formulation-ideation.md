---
date: 2026-06-11
topic: essay-closing-math-formulation
focus: replace blog_post.md draft Equations 11/12 (per-job context argmin + storage/access organization argmin — author: "doesn't feel right") with the canonical formulation, grounded in what the literature has already formalized
mode: repo-grounded
---

# Ideation: The Closing Math for "Context Engineering: What Comes After Gradient Descent"

## Grounding Context

Subject: the next section of `blog_post.md` must introduce the optimization function(s) the rest of the paper implements. Essay so far: training = capability accumulation (∑σᵢxᵢᵀ, Eqs 1–4); context = coordination (ΔW_C biases trigger scores, Eqs 5–10); L1–L6 ladder = policies for constructing ΔW_C; closing section ends on "what is the next small job, and what is the very best context for it?" Draft Eq 11: C*(q)=argmin_{C⊆K,|C|≤B} E[job loss|C]; Eq 12: G*=argmin_G [MDL storage + E_q access cost], agents inside G.

Research findings (web): **fast loop crowded** — Context-Engineering survey (arXiv:2507.13334) canonical single-loop objective F* = argmax_F E[Reward(LM(C_F(τ)))] s.t. |C|≤L_max; REPLUG (2301.12652) KL distillation; IB-RAG (2406.01549) IB Lagrangian; AdaGReS (2512.25052)/OpenReview submodular knapsack; EPR; CEIL — all proxies of true task loss. **Slow loop open** — no agent-memory paper (MemGPT/HippoRAG/A-MEM) states a formal organization objective; classical ancestors: Knuth optimal BST (min Σ wᵢ·depth), Huffman, splay-tree static optimality (Sleator–Tarjan; unknown distribution), map equation (Rosvall–Bergstrom; two-level walk codelength; never applied to skill/agent graphs), Garicano QJE 2000 knowledge hierarchies (uncited in LLM lit), Marschak–Radner, CLS (McClelland/O'Reilly), divide-and-conquer noise condition (2506.16411). **The two-timescale pairing (slow loop minimizing the cost of solving the fast loop) is unclaimed.**

Repo constraints (learnings): theory.tex — objectives are never-observed targets approached by gated improvement, not computed argmins; MDL is code-relative ("criterion, not certificate"); prompting-as-training.tex — semi-parametric class f_{L,R}(x)=LM(·|x,R(x,L)) with ceiling J*_ctx ≤ J*_wt (Eq 11 should read as its inner step); prior ideation chose usage-conditioned L(traces|G) over L(ideas|G); DESIGN.md — ownership ≠ reachability (partition prices routing; must not constrain the feasible set).

## Topic Axes

A1 Fast-loop equation form · A2 Slow-loop equation form · A3 Coupling (one vs two functions) · A4 Agents in the math · A5 Essay presentation/positioning

## Ranked Ideas

### 1. The composed closing section (nested equation + glosses)
**Description:** One displayed equation: G* = argmin_G { E_jobs[ min_{C ⊆ reach_G(q), |C| ≤ B} D(q,C) ] + λ·L(G) }, typeset beside SGD's W* = argmin_W E[loss] for the bracket-rhyme that answers the essay's title; inner min glossed as a Belady-style oracle (ideas #2); slow cost defined as one usage-conditioned codelength (idea #3); agents derived via Garicano (idea #4); splay honesty clause (idea #5); budget-as-price aside (idea #6).
**Axis:** A3 (synthesis)
**Basis:** direct: composed from survivors 2–7 below; cross-frame convergence (5 of 6 frames proposed the nesting independently).
**Rationale:** Fixes every diagnosed pain at once: disconnection (nesting), pretend-solvability (Belady), mixed units (one code), bolted-on agents (Garicano), hidden stationarity assumption (splay), and the missing ΔW_C link (λ/interference aside).
**Downsides:** Densest option; needs careful Medium pacing.
**Confidence:** 90% · **Complexity:** Medium · **Status:** Explored — developed via ce-brainstorm 2026-06-11

### 2. One nested equation, positioned against the field's single-loop objective
**Description:** The bilevel form above as the only displayed optimization; the survey's canonical objective is recognizably the inner term — contribution sentence: "the field optimized the inner bracket; here is the outer one."
**Axis:** A3 · **Basis:** external: survey 2507.13334; Garicano 2000 (same nesting). **Confidence:** 90% · **Complexity:** Low · **Status:** Explored (as part of #1)

### 3. Belady framing: the inner min is an oracle, not a computation
**Description:** Belady's OPT cache rule (provably optimal, needs the future) as the presentation device: the fast argmin is the clairvoyant ideal; every L-level is a replacement policy judged by its gap to OPT; "criterion, not certificate."
**Axis:** A5 · **Basis:** external: Belady 1966; Sleator–Tarjan competitive analysis; direct: repo posture. **Confidence:** 90% · **Complexity:** Low · **Status:** Explored (as part of #1)

### 4. One code, not two costs (map equation / usage-MDL slow loop)
**Description:** Replace S(G)+A(G) with L(G) + L(traces|G): storage and access as two levels of one code (map equation = the graph-walk special case); computable on logged traces via Infomap; junk = bits that compress nothing.
**Axis:** A2 · **Basis:** external: Rosvall–Bergstrom 2008; Rissanen; direct: repo's usage-conditioned preference. **Confidence:** 85% · **Complexity:** Medium · **Status:** Explored (as part of #1)

### 5. Garicano agents: the partition as a theorem
**Description:** Partition objective = within-module search cost + between-module routing cost; both extreme partitions blow up; hierarchy emerges (Garicano 2000, uncited in LLM literature); modern split condition = 2506.16411's noise-scaling inequality.
**Axis:** A4 · **Basis:** external: Garicano QJE 2000; 2506.16411. **Confidence:** 85% · **Complexity:** Medium · **Status:** Explored (as part of #1)

### 6. Splay honesty: self-organization with a competitive guarantee
**Description:** The job distribution is unknown and drifting, so the slow loop is an online self-organizing rule with a static-optimality-style guarantee, not a solved argmin — which is *why* it is a loop; ownable conjecture: static optimality for skill graphs under add_idea().
**Axis:** A2/A5 · **Basis:** external: Sleator–Tarjan 1985; direct: theory.tex improvement-not-argmax posture. **Confidence:** 80% · **Complexity:** Medium · **Status:** Explored (as part of #1)

### 7. Budget as price: λ survives infinite context
**Description:** B→∞ flip: junk still steers (Eq 10), so interference, not space, is the binding constraint; cost enters as +λ|C|; payoff line: long-context models do not kill context engineering.
**Axis:** A1/A5 · **Basis:** direct: essay Eq 10; external: IB-RAG Lagrangian precedent. **Confidence:** 85% · **Complexity:** Low · **Status:** Explored (as part of #1)

### 8. Bellman flourish: the closing questions as one recursion
**Description:** V(s) = min_job min_C [ℓ(job,C) + V(s′)] — the essay's final two questions read off as the two mins; makes decompose-in-time first-class. Optional.
**Axis:** A3/A5 · **Basis:** direct: blog_post.md final line. **Confidence:** 70% · **Complexity:** Low · **Status:** Unexplored

## Rejection Summary

| # | Idea | Reason Rejected |
|---|------|-----------------|
| 1 | Policy-over-subsets as the headline form | Absorbed into #2's survey positioning; standalone π* is the survey's own form |
| 2 | Bayes prior/posterior formulation | Introduces new machinery in the essay's final section |
| 3 | Denning working set W(t,τ) | Decorative here; locality idea survives in prose |
| 4 | Register-allocation liveness ("evict after last use") | Niche; kept as a footnote concept |
| 5 | CLS/two-timescale coupling as an equation | Kept as a citation; nesting (#2) already supplies the coupling |
| 6 | Improvement functional / sup-sup coordinate ascent | Repo-internal posture; too dry for a Medium close |
| 7 | Operator approximation ‖ΔW_C − ΔW*‖ | ΔW* is an oracle — re-smuggles the "needed set" the author rejected |
| 8 | SNR objective with capability subspace T | Same oracle smuggling (its own meeting-test admits it) |
| 9 | Pairwise interaction term φ(c,c′) | Footnote: it is what knowledge-graph edges estimate |
