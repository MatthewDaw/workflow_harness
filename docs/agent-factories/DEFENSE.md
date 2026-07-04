# The Agent Factory — Architecture & Defense

> **Purpose of this doc:** the presentation-grade statement of what the factory is, why
> it's shaped this way, and the answers to the hard questions. Everything here is
> grounded in 2025–26 research and production systems (sources throughout), plus
> mechanisms already proven in our own installed skills. The detailed group designs live
> in [`groups/`](./groups/) and [`self-improvement/`](./self-improvement/).

---

## The pitch (one sentence)

**You tell the factory what you want; it makes you define it well; then one standard
loop closes the gap between your repo and that definition — and the factory itself gets
better every time you use it.**

## What it's made of (the anatomy)

Before the runtime story, the structural one — the factory is just three things:

1. **Agents, one definition:** every agent is **a prompt command + a set of skills**.
   Nothing else exists. One file to write, one unit to version, one unit to improve.
2. **Families:** agents are registered under families — **research · build · verify ·
   improve** — plus the orchestrator above them. Extending the factory = registering
   one more agent into a family; the machine never changes.
3. **The orchestrator:** the single agent a user talks to. It takes the task,
   instructions, and scope, then composes *retrieved* registered agents into the
   workflow below. It resolves every dispatch through the registry index — top-k vector
   search over agent/skill descriptions — so its context stays O(k) while the catalog
   grows O(n) ([registry health](./self-improvement/registry.md)).

The loops below are what this anatomy does at runtime.

## The whole system is three loops

```
┌─────────────────────────────────────────────────────────────────────┐
│  1. DEFINE      user + agent turn intent into a checkable spec      │
│     (scope-tiered: one sentence for a bugfix … full spec for a      │
│      greenfield build)                                              │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  2. CONVERGE    supervisor measures the GAP between repo and spec,  │
│     ticketizes the gap, a standardized churn workflow works the     │
│     tickets, verification gates check everything —                  │
│     LOOP UNTIL THE GAP LIST IS EMPTY                                │
└──────────────────────────────┬──────────────────────────────────────┘
                               ▼
┌─────────────────────────────────────────────────────────────────────┐
│  3. COMPOUND    in the background, the factory watches every        │
│     interaction, mines what worked and what didn't, and ingests     │
│     those learnings back into its own skills (auto-healing)         │
└─────────────────────────────────────────────────────────────────────┘
```

That's the entire architecture. Every detail below is one of these three loops zoomed in.

---

## Loop 1: DEFINE — make the user actually think

**The #1 documented failure of autonomous coding agents is ambiguous requirements.**
Devin's 2025 production review: it "handles clear upfront scoping well, but not mid-task
requirement changes" and "usually performs worse when you keep telling it more after it
starts." The factory's answer: the work of thinking happens *before* the loop starts,
and the factory actively interviews the user to force it (office-hours style — probing
questions, not a form).

**Scope-adaptive depth.** The single biggest cause of spec-driven-development fatigue is
writing a PRD for a one-line fix (Kiro generated 16 acceptance criteria for a small bug
fix — that's the anti-pattern). So intake classifies the task into a tier, and the tier
sets the spec depth:

| Tier | Looks like | Define output |
|------|-----------|---------------|
| **T0 micro** | typo, config tweak | one sentence: repro + expected behavior |
| **T1 task** | single-concern change, <5 files | mini-spec: problem, numbered acceptance criteria, out-of-scope, verification mapping |
| **T2 feature** | new user-facing capability | full spec dir: `spec.md` + `design.md` + `tasks.md` + `traceability.md` |
| **T3 system** | greenfield / new service / major refactor | T2 + ADRs + API contracts + the upstream groups (goal research → architecture → feature list) run first |

This is where the old "modes" went: **greenfield / feature / fix / research aren't
separate pipelines anymore — they're intake tiers that decide how much Define runs
before the one standard Converge loop.** Same machine for everything; only the on-ramp
changes. (Mode-based recipes and the gap loop turn out to be the same idea at different
zoom levels.)

**The spec is checkable, not prose.** Every acceptance criterion gets an ID (AC-001…)
written in EARS form ("WHEN <trigger>, the system SHALL <response>") — a mechanical
translation away from a test. Every task references the AC it satisfies. A traceability
table maps AC → test → status. Open questions must hit zero before Converge starts.
(Format per AWS Kiro / GitHub Spec Kit / OpenSpec — the converged industry grammar.)

**Already proven in-house:** our `ac-plan`/`ac-brainstorm` skills do stable U-IDs with
origin tracing (requirement IDs flow into plan units, into commits) — that's exactly the
traceability chain Define produces.

---

## Loop 2: CONVERGE — the gap loop

The user's core instinct, validated hard by research: **a supervisor that repeatedly
asks "what's the gap between the repo and the goal, fully completed and rigorously
tested?", turns gaps into tickets, and churns them.** This is the "Ralph" pattern
(Geoffrey Huntley / HumanLayer), which both Anthropic (ralph-wiggum plugin) and OpenAI
(Codex `/goal`) have now shipped natively. A 14-hour unattended Ralph run upgraded React
16→19; ~$10/hour economics.

But the research is equally clear about *why* naive loops fail, so Converge has four
load-bearing rules:

### Rule 1 — Gaps are measured, not vibed
The loop's quality is bounded by its verifier ("loop quality cannot exceed oracle
quality"). So the gap assessment is **mechanical first, judgment second**:

```
GAPS = unchecked tasks in tasks.md            (implementation gaps)
     + ACs with no linked test                (coverage gaps)
     + ACs whose linked tests FAIL            (behavioral gaps)
     + unresolved open questions in the spec  (definition gaps)
     + stale tests (spec version mismatch)    (drift gaps)
```

Empty list + green gates = done. An LLM judgment pass layers on top (drift, slop,
integration smells — these become new gap entries), but the floor is mechanical.
**The agent never gets to declare victory** — completion is a structured state change
derived from the gap ledger, never the model saying "I'm done" (the false-completion
problem is the single most documented failure mode in autonomous loops).

### Rule 2 — Fresh context every iteration, state in the repo
Each loop iteration is a fresh session that re-derives state from files: the spec, the
gap ledger, `tasks.md`, a run constitution. Context accumulation measurably degrades
agents over long horizons (SlopCodeBench: complexity erosion in 80% of long
trajectories; no agent survives multi-checkpoint tasks with retained context). Files on
disk make every crash resumable and every iteration auditable. Our `hq-orchestrate`
(constitution file) and `ac-work` (idempotent "already done?" pre-check per unit)
already implement this.

### Rule 3 — Tickets sized under the reliability cliff
METR's data: agent reliability falls off a cliff as task length grows (50% success at
~hours-scale; near-100% well below it; the horizon doubles every ~4–7 months). So the
ticketizer cuts gaps into **vertical slices sized for ~80–90% reliability** (roughly 1–3
files, one observable behavior, own acceptance test) — and as models improve, the same
architecture just takes bigger bites. Tickets meet the Definition of Ready already
specified in [features-to-specs](./groups/features-to-specs.md) (Gherkin ACs, explicit
file paths, contracts, style exemplar, boundaries).

### Rule 4 — Verification is layered, autonomous, and runs before any human looks
The standardized churn is the machinery we already designed
([specs-to-code](./groups/specs-to-code.md): Kahn scheduler, worktree-per-ticket,
sequential merge gate; [validation](./groups/validation.md): parallel sweeps), organized
as a gate stack:

```
L1 deterministic   typecheck, lint, build, unit tests — exit codes, never transcripts
L2 spec compliance review THEN code-quality review (two-stage, in that order —
                   our subagent-driven-development skill's non-negotiable ordering)
L3 LLM review      persona panel; pass^k (all k attempts pass), not pass@k
L4 integration     E2E / browser dogfooding in the sandbox (ac-dogfood's
                   flowchart→test-matrix method)
L5 human           spec-level and architecture decisions only — never line-by-line
                   linting that L1–L4 should have caught
```

The Factory.ai rule we adopt verbatim: **the orchestrator never trusts chat output** —
every phase produces a file artifact and an exit code, and the supervisor reads those.

### Stall & escalation (the loop can't run away)
Gap-count trend not shrinking across N planning cycles, no diff produced, same error
repeating (circuit breaker), or budget cap hit → escalate to human with the failed-
approaches log. Irreversible actions (deploy, data deletion) always sync-gate. The
planning evidence (SWE-Manager "golden proposals": plan-first beats dive-in; Devin's
upfront-scoping sweet spot) is why Define exists at all — the staged pipeline isn't an
alternative to the loop, **it's the loop's initialization.**

---

## Loop 3: COMPOUND — self-healing (skills *and* agents)

Self-healing has two halves: the **content** of skills learns from use, and the
**structure** of the catalog keeps itself clean as it grows.

**Structure heals** ([registry health](./self-improvement/registry.md)):
- **Skills never repeat themselves** — nearest-neighbor dedup before any skill is
  created or edited (≥0.90 duplicate → route to the existing skill; 0.85–0.90 → merge).
  One concern, one home — so "where does this idea go?" always has exactly one answer.
- **Auto-split when too large** — an oversized skill splits into child skills; an agent
  whose job stops fitting one honest description splits into sub-agents. Enforced like a
  lint rule, executed through the gated skill-editor.
- **Top-k search, never full-catalog loading** — the orchestrator and the ingestion
  pipeline resolve everything through a vector index (k ≈ 5–10). Context stays O(k)
  while the catalog grows O(n): thousands of registered agents, constant footprint.
  (Proven pattern — Claude Code's own deferred tools + ToolSearch.)
- **Disambiguation** — index ranking → family ownership → cheap router call → ask the
  user; a description collision between two agents is itself a registry smell that feeds
  the insight queue.

**Content heals** — the factory's skills are editable artifacts, improved mostly
invisibly:

- **Watch:** every interaction is captured (privacy-gated, tail-sampled — failures 100%,
  successes ~10%) and pinned to the exact prompt/skill version that produced it.
- **Mine:** recurring friction, corrections, and wins become evidence-backed insights
  (promotion bar: ≥3 distinct sessions, ≥2× baseline failure rate, two-window
  persistence — never a single anecdote).
- **Ingest:** insights become **small, additive edits to skills** — a guardrail sentence,
  a few-shot example, a corrected default — applied *in the background as the user works*.
  User corrections are the highest-value signal: when you fix the agent's output or
  re-prompt, that's a labeled training example for the skill that failed.
- **Gate by blast radius:** additive low-risk ingestions auto-apply behind a canary
  (5% → ramp, auto-rollback); structural edits and new skills require human approval.
  The evaluator, approval gate, and rollback machinery are an **immutable core** the
  loop cannot edit (enforced at the filesystem/permissions layer — the meta-level
  reward-hacking defense).
- **Measure or revert:** every ingestion carries a predicted metric; sequential testing
  keeps wins and rolls back regressions. Kept changes become the new baseline.

This is the "Ratchet Principle" from harness-engineering practice: *every failure that
happens twice is a harness bug* — and the evidence says the harness, not the model, is
where compounding advantage lives ("a decent model with a great harness beats a great
model with a bad harness"). Self-improving harness research backs the ceiling: SICA
self-edited from 17% → 53% on SWE-bench Verified.

Full detail: [`self-improvement/`](./self-improvement/).

---

## Why this shape and not the alternatives

| Alternative | Why not |
|---|---|
| **One big agent, one long session** | Context degradation is measurable and monotonic (SlopCodeBench); METR cliff caps reliable task length. The loop with fresh context per iteration is the architectural fix, not a prompt fix. |
| **Pure staged pipeline (plan → build → test, once through)** | Can't absorb mid-task discovery; Devin's documented failure zone. The gap loop re-derives state every iteration, so new information just becomes new gaps. |
| **Pure Ralph loop, no Define phase** | Loop quality is bounded by the verifier; without a checkable spec there's nothing to converge *to*. Plan-first measurably beats dive-in (SWE-Manager; HumanLayer themselves say RPI "falls flat for greenfield"). |
| **Naive parallel multi-agent** | Cognition's "Don't Build Multi-Agents": parallel writers make conflicting implicit decisions. We parallelize reads freely; writes go through worktree isolation + a sequential merge gate (and shared-workspace parallel agents measurably perform *below* a single agent). |
| **Trust the model's self-report** | Agents declare completion as a language pattern regardless of state. Exit codes, file artifacts, structured state transitions — never transcripts. |

---

## Anticipated objections (the defense)

**"How do you know it's actually done?"**
The verifier owns done, not the agent. Done = the mechanical gap list is empty (every AC
has a passing linked test, every task checked, zero open questions) *and* the L1–L4 gate
stack is green. Completion is a structured state change; the model's prose is ignored.

**"Isn't writing specs a huge overhead for small tasks?"**
Tier-adaptive. A T0 bugfix's "spec" is one sentence and a repro. The factory refuses to
generate T2 ceremony for T0 work — that refusal is a design rule, because spec fatigue
is the documented adoption killer.

**"What if it loops forever / burns money?"**
Mechanical stop conditions: gap-trend stall detection, same-error circuit breaker,
no-diff detection, wall-clock and token budgets, failed-approaches log. Stall →
escalate with evidence, not silent retry. Ralph economics are known (~$10/hr) and
capped per run.

**"What if the spec itself is wrong?"**
That's a first-class outcome, not a failure: a behavioral gap offers three fixes —
fix the code, fix the spec, or fix the test. Spec changes are classified (additive →
auto-accept; breaking → human gate) and versioned, so tests know when they're stale.

**"Why will it actually get better over time?"**
Because improvement is measured, not assumed: ingestions ship with predicted metrics,
canary + sequential testing keeps wins and reverts losses, and Goodhart guards
(counter-metrics + guardrails) block metric-gaming "improvements." Kept changes ratchet.

**"Why should we believe multi-agent parallelism helps at all?"**
We only parallelize where evidence supports it: isolated read/research fan-out
(Anthropic's research system: +90% over single-agent) and write-parallelism *only* with
worktree isolation + merge gates (+14 to +27 points vs shared workspace). Parallelism is
capped (~4–6) because integration overhead eats gains beyond that.

---

## What changed from the v1 docs (honest changelog)

1. **The supervisor's modes collapsed into intake tiers.** Greenfield/feature/fix/
   research were separate recipe chains; now they're depths of Define feeding one
   standard Converge loop. Simpler to explain, and brownfield/greenfield stop being
   different machines. ([supervisor.md](./groups/supervisor.md) updated.)
2. **"Validation" became the gate stack inside the loop**, not a stage after coding —
   per-ticket gates (L1–L3) plus batch-level integration (L4), with re-validation on the
   merged state retained.
3. **The spec/traceability artifacts are now the factory's spine.** The groups
   (goal-research, architecture, feature-list, features-to-specs) are unchanged as
   workers — but their outputs all land in the Define artifact set that the gap
   assessor reads.
4. **Self-improvement got promoted** from "a meta-group" to the third pillar of the
   pitch, with explicit background auto-healing behavior.

## The five principles (closing slide)

1. **Define done before you start, in machine-checkable terms.** The input to the
   factory is a falsifiable spec, not a vibe.
2. **The supervisor owns context; workers execute with fresh, minimal context.**
   Read-parallel, write-sequenced, state in files.
3. **The harness is the product.** Every repeated failure becomes a permanent rule;
   skills compound.
4. **Verification is autonomous and layered; humans review intent, not lint.**
5. **Size work to today's reliability cliff; the architecture scales as models do.**

---

### Source spine (for the appendix slide)

Ralph: ghuntley.com/ralph, how-to-ralph-wiggum, Anthropic ralph-wiggum plugin, Codex
`/goal` · Spec-driven: AWS Kiro (EARS), GitHub Spec Kit, OpenSpec deltas, Böckeler/
martinfowler.com · Verification: SWE-PRM, agentic-verifier scaling, Meta mutation
testing, Factory.ai TDD orchestration · Multi-agent: Cognition "Don't Build
Multi-Agents", Anthropic multi-agent research system + Building Effective Agents ·
Capability: METR time horizons, SWE-bench Pro/SlopCodeBench · Self-improvement: SICA,
harness-engineering (Osmani), HumanLayer ACE · In-house: ac-plan U-IDs,
subagent-driven-development two-stage review, hq-orchestrate constitution, ac-work
idempotent re-execution, ac-dogfood flowchart test matrices.
