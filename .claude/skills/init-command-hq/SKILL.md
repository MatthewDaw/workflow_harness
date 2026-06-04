---
name: init-command-hq
description: >-
  Run inside the claude+ PTY to scaffold the GitHub-side files Command HQ reads
  for a project: the high-level `PRD.md` (Project Overview goal + Supporting
  Outcomes), a headline `docs/plans/` requirements doc with `completion:`
  frontmatter (Detailed Requirements tab + the Project Requirements progress
  bar), and a legacy `PROGRESS.md` fallback. It writes placeholder files only
  where they are missing — it never overwrites existing content — then commits
  and pushes them with the developer's own git/gh so a freshly connected repo
  stops showing the "needs files" / 0% empty state. Use when the user says
  "/init-command-hq", "init command hq", "scaffold the HQ files", "set up
  PRD.md", or connects a repo that has no PRD/plan docs yet.
---

# /init-command-hq

Bootstrapper for the **GitHub side** of a Command HQ project. HQ reads a repo
through a read-only GitHub App; if the files it expects are absent, the Project
Overview, Project Requirements, and Detailed Requirements tabs render their
empty states (`missingFiles: ["PRD.md", "PROGRESS.md"]`, no requirement docs,
bar at `0%`). This skill creates **placeholders** for those files so a new repo
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
| **Project Requirements** (bar) | headline `docs/plans/**/*.md` | top doc's `completion:` frontmatter → `progressPct`. Falls back to `PROGRESS.md` `N%`, then `0`. |
| **Detailed Requirements** | `docs/plans/**/*.md` | doc tree + per-doc `completion:` frontmatter badge; selecting a doc serves its raw markdown. |
| (legacy fallback) | `PROGRESS.md` (repo root) | first `N%` → progress when no plan-doc frontmatter exists. |

**Not a repo file:** the **Project Requirements tab _body_** (the high-level
requirements prose) is **HQ-owned** — stored in DynamoDB, edited inline in HQ
via `PUT /projects/:id/requirements`. This skill cannot and does not create it;
it tells the user to fill that in inside Command HQ.

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

3. **Scaffold the headline plan doc** `docs/plans/overview.md` **only if no
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

   (`completion: 0` is the placeholder; `/update-progress` overwrites it with the
   computed number on later runs.)

4. **Scaffold `PROGRESS.md`** (repo root) **only if missing** — the legacy
   fallback so progress resolves even before any plan-doc frontmatter is trusted:

   ```markdown
   # Progress

   0% complete
   ```

5. **Report what was created vs. skipped**, then **commit + push.** Stage only
   the files this skill created. Commit with a message like
   `chore(hq): scaffold Command HQ project files via /init-command-hq` (include
   the repo's Co-Authored-By trailer). Push to the current branch's upstream with
   `git push` (use `gh` only if auth/PR is needed). Never use the GitHub Contents
   API — push with the dev's own git so HQ's next read sees the new SHA.
6. **Tell the user the two manual follow-ups:**
   - Fill in the **Project Requirements body inside Command HQ** (HQ-owned, not
     in the repo).
   - Replace the placeholder goal / requirements text, then run
     **[[update-progress]]** to compute real `completion:` numbers.

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App,
finds `PRD.md` (goal + SOs), the `docs/plans/` tree, and the `completion:`
frontmatter, and the three tabs leave their empty states. HQ never writes back.

## Worked example (against a fresh repo)

```
/init-command-hq
```

Expected behavior on a repo with no HQ files:

1. `git rev-parse --show-toplevel` → repo root confirmed.
2. No `PRD.md` → write the placeholder (heading + `Goal:` + `SO-1`).
3. No `docs/plans/**/*.md` → create `docs/plans/overview.md` with
   `completion: 0` frontmatter.
4. No `PROGRESS.md` → write `0% complete`.
5. Print:

   ```
   init-command-hq — scaffolded:
     created  PRD.md
     created  docs/plans/overview.md   (completion: 0)
     created  PROGRESS.md
     skipped  (none already present)

   Next:
     - Edit Project Requirements body in Command HQ (HQ-owned, not in the repo).
     - Replace placeholder text, then run /update-progress for real numbers.
   ```

6. `git add PRD.md docs/plans/overview.md PROGRESS.md && git commit -m "chore(hq):
   scaffold Command HQ project files via /init-command-hq" && git push`.

On a repo that already has, say, `PRD.md` and a `docs/plans/` tree, it reports
`skipped PRD.md`, `skipped docs/plans/** (N docs present)`, and only creates the
genuinely-missing files (or nothing, committing nothing).

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running it
in a repo with no HQ files: it creates `PRD.md`, `docs/plans/overview.md` (with
`completion: 0`), and `PROGRESS.md`, pushes them, and re-running it creates
nothing (idempotent). After an HQ refresh the Overview shows the goal, the
Detailed Requirements tab lists the headline doc, and the bar reads `0%` instead
of the empty state.
