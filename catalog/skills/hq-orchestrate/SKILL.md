---
name: hq-orchestrate
description: >-
  Run inside the claude+ PTY to drive a supervised, multi-agent workflow end to
  end. You pick a SUPERVISOR agent (runs on opus), a TICKET-ASSIGNER agent, an
  optional VALIDATION agent, and a goal — typed out, or discovered by a
  state-assessor agent that inspects the project and decides whether more work is
  even needed. The supervisor builds the goal, hands it to the ticket assigner to
  break into a dependency-ordered ticket DAG, then spawns claude worker instances
  on as many tickets in parallel as their dependencies allow. It runs on a /loop
  cron, checking each worker every tick — on target, complete, clean, non-
  duplicative — and sends steering nudges when one drifts. When all tickets are
  done it optionally runs the validation agent (evals + code quality), lands the
  work to main, and self-terminates its own /loop cron. Use when the user says
  "/hq-orchestrate", "start an agent workflow", "orchestrate agents on this
  goal", "spin up a supervised swarm", or "run a supervisor over these tickets".
---

# /hq-orchestrate

The client-side **driver** for a supervised agent workflow. Where
[[hq-optimize-agent]] runs a single refine loop in-session and [[hq-endforge]]
distills one slice into one agent, this skill stands up a whole **supervisor →
ticket-assigner → parallel workers → validation** pipeline and babysits it to
completion. It complements the Command HQ **Workflows** catalog (a `workflow` is
a DAG of catalog agents — `nodes` with `dependsOn` edges and optional `rerun`
loops, `packages/shared/src/dto.ts` `workflowSchema`): that catalog stores a
*static* DAG; this skill *runs* a **dynamic** one whose tickets the ticket-
assigner agent generates at launch.

## Where this runs

In the developer's claude+ session (the PTY), inside a connected repo, on the
developer's own subscription. It uses:

- the **agents REST** (the same device/session bearer token the other HQ skills
  use) to load the chosen agents' prompts — `GET /agents/<name>?tier=<t>&id=<id>`;
- **`/loop`** for the supervisor's recurring checkpoint cadence (one cron job, on
  opus), which it **cancels itself** when the workflow finishes;
- **background claude workers** (claude+ spawns) — one per in-flight ticket, each
  in its own **`git worktree`** off main so parallel edits never collide;
- the steering / send-message path to **nudge** a running worker mid-flight;
- the developer's own `git` to land finished work to main.

No Bedrock, no server-side model call — every supervisor judgement and every
worker is Claude Code reasoning, here, on the subscription. The backend only
**stores** agents/workflows; it never executes them.

## Inputs it gathers

Gather these up front; ask only for the ones missing.

- **Supervisor agent (required).** The orchestrator that drives everything. Load
  it with `GET /agents/<name>`; **force its model to opus** for this run
  regardless of the stored `model` (the supervisor needs the strongest reasoning
  for steering judgements). Default scope to the caller's user scope; ask if the
  name is ambiguous.
- **Goal (required) — one of two forms:**
  - **Typed goal** — a sentence/paragraph the user provides directly; OR
  - **State-assessor agent** — an agent name. Run it first; it inspects the
    project state (the repo, `docs/PRD.html` / `docs/plans/`, open work, the HQ
    project) and returns `{ goal, moreWorkNeeded, rationale }`. If
    `moreWorkNeeded` is false, **report that and stop** — there is nothing to
    orchestrate.
- **Ticket-assigner agent (required).** Turns the goal into concrete tickets and
  the dependency edges between them. Load via `GET /agents/<name>`.
- **Validation agent (optional).** If given, it runs end-to-end checks (evals +
  code-quality) after the work lands-ready. If omitted, the validation step is
  skipped.
- **Parallelism cap (optional).** Max concurrent workers (default 4) so the
  swarm stays observable.

## Steps

1. **Resolve the project & load the agents.** Resolve `$CLAUDE_PLUS_PROJECT_ID`
   (the injected contract — read it, never guess). `GET` the supervisor, ticket-
   assigner, and (if given) validation + state-assessor agents at their scopes;
   a 404 means an unreadable scope or missing agent — stop and report which one.
   Capture each agent's `prompt`, `skills`, `tools`.

2. **Establish the goal.** If the goal was typed, use it verbatim. Otherwise run
   the **state-assessor** agent and take its `goal`; if it reports no more work
   is needed, stop here.

3. **Start the supervisor on opus + open the /loop.** Begin the supervisor as the
   driving context (its loaded prompt + the goal, model forced to opus). Register
   its checkpoint cadence as a **single `/loop` cron job** (e.g. `/loop 3m
   /hq-orchestrate --resume <runId>`), and record the run state (goal, agents,
   tickets, worktrees, cron id) in a local marker — e.g.
   `.claude/orchestrate/<runId>.json` — so each loop tick resumes the same run.

4. **Assign tickets (supervisor → ticket-assigner).** The supervisor hands the
   goal to the **ticket-assigner** agent, which returns a **ticket DAG**: each
   ticket `{ id, title, prompt, dependsOn: [ids] }`. This is exactly the
   `workflowSchema` node shape (`id` / `prompt` / `dependsOn`, acyclic) — validate
   it the same way (unique ids, every `dependsOn` resolves, no cycle) and reject /
   ask for a re-plan if it doesn't. **Optional:** persist the plan as a catalog
   Workflow (the same record [[hq-add-workflow]] registers) via `POST /workflows`
   (`{ name, nodes }`) so the run is reviewable in HQ; skip if the user doesn't
   want it saved.

5. **Spawn workers by dependency order.** Compute the **ready set** — tickets
   whose `dependsOn` are all already done — and, up to the parallelism cap, spawn
   one **background claude worker per ready ticket**, each in its own
   `git worktree add ../wt-<ticketId> -b work/<ticketId> main` with the ticket's
   `prompt` as its task. Tickets with unmet deps wait.

6. **Supervise every loop tick — check, then nudge.** On each `/loop` wake, for
   every in-flight worker the supervisor reads the worker's progress (its output /
   diff) and judges it against the high-level bar:
   - **On target** — still solving its ticket, not drifting into scope creep;
   - **Complete** — not missing requirements / edge cases the ticket named;
   - **Clean** — readable, follows repo conventions, no dead code;
   - **Non-duplicative** — not re-implementing something that already exists
     (point it at the existing code instead);
   - **Anything else high-level** — correct ordering, foundation-first, known
     dead-ends avoided.
   When a worker drifts, **send it a short steering nudge** (a targeted message to
   the running worker — "you're duplicating `db/repo.ts`'s `putSkill`; reuse it",
   "the ticket also needs the 404 path"), not a full restart. Mark a worker's
   ticket **done** when its work satisfies the bar, then **unblock and spawn**
   any tickets whose deps are now all met (back to step 5). Keep the marker file
   current each tick.

7. **Optional validation.** When **all** tickets are done, if a validation agent
   was given, run it end-to-end (evals + code-quality across the combined work).
   If it surfaces real problems, the supervisor reopens the affected tickets
   (spawns fix-workers, step 5) and loops again; otherwise proceed.

8. **Land to main + self-terminate.** Once everything is done (and validated, if
   a validator ran), land each worktree branch onto main with the developer's own
   `git` (commit if needed, then `git switch main && git merge --ff-only
   work/<id>` or the repo's landing convention), push, and remove the worktrees.
   Then **cancel the supervisor's own `/loop` cron job** and clear the marker —
   the run is over and nothing keeps waking. Print a final summary: tickets
   completed, validation verdict, the merge SHAs.

## Resume (each /loop tick)

`/hq-orchestrate --resume <runId>` reloads `.claude/orchestrate/<runId>.json`
and re-enters at step 6: read workers, judge, nudge, advance the DAG, spawn newly
ready tickets, and — when the DAG is fully done — run validation, land, and
cancel its own cron. The marker is the single source of truth so a tick never
double-spawns a ticket already in flight.

## Worked dry-run example (against THIS repo)

```
/hq-orchestrate --supervisor swarm-lead --assigner ticketer \
  --validate evals-runner --goal "add a /workflows tab to the web app"
```

1. Resolves the project; `GET`s `swarm-lead` (model forced to opus), `ticketer`,
   `evals-runner`.
2. Goal is typed → used verbatim (no state-assessor run).
3. Starts `swarm-lead` on opus; opens `/loop 3m /hq-orchestrate --resume r-7f3`;
   writes `.claude/orchestrate/r-7f3.json`.
4. `ticketer` returns 4 tickets: `dto` (no deps), `rest` (deps: `dto`), `web-list`
   (deps: `rest`), `web-detail` (deps: `rest`). DAG validates; saved as a
   `POST /workflows { name:"workflows-tab", nodes }` record for review.
5. Ready set = `{dto}` → spawn one worker in `../wt-dto` on `work/dto`.
6. Tick 1: `dto` worker is on target → let it run. Tick 2: `dto` done → unblock
   `rest`, spawn it. Tick 3: `rest` worker is re-deriving a key scheme that
   already lives in `db/keys.ts` → nudge "reuse `workflowKey` from db/keys.ts,
   don't invent one". Tick 4: `rest` done → `web-list` + `web-detail` both ready
   → spawn both (cap 4). … workers finish.
7. All tickets done → run `evals-runner` end-to-end; it passes.
8. Land `work/dto`, `work/rest`, `work/web-list`, `work/web-detail` to main,
   push, remove worktrees, **cancel the `/loop r-7f3` cron**, clear the marker,
   and print the summary.

(Counter-example for step 2: had `--goal` been replaced by `--assess
state-checker` and the assessor returned `moreWorkNeeded:false`, the skill would
print "state-checker reports the goal is already met — nothing to orchestrate"
and stop before opening any loop.)

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: it loads the
supervisor / ticket-assigner / (optional) validation agents from the agents REST,
forces the supervisor to opus, establishes the goal (typed or via a state-assessor
that can short-circuit when no work is needed), assigns a dependency-ordered
ticket DAG, spawns parallel workers in per-ticket worktrees up to the cap,
supervises them on a self-cancelling `/loop` cron (checking on-target /
complete / clean / non-duplicative and sending nudges, never silent restarts),
optionally runs validation, lands the work to main, and cancels its own cron when
done. It never leaves a dangling loop or worktree after a completed run.
