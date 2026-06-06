---
name: hq-init
description: >-
  Run inside the claude+ PTY to scaffold the three GitHub-side files Command HQ
  reads for a project: `docs/PRD.html` (the Project Overview goal + Project
  Requirements body + bar, with `completion:` frontmatter and a `Product goal:`
  line), `docs/plans/specs_overview.html` (the Detailed Requirements headline,
  with `completion:` frontmatter), and `docs/plans/features/example_feature.html`
  (a template feature doc). It writes placeholder files only where they are
  missing — it never overwrites existing content — then commits and pushes them
  with the developer's own git/gh so a freshly connected repo stops showing the
  "needs files" / 0% empty state. Use when the user says "/hq-init", "init",
  "scaffold the HQ files", or connects a repo that has no PRD/plan docs yet.
---

# /hq-init

Bootstrapper for the **GitHub side** of a Command HQ project. HQ reads a repo
through a read-only GitHub App; if the files it expects are absent, the Project
Overview, Project Requirements, and Detailed Requirements tabs render their
empty states (no goal, no Project Requirements body, no requirement docs, bar at
`0%`). This skill creates **placeholders** for exactly three files so a new repo
shows up cleanly, then hands off to [[hq-update-progress]] (which computes and
pushes the real `completion:` numbers later).

It is the **complement** of [[hq-update-progress]]: this one creates the files,
that one keeps their numbers current. Neither calls an HQ endpoint — both push
to GitHub and HQ pulls on refresh.

## When this runs

In the developer's claude+ session (the PTY), from inside a connected repo. It
shells out to `git` and `gh` with the developer's own credentials. It is
**idempotent and non-destructive**: it only writes a file that does not already
exist, and prints what it skipped.

## The three files (and nothing else)

This skill scaffolds **only** these three files. There is no repo-root `PRD.md`
and no `PROGRESS.md` — everything HQ needs lives under `docs/`.

| HQ surface | Repo file | What's parsed |
| --- | --- | --- |
| **Project Overview** (goal) | `docs/PRD.html` | the `Product goal:` line (markdown `**Goal:**` or an HTML `<strong>Product goal:</strong>` label) → the goal shown on the project card. |
| **Project Requirements** (body **and** bar) | `docs/PRD.html` | the high-level requirements prose → the read-only body; `completion:` frontmatter → `progressPct`. |
| **Detailed Requirements** (headline + bar fallback) | `docs/plans/specs_overview.html` | the top-of-tree overview doc; its `completion:` frontmatter is the Detailed Requirements headline number. |
| **Detailed Requirements** (per-feature) | `docs/plans/features/*.html` | each feature doc + its `completion:` frontmatter badge; `example_feature.html` is the seed template. |

The backend parsing lives in `packages/backend/src/github/history.ts`
(`parseGoal`, `parseCompletionFrontmatter`) and `app.ts`
(`readFramingWithCompletion`, which reads `docs/PRD.{md,html}` for both the goal
and the bar). HQ reads `.md` or `.html`; we scaffold `.html`.

## Steps

1. **Confirm the repo root.** Run from the top of the connected git repo. Verify
   with `git rev-parse --show-toplevel`, and create `docs/` and
   `docs/plans/features/` if they don't exist.

2. **Scaffold `docs/PRD.html`** (the Project Overview goal **and** the Project
   Requirements body + bar) **only if missing.** It MUST start with a
   `completion:` frontmatter block (so the bar reads `0%` cleanly) and contain an
   explicit `Product goal:` line (so `parseGoal` populates the card goal),
   followed by a placeholder requirements body:

   ```html
   ---
   completion: 0
   ---

   <h1><Project Name> — Project Requirements</h1>
   <p><strong>Product goal:</strong> <one sentence describing the outcome this
   project drives>.</p>
   <p>This document is the high-level <strong>Project Requirements</strong> for the
   product: the outcomes management expects, not how they are built. The detailed
   breakdown lives under <a href="./plans/specs_overview.html"><code>docs/plans/</code></a>.</p>
   <h2>Required outcomes</h2>
   <ul>
     <li><strong><first outcome>.</strong> <what it means>.</li>
   </ul>
   ```

   (`completion: 0` is the placeholder; `/hq-update-progress` overwrites it with
   the computed number on later runs.)

3. **Scaffold `docs/plans/specs_overview.html`** (the Detailed Requirements
   **headline** — the top of the `docs/plans/` tree) **only if missing.** It MUST
   start with a `completion:` frontmatter block so the headline bar reads `0%`
   instead of falling through:

   ```html
   ---
   status: active
   type: overview
   completion: 0
   ---

   <h1><Project Name> — Specs Overview</h1>
   <p>The map of this project's detailed requirements. Each feature is broken out
   into its own doc under <code>docs/plans/features/</code>; each carries its own
   <code>completion:</code> frontmatter and appears under the Detailed
   Requirements tab.</p>
   <h2>Features</h2>
   <ul>
     <li><a href="./features/example_feature.html">example_feature.html</a> — <one-line summary></li>
   </ul>
   ```

4. **Scaffold `docs/plans/features/example_feature.html`** (the seed feature
   doc) **only if no `docs/plans/features/*.html` already exists.** It MUST start
   with a `completion:` frontmatter block:

   ```html
   ---
   status: active
   type: feature
   completion: 0
   feature: example-feature
   ---

   <h1>Example Feature</h1>
   <p>Replace this with a real feature. Copy this file per feature
   (<code>docs/plans/features/&lt;name&gt;.html</code>); each gets its own
   <code>completion:</code> frontmatter and shows up under Detailed Requirements.</p>
   <h2>Requirements</h2>
   <ul>
     <li><first requirement for this feature></li>
   </ul>
   ```

5. **Report what was created vs. skipped**, then **commit + push.** Stage only
   the files this skill created. Commit with a message like
   `chore(hq): scaffold Command HQ project files via /hq-init` (include the
   repo's Co-Authored-By trailer). Push to the current branch's upstream with
   `git push` (use `gh` only if auth/PR is needed). Never use the GitHub Contents
   API — push with the dev's own git so HQ's next read sees the new SHA.

6. **Tell the user the manual follow-up:** replace the placeholder goal /
   requirements text in `docs/PRD.html`, the feature list in
   `docs/plans/specs_overview.html`, and the seed `example_feature.html` (copy it
   per real feature), then run **[[hq-update-progress]]** to compute real
   `completion:` numbers.

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App,
reads `docs/PRD.html` (goal via `parseGoal` + Project Requirements body + bar via
`completion:`), and walks the `docs/plans/` tree (`specs_overview.html` headline +
`features/*.html`), so the three surfaces leave their empty states. HQ never
writes back.

## Worked example (against a fresh repo)

```
/hq-init
```

Expected behavior on a repo with no HQ files:

1. `git rev-parse --show-toplevel` → repo root confirmed; `docs/plans/features/`
   created.
2. No `docs/PRD.html` → write the placeholder (`completion: 0` frontmatter +
   `Product goal:` line + requirements body).
3. No `docs/plans/specs_overview.html` → write the headline overview with
   `completion: 0`.
4. No `docs/plans/features/*.html` → write `example_feature.html` with
   `completion: 0`.
5. Print:

   ```
   hq-init — scaffolded:
     created  docs/PRD.html                              (completion: 0)
     created  docs/plans/specs_overview.html             (completion: 0)
     created  docs/plans/features/example_feature.html   (completion: 0)
     skipped  (none already present)

   Next:
     - Replace placeholder text in docs/PRD.html, docs/plans/specs_overview.html,
       and docs/plans/features/example_feature.html (copy it per real feature),
       then run /hq-update-progress for real numbers.
   ```

6. `git add docs/PRD.html docs/plans/specs_overview.html docs/plans/features/example_feature.html
   && git commit -m "chore(hq): scaffold Command HQ project files via /hq-init"
   && git push`.

On a repo that already has, say, `docs/PRD.html` and a `docs/plans/features/`
tree, it reports `skipped docs/PRD.html`, `skipped docs/plans/features/** (N docs
present)`, and only creates the genuinely-missing files (or nothing, committing
nothing).

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running it
in a repo with no HQ files: it creates `docs/PRD.html`,
`docs/plans/specs_overview.html`, and `docs/plans/features/example_feature.html`
(each with `completion: 0`), pushes them, and re-running it creates nothing
(idempotent). After an HQ refresh the Overview shows the `Product goal:`, the
Project Requirements tab renders `docs/PRD.html` read-only, the Detailed
Requirements tab lists the overview + example feature, and the bars read `0%`
instead of the empty state.
