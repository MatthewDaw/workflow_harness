---
name: hq-init-command-hq
description: >-
  Run inside the claude+ PTY to scaffold the GitHub-side files Command HQ reads
  for a project: the repo-root `PRD.md` (Project Overview goal + Supporting
  Outcomes), `docs/PRD.md` (the Project Requirements body + bar, with
  `completion:` frontmatter), a headline `docs/plans/` requirements doc with
  `completion:` frontmatter (Detailed Requirements tab + bar), and a legacy
  `PROGRESS.md` fallback. It writes placeholder files only
  where they are missing — it never overwrites existing content — then commits
  and pushes them with the developer's own git/gh so a freshly connected repo
  stops showing the "needs files" / 0% empty state. Use when the user says
  "/hq-init-command-hq", "init command hq", "scaffold the HQ files", "set up
  PRD.md", or connects a repo that has no PRD/plan docs yet.
---

# /hq-init-command-hq

Bootstrapper for the **GitHub side** of a Command HQ project. HQ reads a repo
through a read-only GitHub App; if the files it expects are absent, the Project
Overview, Project Requirements, and Detailed Requirements tabs render their
empty states (`missingFiles: ["PRD.md", "PROGRESS.md"]`, no `docs/PRD.md` body,
no requirement docs, bar at `0%`). This skill creates **placeholders** for those
files so a new repo
shows up cleanly, then hands off to [[update-progress]] (which computes and
pushes the real `completion:` numbers later).

It is the **complement** of [[update-progress]]: this one creates the files,
that one keeps their numbers current. Neither calls an HQ endpoint — both push
to GitHub and HQ pulls on refresh.

## When this runs

In the developer's claude+ session (the PTY), from inside a connected repo. It
shells out to `git` and `gh` with the developer's own credentials. It is
**idempotent and non-destructive**: it only writes a file that does not already
exist, and prints what it skipped.

## What each tab reads (the contract this skill satisfies)

The backend parsing lives in `packages/backend/src/github/history.ts`; these are
the exact files and formats it looks for.

| HQ tab | Repo file(s) | What's parsed |
| --- | --- | --- |
| **Project Overview** | `PRD.md` (repo root) | `Goal:` line (or first `#` heading) → the goal; `SO-…` tokens → Supporting Outcome ids. Absent → listed in `missingFiles`. |
| **Project Requirements** (body **and** bar) | `docs/PRD.md` | the high-level requirements prose → the read-only body; `completion:` frontmatter → `progressPct`. Falls back (bar only) to the headline `docs/plans/**/*.md` `completion:`, then `PROGRESS.md` `N%`, then `0`. |
| **Detailed Requirements** | `docs/plans/**/*.md` | doc tree + per-doc `completion:` frontmatter badge; selecting a doc serves its raw markdown. |
| (legacy fallback) | `PROGRESS.md` (repo root) | first `N%` → progress when no plan-doc frontmatter exists. |

**Two different PRD files — do not confuse them:** the repo-root `PRD.md` feeds
the **Project Overview** tab (goal + `SO-…` ids). `docs/PRD.md` is a **separate
file** that feeds the **Project Requirements** tab (its body **and** its bar).
Both are repo files read read-only from GitHub — neither is HQ-owned. This skill
scaffolds both where missing.

## Steps

1. **Confirm the repo root.** Run from the top of the connected git repo (where
   `PRD.md` belongs). Verify with `git rev-parse --show-toplevel`.
2. **Scaffold `PRD.md`** (repo root) **only if missing.** Write a placeholder
   whose first heading is the project name and which contains an explicit
   `Goal:` line and a Supporting Outcomes section with at least one `SO-…` id,
   so `parsePrd` extracts a goal + outcome ids:

   ```markdown
   # <Project Name>

   Goal: <one sentence describing the outcome this project drives>

   ## Supporting Outcomes

   - SO-1: <the first supporting outcome this project owns>
   ```

3. **Scaffold `docs/PRD.md`** (the **Project Requirements** source — distinct
   from the repo-root `PRD.md`) **only if missing.** Both its body and its bar
   come from this file. It MUST start with a `completion:` frontmatter block so
   the Project Requirements bar reads `0%` cleanly, followed by a placeholder
   high-level requirements body:

   ```markdown
   ---
   completion: 0
   ---

   # <Project Name> — Project Requirements

   The high-level requirements management writes for this project — the outcome-
   level "what", not the detailed breakdown. Each bullet ladders up to a
   Supporting Outcome and is expanded under the Detailed Requirements tab
   (`docs/plans/**/*.md`).

   ## Requirements

   - <first high-level requirement>
   ```

   (`completion: 0` is the placeholder; `/hq-update-progress` overwrites it with
   the computed number on later runs.)

4. **Scaffold the headline plan doc** `docs/plans/overview.md` **only if no
   `docs/plans/**/*.md` exists.** This is the doc HQ treats as the headline (top
   of the tree) for both the Project Requirements bar and the Detailed
   Requirements root. It MUST start with a `completion:` frontmatter block so the
   bar reads `0%` cleanly instead of falling through:

   ```markdown
   ---
   completion: 0
   ---

   # <Project Name> — Requirements

   High-level requirements for this project. Break detailed work into additional
   `docs/plans/**/*.md` files; each gets its own `completion:` frontmatter and
   appears under the Detailed Requirements tab.

   ## Requirements

   - [ ] <first requirement>
   ```

   (`completion: 0` is the placeholder; `/hq-update-progress` overwrites it with the
   computed number on later runs.)

5. **Scaffold `PROGRESS.md`** (repo root) **only if missing** — the legacy
   fallback so progress resolves even before any plan-doc frontmatter is trusted:

   ```markdown
   # Progress

   0% complete
   ```

6. **Report what was created vs. skipped**, then **commit + push.** Stage only
   the files this skill created. Commit with a message like
   `chore(hq): scaffold Command HQ project files via /hq-init-command-hq` (include
   the repo's Co-Authored-By trailer). Push to the current branch's upstream with
   `git push` (use `gh` only if auth/PR is needed). Never use the GitHub Contents
   API — push with the dev's own git so HQ's next read sees the new SHA.
7. **Tell the user the manual follow-up:** replace the placeholder goal /
   requirements text in `PRD.md`, `docs/PRD.md`, and `docs/plans/overview.md`,
   then run **[[update-progress]]** to compute real `completion:` numbers.

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App,
finds `PRD.md` (goal + SOs), `docs/PRD.md` (Project Requirements body + bar), the
`docs/plans/` tree, and the `completion:` frontmatter, and the three tabs leave
their empty states. HQ never writes back.

## Worked example (against a fresh repo)

```
/hq-init-command-hq
```

Expected behavior on a repo with no HQ files:

1. `git rev-parse --show-toplevel` → repo root confirmed.
2. No `PRD.md` → write the placeholder (heading + `Goal:` + `SO-1`).
3. No `docs/PRD.md` → write the Project Requirements placeholder with
   `completion: 0` frontmatter + a high-level requirements body.
4. No `docs/plans/**/*.md` → create `docs/plans/overview.md` with
   `completion: 0` frontmatter.
5. No `PROGRESS.md` → write `0% complete`.
6. Print:

   ```
   init-command-hq — scaffolded:
     created  PRD.md
     created  docs/PRD.md              (completion: 0)
     created  docs/plans/overview.md   (completion: 0)
     created  PROGRESS.md
     skipped  (none already present)

   Next:
     - Replace placeholder text in PRD.md, docs/PRD.md, and docs/plans/overview.md,
       then run /hq-update-progress for real numbers.
   ```

7. `git add PRD.md docs/PRD.md docs/plans/overview.md PROGRESS.md && git commit -m
   "chore(hq): scaffold Command HQ project files via /hq-init-command-hq" &&
   git push`.

On a repo that already has, say, `PRD.md` and a `docs/plans/` tree, it reports
`skipped PRD.md`, `skipped docs/plans/** (N docs present)`, and only creates the
genuinely-missing files (or nothing, committing nothing).

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running it
in a repo with no HQ files: it creates `PRD.md`, `docs/PRD.md` (with
`completion: 0`), `docs/plans/overview.md` (with `completion: 0`), and
`PROGRESS.md`, pushes them, and re-running it creates nothing (idempotent). After
an HQ refresh the Overview shows the goal, the Project Requirements tab renders
`docs/PRD.md` read-only, the Detailed Requirements tab lists the headline doc,
and the bars read `0%` instead of the empty state.
