# Ideation: Greenfield Leverage for the Agent-Families Factory

**Date:** 2026-06-10
**Mode:** repo-grounded + external research
**Focus:** `docs/agent-families/DESIGN.md` is optimized for brownfield reverse-coding (target app = behavioral oracle). How do we get comparable leverage on greenfield projects — where there is no target, the user's idea is vague, and the user is a fallible oracle? Two user-stated priorities: (1) the planner should push the user to actually think everything through; (2) a principled way to grow the skill library without a codebase to train against — skill priors plus fast in-engagement skill acquisition are assumed parts of the answer, but the training story matters most.
**Grounding:** full read of DESIGN.md; survey of `docs/agent-factories/` (the Define-chain design); one external research pass (elicitation agents, oracle-free grading, synthetic-task skill bootstrapping). Citations inline.

> Ideation artifact: ranked directions, not a plan. Survivors feed a DESIGN.md revision (likely a new section: "Greenfield mode") or a dedicated brainstorm.

---

## The headline reframe: the pipeline is already greenfield — the *oracle* is what's brownfield

The information wall (§9) means plan/work/verify never see the target. From the pipeline's seat, every episode already *is* a greenfield engagement: a prompt, a question channel, an acceptance loop. What the target actually provides is three training-side things:

1. **A grading oracle** — the feature registry + differential scenarios.
2. **A perfect requirement source** — the explorer answers every question correctly.
3. **An elicitation signal** — the gap between the underselling prompt and the full registry.

So "greenfield support" decomposes cleanly: keep the pipeline; replace the *epistemics of the oracle*. The deep gap is #2: real greenfield users don't undersell a known app — they **don't know the answers themselves**. The design explicitly punts on this ("real-world fallible users are handled later via hand-written insights," §9). That punt is exactly where greenfield leverage lives, and it turns out to be trainable.

A second convergence: the sibling design `docs/agent-factories/` already specifies the *deployment-time* greenfield Define chain (goal research → architecture → feature list → specs, T0–T3 intake tiers, DEFENSE.md). The two designs meet naturally: **agent-factories' Define chain describes what a good greenfield planner *does*; agent-families' training loop is how it *gets good at it*.** Several survivors below are "import a Define-chain mechanism as planner-family skill content, then make it trainable."

---

## Theme A — The training story (the principled way to grow greenfield skills)

### S1 — Backtranslated greenfield episodes: the founder simulator (lead idea)

**Basis:** `direct:` explorer/registry machinery (§9–10), traceability schema (§12); `external:` SWE-RL self-play (mutation defines ground truth, arXiv 2512.18552), AgentSynth information asymmetry (generation is easier than solving, arXiv 2506.14205).

Today the explorer is a *user who has seen the app*. Add a second training-side persona built from the same per-target apparatus: a **founder who wants the app to exist but has never seen it**. Mechanically:

- Grader-side, derive a **degraded mental model** from the full feature registry: drop features, blur details, inject a fixation on the wrong thing, add one or two latent contradictions. The degradation is *generated and logged* — the grader knows exactly what the founder knows.
- Run the episode in greenfield mode: no UAT-against-target, founder answers questions truthfully **from the degraded model only**. Where the model is silent, the founder says "I hadn't thought about that — what do you suggest?" (scripted honesty about ignorance, not noise).
- The **full registry remains the hidden grading oracle**: the finished build is graded differentially against the real target exactly as today.

This is backtranslation: target app → spec-shaped fog → rebuild → grade against the target. It converts greenfield into a task with ground truth, and the new gradable quantities are precisely the greenfield skills:

- **Recovery:** what fraction of registry features did elicitation + proposal surface despite the founder not volunteering them?
- **Proposal quality:** which features the planner *proposed* and the founder accepted (the registry says which proposals were "right").
- **Decision surfacing:** which load-bearing decisions were made explicitly vs. landed by accident (see S2).

Reflector attribution survives: Stage A's step 1 (`COMMUNICATED?`) gains branches — *founder-didn't-know + planner-didn't-ask* vs. *founder-didn't-know + planner-didn't-propose* vs. *planner-asked-wrong-question*. Each branch is a different skill family getting pressure. Insights mined here are elicitation/design insights — the exact `domain:`/`universal` content a real greenfield engagement retrieves.

**Why this doesn't violate the perfect-oracle policy (§9):** that policy exists because *unknown* unreliability adds unrecoverable grading variance. The founder's ignorance is **scripted and known** — degrade *knowledge*, never *truthfulness*. The signal stays fully recoverable; replays reproduce (seed the degradation by episode ID, same trick as persona rotation).

**Curriculum knob:** degradation severity anneals exactly like the question budget — epoch-1 founders are nearly complete; late-epoch founders are vague, contradictory, fixated. Two annealing levers (question budget, founder fog) define the elicitation curriculum.

### S2 — The decision registry: grade decisions, not just features

**Basis:** `direct:` feature-registry pre-research (§10), traceability schema (§12), probe-question taxonomy (§17); `external:` CLARITI's Shapley finding that risk/failure information outranks feature wishes (arXiv 2604.14624); RAT/premortem PM practice.

Greenfield's unit of failure is usually not a missed feature — it's an **unmade decision** (auth model, tenancy, data lifecycle, empty states, error semantics, permissions, deletion semantics). Add a grader-side **decision registry** (`DEC-*`) per target during pre-research: every target app embodies hundreds of resolved design decisions, extractable the same way FEATs are. Then:

- New plan lint: every DEC in the episode's scope must be **explicitly resolved** — asked about, proposed-with-default, or logged as a tagged assumption (S5). Implicit resolution is a typed failure.
- New metric alongside features-recovered-per-question: **decisions-surfaced-per-question**.
- Reflector attribution: "scenario failed because DEC-14 was never surfaced" is a deterministic join, same as FEAT misses.

Cross-target, the decision registries are the *principled* seed for the probe-question taxonomy (§17 already plans to seed it from clustered FEATs — DECs are the better substrate): "every multi-user target had role-gating decisions" becomes a planner skill by recurrence promotion (§11), not by hand-writing. **This is the answer to "skill priors, principled": the brownfield corpus IS the greenfield prior — mine the decisions, not just the failures.**

### S3 — Pure-greenfield episodes via spec-as-hidden-rubric (use sparingly)

**Basis:** `external:` GER-Eval rubric-from-description (arXiv 2602.08672 — but human correlation drops to ~0.3 in knowledge-heavy domains), exam-generation / information-asymmetry patterns; `direct:` deployment-mode verifier (§10) already defines the target-free instrument set.

For true novelty (no backtranslation source): a grader-side **brief author** writes a product brief *plus a hidden decision/feature rubric* before the episode; the pipeline builds from the brief; grading runs against the hidden rubric + the metamorphic tier + baseline checklist + absolute budgets — i.e., the deployment-mode verifier plus a hidden spec. Weaker oracle (the rubric is LLM-authored, so judge-noise is higher) — control-chart it, and keep S1 as the volume training mode. Value: detects skills that only work because backtranslated founders are secretly registry-shaped.

### S4 — Greenfield rebuild probes for the one-shot curve

**Basis:** `direct:` §11 rebuild probes, one-shot metric.

Track the capability curve separately per mode: a **greenfield one-shot rate** (S1-mode episodes) alongside the brownfield one. The headline product claim for greenfield is "from vague idea to working v1 with N questions and one shot" — make N and the shot count first-class curve metrics so the curriculum (S1's two annealing levers) has something to optimize against.

---

## Theme B — Making the planner push the user to think (deployment behavior, trained by Theme A)

### S5 — The assumption ledger: a typed artifact, linted like everything else

**Basis:** `direct:` "thin LLM judgments inside thick mechanical contracts" house style, plan lints (§7), traceability schema (§12); `external:` Riskiest Assumption Test; Shape Up "rabbit holes / no-gos"; underspecification cost ≈ 22.6% accuracy drop (arXiv 2505.13360).

Every greenfield plan carries `ASSUME-*` records: `{claim, basis, risk_if_wrong, cheapest_test, status: open|confirmed|invalidated}`. Mechanical lints:

- Every REQ traces to a MSG **or** an ASSUME — silent invention of requirements becomes a lint failure, not a vibe.
- Top-k assumptions by risk×uncertainty must be confirmed through the question channel before increment 1 (RAT, operationalized).
- A premortem pass ("it's three months later and this failed — why?") is a cheap generator feeding the ledger.

This converts "did the planner think it through?" into joins. In S1 training, the grader scores ledger **precision** (fraction of assumptions that were actually load-bearing per the registry) and **recall** (registry deltas that should have been assumptions but weren't) — so ledger quality is trainable, not aspirational.

### S6 — Propose, don't interrogate (and train the difference)

**Basis:** `external:` proposal-anchored elicitation outperforms open questions (CLARITI question-efficiency; UA-CodingAgent ask-or-assume calibration, arXiv 2603.26233); `direct:` question budget (§9) already prices questions.

Planner contract for greenfield: under the question budget, **never spend a question on an open "what do you want?" when a 2–3-option proposal with a recommended default fits**. Scripted founder behavior in S1 enforces the gradient: degraded founders give low-information answers to open questions and decisive answers to concrete proposals (documented human behavior). The budget then *teaches* proposal-first elicitation instead of us hand-writing it as a rule. New metric: **proposal acceptance rate** and information-gained-per-question (EVPI-flavored — rank candidate questions by expected plan-variance reduction; SAGE-Agent shows 1.5–2.7× fewer questions at higher coverage).

### S7 — Synthetic stakeholder panel before spending the user's budget

**Basis:** `external:` synthetic-persona panels (arXiv 2509.02605 — with documented blind spots); iReDev's Interviewer/EndUser/Deployer trio (arXiv 2507.13081); `direct:` the wall (§9) — panels are pipeline-side, they never need target access.

Before burning real questions: spin a panel of cheap personas (end user, admin, attacker, support rep, the user's accountant) against the draft spec; each emits "the question I'd need answered." Dedup, EVPI-rank, then spend the real budget only on what survives. Panels are known to miss relational/experiential signal — so panel output *feeds* the question ranker, never replaces user contact.

### S8 — Walking-skeleton lint: increment 1 must traverse the riskiest decisions

**Basis:** `direct:` agent-factories' Walking Skeleton (goal-to-feature-list group), DEC registry (S2), plan lints (§7).

Greenfield-mode plan lint: the first increment must be a vertical slice exercising the top-risk open decisions (auth, data model, deployment seam) — not the easiest CRUD. Rationale: the user's feedback on a walking skeleton is the cheapest real-world oracle available, and it lands while redirection is cheap. This is the deployment analogue of S1's acceptance loop.

### S9 — Adopt the Define chain as planner-family seed skills

**Basis:** `direct:` docs/agent-factories/groups/* (goal research, goal→architecture, goal→feature-list, features→specs; MoSCoW, EARS-form ACs, Definition-of-Ready, boring-tech-first ADRs).

The agent-factories Define chain is, in agent-families terms, a **hand-authored initial skill set for the planner family's greenfield specialty** — exactly the kind of content the library would otherwise take epochs to discover. Import it through the front door: rewrite each mechanism as structural insights (`precondition / action-pattern / expected-outcome`, scope-tagged `universal` or `domain:greenfield-define`), registered via `add_idea`, quarantined, validated on the S1 micro-benchmark like any other batch. Two designs, one library; nothing bypasses governance.

---

## Theme C — Skill priors and fast acquisition without a codebase (the follow-up question)

### S10 — Researched-insight induction: a new provenance, the same gauntlet

**Basis:** `direct:` add_idea lifecycle (§5), quarantine/validation, ratchet cap; `external:` Library Drift's warning (ungoverned LLM-authored libraries ≈ +0.0pp) — which applies *double* to research-sourced text.

When an engagement lands in a domain with no `domain:` insights (greenfield healthcare app, say), run a **domain induction pass**: a research agent (web + docs) emits candidate insights *in the structural schema*, tagged `provenance: researched`, entering the normal lifecycle — quarantined, validated, capped, fitness-tracked. Validation substrate when there's no failed-slice to replay: (a) plan-lint deltas on the live project (did the plan get measurably tighter?), (b) the S1 founder micro-benchmark for elicitation-class insights. Key discipline: research is a **source**, never a bypass — a researched insight that never earns a retrieval-with-win retires like any other. This is the "quick way to research and add skills during planning," made governable.

### S11 — Cross-target decision mining is the prior factory (restating S2's punchline as the answer)

The most *principled* skill prior isn't research — it's **distillation from the brownfield loop you already run**. Recurrence promotion (§11) already elevates cross-target lessons; add the decision-registry miner (S2) and the brownfield corpus becomes a generator of greenfield judgment: probe taxonomies, default-stack decisions, "apps of archetype X always need Y." Research-sourced priors (S10) fill cold-start gaps; mined priors replace them as targets accumulate (fitness decides, per the ratchet). Telemetry worth adding: per-insight `provenance` vs. fitness curves — if researched insights systematically lose to mined ones, the induction pass should shrink to cold-start-only.

### S12 — Run-scoped memory already covers in-engagement acquisition — just route greenfield episodes through it

**Basis:** `direct:` §13 two-tier memory, reflector success channel (§12).

No new machinery needed for "learn fast during the project": run-scoped working memory legally accumulates project-specific conventions; the reflector's success channel nominates survivors for generalization. The only gap is that greenfield engagements never appear in training, so their distinctive lessons (elicitation moves, proposal patterns, assumption-ledger hygiene) never enter the rotation. S1 closes that: greenfield episodes in the curriculum ⇒ the existing slow loop grows greenfield skills with zero new mechanism.

---

## What I'd cut or defer

- **Fallible-answer simulation (founder sometimes wrong, not just ignorant):** defer. Known-ignorance (S1) preserves signal recoverability; scripted *wrongness* reintroduces the grading-variance problem §9 was designed to avoid. Revisit only if real-deployment feedback shows contradiction-handling is a top failure class.
- **Style/persona variation for founders:** same policy as §9 — fixed style until elicitation scores plateau.
- **Training the planner on third-party PRD corpora:** the provenance/validation story is weak (no oracle, no replay); S1 + S10 dominate it.

## Recommended adoption order

1. **S2 (decision registry)** — pure grader-side addition; pays off for brownfield attribution immediately and is the substrate for everything else.
2. **S5 (assumption ledger)** — a plan-lint extension; cheap, deployment-visible immediately.
3. **S1 (founder simulator)** — the structural piece; needs the registry + degradation generator; unlocks the greenfield training mode.
4. **S9 + S10 (Define-chain seeds + researched induction)** — library content, gated by Phase 0 machinery existing.
5. **S6/S7/S8** — planner-family behavior that mostly *emerges* from S1's gradient plus a handful of seeded insights; ship the lints (S8) early, let training sharpen the rest.
6. **S3/S4** — instruments for the late curriculum; defer until S1 episodes are routine.

## Open questions this forces

- Does the founder's degraded model live in the traceability store as first-class rows (`KNOW-*`?) so Stage A's new branches stay deterministic joins rather than judgments?
- What fraction of the episode rotation should be greenfield-mode? (Guess: start ~1 in 4 after Phase 3 machinery stabilizes; let the two one-shot curves arbitrate.)
- Should DEC extraction reuse the FEAT pre-research agent or be a separate pass? (Same instruments, different prompt — likely one pass, two output tables.)
- Deployment parity for the assumption ledger: does the real user *see* the ledger (probably yes — it's the single best "make the user think" artifact), and does confirming an assumption consume question budget?
