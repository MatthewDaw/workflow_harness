# Ideation: Weekly "Chess Layer" + Alignment/Conformity Metric

**Date:** 2026-06-10
**Mode:** repo-grounded (ce-ideate)
**Focus:** Two underspecified elements of the weekly-commit module (origin PRD `docs/inspiration/st6_prd.md`, plan `docs/plans/2026-06-10-006-feat-weekly-commit-lifecycle-plan.md`): (1) the "chess layer" categorization+prioritization scheme, (2) the conformity/alignment metric now that Supporting-Outcome linkage is hard-enforced.
**Grounding:** repo context (already loaded) + 1 web-research pass on prioritization/categorization taxonomies and OKR/sprint health metrics + 6 divergent ideation frames.

> This is an ideation artifact: ranked directions worth exploring, not a plan or a spec. The selected directions feed `ce-brainstorm` (to define one precisely) or `ce-iterate-plan` (to fold into plan-006).

---

## The headline: the frames converged

Independent frames (pain, inversion, assumption-breaking, leverage, cross-domain, constraint-flip) were run separately. They collapsed onto a small set of mechanisms — convergence count in brackets is how many of the 6 frames independently proposed it:

| Converged mechanism | Frames | Verdict |
| --- | --- | --- |
| **Strategic Concentration Index** (Herfindahl/entropy over RCDO nodes) replaces conformity | **5/6** | **Lead survivor** |
| **Delete the "ladders-up" score** (structurally 1.0 under hard enforcement) | 4/6 | **Adopt** (pairs with above) |
| **Derive category/priority from plan-unit + RCDO position** (don't hand-tag) | 4/6 | **Adopt** |
| **Carry-forward aging** as the real reconciliation-health signal | 4/6 | **Adopt** |
| **WSJF-from-the-tree** for priority/leverage | 3/6 | **Adopt** |
| **Manager triage = exception/divergence, not a grid** | 4/6 | Strong secondary |
| **Strategic starvation** (which SOs got *zero*) | 2/6 | Strong companion |

External research corroborates the lead: **no real tool implements a continuous strategic-concentration metric over objective nodes** — it's unoccupied design space, and the enforced RCDO link makes it computable for free. The "chess piece" metaphor has **no real prior-art framework** — it's only blog rhetoric, so any chess semantics here are a free (and novel) invention.

---

## Survivors

### S1 — Replace conformity with a Strategic Concentration Index (lead)
**Subject:** alignment metric · **Basis:** `external:` Herfindahl-Hirschman Index (antitrust/portfolio), Viva Goals ">5 objectives" focus rule; `direct:` enforced SO link + reconciled-weeks-as-single-source + org GSI.

Once linkage is *enforced*, "does work ladder up?" is always 100% — a dead thermometer. The live question becomes: **given everyone ladders up, do they ladder to the *same few things* or spray across the tree?** Compute a Herfindahl/entropy concentration index over the RCDO nodes a person's reconciled commits touch in a window (priority-weighted). High = focused thrust; low = scattered. Because reconciled weeks are the single roll-up source and the org GSI exists, the *same statistic folds up* to a team and org number in one pass — an IC focus number, a manager number, and an org number from one formula.

- **Drives (IC):** "you're spread across 9 outcomes — pick a thrust."
- **Drives (manager):** sort reports by concentration to find who's overextended before it shows up as missed commitments.
- **Open question it forces:** is low concentration *bad*, or healthy breadth for some roles? → the metric should likely be **divergence from a *declared* posture** (focus quarter vs. explore quarter), not raw concentration (see S1b).

**S1b variant (from assumption-breaking):** measure **declared-vs-actual concentration delta** — the RCDO owner sets an intended concentration for the period; the metric scores divergence from intent, signed. Stops the index from punishing legitimate exploration weeks. *Recommended refinement of S1.*

### S2 — Delete the "ladders-up" conformity score outright
**Subject:** alignment metric · **Basis:** `direct:` SO-linkage hard-enforced → laddering is structural.

A score that can't vary trains everyone to ignore the dashboard (the exact fate of the old prose `conformityScore`). The only legitimate residue is the **count of items that *failed* to lock** (the enforcement-gate exception log) — which is an exception list, not a metric. Adopt S2 as the negative half of S1: remove the redundant number, free the "alignment" slot for concentration (S1) + starvation (S6).

### S3 — Derive the chess layer; don't make humans hand-tag it
**Subject:** categorization + prioritization · **Basis:** `direct:` plan U-IDs already encode work-type (phase/section) + a requirements-traceability table; agent proposes from git+plan; `reasoned:` single-source-of-truth.

The plan unit already carries the intent. Compute `category` and `priority` as a **read-through projection of the plan unit + the linked SO's RCDO position** at proposal time, rather than a dropdown a human (or agent) fills and then lets rot. "Change a commit's category" = "change the plan." The agent fills it with zero interview; the human *overrides only*, and an override that contradicts the derived value is visibly flagged. Agent-native parity is trivially satisfied because the derivation is identical whoever triggers it.

- **Tradeoff:** loses per-commit manual nuance; mitigated by the logged-override path.
- **Pairs with S4** (the derivation function for priority).

### S4 — Priority = WSJF inherited from the tree (Cost of Delay from RCDO position)
**Subject:** prioritization/leverage · **Basis:** `external:` WSJF = CoD/JobSize; `direct:` commits hard-linked into a weighted RCDO tree.

Don't ask humans to rank. Compute leverage as WSJF where **Cost of Delay is inherited from the commit's position in the RCDO tree** (weight/proximity of its Rally Cry → DO → Outcome → SO chain, and how far behind that branch is) and **JobSize is the plan unit's estimate**. Strategy weights live in *one* place (the tree), so re-weighting the tree re-prioritizes every open commit instantly. This also kills KTD4's redundant `priority` enum + free `order` integer double-bookkeeping — the list self-sorts by derived leverage, with manual drag as a flagged override.

### S5 — Carry-forward aging is the honest reconciliation-health signal
**Subject:** reconciliation health · **Basis:** `external:` spillover rate + sprint-count aging (≥3 = decompose/kill), actuarial claims-development run-off; `direct:` carry chains (`carriedFromWeek`/`carriedToWeek`) already in plan KTD3.

Say/Do (completed/committed) is gameable (commit less, score higher) and averages away corpses. The un-gameable signal is already in the data: **carry-depth** — walk the carry chain and surface how many lock-cycles each open commit has survived. Apply the ≥3-cycle "decompose or kill" nudge. Two strong refinements from the frames:
- **Sacrifice vs. Slip vs. Zugzwang (chess, made real):** distinguish a *deliberately* deprioritized carry (sacrifice — don't ding Say/Do) from an unplanned spillover (slip), and name the state where every open unit is net-negative leverage (zugzwang = "the plan line is spent, re-plan"). Gives carry-forward provenance real vocabulary.
- **Claims-development triangle:** track how much of week-N's locked scope is still unreconciled in N+1, N+2 — a worsening triangle means *systematic* over-commitment, not a bad week.

### S6 — Strategic starvation / coverage (the negative space)
**Subject:** alignment metric · **Basis:** `reasoned:` enforcement makes "ladders up?" tautological, so measure the absence; `external:` Tability NCS inverted to score gaps.

Companion to S1: measure **which Supporting Outcomes received *zero* commits this week**, weighted by how far behind they are. Can't be gamed by linking (it scores what *didn't* happen). "Three of four committed SOs got fed; the customer-trust SO has been starved 3 weeks" is a sentence a completion metric structurally cannot produce.

### S7 — Manager triage = exception brief / divergence, not a board
**Subject:** manager team-triage · **Basis:** `direct:` agent already proposes commits + org GSI; `external:` emergency-triage "expectant" category, KL-divergence; `reasoned:` parity.

Since category/priority/alignment are now *derived* (S1/S3/S4), there's nothing for a human to scan — only exceptions. The agent emits a weekly **exception brief**: highest-leverage commit not started, lowest-concentration owner, oldest carry, the SO starved longest, anything that failed to lock. Default state renders as "nothing needs you." Strong variant: rank reports by **KL-divergence between where their effort concentrates and where the org's active Rally Cries concentrate** — surfaces "aligned but working the wrong corner of the board," reusing the S1 distribution. Borrow triage's **"expectant"** category for explicit permission to *stop* funding a dying objective — a thing RICE/WSJF never license.

### S8 — Reconciliation calibration writes back into agent proposals (compounding)
**Subject:** reconciliation health → leverage · **Basis:** `direct:` per-project memories/learnings + agent-proposes model; `external:` Say/Do, Linear completion weighting.

On reconcile-complete, persist a per-person calibration (locked-vs-done rate, did "critical" ship first). The `/hq-weekly-update` agent reads it next week and **proposes a right-sized commit set** ("you complete ~60% of what you lock — here are the 6 highest-leverage items, not 10"). Turns reconciliation from a report into *training data for the proposing agent* — the harness plans better the more weeks it sees. Highest-compounding idea; fits this repo's ethos better than any static dashboard.

---

## Rejected (with reasons)

- **Single composite "Alignment Health" gauge as the only number** — collapsing concentration + orphan + aging into one 0–100 score hides the sub-signals and is premature; keep them distinct with a headline + drill-down. *Reject as the model; keep "headline + drill-down" as a UI principle.*
- **Continuous reconciliation (reconcile every merge)** — conflicts with the already-decided weekly LOCKED→RECONCILING lifecycle and is large scope. *Defer; revisit if weekly batching proves too coarse.*
- **Structured `blockers[]` at lock / reconcile pre-flight checklist** — real and worth doing, but it's enforcement UX, not a chess-layer/metric idea. *Route into plan-006 U4/U10/U11, not an ideation survivor.*
- **Priority-as-budget the carry-chain spends** — clever but over-couples mechanics; risk of rigidity. *Keep as a possible refinement of S4, not standalone.*

## Two tensions to resolve before building (escalate to brainstorm)

These surfaced from the constraint-flip frame and directly contest a decision already made (hard 1:1 SO linkage). They're not survivors — they're **forks the user must settle**, because they'd reshape the schema:

- **T1 — Fan-out linkage (1:1 → 1:N with fractional weight).** Cross-cutting work (a shared library, a migration, a platform refactor) doesn't fit one leaf SO; forcing it understates its reach and may push the agent to pick an arbitrary link. Allowing a commit to fan out to N SOs with weights summing to 1.0 makes leverage legible ("this refactor moves 4 Defining Objectives") — but complicates roll-up and the enforced-link story. **Decision needed:** strict 1:1 (current) vs. weighted 1:N.
- **T2 — Orphan / unmodeled work under hard enforcement.** Incident response, infra rot, and exploration ladder to *nothing yet*. A strict 1:1 mandate launders them into fake SO links — corrupting the single-source roll-up. An explicit `ORPHAN` lane with a reason enum (KTLO/Incident/Exploration/ExternalAsk) keeps the tree honest and surfaces strategy gaps via an "orphan ratio." **Decision needed:** does hard-enforcement admit a structured orphan escape hatch?

---

## Recommendation

A coherent, mutually-reinforcing bundle falls out of the convergence — and it's *smaller* than the original plan-006 chess layer + conformity design:

1. **Metric:** delete the ladders-up score (S2); replace with **Concentration Index + declared-posture delta (S1/S1b)**, **starvation (S6)**, and **carry-aging (S5)** — three distinct, un-gameable signals computed off the single-source roll-up.
2. **Chess layer:** **derive** category + WSJF-from-the-tree priority (S3/S4) instead of hand-tagged enums; optionally give "chess" real teeth via tempo/sacrifice/zugzwang on the carry chain (S5).
3. **Manager view:** exception/divergence brief (S7), agent-authored — parity-native.
4. **Compounding hook:** reconciliation calibration feeds the proposing agent (S8).
5. **Settle T1/T2 first** — they change the schema, so they gate the rest.

Next step options: `ce-brainstorm` one of these (the Concentration Index / S1 is the highest-value, highest-novelty candidate) to pin exact formulas and edge cases; or `ce-iterate-plan` to fold the adopted bundle into plan-006's KTD4/KTD8 and the relevant units.
