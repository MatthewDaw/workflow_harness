---
name: hq-orchestrate
description: >-
  A /plan-style front end for a supervised, multi-agent workflow, run in one
  claude+ thread in two phases. PHASE 1 (interactive, like /create_plan): pick a
  supervisor agent (runs on opus), a ticket-assigner agent, an optional validation
  agent, and a goal (typed, or discovered by a state-assessor agent that decides
  whether more work is even needed); collaboratively build the ticket DAG AND the
  supervisor's management plan — how it moves the run through its development
  phases (build → review → validate → merge). It writes a CONSTITUTION to a tmp
  scratch dir (not into the repo), then clears context. PHASE 2 (fresh context):
  the supervisor boots — it reloads the constitution and pulls its OWN prompt +
  skills from the HQ catalog, forces opus, then spins up parallel claude workers
  per ready ticket, supervises them on a self-cancelling /loop (on target, complete,
  clean, non-duplicative — nudging drifters), optionally validates, lands to main,
  and self-terminates. Use when the user says "/hq-orchestrate", "plan an agent
  workflow", "orchestrate agents on this goal", or "spin up a supervised swarm".
---

# /hq-orchestrate

A **two-phase orchestration skill** that runs in a single claude+ thread:

1. **PLAN (interactive)** — works like [[create_plan]]: gather the workflow,
   research the goal, and collaboratively settle both **what** gets built (the
   ticket DAG) and **how the supervisor will run it** (its phase machine, bar,
   and policies). The output is a **constitution** written to a scratch/tmp dir —
   *not* a document committed into the repo. The phase ends by **clearing the
   context**.
2. **SUPERVISE (fresh context)** — after the clear, the supervisor **boots from
   the constitution**: it reloads the run, **pulls its own prompt + skills from
   the HQ catalog**, forces its model to opus, and then drives the swarm
   (spawn → review → nudge → validate → merge → self-terminate).

The split is deliberate: planning is chatty and fills a context window; execution
wants a clean one. The constitution is the **only** thing that survives the clear,
so it must carry everything the fresh load needs to rebuild the run from zero.

This complements the Command HQ **Workflows** catalog ([[hq-add-workflow]]
registers a *static* DAG of agents; this skill plans and *runs* a *dynamic* one)
and the single-agent loops [[hq-optimize-agent]] / [[hq-endforge]].

## Modes

- **`/hq-orchestrate [goal]`** → **PLAN** mode (default). Interactive.
- **`/hq-orchestrate --supervise <runId>`** → **SUPERVISE** mode. This is what the
  post-clear re-entry runs; it expects a constitution already on disk.

## Where this runs

In the developer's claude+ session (the PTY), inside a connected repo, on the
developer's own subscription. It uses the **agents/skills/workflows REST** (the
device/session bearer token the other HQ skills use), **`/loop`** for the
supervisor's checkpoint cadence (one cron, on opus, self-cancelled at the end),
**background claude workers** each in its own **`git worktree`** off main, the
steering/send-message path to **nudge** a running worker, and the developer's own
`git` to land work. No Bedrock, no server-side model call — every supervisor
judgement and every worker is Claude Code reasoning here, on the subscription;
the backend only **stores** agents/workflows.

**Where the agents/skills live.** Only the `command-hq-starter` bundle's SKILL.md
*source* is in git (`catalog/skills/`). Every catalog **agent**, every other
**skill**, and every **workflow** lives in the HQ catalog (DynamoDB), addressed by
name and retrieved over REST (`GET /agents/<name>`, `GET /skills/<name>`,
`GET /workflows/<name>`). So neither the supervisor nor a worker reads its
instructions from the worktree filesystem — each **pulls them from the catalog**.

**Where the constitution lives.** In a **scratch/tmp dir outside the tracked
tree** — `<tmp>/hq-orchestrate/<runId>/` (where `<tmp>` is the OS temp dir,
`$TMPDIR` / `%TEMP%`). Never under `catalog/`, `packages/`, etc. This checkout may
run several agentic feature builds at once; keeping run state in tmp means it
never lands in a commit and never tangles with foreign WIP in the working copy.

---

## Phase 1 — PLAN (interactive, current context)

Like [[create_plan]]: be skeptical, research before asking, and get buy-in at each
step. Do **not** write the constitution in one shot — settle the pieces with the
user first.

### Inputs it gathers

- **Supervisor agent (required).** The orchestrator. Verify it resolves
  (`GET /agents/<name>` — just confirm it exists + capture its scope `{tier,id}`;
  the full prompt is pulled later, on the fresh load). It will run on **opus**
  regardless of its stored model.
- **Goal (required) — one of:**
  - **Typed goal** — a sentence/paragraph the user gives directly; OR
  - **State-assessor agent** — run it first; it inspects the project (repo,
    `docs/PRD.html` / `docs/plans/`, the HQ project) and returns
    `{ goal, moreWorkNeeded, rationale }`. If `moreWorkNeeded` is false, **report
    and stop** — nothing to orchestrate, no constitution written.
- **Ticket-assigner agent (required).** Turns the goal into tickets + dependency
  edges. Verify it resolves.
- **Validation agent (optional).** Runs end-to-end evals + code-quality after the
  build lands-ready. Omit to skip validation.
- **Parallelism cap (optional).** Max concurrent workers (default 4).

### Steps

1. **Resolve the project & verify the agents.** Read `$CLAUDE_PLUS_PROJECT_ID`
   (the injected contract — never guess). `GET` each named agent just to confirm
   it resolves and to record its scope; a 404 means an unreadable scope or missing
   agent — stop and report which one.
2. **Establish the goal** (typed, or via the state-assessor; the assessor may
   short-circuit the whole run).
3. **Build the ticket DAG with the assigner — interactively.** Hand the goal to
   the ticket-assigner agent; it returns tickets `{ id, title, prompt,
   dependsOn:[ids] }` (the `workflowSchema` node shape — unique ids, resolvable
   `dependsOn`, acyclic). **Review it with the user** like a plan: scope, ordering,
   what's explicitly *out* of scope, missing edge cases. Iterate until they're
   satisfied. *(Optional: persist the agreed DAG as a catalog Workflow via
   `POST /workflows { name, nodes }` — the record [[hq-add-workflow]] registers —
   so the plan is reviewable in HQ.)*
4. **Settle the supervisor's MANAGEMENT PLAN — how it runs the workflow and moves
   through development phases.** This is the part the user must see and approve, not
   just the ticket list. Pin down, in plain terms:
   - **The phase machine** the supervisor advances through and the transition
     criteria, e.g. **BUILD** (workers implement ready tickets) → **REVIEW**
     (each worker judged against the bar, drifters nudged, finished tickets
     promoted) → **INTEGRATE** (completed tickets unblock downstream; combined work
     coheres) → **VALIDATE** (optional validation agent) → **MERGE** (land to
     main) → **DONE** (self-terminate). State *when* it moves: e.g. BUILD→VALIDATE
     only when every ticket is done; VALIDATE→MERGE only on a passing validation
     (or immediately if no validator).
   - **The review bar** each worker is held to — on target (no scope creep),
     complete (no missed requirements/edge cases), clean (repo conventions, no dead
     code), non-duplicative (reuse, don't reinvent), plus correct ordering /
     foundation-first.
   - **Steering policy** — nudge a drifting worker with a targeted message; never
     silent-restart.
   - **Merge policy** — land each `work/<ticketId>` branch to main with the dev's
     own git; push.
   - **Self-termination** — cancel the `/loop` cron and clear the tmp run dir when
     DONE.
5. **Write the constitution + plan to tmp, then clear context.** Create
   `<tmp>/hq-orchestrate/<runId>/`, write `constitution.md` (template below) and a
   `plan.md` (the human-readable DAG + scope). Print the resume command and the
   tmp path, then **clear the context** so execution starts clean:

   ```
   Plan ready. Constitution written to <tmp>/hq-orchestrate/<runId>/constitution.md
   To start the supervisor on a fresh context:
     /clear
     /hq-orchestrate --supervise <runId>
   ```

   (If your harness can chain it, follow the `/clear` immediately with the
   `--supervise` re-entry so the handoff is automatic; otherwise the user runs the
   two lines. Either way the supervisor starts in **this same thread**, just with a
   cleared window.)

### The constitution (the doc that survives the clear)

`constitution.md` must let a **zero-memory** fresh context rebuild the entire run.
It is governance + state, not prose. Include:

```markdown
# Orchestration Constitution — run <runId>

projectId: <CLAUDE_PLUS_PROJECT_ID>
goal: <the goal>

## Agents (pull from the catalog on boot — these are name pointers, not prompts)
supervisor:      { name: <name>, scope: <tier>:<id>, model: opus }   # force opus
ticket-assigner: { name: <name>, scope: <tier>:<id> }
validation:      { name: <name>, scope: <tier>:<id> } | none
state-assessor:  { name: <name>, scope: <tier>:<id> } | none

## Run config
parallelismCap: <N>
worktreePrefix: ../wt-       branchPrefix: work/

## Ticket DAG (status advances across the run)
- id: <id>  title: <t>  dependsOn: [<ids>]  status: todo|in-flight|done  prompt: |
    <the ticket task prompt>
- ...

## Phase machine + transitions
BUILD → REVIEW → INTEGRATE → VALIDATE → MERGE → DONE
- enter VALIDATE when: all tickets status=done
- VALIDATE → MERGE when: validation passes (or skip VALIDATE if validation=none)
- on DONE: cancel the /loop cron, remove worktrees, delete this tmp run dir

## Review bar (apply to every worker each tick)
on-target · complete · clean · non-duplicative · correct ordering/foundation-first

## Policies
- steering: nudge drifters with a targeted message; never silent-restart
- workers: spawn with the retrieval bootstrap (they pull their own agent prompt +
  skills from the catalog — see below); one git worktree per ticket off main
- merge: land each work/<id> to main with the dev's own git, then push
```

---

## Phase 2 — SUPERVISE (fresh context, after the clear)

Entered by `/hq-orchestrate --supervise <runId>`. The context is **empty** — it
inherited nothing from the planning phase. So the **first actions, before anything
else**, hydrate the supervisor:

1. **Reload the run.** Read `<tmp>/hq-orchestrate/<runId>/constitution.md` fully.
   It is the source of truth for goal, agents, DAG, phase machine, and policies.
2. **Load the supervisor's OWN prompt + skills from the catalog.** The
   constitution names the supervisor; fetch it now (same pull pattern the workers
   use, applied to the supervisor itself on this fresh load):
   ```
   HQ="$CLAUDE_PLUS_API_URL"; TOK="$(sed -n 2p ~/.claude-plus/credentials)"
   GET $HQ/agents/<supervisor>?tier=<tier>&id=<id>
       → ADOPT its `prompt` as your operating instructions; note its skills/tools.
   for each skill in that agent's `skills`: GET $HQ/skills/<name>
       → read the returned `body` (the SKILL.md) and follow it.
   ```
   **Force the model to opus.** Nothing is assumed in-context: the agent's prompt
   and skill bodies are catalog records (DynamoDB), pulled here — not carried
   across the clear, not read from the worktree.
3. **Open the supervision `/loop`.** Register one cron — e.g.
   `/loop 3m /hq-orchestrate --supervise <runId>` — so each tick re-enters here,
   re-reads the constitution (now the live run state), and advances the phase
   machine. (Re-hydrating the prompt each tick is cheap and keeps every tick on the
   same instructions.)

Then drive the phase machine from the constitution:

4. **BUILD — spawn workers by dependency order.** Compute the **ready set**
   (tickets whose `dependsOn` are all `done`) and, up to `parallelismCap`, spawn one
   background claude worker per ready ticket, each in
   `git worktree add ../wt-<id> -b work/<id> main`, using the **retrieval
   bootstrap** below (not an inlined context dump). Tickets with unmet deps wait.
5. **REVIEW — check, then nudge.** Each tick, for every in-flight worker, judge its
   progress against the constitution's **review bar**. When a worker drifts, send a
   short **steering nudge** ("you're duplicating `db/repo.ts`'s `putSkill`; reuse
   it"; "the ticket also needs the 404 path"), never a silent restart. Mark a
   ticket `done` when it satisfies the bar; update the constitution's DAG status.
6. **INTEGRATE.** Newly-`done` tickets unblock downstream ones → back to BUILD for
   the next ready set, until the whole DAG is `done`.
7. **VALIDATE (optional).** When all tickets are `done`, if a validation agent is
   named, run it end-to-end (evals + code-quality across the combined work). On
   real failures, reopen the affected tickets (back to BUILD); else proceed.
8. **MERGE + DONE.** Land each `work/<id>` to main with the dev's own git
   (`git switch main && git merge --ff-only work/<id>` or the repo's landing
   convention), push, remove the worktrees. Then **cancel the `/loop` cron**, delete
   `<tmp>/hq-orchestrate/<runId>/`, and print a final summary (tickets completed,
   validation verdict, merge SHAs). Nothing keeps waking.

## What a worker inherits (and how it pulls the rest)

Same principle as the supervisor's boot: a spawned worker boots with an **empty
context** and inherits nothing. The supervisor hands it a light **bootstrap** and
the worker **retrieves** its full context from the catalog as its first action —
pull, not push. This keeps spawns cheap and the catalog the single source of truth.

```
You are the worker for ticket <ticketId> in orchestration run <runId>.
You run AS the catalog agent "<agent>" (scope <tier>:<id>).

FIRST — hydrate your own context before writing any code:
  HQ="$CLAUDE_PLUS_API_URL"; TOK="$(sed -n 2p ~/.claude-plus/credentials)"
  1. GET $HQ/agents/<agent>?tier=<tier>&id=<id>  → adopt its `prompt` as your
     operating instructions; note its `skills` and `tools`.
  2. For each skill in that agent's `skills`: GET $HQ/skills/<name> → read the
     returned `body` (the SKILL.md) and follow it when relevant.
  (These are catalog records in DynamoDB, not files in this worktree — fetch them.)

THEN do your ticket:
<the ticket prompt>

Depends on (already done): <dep ids>.  Feeds: <downstream ids>.
Work only in this worktree on branch work/<ticketId>. Stay in scope; if you find
yourself reimplementing something that already exists, stop and reuse it.
Heed any steering messages the supervisor sends.
```

Because agents and skills are **name-pointers resolved from the catalog**, both the
supervisor and its workers fetch exactly the versions HQ currently serves — no
drift from whatever happens to be on disk. (Materializing an agent as a
`.claude/agents/<name>.md` subagent is an optional optimization for the
system-prompt half; the **skill bodies** are still catalog records pulled by REST.)

## Worked dry-run example (against THIS repo)

```
/hq-orchestrate --supervisor swarm-lead --assigner ticketer \
  --validate evals-runner --goal "add a /workflows tab to the web app"
```

**Plan phase:**
1. Resolves the project; verifies `swarm-lead`, `ticketer`, `evals-runner` resolve.
2. Goal typed → used verbatim.
3. `ticketer` proposes 4 tickets: `dto` (no deps), `rest` (deps: `dto`),
   `web-list` (deps: `rest`), `web-detail` (deps: `rest`); reviewed with the user,
   DAG validates.
4. Management plan agreed: BUILD→REVIEW→INTEGRATE→VALIDATE→MERGE→DONE, cap 4,
   bar + nudge policy, ff-merge to main, self-terminate.
5. Writes `%TEMP%/hq-orchestrate/r-7f3/constitution.md` + `plan.md`; prints:
   `/clear` then `/hq-orchestrate --supervise r-7f3`. Context cleared.

**Supervise phase (fresh context):**
1. Reads `constitution.md`. 2. `GET /agents/swarm-lead` → adopts its prompt, pulls
its skills, model forced to opus. 3. Opens `/loop 3m /hq-orchestrate --supervise
r-7f3`. 4. Ready set `{dto}` → one worker in `../wt-dto` with the retrieval
bootstrap. Next ticks: `dto` done → spawn `rest`; `rest` re-derives a key scheme
that already lives in `db/keys.ts` → nudge "reuse `workflowKey`, don't invent
one"; `rest` done → `web-list` + `web-detail` both ready → spawn both. 7. All done
→ `evals-runner` passes. 8. ff-merge all four to main, push, remove worktrees,
cancel the `/loop`, delete the tmp run dir, print the summary.

(Counter-example: had `--goal` been `--assess state-checker` and the assessor
returned `moreWorkNeeded:false`, the plan phase prints "state-checker reports the
goal is already met — nothing to orchestrate" and stops before writing any
constitution or clearing context.)

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: the plan phase
gathers the supervisor / ticket-assigner / (optional) validation agents and the
goal (typed or via a state-assessor that can short-circuit), interactively settles
the ticket DAG **and** the supervisor's management/phase plan, and writes a
constitution to a tmp scratch dir (never into the tracked repo) before clearing
context. The supervise phase boots on a fresh context by reloading that
constitution and **pulling the supervisor's own prompt + skills from the catalog**
(model forced to opus), then advances BUILD→REVIEW→INTEGRATE→VALIDATE→MERGE→DONE —
spawning parallel workers (each handed a retrieval bootstrap, not an inlined
context dump), nudging drifters, optionally validating, landing to main, and
self-cancelling its `/loop` and deleting the tmp run dir when done. It never
inlines context that should be pulled, and never leaves a dangling loop, worktree,
or tmp run dir after a completed run.
