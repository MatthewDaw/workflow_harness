---
status: active
type: feature
created: 2026-06-02
completion: 88
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
   goals management writes — the equivalent of an `st6_prd.md`. It is the repo file
   **`docs/PRD.md`, rendered read-only from GitHub.** GitHub is the source of truth;
   HQ only renders it (no edits, no injected data) — distinct from the repo-root
   `PRD.md`, which feeds the Project Overview tab.
   - Rendered in HQ; a **full-screen markdown reader** view.
   - Its `completion:` frontmatter drives the Project Requirements progress bar.
   - Each item shows a **green check** the moment it is *verified* complete.

3. **Detailed Requirements (per-repo, long form).** The breakdown that expands on
   the high-level goals — **rendered straight from the repo's `docs/plans/` on
   GitHub** (a top-level document plus lower-level documents). **GitHub is the
   source of truth; HQ only renders it** read-only and in sync — no edits, no
   injected data. Completion shows wherever a doc has a
   ticked GitHub task item (`- [x]`).
   *Code today (built):* the backend reads the `docs/plans/` tree from GitHub and
   serves it read-only (`packages/backend/src/github/app.ts`,
   `rest/projects.ts` docs-tree + per-file endpoints); the web renders it via
   `packages/web/src/screens/ProjectDetail/DetailedRequirements.tsx` +
   `components/MarkdownView.tsx` (react-markdown + GFM task lists). The browser
   never talks to GitHub directly — the backend is the only reader.

## Where completion lives

**`docs/PRD.md` and every doc in `docs/plans/` carry a `completion:` percentage in
their frontmatter (at the top). The website reads those numbers directly — it does
not recompute them.**

- **`docs/PRD.md`**'s `completion:` is the headline number on the high-level
  **Project Requirements** bar.
- The **top-of-`docs/plans/` file** (`command-hq-overview.md`) represents the whole
  detailed breakdown; its `completion:` is the headline number shown on the
  **Detailed Requirements** root.
- Each lower-level doc shows its own `completion:` in the Detailed doc-tree.
- Item-level detail still uses GitHub task items (`- [x]`) inside the docs; the
  headline bar is the frontmatter `completion:`.
- `/update-progress` (a client-side Claude skill) computes the `completion:` % and
  pushes it to GitHub; **HQ reads it, never writes.** The number in the GitHub
  `.md` is the single source of truth for the bar.

```
/update-progress (client-side: Claude computes % from code vs docs)
   → pushes completion: % to docs/PRD.md AND docs/plans/ .md → HQ reads it (HQ never writes)
   → Project Requirements bar (docs/PRD.md) + Detailed root (docs/plans/ headline).
     Also emits a compliance report.
```

*Code today (built):* `packages/backend/src/github/history.ts` parses the
`completion:` frontmatter (`docs/PRD.md` for the Project Requirements bar, the top
`docs/plans/` doc for the Detailed root, falling back to legacy `PROGRESS.md %`);
`rest/projects.ts` stores the bar value as the project's `progressPct`
(with `framingReadAt`/`framingStale` for last-known-on-failure);
`projections/rollup.ts` (re-pointed off tickets in the migration) derives a
Supporting-Outcome leaf % from that stored value. The web bars read the stored
number.

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
   actually built and flagging drift (between the high-level `docs/PRD.md`
   Project Requirements and the detailed `docs/plans/` docs) — the reconciliation
   surface.

**Completion is never typed in by hand** — it is earned by passing the gate.

### Definition of Done lives in Company Objectives

A config block on the Objectives pane defines what `/update-progress` must verify
before a check turns green. Org-wide default = **unit tests passing**; projects
may *tighten* (e.g. add prod-E2E) but not drop below the org floor.

*Code today (built, advisory):* `packages/backend/src/rest/dod.ts` exposes
`GET /dod` (the org Definition of Done, defaulting to the floor when unset) and
`PUT /dod` (admin-only). As built it is **advisory** — surfaced in HQ and reported
on by the compliance report, but it does **not** hard-block a progress push. The
prod-E2E *enforced* gate remains deferred (see Open questions).

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

- **Built:** objectives tree CRUD; GitHub `completion:` frontmatter read + store
  (with legacy `PROGRESS.md %` fallback); roll-up **re-pointed off tickets** onto
  the stored `progressPct`; the docs-tree read endpoints; the two-tier
  (Project/Detailed) requirements UI with `MarkdownView` and a full-screen reader;
  the read-only `docs/PRD.md`-sourced Project Requirements; the advisory
  Definition-of-Done REST;
  the `/update-progress` skill (`.claude/skills/update-progress/`).
- **Deferred:** the **enforced** prod-E2E verified-completion gate (and
  `completion:` frontmatter integrity) — for v1 the pushed % is `/update-progress`'s
  computed estimate, not a gated guarantee.
