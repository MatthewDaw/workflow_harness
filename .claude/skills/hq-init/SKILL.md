---
name: hq-init
description: >-
  Run inside the claude+ PTY to scaffold the GitHub-side files Command HQ
  reads for a project: `docs/PRD.html` (the Project Overview goal + Project
  Requirements body + bar, with `completion:` frontmatter and a `Product goal:`
  line), `docs/plans/specs_overview.html` (the Detailed Requirements headline,
  with `completion:` frontmatter), `docs/plans/features/example_feature.html`
  (a template feature doc), and `docs/wireframe.html` (an example UI wireframe
  previewed on the Project Requirements tab). It writes placeholder files only
  where they are missing — it never overwrites existing content — then commits
  and pushes them with the developer's own git/gh so a freshly connected repo
  stops showing the "needs files" / 0% empty state. Use when the user says
  "/hq-init", "init", "scaffold the HQ files", or connects a repo that has no
  PRD/plan docs yet.
---

# /hq-init

Bootstrapper for the **GitHub side** of a Command HQ project. HQ reads a repo
through a read-only GitHub App; if the files it expects are absent, the Project
Overview, Project Requirements, and Detailed Requirements tabs render their
empty states (no goal, no Project Requirements body, no requirement docs, bar at
`0%`, no wireframe preview). This skill creates **placeholders** for four files
so a new repo shows up cleanly, then hands off to [[hq-update-progress]] (which
computes and pushes the real `completion:` numbers later).

It is the **complement** of [[hq-update-progress]]: this one creates the files,
that one keeps their numbers current. Neither calls an HQ endpoint — both push
to GitHub and HQ pulls on refresh.

## When this runs

In the developer's claude+ session (the PTY), from inside a connected repo. It
shells out to `git` and `gh` with the developer's own credentials. It is
**idempotent and non-destructive**: it only writes a file that does not already
exist, and prints what it skipped.

## The four files (and nothing else)

This skill scaffolds **only** these four files. There is no repo-root `PRD.md`
and no `PROGRESS.md` — everything HQ needs lives under `docs/`.

| HQ surface | Repo file | What's parsed |
| --- | --- | --- |
| **Project Overview** (goal) | `docs/PRD.html` | the `Product goal:` line (markdown `**Goal:**` or an HTML `<strong>Product goal:</strong>` label) → the goal shown on the project card. |
| **Project Requirements** (body **and** bar) | `docs/PRD.html` | the high-level requirements prose → the read-only body; `completion:` frontmatter → `progressPct`. |
| **Detailed Requirements** (headline + bar fallback) | `docs/plans/specs_overview.html` | the top-of-tree overview doc; its `completion:` frontmatter is the Detailed Requirements headline number. |
| **Detailed Requirements** (per-feature) | `docs/plans/features/*.html` | each feature doc + its `completion:` frontmatter badge; `example_feature.html` is the seed template. |
| **Project Requirements** (wireframe preview) | `docs/wireframe.html` | the full HTML file is served verbatim → a scaled preview on the Project Requirements tab that links to a full-screen `requirements/wireframe` route. |

The backend parsing lives in `packages/backend/src/github/history.ts`
(`parseGoal`, `parseCompletionFrontmatter`) and `app.ts`
(`readFramingWithCompletion`, which reads `docs/PRD.{md,html}` for both the goal
and the bar; `readWireframe`, which serves `docs/wireframe.html` to the
`/projects/:id/wireframe` endpoint). HQ reads `.md` or `.html`; we scaffold
`.html`.

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

5. **Scaffold `docs/wireframe.html`** (the example UI wireframe previewed on the
   Project Requirements tab) **only if missing.** It is a standalone, fully
   self-contained HTML document (inline `<style>`, no external assets — HQ serves
   it verbatim into a sandboxed iframe, so external CSS/JS/links won't load). No
   frontmatter is needed; it is not parsed for `completion:`. Keep it small but
   real so the preview shows something:

   ```html
   <!doctype html>
   <html lang="en">
   <head>
     <meta charset="utf-8" />
     <meta name="viewport" content="width=device-width, initial-scale=1" />
     <title><Project Name> — Wireframe</title>
     <style>
       body { margin: 0; font-family: system-ui, sans-serif; color: #1a1a1a; background: #f6f6f4; }
       header { padding: 16px 24px; background: #1a1a1a; color: #fff; font-weight: 600; }
       main { display: grid; grid-template-columns: 200px 1fr; gap: 16px; padding: 24px; }
       nav a { display: block; padding: 8px 12px; border-radius: 6px; color: #444; text-decoration: none; }
       nav a.on { background: #e8e8e4; color: #000; font-weight: 600; }
       .card { background: #fff; border: 1px solid #e2e2dd; border-radius: 8px; padding: 16px; margin-bottom: 12px; }
     </style>
   </head>
   <body>
     <header><Project Name></header>
     <main>
       <nav>
         <a class="on" href="#">Overview</a>
         <a href="#">Requirements</a>
         <a href="#">Sessions</a>
       </nav>
       <section>
         <div class="card"><h2>Replace this wireframe</h2><p>Sketch the primary screen for this project here.</p></div>
         <div class="card">A second placeholder block.</div>
       </section>
     </main>
   </body>
   </html>
   ```

   (Replace it with the project's real wireframe — for example, move an existing
   `wireframe.html` from the repo root into `docs/wireframe.html`.)

6. **Report what was created vs. skipped**, then **commit + push.** Stage only
   the files this skill created. Commit with a message like
   `chore(hq): scaffold Command HQ project files via /hq-init` (include the
   repo's Co-Authored-By trailer). Push to the current branch's upstream with
   `git push` (use `gh` only if auth/PR is needed). Never use the GitHub Contents
   API — push with the dev's own git so HQ's next read sees the new SHA.

7. **Tell the user the manual follow-up:** replace the placeholder goal /
   requirements text in `docs/PRD.html`, the feature list in
   `docs/plans/specs_overview.html`, the seed `example_feature.html` (copy it
   per real feature), and the placeholder `docs/wireframe.html`, then run
   **[[hq-update-progress]]** to compute real `completion:` numbers.

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App,
reads `docs/PRD.html` (goal via `parseGoal` + Project Requirements body + bar via
`completion:`), walks the `docs/plans/` tree (`specs_overview.html` headline +
`features/*.html`), and serves `docs/wireframe.html` to the Project Requirements
wireframe preview, so those surfaces leave their empty states. HQ never writes
back.

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
5. No `docs/wireframe.html` → write the placeholder wireframe.
6. Print:

   ```
   hq-init — scaffolded:
     created  docs/PRD.html                              (completion: 0)
     created  docs/plans/specs_overview.html             (completion: 0)
     created  docs/plans/features/example_feature.html   (completion: 0)
     created  docs/wireframe.html                        (example wireframe)
     skipped  (none already present)

   Next:
     - Replace placeholder text in docs/PRD.html, docs/plans/specs_overview.html,
       and docs/plans/features/example_feature.html (copy it per real feature),
       swap docs/wireframe.html for the real wireframe, then run
       /hq-update-progress for real numbers.
   ```

7. `git add docs/PRD.html docs/plans/specs_overview.html docs/plans/features/example_feature.html docs/wireframe.html
   && git commit -m "chore(hq): scaffold Command HQ project files via /hq-init"
   && git push`.

On a repo that already has, say, `docs/PRD.html` and a `docs/plans/features/`
tree, it reports `skipped docs/PRD.html`, `skipped docs/plans/features/** (N docs
present)`, and only creates the genuinely-missing files (or nothing, committing
nothing).

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running it
in a repo with no HQ files: it creates `docs/PRD.html`,
`docs/plans/specs_overview.html`, `docs/plans/features/example_feature.html`
(each with `completion: 0`), and `docs/wireframe.html`, pushes them, and
re-running it creates nothing (idempotent). After an HQ refresh the Overview
shows the `Product goal:`, the Project Requirements tab renders `docs/PRD.html`
read-only plus a wireframe preview that links to the full-screen
`requirements/wireframe` route, the Detailed Requirements tab lists the overview
+ example feature, and the bars read `0%` instead of the empty state.
