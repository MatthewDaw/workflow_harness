---
name: startforge
description: >-
  Open an AgentForge capture boundary inside the claude+ PTY. It lands the
  current branch's work to main (commit + push) for a clean baseline, then
  creates a fresh worktree + branch off main and records the start marker (base
  commit) that /endforge will diff against. If the work can't be landed cleanly
  it ABORTS with an explanatory warning and makes no changes. Pair with
  /endforge. Use when the user says "/startforge", "start a forge", "begin
  capturing this work", or "mark this slice for an agent".
---

# /startforge

The first half of the AgentForge v1 command pair (distiller-first, per
`docs/plans/command-hq/05-agentforge.md`). It brackets a slice of work so
`/endforge` can distill it into a reusable agent. This half only opens the
boundary; it never drafts or registers anything.

## When this runs

In the developer's claude+ session, inside a connected git repo. It shells out
to `git` (and `gh` if push needs auth). claude+ already captures the session
(transcript + skills used) — `/startforge` adds no new capture mechanism; it
just records where the slice begins.

## Steps (F1 in 05-agentforge.md; R1–R6)

1. **Description (R1).** Accept `/startforge [what you're working on]`. If
   absent, ask the user what they're about to work on. Keep the answer as the
   forge's working description (feeds `/endforge`'s distillation).
2. **Name (R2).** Use a user-provided name if given; otherwise generate a slug
   from the description and ask the user to accept or change it.
3. **Land current work to main (R3).** Commit any pending work on the current
   branch and push it so main is the clean baseline:
   - `git add -A && git commit` (skip if clean), then bring main current and
     land the branch onto it (`git switch main && git pull && git merge --ff-only`
     or a push of the branch + merge — match the repo's landing convention).
4. **Abort cleanly if unlandable (R4).** If there are merge conflicts, unrelated
   WIP, or anything that would force a messy land, **abort**: make no branch, no
   worktree, no commit beyond what the user already had. Print a warning that
   lists exactly what blocked the land (conflicting files, dirty unrelated
   changes) and stop. Leave the repo as it was found.
5. **Branch + worktree off main (R5).** Create a fresh branch off the now-clean
   main and a dedicated worktree for the forge work (so the slice is isolated):
   `git worktree add ../forge-<name> -b forge/<name> main`.
6. **Record the start marker (R5, R6).** Persist the base commit SHA (the main
   tip the branch forked from) plus the description + name as the forge's start
   marker — written to a small local marker file in the worktree (e.g.
   `.claude/forge/<name>.json`: `{ name, description, baseСommit, startedAt }`).
   `/endforge` reads this to compute the diff `baseCommit..HEAD`.

The user then does the work in the forge worktree (F2 — no new action).

## Worked dry-run example (against THIS repo)

```
/startforge "skill-authoring flow: write SKILL.md files for client commands"
```

1. Description captured; name generated → `skill-authoring-flow`, user accepts.
2. Current branch has the four new SKILL.md files committed; main is clean and
   fast-forwardable → lands them to main and pushes.
3. (Counter-example for R4: if instead there were conflicting unrelated WIP, it
   would print "Aborting forge: 2 unrelated dirty files (foo.ts, bar.ts) block a
   clean land. Commit or stash them, then re-run." and make no changes.)
4. `git worktree add ../forge-skill-authoring-flow -b forge/skill-authoring-flow main`.
5. Writes `.claude/forge/skill-authoring-flow.json` with the base commit SHA.
6. Prints: "Forge 'skill-authoring-flow' open. Base <sha>. Work in
   ../forge-skill-authoring-flow, then run /endforge."

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: with a
cleanly landable branch it lands to main, creates the worktree/branch, and
writes a start marker; with unlandable WIP it aborts with a warning and changes
nothing.
