---
status: active
type: plan
created: 2026-06-03
completion: 90
feature: plan-mapping
---

# Plan — Source Project Requirements from `docs/PRD.md`

**Type:** `refactor` (re-point an existing tab's source of truth) + a small skill
extension. **Depth:** Standard. **Target repo:** this repo (`workflow_harness`).

Repoint the **Project Requirements** tab so its body and progress bar come from a
repo file — `docs/PRD.md` — read-only from GitHub, instead of the HQ-owned
markdown stored in DynamoDB. Extend `/hq-update-progress` so it also computes and
writes `docs/PRD.md`'s `completion:` frontmatter, the same way it already does for
the `docs/plans/` tree. After this, the two requirement bars are driven by two
distinct, code-audited numbers:

- **Project Requirements** bar + body ← `docs/PRD.md` (high level).
- **Detailed Requirements** tree + bar ← `docs/plans/**` (unchanged).

There is **no UI button and no remote trigger** — `/hq-update-progress` is run
locally in the claude+ PTY, as today (explicitly de-scoped by the user).

---

## Problem frame

The user wants both requirement tabs' progress bars to reflect *actual code
progress against the actual requirements*, with the Project Requirements tab
specifically reading from `docs/PRD.md` and Detailed Requirements from
`docs/plans/`.

What already exists (built + tested) — see
[01-plan-mapping.md](./01-plan-mapping.md):

- `/hq-update-progress` already audits `docs/plans/**`, computes a code-vs-docs
  `completion:` per doc, writes the frontmatter, commits/pushes, and emits a
  compliance report. **The progress-computation engine is reused, not rebuilt.**
- **Detailed Requirements** already renders `docs/plans/**` read-only from GitHub;
  its bar is the docs-tree top doc's `completion:`. ✅ Already matches the ask.
- **Project Requirements** is the one mismatch: today its body is **HQ-owned**
  markdown (DynamoDB, edited inline, `PUT /projects/:id/requirements`) and its bar
  reads `project.progressPct`, which is currently sourced from the `docs/plans/`
  top doc. The ask is to source the body + bar from `docs/PRD.md` instead.

So this is a re-pointing job plus a skill extension, not a new subsystem.

### Scope boundaries

In scope:

- Project Requirements body + bar sourced from `docs/PRD.md` (read-only).
- `progressPct` re-sourced from `docs/PRD.md`'s `completion:` (with graceful
  fallback to today's sources so repos without `docs/PRD.md` don't regress).
- `/hq-update-progress` extended to audit + write `docs/PRD.md`.
- Create the initial `docs/PRD.md` for this repo.
- Reconcile the contract docs + `/hq-init-command-hq` scaffolder.

#### Deferred to follow-up work

- Any HQ web **button** or **remote trigger** of the skill — explicitly dropped
  for now (the skill stays local-only).
- The enforced prod-E2E verified-completion gate (already deferred in
  [01-plan-mapping.md](./01-plan-mapping.md)).
- `completion:` frontmatter integrity (hand-editable) — unchanged.

---

## Key technical decisions

- **KTD-A — `progressPct` is re-sourced, not duplicated.** The Project
  Requirements bar reads `project.progressPct`. Rather than add a parallel field,
  change where `readFramingWithCompletion()` derives `progressPct` from. New
  precedence: `docs/PRD.md` `completion:` → (fallback) `docs/plans/` top doc
  `completion:` → (fallback) `PROGRESS.md` `N%` → `0`. `DetailedRequirements.tsx`
  is unaffected because it already reads the docs-tree top doc directly, not
  `progressPct`. This also means the **objectives roll-up** now ladders up from
  the high-level PRD number, which is the more correct altitude
  ("every repo's requirements ladder up to a Supporting Outcome").

- **KTD-B — Reuse `GET /projects/:id/requirements`, drop the write path.** Keep
  the existing read endpoint + frontend hook (`useGetProjectRequirementsQuery`,
  response shape `{ markdown }`) so the wiring stays stable, but change its
  implementation to read `docs/PRD.md` from GitHub via the injected GitHub App
  instead of DynamoDB. Remove `PUT /projects/:id/requirements`, its frontend
  mutation, and the now-dead `repo.getProjectRequirements` / `putProjectRequirements`
  DB methods. GitHub-unreachable degrades to `{ markdown: '', stale: true }`,
  matching the doc-content endpoint's failure posture.

- **KTD-C — `docs/PRD.md` is a distinct file from the repo-root `PRD.md`.** The
  root `PRD.md` keeps feeding the **Project Overview** tab (goal + `SO-…` ids via
  `parsePrd`) untouched. `docs/PRD.md` is the new **Project Requirements** source.
  The contract docs + `/hq-init-command-hq` table must spell out both so the two
  are not confused.

- **KTD-D — `docs/PRD.md` lives outside the `docs/plans/` prefix on purpose.** The
  `docs/plans/`-prefixed reads (`readTree`, `readDocContent`, `listDocs`) stay
  scoped to the Detailed Requirements tree. `docs/PRD.md` is read with the plain
  `readFile('docs/PRD.md')` path (Contents API, already used for root `PRD.md`),
  so it never leaks into the Detailed doc tree.

- **KTD-E — Reuse `parseCompletionFrontmatter`.** It is already a pure,
  doc-agnostic parser; `docs/PRD.md` carries the same `---\ncompletion: N\n---`
  block. No new parser.

---

## High-level data flow (after this change)

```
docs/PRD.md  ──readFile()──▶ getProjectRequirements()  ──▶ Project Requirements BODY (read-only)
   │ completion: frontmatter
   └──parseCompletionFrontmatter──▶ readFramingWithCompletion().progressPct
                                          ├──▶ Project Requirements BAR
                                          └──▶ objectives roll-up (rollup.ts, unchanged math)

docs/plans/**  ──listDocs()──▶ Detailed Requirements tree + bar   (UNCHANGED)

/hq-update-progress (local, in claude+ PTY)
   audits code-vs-docs ──▶ writes completion: into BOTH docs/PRD.md AND docs/plans/**
   ──git push──▶ GitHub  ──▶ HQ reads on refresh (HQ never writes)
```

---

## Implementation units

### U1. Create `docs/PRD.md` as the Project Requirements source

**Goal:** Add the repo's `docs/PRD.md` with `completion:` frontmatter and a
high-level requirements body, so the re-pointed tab has something real to render.

**Requirements:** The Project Requirements tab must pull from `docs/PRD.md`.

**Dependencies:** none.

**Files:**
- `docs/PRD.md` (create)

**Approach:** Author a concise high-level requirements doc (the "what management
writes" altitude — the five features from
[command-hq-overview.md](../command-hq-overview.md), as outcome-level bullets, not
the detailed breakdown). Seed content from the existing `docs/st6_prd.md`
inspiration where useful, but keep it product-requirement prose, not a plan. Start
the file with a `---\ncompletion: 0\n---` frontmatter block (the placeholder;
`/hq-update-progress` overwrites it). Do **not** delete or move `docs/st6_prd.md`
(it remains the cited inspiration) or the repo-root `PRD.md` (Project Overview).

**Patterns to follow:** the frontmatter convention used by every `docs/plans/`
doc; the high-level-vs-detailed split described in
[01-plan-mapping.md](./01-plan-mapping.md) ("The three altitudes").

**Test scenarios:** Test expectation: none — content authoring (a markdown doc,
no behavior). Verified indirectly by U2's parser tests reading a `docs/PRD.md`
fixture.

**Verification:** `docs/PRD.md` exists, starts with a `completion:` frontmatter
block, and reads as high-level requirements (not a detailed plan).

---

### U2. Backend — source Project Requirements body + `progressPct` from `docs/PRD.md`

**Goal:** Make the backend read the Project Requirements body and the
`progressPct` bar value from `docs/PRD.md` (read-only via the GitHub App), and
retire the HQ-owned write path.

**Requirements:** Project Requirements tab + bar driven by `docs/PRD.md`; Detailed
Requirements unchanged.

**Dependencies:** U1 (a real file to read; tests use fixtures so this is a soft
dep).

**Files:**
- `packages/backend/src/github/app.ts` (modify) — add a small
  `readPrdDoc()` helper (or inline `readFile('docs/PRD.md')`); change
  `readFramingWithCompletion()` so `progressPct` precedence is
  `docs/PRD.md` completion → `docs/plans/` top-doc completion → `PROGRESS.md` →
  `0`.
- `packages/backend/src/rest/projects.ts` (modify) — repoint
  `getProjectRequirements` to read `docs/PRD.md` via the injected GitHub App
  (`{ markdown }`, `{ markdown: '', stale: true }` when no app / unreachable);
  remove `putProjectRequirements` and its route branch in `handler`.
- `packages/backend/src/db/repo.ts` (modify) — remove now-dead
  `getProjectRequirements` / `putProjectRequirements` methods (and any
  requirements-specific key in `db/keys.ts`). If removal proves entangled, leaving
  them orphaned is an acceptable fallback — note it in the PR.
- `packages/backend/src/github/history.ts` — **no change** (reuse
  `parseCompletionFrontmatter`).
- Tests: `packages/backend/src/github/app.test.ts`,
  `packages/backend/src/rest/projects.test.ts` (modify);
  remove/replace any `putProjectRequirements` test; add a `docs/PRD.md` fixture.

**Approach:** `readFraming()` already reads root `PRD.md`; add a parallel
`readFile('docs/PRD.md')` for the new source. In `readFramingWithCompletion()`,
compute `prdCompletion = parseCompletionFrontmatter(docsPrdMd)` and prefer it over
the existing `topCompletion`. `getProjectRequirements` builds the GitHub App via
the existing `deps.githubFor ?? defaultGithubFor` pattern (mirror
`getProjectDocContent`'s structure for the unreachable/stale branch).

**Patterns to follow:** `getProjectDocContent` (GitHub App acquisition + stale
fallback); `readFramingWithCompletion` (Promise.all + precedence); the read-only,
never-throw-on-GitHub-failure posture used across `rest/projects.ts`.

**Test scenarios:**
- `readFramingWithCompletion`: `docs/PRD.md` with `completion: 42` present →
  `progressPct === 42`, overriding a different `docs/plans/` top-doc completion.
- `readFramingWithCompletion`: `docs/PRD.md` absent → falls back to the
  `docs/plans/` top-doc completion (no regression for repos without `docs/PRD.md`).
- `readFramingWithCompletion`: neither present → falls back to `PROGRESS.md` `N%`,
  then `0` (existing behavior preserved).
- `getProjectRequirements`: GitHub App returns `docs/PRD.md` body → response
  `{ markdown: <body> }`.
- `getProjectRequirements`: no GitHub App (not connected) → `{ markdown: '',
  stale: true }`, **not** a 500.
- `getProjectRequirements`: GitHub App throws (unreachable) → `{ markdown: '',
  stale: true }`, not a 500.
- `handler`: `PUT /projects/:id/requirements` no longer routes to a write handler
  (404 or method-not-allowed, matching how unknown routes fall through today).
- Covers the contract: Detailed Requirements endpoints (`/docs`, `/docs/content`)
  are untouched — a regression test that `getProjectDocs` still returns the
  `docs/plans/` tree.

**Verification:** Backend suite green; `progressPct` reflects `docs/PRD.md`'s
`completion:` when present; the requirements read endpoint serves `docs/PRD.md`;
the write path is gone.

---

### U3. Web — Project Requirements tab renders `docs/PRD.md` read-only

**Goal:** Turn the Project Requirements tab into a read-only GitHub-sourced
renderer (like Detailed Requirements): drop the inline editor, the `✎ Edit` flow,
and the `PUT` mutation; keep the bar + full-screen reader.

**Requirements:** Project Requirements tab pulls from `docs/PRD.md`; read-only.

**Dependencies:** U2 (endpoint now serves `docs/PRD.md`).

**Files:**
- `packages/web/src/screens/ProjectDetail/ProjectRequirements.tsx` (modify) —
  remove `editing`/`draft` state, `startEdit`/`onSave`, the `✎ Edit` button, the
  editor `<textarea>`, and the `usePutProjectRequirementsMutation` import/use.
  Keep `useGetProjectRequirementsQuery` (now GitHub-sourced), the `Bar`, and the
  full-screen reader. Update copy: subtitle/label to reflect "from GitHub
  `docs/PRD.md`, read-only" and the completion source. `ProjectRequirementsFull`
  is already read-only — leave its logic, refresh copy only.
- `packages/web/src/api/baseApi.ts` (modify) — remove the
  `putProjectRequirements` mutation + its export `usePutProjectRequirementsMutation`;
  keep `getProjectRequirements`. Optionally surface `stale` on `ProjectRequirements`.
- Tests: `packages/web/src/screens/ProjectDetail/ProjectRequirements.test.tsx`
  (modify) — drop edit/save assertions; add read-only + render-from-endpoint
  assertions.

**Approach:** Mechanical removal of the edit affordances; the read + bar paths
already exist. Make sure no remaining reference to the removed mutation breaks the
build (grep `usePutProjectRequirementsMutation`).

**Patterns to follow:** `DetailedRequirements.tsx` (read-only, GitHub-sourced
render with a `Bar`); the existing `MarkdownView` usage already in this file.

**Test scenarios:**
- Renders the markdown returned by `getProjectRequirements` (mock returns a
  `docs/PRD.md` body) in a `MarkdownView`.
- The bar shows `project.progressPct`.
- No `✎ Edit` button / editor `<textarea>` is present (read-only).
- Empty body → the "No requirements yet" empty state still renders (now phrased
  for the GitHub-sourced case, e.g. "No `docs/PRD.md` found").
- Full-screen reader route still renders the body read-only.

**Verification:** Web suite green; the tab renders `docs/PRD.md` with a progress
bar and no editing UI; no dangling references to the removed mutation.

---

### U4. Skill — extend `/hq-update-progress` to audit + write `docs/PRD.md`

**Goal:** Make the local skill compute and write `docs/PRD.md`'s `completion:`
frontmatter (the Project Requirements headline number) alongside the existing
`docs/plans/**` audit, and push it.

**Requirements:** Project Requirements bar updates from real code progress, driven
by the same local skill.

**Dependencies:** U1 (the file to write into); conceptually pairs with U2 (the
backend that reads it).

**Files:**
- `.claude/skills/hq-update-progress/SKILL.md` (modify) — add `docs/PRD.md` to the
  audited set: step 1 (locate) includes `docs/PRD.md` as the **Project
  Requirements headline**; step 5 (compute) scores `docs/PRD.md` from code-vs its
  high-level requirements; step 6 (write) updates its frontmatter; step 7 (commit)
  stages `docs/PRD.md` in addition to `docs/plans/**`; step 8 (report) lists it.
  Update the "what HQ shows" framing: **Project Requirements bar ← `docs/PRD.md`
  completion; Detailed Requirements ← `docs/plans/` headline** (today's text says
  both come from the `docs/plans/` headline — correct that).

**Approach:** Documentation/skill authoring only. Keep the advisory-DoD, never-
block posture intact. Make the headline/altitude split explicit so the skill's
mental model matches U2's precedence (PRD = Project Requirements, plans =
Detailed).

**Patterns to follow:** the existing step structure + worked dry-run example in
the same file.

**Test scenarios:** Test expectation: none — SKILL.md authoring. Verified by
running `/hq-update-progress` locally: it edits `docs/PRD.md`'s `completion:`,
pushes, and the Project Requirements bar reflects the pushed number after an HQ
refresh.

**Verification:** The skill's steps name `docs/PRD.md`; a local run writes its
`completion:` and the compliance report lists it.

---

### U5. Reconcile contract docs + `/hq-init-command-hq`

**Goal:** Keep the source-of-truth docs honest: Project Requirements is now a
**repo file (`docs/PRD.md`)**, no longer HQ-owned, and the scaffolder creates it.

**Requirements:** Documentation reflects the new contract; a freshly connected
repo scaffolds `docs/PRD.md`.

**Dependencies:** U2–U4 (the behavior the docs now describe).

**Files:**
- `.claude/skills/hq-init-command-hq/SKILL.md` (modify) — add a step to scaffold
  `docs/PRD.md` (with `completion: 0` frontmatter + a placeholder high-level
  body) only if missing; update the "What each tab reads" table so Project
  Requirements (body **and** bar) maps to `docs/PRD.md`; remove the "Project
  Requirements body is HQ-owned, fill it in inside HQ" follow-up.
- `docs/plans/command-hq/01-plan-mapping.md` (modify) — update altitude 2
  (Project Requirements) from "a single markdown file owned by Command HQ" to
  "repo file `docs/PRD.md`, rendered read-only from GitHub"; update "Where
  completion lives" so the Project Requirements bar = `docs/PRD.md` completion and
  the Detailed root = `docs/plans/` headline.
- `docs/plans/command-hq-overview.md` (modify) — update the ASCII "how the pieces
  fit" diagram + the "Source of truth" line so Project Requirements is GitHub-
  sourced (`docs/PRD.md`), not HQ-owned.

**Approach:** Documentation reconciliation. Be precise about the two PRD files
(root `PRD.md` = Project Overview; `docs/PRD.md` = Project Requirements) to avoid
confusion (KTD-C).

**Test scenarios:** Test expectation: none — documentation. (`/hq-init-command-hq`
is a doc/skill; its scaffolding is verified by running it in a repo missing
`docs/PRD.md`.)

**Verification:** No doc still describes Project Requirements as HQ-owned; the init
skill scaffolds `docs/PRD.md`; the two PRD files are clearly distinguished.

---

## System-wide impact

- **Objectives roll-up** (`packages/backend/src/projections/rollup.ts`) consumes
  `progressPct`. Its math is unchanged, but its input now reflects `docs/PRD.md`
  (high level) rather than the `docs/plans/` top doc. This is intended (KTD-A) and
  is the more correct altitude, but it shifts the displayed roll-up number for any
  connected project — call it out in the PR.
- **Repos without `docs/PRD.md`** must not regress: the fallback chain (KTD-A)
  preserves today's behavior until a `docs/PRD.md` exists. U2's fallback tests
  guard this.
- **Weekly** (`rest/weekly.ts`) is independent of this change (it round-trips a
  client-posted report) — no impact.

## Risks & mitigations

- **Two-PRD confusion** (root `PRD.md` vs `docs/PRD.md`). Mitigation: KTD-C + explicit
  contract-table updates in U5.
- **Dead-code removal entanglement** (the DynamoDB requirements methods/keys).
  Mitigation: U2 allows leaving them orphaned with a PR note if removal is messy.
- **Stale bar after a `docs/PRD.md` edit** until an HQ refresh. This matches
  existing behavior for all GitHub-sourced data (refresh-on-read); no new risk.

## Open questions (deferred to implementation)

- Exact seed content/length of `docs/PRD.md` (U1) — an authoring choice, resolved
  when writing it.
- Whether to remove vs orphan the DynamoDB requirements methods (U2) — decided at
  implementation time based on coupling.
