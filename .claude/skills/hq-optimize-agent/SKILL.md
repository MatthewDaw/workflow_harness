---
name: hq-optimize-agent
description: >-
  Run inside the claude+ PTY to refine an existing agent's system prompt using
  Claude Code itself — no external model API, no Bedrock; it runs on the
  developer's own subscription. It loads the agent from Command HQ, runs an
  iterative refine → self-judge → keep-best loop (scoring specificity,
  groundedness, coverage, actionability), optionally in a fresh git worktree,
  shows the trajectory + best prompt, and on confirmation writes the improved
  prompt back to HQ via the existing agents REST at the agent's own scope. Use
  when the user says "/hq-optimize-agent", "optimize an agent", "refine this
  agent's prompt", or "tighten my agent prompt".
---

# /hq-optimize-agent

The client-side replacement for the (removed) server-side AgentForge prompt
optimizer. Where the old path ran a Bedrock-backed generate/judge loop inside a
Lambda, this skill does the **same refine loop directly in this claude+ session
using Claude Code** — the developer's subscription. The backend no longer
optimizes anything; it only **stores** the result via the agents registry REST.

## Where this runs

In the developer's claude+ session (the PTY), inside a connected repo. It uses
the **same authenticated HTTP transport the other HQ skills use** (the
device/session bearer token already established for this claude+ instance) to
read and write agents. No model keys, no Bedrock, no external API — Claude Code
generates and scores prompt candidates itself.

## Inputs it gathers

- **Target agent (required).** The agent to refine, identified by `name` and its
  explicit scope `{ tier, id }` (org / user / project). Ask the user which agent
  if not given; default the scope to the caller's own user scope.
- **Grounding context (auto).** The agent's stored `description`/name, its
  `skills[]` (what the prompt must cover), and any evidence the user supplies
  (e.g. a representative session transcript, the relevant repo files). Coverage
  and groundedness are scored against this context.
- **Budget (optional).** `maxRounds` (default 4) and `minImprovement` (default
  1.0, on the 0–100 scale) — the plateau / early-stop threshold.

## Steps

1. **Load the agent (read).** GET the agent at its explicit scope:
   `GET /agents/{name}?tier=<tier>&id=<id>` with the bearer token. A 404 means
   the scope is unreadable or the agent does not exist — stop and report.
   Capture `prompt` (the starting point), `model`, `skills`, `name`.
2. **Optionally isolate in a worktree.** If the user wants the refinement kept
   off their working checkout (e.g. to diff candidates or stash scratch files),
   create a fresh git worktree + branch off the current HEAD and run the loop
   there. This is optional — the loop itself touches no repo files, so a worktree
   is only for the user's own scratch/notes. Clean it up when done.
3. **Run the refine → self-judge → keep-best loop (Claude Code, in-session).**
   This mirrors the deleted backend loop, but every `generate` and `judge` is
   Claude Code reasoning here — no network model call:
   - **Round 0 (baseline):** self-judge the starting prompt, scoring 0–100 on
     four axes — **specificity** (concrete, not vague filler), **groundedness**
     (anchored in the evidence/skills, no invented capability), **coverage**
     (addresses every skill the agent declares), **actionability** (tells the
     agent what to do, in what order). Record the score + a critique of what to
     fix. This baseline is the floor — never return worse than it.
   - **Each refinement round (up to `maxRounds`):** rewrite the current best
     prompt to address its critique, staying grounded in the evidence and
     covering the skills; avoid generic AI-slop. Self-judge the candidate on the
     same four axes. **Keep the candidate only if it scores strictly higher.**
   - **Early-stop:** if a round improves the best by less than `minImprovement`
     (a plateau, including a regression), stop.
   - Keep a `history` of `{ round, score, critique }` — the trajectory.
4. **Show the result.** Present the best prompt found, its score, and the
   round-by-round trajectory (so the user sees the gradient, not just the final
   text). Make clear it is **not yet saved**.
5. **Write back on confirmation (store-only).** Only if the user confirms,
   persist the improved prompt by re-saving the agent at its **own scope** via
   the existing agents REST — re-send the full agent record with the new
   `prompt` (other fields unchanged):
   - `PUT /agents/{name}` (or `POST /agents`; the handler upserts) with body
     `{ name, scope: { tier, id }, model, prompt: <best>, skills, tools }`
     (the `agentSchema` shape) and the bearer token.
   `canWriteScope` gates the write: a user may write their own user/project
   scope without admin; an **org-scope** write requires the `custom:admin` claim.
   If the caller cannot write the agent's scope, report it and leave HQ
   unchanged (offer to save a copy at the caller's user scope instead).
6. **Confirm.** Print the agent's HQ URL and the stored score so the change is
   visible. The refined prompt reaches consumers through the existing
   config-sync drift/pull.

## Self-judge rubric (the four axes, 0–100 each → overall)

- **Specificity** — concrete instructions and constraints; penalize vague filler
  ("be helpful", "do a good job") and hedging.
- **Groundedness** — every claim/capability is supported by the evidence or the
  declared skills; penalize invented tools or unsupported scope.
- **Coverage** — the prompt addresses every skill the agent declares; penalize
  silent gaps.
- **Actionability** — the agent knows what to do and in what order (foundation
  first, correct ordering, known dead-ends avoided); penalize a prompt that only
  describes, never directs.

Average (or weight to taste) into a single 0–100 the loop optimizes against.
The optimizer never regresses below the baseline and returns the best variant
seen.

## Worked dry-run example (against THIS repo)

```
/hq-optimize-agent builder --scope user:matt
```

1. `GET /agents/builder?tier=user&id=matt` → prompt "do stuff", skills
   `[gh, browse]`, model `claude-sonnet-4`.
2. (User declines a worktree — the loop touches no files.)
3. Round 0 self-judge: 42 ("vague; no ordering; doesn't reference gh/browse").
   Round 1 rewrite + judge: 68 (kept). Round 2: 81 (kept). Round 3: 82 — gain
   < 1.0 → plateau, stop. Best = the round-2 prompt, score 81.
4. Show the 42 → 68 → 81 trajectory and the best prompt; ask to save.
5. User confirms → `PUT /agents/builder` with the improved prompt at
   `{ tier: user, id: matt }` (other fields unchanged).
6. Print: "Saved 'builder' (score 81). Open HQ → Agents → builder to review."

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: it reads an
agent from HQ, runs the in-session refine→judge→keep-best loop entirely on the
Claude Code subscription (no Bedrock / no external API), never returns a prompt
worse than the baseline, and on confirmation re-saves the agent at its own scope
via the existing `PUT/POST /agents` REST (org writes admin-gated). It never
writes without confirmation.
