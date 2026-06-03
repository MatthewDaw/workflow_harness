---
status: active
type: feature
created: 2026-06-02
completion: 35
feature: plan-mapping
---

# Feature 1 — Plan Mapping (goals → breakdown → verified progress)

How a company-level goal narrows down to a repo goal, then to a detailed
breakdown, and how a tool keeps the progress of all of it honest and
up to date.

## The three altitudes

1. **Company Objectives (org-wide, RCDO).** Rally Cries → Defining Objectives →
   Outcomes → Supporting Outcomes. One company tree, admin-curated. Every repo's
   requirements ladder up to a Supporting Outcome here.
   *Code today:* `packages/backend/src/rest/objectives.ts`, tree + cached `pct`
   in `db/repo.ts`; UI `packages/web/src/screens/Objectives/Objectives.tsx`.

2. **Project Requirements (per-repo, high level).** The simple, high-level list of
   goals management writes — the equivalent of an `st6_prd.md`. It is **a single
   markdown file owned by Command HQ; HQ is the source of truth.**
   - Edited in HQ; a **full-screen markdown reader** view.
   - Each item shows a **green check** the moment it is *verified* complete.
   - A user may optionally download a snapshot into the repo via a
     `/sync-requirements` skill (HQ → repo, one direction); HQ stays canonical.

3. **Detailed Requirements (per-repo, long form).** The breakdown that expands on
   the high-level goals — **rendered straight from the repo's `docs/plans/` on
   GitHub** (a top-level document plus lower-level documents). **GitHub is the
   source of truth; HQ only renders it** read-only and in sync — no edits, no
   injected data. Completion shows wherever a doc has a
   ticked GitHub task item (`- [x]`).
   *Code today:* GitHub read via `packages/backend/src/github/` (extend to fetch a
   docs tree, not just `PRD.md`/`PROGRESS.md`). Wireframed in `wireframe.html`
   ("HQ — Project › Detailed Requirements") as a doc-tree + markdown reader.

## Where completion lives

**Every doc in `docs/plans/` carries a `completion:` percentage in its frontmatter
(at the top). The website reads that number directly — it does not recompute it.**

- The **top-of-folder file** (`command-hq-overview.md`) represents the whole
  project; its `completion:` is the headline number shown on **both** the HQ
  high-level **Project Requirements** bar **and** the **Detailed Requirements**
  root. One number, two surfaces.
- Each lower-level doc shows its own `completion:` in the Detailed doc-tree.
- Item-level detail still uses GitHub task items (`- [x]`) inside the docs; the
  headline bar is the frontmatter `completion:`.
- `/update-progress` (a client-side Claude skill) computes the `completion:` % and
  pushes it to GitHub; **HQ reads it, never writes.** The number in the GitHub
  `.md` is the single source of truth for the bar.

```
/update-progress (client-side: Claude computes % from code vs docs)
   → pushes completion: % to GitHub .md → HQ reads it (HQ never writes)
   → Project Requirements bar + Detailed root.  Also emits a compliance report.
```

*Code today:* `packages/backend/src/projections/rollup.ts`, `projections/rollupRepo.ts`
(roll-up engine; extend to read the doc `completion:` front­matter).

## `/update-progress` — the auto-audit tool

A **Command HQ skill** (see [feature 3](./03-claude-code-integration.md) for how
skills register) that a developer runs in their repo. It:

1. **Audits the repo** against the requirement docs (the `docs/plans/` tree + the
   high-level goals).
2. Runs the project's **Definition of Done** gate before marking anything done:
   - **Unit tests passing** (the org-wide floor / default).
   - **End-to-end tests verified in *production*** — not locally. The skill
     confirms the prod E2E suite exists and is green against the deployed
     environment.
3. **Computes the completion %** from what's actually coded up versus the docs (so
   the user doesn't hand-calculate it), then **pushes the number to GitHub**
   (client-side, with the dev's own git) by writing the `completion:` frontmatter.
   HQ reads it from GitHub to move the bar — **HQ never writes to GitHub.**
4. **Generates a compliance report** comparing the requirements against what's
   actually built and flagging drift (including between the HQ-owned Project
   Requirements and the GitHub docs) — the reconciliation surface.

**Completion is never typed in by hand** — it is earned by passing the gate.

### Definition of Done lives in Company Objectives

A config block on the Objectives pane defines what `/update-progress` must verify
before a check turns green. Org-wide default = **unit tests passing**; projects
may *tighten* (e.g. add prod-E2E) but not drop below the org floor.

## Open questions

- Exact shape of the markdown editor + versioning for Project Requirements.
- How `/update-progress` discovers "the prod E2E suite" and the deployed target.
- How the high-level HQ requirements file maps onto the `docs/plans/` docs for per-item check roll-up.
- **[Deferred — 2026-06-03 review]** The verified-completion gate (prod-E2E suite
  discovery + binding the deployed env to the committed code) and `completion:`
  integrity (the frontmatter is hand-editable, bypassing the gate) are unresolved —
  the "verified, not self-reported" guarantee is not yet enforceable.
- **Interaction states to define (design phase):** Project Requirements editor
  (edit / save / conflict / versioning), Detailed Requirements sync-failure / empty
  / first-connect, and the progress zero-state.

## Status

- **Built:** objectives tree CRUD; GitHub `PRD.md`/`PROGRESS.md` parse (read-only,
  at connect time). The existing roll-up engine is ticket-based and is superseded
  by the manual/computed `completion:` model (no tickets) — needs rework.
- **Not built:** HQ-owned editable requirements + full-screen reader + per-item
  checks UI; the two-tier (Project/Detailed) split; the `/update-progress` skill;
  the prod-E2E Definition-of-Done gate; Definition-of-Done config UI.
