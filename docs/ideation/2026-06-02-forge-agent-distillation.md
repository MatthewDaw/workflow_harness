# Forge: capture a phase of work → distill a reusable agent

- **Date:** 2026-06-02
- **Status:** Ideation — converged on a minimal v1; ready to take into brainstorming/planning.
- **Topic:** A bundled claude+ command pair (`/startforge` … `/endforge`) that captures a
  slice of work and distills it into a small, reusable, org-wide agent — a short,
  focused prompt plus a curated skill set — aimed at one kind of phase problem.

## The gist (one sentence)

> Mark a slice of work, then distill it into a small reusable agent — a short
> prompt plus a curated set of skills aimed at one kind of phase problem — and
> register it in Command HQ for everyone.

## End goal

**Short, focused prompts with a reasonably curated skill set, each good at a specific
phase problem.** Forged agents are phase specialists: narrow scope, a small prompt, a
handful of skills. The prompt's job is to make the agent do **sound, well-sequenced
work** — build a solid foundation first, do things in the right order, and avoid
dead-ends/bad ideas. Prompts stay concise as a matter of *clarity* (no bloat), **not**
because token spend is an optimization target — we explicitly do **not** optimize for
fewest tokens.

## Lifecycle

1. **`/startforge [what you're working on]`** — marks the start point: records the
   base commit, and optionally creates a worktree + branch. If the user didn't say
   what they're working on, ask, and generate (or accept) a name. This is the only
   ceremony.
2. **…do the work…** — the per-repo daemon already captures the session (events,
   transcript, and the skills/tools actually used).
3. **`/endforge`** — distills the captured work into a registered agent (details
   below).

## What `/endforge` does

a. **Distill a seed prompt + curate the skill set.**
   - *Prompt:* a first draft that reproduces the captured work. It **is allowed to
     contain project-specific instructions** at this stage.
   - *Skills:* the distiller has **full access to the entire skill catalog**, but
     anchors on the **skills actually used** in the captured session, rounding the
     set out from the catalog only where something obviously fits. **Skills are
     curated, not searched/optimized.**

b. **Gradient-descent the prompt (prompt only).**
   - Run the seed prompt through a **third-party prompt-gradient-descent tool** to make
     the agent produce **sounder, better-sequenced work** — the objective is **outcome
     + process quality** (reproduce-or-improve the captured work, built in a good
     order, foundation-first, no dead-ends). **Token spend is not part of the
     objective.** (See Tooling section for the v1 tool choice; behind a thin
     `PromptOptimizer` interface.)
   - The optimizer needs an objective + an eval case to descend on. **The captured
     diff/session is that eval target** — the captured work both seeds the prompt and
     grades the optimizer's candidates.
   - The optimizer touches **only the prompt**. Skills are decided once by curation
     and handed to registration untouched.
   - The prompt should explicitly encode **principles + sequencing + failure-avoidance**
     ("here's the good order; here are the dead-ends to skip"), partly as standing
     distillation guidance and partly extracted from the good ordering actually followed
     in the captured session.

c. **Split pass (after optimization).**
   - Decompose the optimized prompt into two pieces:
     1. **Reusable prompt** — the generalizable, project-agnostic agent/skill
        instructions. *This is what gets registered and reused on other projects.*
     2. **Project-specific instructions** — what this particular unit of work was
        doing. **Mostly documentation**; rides along as the agent's forge-provenance
        record, **not** shipped into other projects at runtime.
   - Why split *after* optimizing: the optimizer needs the specifics in the prompt to
     hit the eval target; generalization is a single decomposition step at the end
     rather than a constraint that hobbles the descent.

d. **Register org-wide.**
   - Register the agent = `{ reusable prompt, model, skill refs }` to Command HQ at
     **org scope, for all users.**
   - **Co-register any new skills** the agent points to (no dangling skill pointers).
   - Write the **agent ↔ skill association both ways**: the agent lists the skills it
     uses; a skill records the agent it's likely to be used in.
   - Keep the project-specific instructions as the agent's forge-provenance doc.

## How this builds on what already exists

This is largely the **capture/authoring side** of machinery already in the repo:

- **Forge backend** (`packages/backend/src/forge/`): `embed.ts` (session →
  summary → embedding, with `SessionActivity.skills`/`tools`), `propose.ts`
  (`aggregateFrequencies`/`splitConfidence` over sessions → drafted agent), `search.ts`.
  The distiller reuses `propose.ts`'s drafter for the seed prompt and the frequency
  aggregation as the skill-curation spine. The **trigger** changes (precise
  start/end checkpoint instead of a fuzzy text query); the pipeline mostly stays.
- **Config sync / Agents+Skills registry** (`wrapper/internal/config/`): `Item`
  (`Kind = agent | skill`, content-hashed), `Diff` (push/pull/differs drift),
  scoped `org | user#uid | proj#pid`. Registration rides this: publish skills first,
  then the agent; distribution to all users is the existing `needs_pull` drift +
  reconcile.
- **Capture** (`wrapper/internal/capture/`): already records sessions, transcripts,
  and skills used — so "the skills it uses" is observable for free.

### Kill the Forge tab

The current `tab_forge.go` is the **read-side** authoring UI (fuzzy description →
semantic-search past sessions → propose). The command flow supersedes it with
precise evidence, so the tab becomes a weaker duplicate and should be removed.
**Keep** the **Agents/Skills tab** (config drift/reconcile) as the browse/manage
surface where org-published agents appear and pull down. **Repurpose**, don't delete,
the backend `forge/` code.

## Decisions settled during ideation

- **Interface = commands, not a GUI.** No dedicated Forge panel; the "UI" is the
  `/startforge` → `/endforge` lifecycle plus the `/endforge` approval prompt.
- **Prompt: yes gradient descent** — prompt only, **run via Claude Code on the user's
  Claude subscription** (not Bedrock, not a third-party framework).
- **Objective = outcome + process quality**, single axis. **No token-spend
  optimization.** The prompt encodes good principles/sequencing/failure-avoidance.
- **Keep it light** — a few-iteration Claude Code refine/run/judge loop in a worktree;
  do not build a process-eval harness or adopt a heavyweight optimization framework.
- **Skills: no gradient descent.** Full-catalog access + curation anchored on actual
  usage.
- **One optimizer, specifics allowed in, split afterward** into reusable prompt +
  project-specific doc.
- **Register at org scope, for all users**, co-registering new skills, with
  bidirectional agent↔skill links.

## Deferred (recoverable later, not in v1)

- **Self-built eval/optimization harness** (sandboxed re-execution + scoring) — we
  buy this via the third-party tool instead of building it.
- **Continual propagation / trainset loop** — trying out a registered agent would
  silently open a `/startforge`, and a dissatisfied run would re-optimize over the
  new trial ∪ previous sessions (a per-agent "trainset", with a `parentAgent` +
  `satisfaction` link on session records, regression-guarded against the originals,
  emitting a new pinned version). Deferred entirely; for now, just `/startforge` →
  `/endforge` again.
- **Prompt × skill co-optimization** (DSPy-style joint search) — skills stay curated.
- **"Is this a clear feature type?" classifier gate** — folded into the distiller: if
  there's nothing coherent to save, it says so.
- **Version pinning + publish governance/review** — matters once many agents exist
  and org-wide publish-by-anyone becomes a supply-chain surface.

## Risks / things to watch

- **Split-pass fidelity** (axis C) — leave specifics in the reusable prompt and the
  agent overfits to one repo; strip too aggressively and it loses useful patterns.
  This single pass is where the project-agnostic guarantee is actually enforced.
- **`/startforge` push-to-main / worktree ceremony** is destructive and surprising —
  needs guardrails and a sane default for dirty trees / abandoned forges.
- **Org-wide publish-by-anyone** — a supply-chain surface for 175+ users; likely
  wants a review step before deferral above is reversed.
- **Optimizer cost/latency** — gradient-descent tools make many model calls; the
  `/endforge` optimize step is heavy but explicit and infrequent.
- **Python optimizer ↔ Go/TS stack** — the optimizer is a server-side step in the
  forge backend, not in the Go wrapper.

## Open questions for planning

- Exact `/startforge` git semantics: always worktree+branch, or optional? push-to-main
  guardrails; dirty-tree handling; abandoned-forge cleanup.
- Where the optimizer runs (forge backend service vs. workflow step) and how the
  captured work is packaged as its eval target.
- Skill provenance for **new** skills the distiller mints — how they're named,
  scoped, and reviewed before org publish.
- Agent naming / dedup against existing registered agents.

---

# Tooling & best practices (2026 web research)

Added 2026-06-02 from a 4-way web-research pass (prompt optimizers, eval/scoring,
session→agent distillation, prompt-compression + skill selection). Sources at the
end of this section.

## Headline findings that refine the design

1. **The objective is single-axis: outcome + process quality.** We do **not** optimize
   for token spend (see Goal). The optimizer pushes the prompt to produce sounder,
   better-sequenced work — reproduce-or-improve the captured task, foundation-first, no
   dead-ends. Dropping the token term also removes the multi-objective Pareto / token-
   penalty machinery the research describes.
2. **Keep the "good principles" mechanism light — don't overengineer it.** We are *not*
   building a process-eval harness that scores "did it build in a good order." Sound
   sequencing/principles are **content the prompt encodes** — standing distillation
   guidance plus the good ordering extracted from the captured session (as principle +
   failure-mode fields). The optimizer just refines that prompt against whether the work
   comes out sound.
3. **One captured task = N=1 → overfitting risk.** A single example lets the optimizer
   game the specific case. Cheap mitigation: a couple of paraphrased variants of the
   captured task — but keep it light; don't over-invest here for v1.
4. **Quality gate before org-wide publish is non-negotiable.** Strongest number in the
   research: **curated skills +16.2pp; unverified self-generated skills −1.3pp** (SoK
   2026). This partially *un-defers* governance — v1 needs at least a **transfer-
   validation gate** (the forged agent must succeed once on a *different* repo than the
   source) before it goes live for everyone.
5. **Generalize via trajectory comparison, not self-reflection.** ExpeL: diffing a
   success vs. failure trajectory generalizes better than asking the model to explain
   itself. Strip project-specifics in layers (extract pattern → redact repo/paths/stack
   → check it still reads on a blank repo).
6. **Skill curation = frequency + dedup (retrieval later).** Confirms "no search over
   skills." Anchor on skills actually used (`aggregateFrequencies` already exists) and
   **drop overlapping skills** (selection between near-duplicates is ~random). Keep the
   set small; dependency-subgraph and retrieval/deferred-loading are refinements to add
   only if needed — not v1.

## Recommended approach — Claude Code on your subscription (keep it light)

**Decision: the model doing the optimization is Claude, run through Claude Code on the
user's own subscription — not Bedrock, not a third-party framework's API.** Rationale:
we're optimizing prompts *for Claude Code builds*, so the optimization (and its eval)
should run in the same harness and on the same model the forged agent will actually use.
Same-environment optimization = higher fidelity, and it spends the subscription rather
than metered Bedrock/API budget.

This rules out the heavyweight options the research surfaced as *products*: the **Bedrock
Advanced Prompt Optimizer** (AWS-metered, wrong model account) and the Python frameworks
(**TextGrad / DSPy / DeepEval**) that drive their own model calls via litellm/Bedrock.
Their *ideas* still apply — LLM-as-optimizer / textual-gradient refinement, correctness-
first scoring, trajectory-pair generalization — but we implement them as a small loop,
not by adopting a framework. Also *not* DSPy/MIPROv2 specifically: it needs 20+ labeled
examples we won't have per agent.

**The loop (deliberately minimal):**
1. Run the **candidate prompt in a Claude Code session** against the captured task in a
   fresh worktree (we already create worktrees at `/startforge`, so the eval sandbox is
   free).
2. **Judge the result** — primarily *did the work come out sound?* Run the captured
   tests if any; otherwise a short Claude rubric pass. Execution-based signal is
   bias-free and is the thing to lean on.
3. **Refine the prompt** — Claude proposes the next version from the critique.
4. Repeat **a few** iterations (≈3–5), then stop. No convergence theory, no Pareto
   front, no sandbox service beyond the worktree.

Build it behind a thin `PromptOptimizer` interface so the loop internals can change
later, but v1 is just "Claude Code refining a prompt against a worktree run, a handful of
times."

**Judge caveat:** best practice is a cross-family judge (don't let Claude grade Claude —
self-preference bias). That conflicts with "stay on the Claude subscription," so v1
resolves it by **leaning on execution-based eval** (run the work, see if it holds up) and
treating the Claude rubric as a secondary signal — rather than standing up a second model
account just to judge.

## LLM-as-judge best practices (when no tests exist)
- Best practice is a **cross-family judge** (don't let Claude grade Claude —
  self-preference bias); **v1 instead leans on execution-based eval** (run the work in a
  worktree) since the optimizer stays on the Claude subscription — see judge caveat above.
- **Swap-and-average** candidate order to cancel position bias; use chain-of-thought
  rubric scoring (Zheng et al. 2023 is the canonical reference).
- Add an explicit **brevity criterion** or the judge rewards verbosity.
- **Diff/patch similarity is NOT correctness** — use it only as a cheap pre-filter;
  prefer behavioral equivalence (run the tests).

## Forged-agent shape (adopt the SoK 4-tuple)
A skill/agent = `S = (C, π, T, R)`: applicability **C** (semantic trigger + optional
keywords), policy **π** (the distilled prompt), termination **T** (success *and*
failure conditions — failure handling is mandatory, not optional), interface **R**
(tools/skills, **pinned model**, required context). Store **summary + full body**
separately (metadata-driven disclosure / lazy load — matches Claude Code and our
existing config model). Identify as `slug:semver`, with **content-hash pinning** for
the skills an agent points to (we already hash items in `internal/config`).

## Registry / distribution best practices
- **Hash-pin** skill pointers at consumption (LangSmith/`skill:<hash>` pattern);
  `latest` is for experimentation only.
- **Environment-gated promotion**: `draft → review → staging → prod`, each gated by an
  eval (Braintrust pattern). Minimum gate for Forge = transfer-validation + one peer
  approval before org publish.
- **CI on skill change**: re-run against a fixture set; block if success rate drops.
- **AGENTS.md interop**: export forged agents in the cross-tool `AGENTS.md` standard so
  Cursor/Codex/Gemini CLI can consume them too.
- **One topic per skill, precise trigger, explicit rationale** (Devin KB practice).

## Sources
- Prompt optimizers: [TextGrad](https://github.com/zou-group/textgrad) ·
  [TextGrad paper (Nature/arXiv)](https://arxiv.org/abs/2406.07496) ·
  [DSPy MIPROv2](https://dspy.ai/api/optimizers/MIPROv2/) ·
  [AdalFlow](https://github.com/SylphAI-Inc/AdalFlow) ·
  [OPRO](https://arxiv.org/abs/2309.03409) ·
  [Bedrock Advanced Prompt Optimization (May 2026)](https://aws.amazon.com/blogs/aws/amazon-bedrock-introduces-new-advanced-prompt-optimization-and-migration-tool/) ·
  [Anthropic Prompt Improver](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-engineering/prompt-improver)
- Eval / scoring: [LLM-as-a-Judge (Zheng et al. 2023)](https://arxiv.org/html/2306.05685v4) ·
  [MOPrompt (multi-objective, 2025)](https://arxiv.org/html/2508.01541v1) ·
  [DeepEval prompt optimization](https://deepeval.com/docs/prompt-optimization-introduction) ·
  [Inspect AI](https://inspect.aisi.org.uk/) ·
  [Anthropic eval guidance](https://docs.anthropic.com/en/docs/test-and-evaluate/develop-tests) ·
  [E2B / sandbox comparison](https://northflank.com/blog/best-code-execution-sandbox-for-ai-agents) ·
  [UTBoost (test adequacy)](https://arxiv.org/pdf/2506.09289)
- Session→agent distillation: [Voyager](https://arxiv.org/abs/2305.16291) ·
  [SkillRL](https://arxiv.org/html/2602.08234v1) ·
  [Trace2Skill](https://arxiv.org/html/2603.25158v1) ·
  [SkillWeaver](https://arxiv.org/abs/2504.07079) ·
  [ExpeL](https://arxiv.org/html/2308.10144v2) ·
  [SoK: Agentic Skills (4-tuple; +16.2/−1.3pp)](https://arxiv.org/html/2602.20867v1) ·
  [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) ·
  [prompt registries 2026](https://www.braintrust.dev/articles/best-prompt-management-tools-2026)
- Compression + skill selection: [LLMLingua-2](https://arxiv.org/html/2403.12968v2) ·
  [LLMLingua repo](https://github.com/microsoft/LLMLingua) ·
  [Lost in the Middle](https://arxiv.org/abs/2307.03172) ·
  [AutoTool (tool overload)](https://arxiv.org/pdf/2511.14650) ·
  [Tool preferences unreliable](https://arxiv.org/pdf/2505.18135) ·
  [Tool RAG / ToolScope](https://next.redhat.com/2025/11/26/tool-rag-the-next-breakthrough-in-scalable-ai-agents/) ·
  [Anthropic: writing effective tools](https://www.anthropic.com/engineering/writing-tools-for-agents)
