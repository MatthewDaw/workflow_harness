---
name: hq-update-progress
description: >-
  Run inside the claude+ PTY to audit a repo against its docs/PRD.md +
  docs/plans/ requirements, compute a code-vs-docs completion percentage per
  doc, write that number into each doc's `completion:` frontmatter, write a
  sentinel-delimited compliance breakdown block into the PRD doc, commit and
  push it to GitHub with the developer's own git/gh credentials, and emit a
  compliance report. Command HQ then READS the pushed numbers to move the
  Project Requirements bar (from docs/PRD.md) and the Detailed Requirements bar
  (from the docs/plans/ headline) — HQ never writes to GitHub. Use
  when the user says "/hq-update-progress", "update progress", "recompute
  completion", "push progress to HQ", or asks to reconcile what's built against
  the requirements docs.
---

# /hq-update-progress

Client-side progress auditor. It is the writer in a read-only-HQ world: **the
`completion:` number in each GitHub `.md` is the single source of truth for the
HQ progress bar, and this skill is the only thing that writes it.** HQ reads it
back (backend `github/history.ts` parses the `completion:` frontmatter and
`rest/projects.ts` serves it); HQ has no write scope on GitHub.

## When this runs

In the developer's claude+ session (the PTY), from inside a connected repo. It
shells out to `git` and `gh` with the developer's own credentials. It does not
call any HQ endpoint — the contract with HQ is entirely "push to GitHub, HQ
pulls."

## Scope of v1 (read this before trusting the number)

- The completion % is **Claude's computed estimate** from code-vs-docs, not a
  verified figure.
- The org declares a **Definition of Done** in Command HQ (the Objectives pane;
  served by `GET /dod`): `requiresUnitTests` (the org-wide floor, default true)
  and `requiresProdE2E` (default false), plus free-form `notes`. This skill
  **reads** that DoD and **reports conformance against it** — see the compliance
  report below. The DoD is **ADVISORY**: it never hard-blocks the progress push
  ("conformity never blocks"). It is the surfaced contract, not a gate.
- The **hard prod-E2E verification gate** described in
  `docs/plans/command-hq/01-plan-mapping.md` (binding the deployed environment to
  the committed code and refusing to mark work done until a green prod E2E suite
  is observed) is **DEFERRED**. When the DoD sets `requiresProdE2E: true`, this
  skill reports it as a checklist item and notes whether a prod E2E suite was
  found/observed — but it does not yet bind the deployed env or block on it. The
  completion % stays Claude's estimate. Say so in the report.
- `completion:` frontmatter is hand-editable; this skill does not enforce
  integrity. It overwrites with its own computed value on each run.

## Steps

1. **Locate the audited docs.** Two sources:
   - `docs/PRD.md` — the **Project Requirements headline**. Its `completion:` is
     the single number HQ shows on the **Project Requirements bar** (the
     high-level, "what management writes" altitude).
   - `docs/plans/**/*.md` — the Detailed Requirements tree. The top-of-folder
     doc (e.g. `docs/plans/command-hq-overview.md`, or the highest-level doc if
     no overview exists) is the **Detailed headline** doc — its `completion:` is
     the number HQ shows on the **Detailed Requirements root**. Every other plan
     doc gets its own per-doc `completion:`.
   If `docs/PRD.md` is absent, audit only the `docs/plans/` tree (no regression
   for repos that have not added a `docs/PRD.md` yet).
2. **Read the requirements.** For each doc, extract its stated requirements:
   frontmatter, the requirements/units/acceptance sections, and any GitHub task
   items (`- [ ]` / `- [x]`). These are the "claimed" surface.
3. **Read the actual code.** Search the repo for the implementation each
   requirement maps to (files named in the doc's file lists, the relevant
   `src/` modules, the tests that cover them). This is the "built" surface.
4. **Fetch + evaluate the Definition of Done.** Read the org-wide DoD (from
   Command HQ, `GET /dod`; if HQ is unreachable, fall back to the org floor —
   `requiresUnitTests: true`, `requiresProdE2E: false`). For each declared
   criterion, check it locally and record a pass/fail/unknown for the report:
   - `requiresUnitTests` — run the project's unit suite (e.g. `npm test`) and
     record whether it is green.
   - `requiresProdE2E` — confirm a prod E2E suite exists and, if observable,
     whether it is green against the deployed environment. The hard binding of
     deployed-env↔committed-code is **deferred**, so when this can't be verified,
     record it as **unknown / not-yet-enforced** rather than a hard fail.
   This evaluation is **ADVISORY** — it shapes the report and may temper the
   estimate, but it **never blocks** the push (next steps run regardless).
5. **Compute completion per doc.** For each doc, score each requirement as
   built / partial / not-built (a partial counts ~0.5), weight by the doc's own
   structure, and roll up to a 0–100 integer. Score `docs/PRD.md` the same way,
   but against its **high-level** requirements (the "what management writes"
   outcome bullets), not the detailed plan units — this is the standalone
   Project Requirements headline number, computed independently of the
   `docs/plans/` tree. The Detailed headline doc's number is the
   weighted mean of its child docs (mirror the roll-up math in
   `packages/backend/src/projections/rollup.ts`: leaf = built-fraction,
   internal = mean of children) so the pushed number matches how HQ rolls up.
   Use the DoD evaluation (step 4) as a corroborating signal — e.g. don't claim
   a doc 100% done if the org DoD's unit-test floor is failing — but do not
   block on it.
6. **Write the frontmatter.** Edit each doc's YAML frontmatter — `docs/PRD.md`
   and every `docs/plans/**` doc — setting or updating `completion:` to the
   computed integer. Preserve all other frontmatter keys and the doc body
   byte-for-byte. If a doc has no frontmatter block, add a minimal one
   (`---\ncompletion: N\n---`).
7. **Write the compliance breakdown block into the PRD doc.** This persists the
   DoD/compliance evaluation into the documentation (so it lives in git and HQ
   can read it) using a sentinel-delimited block that is valid inside BOTH
   markdown and HTML. Target whichever PRD doc exists — `docs/PRD.md` OR
   `docs/PRD.html` (this repo uses `docs/PRD.html`); if both exist, prefer the
   one HQ serves (`.html` if present, else `.md`). Algorithm — reproduce it
   exactly so a future run is deterministic:
   1. **Build the report body** from the completion + DoD/compliance evaluation
      already computed in steps 4–6. The body is a `## Compliance breakdown`
      heading followed by a table with columns **Requirement | Status |
      Evidence**, one row per **PRD deliverable** plus one row per **attestable
      stack/architecture constraint** from the assessment PRD this project is
      built against. The required deliverables (always rows):
      1. **Source Code**
      2. **Technical Documentation**
      3. **Demo Video**
      4. **Test Results**
      5. **AI Usage Log**
      Then the stated stack/architecture **constraints to attest**, e.g.:
      client-side SPA / no SSR (no Next/Remix); data fetching without Redux
      Saga/Thunk; backend Spring Data JPA + Hibernate with Lombok
      `@Getter`/`@Setter`/`@Builder` (not `@Data`); structured as a Vite Module
      Federation remote with a single route entry + shared deps + no hardcoded
      shell/navigation. **Status** is one of `met` / `partial` / `missing` /
      `n/a`. **Evidence** cites a real repo path, a test count, or a one-line
      reason. For THIS repo the Spring/JPA/Hibernate/Lombok and (where
      inapplicable) Module-Federation backend constraints are **N/A** — it is a
      TS/serverless app, not Spring — so mark those `n/a` with a one-line note
      (e.g. "n/a — TS/serverless monorepo, no Spring backend") rather than
      failing them. Never mark a constraint `missing` just because it does not
      apply to this stack.
   2. **Emit in the doc's native format**, detected by the target doc's
      extension: for a `.html` PRD doc emit the body as HTML (`<h2>Compliance
      breakdown</h2>` + a `<table>` with a header row and one `<tr>` per
      requirement); for a `.md` PRD doc emit the body as a markdown `##` heading
      + GitHub-Flavored-Markdown table. Wrap the body in the sentinel comments:
      ```
      <!--hq:compliance v1-->
      ...report body (heading + table)...
      <!--/hq:compliance-->
      ```
      The HTML-comment delimiters are valid inside both markdown and HTML docs.
   3. **(Re)insert the block** so there is **at most ONE** such block, placed
      immediately **after** the leading YAML frontmatter and **before** the
      first body heading/content. To do this: read the target PRD doc; split off
      the leading `---\n…\n---` frontmatter (if any); then **replace** any
      existing block matching the regex
      `/<!--hq:compliance[\s\S]*?<!--\/hq:compliance-->/` with the freshly built
      block — regenerating REPLACES, it never stacks duplicates. If no existing
      block is present, insert the new block right after the frontmatter (and
      before the first heading). Preserve the frontmatter and the rest of the
      body **byte-for-byte**; only the sentinel block region changes.
   This block is the **source of truth** for the compliance panel CommandHQ
   renders between the progress bar and the raw body — the web extracts the
   sentinel-delimited region from the PRD doc and displays it there. Keep it in
   sync by regenerating it on every run.
8. **Commit + push.** Stage only the changed requirements docs — `docs/PRD.md`
   (when present) and the changed `docs/plans/**` files — **plus the PRD doc
   whose compliance block changed in step 7** (`docs/PRD.html` or `docs/PRD.md`).
   Commit with a message like `chore(progress): update completion via
   /hq-update-progress` (include the Co-Authored-By trailer the repo uses). Push
   to the current branch's upstream with `git push` (use `gh` only if auth/PR is
   needed). Never call the GitHub Contents API to write — push with the dev's own
   git, so HQ's read sees the new SHA. The push happens **even if the DoD is not
   met** — the DoD is advisory and conformance is reported, not enforced.
9. **Emit the compliance report** (printed to the session, not posted anywhere):
   - **Definition-of-Done conformance** — restate the org DoD (from step 4) and,
     per criterion, report pass / fail / unknown with evidence:
     - `requiresUnitTests` → "unit tests passing" (which suite, green/red).
     - `requiresProdE2E` → "prod-E2E verified" (suite found? observed green? or
       "not-yet-enforced — hard gate deferred").
     End with the explicit line: **"Definition of Done is advisory — this push
     was not blocked by DoD conformance."**
   - **Requirements vs. built** — per doc (including `docs/PRD.md`): built /
     partial / missing, with the evidence file(s) for each "built."
   - **Drift** — discrepancies between the high-level **Project Requirements**
     (`docs/PRD.md`) and the **Detailed Requirements** (`docs/plans/**`): items
     present in one tier but not the other, or marked done in one but not built.
     This is the reconciliation surface; both tiers are now GitHub-sourced repo
     files, and they can still drift in altitude/coverage.
   - **Computed vs. previous `completion:`** per doc (delta), `docs/PRD.md`
     listed alongside the plan docs.
   - A one-line caveat: "v1 estimate; prod-E2E Definition-of-Done hard gate
     deferred (DoD is reported, not enforced)."
   - **Compliance breakdown** — the same Requirement | Status | Evidence rows
     that were persisted into the PRD doc's sentinel block in step 7 (PRD
     deliverables + attested constraints), so the printed report mirrors what HQ
     will render in the compliance panel.

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App
and parses the `completion:` numbers: `docs/PRD.md`'s drives the **Project
Requirements** bar (and the objectives roll-up), and the `docs/plans/` headline
doc's drives the **Detailed Requirements** root. The web bars reflect both. HQ
also extracts the `<!--hq:compliance … --><!--/hq:compliance-->` sentinel block
from the PRD doc (`docs/PRD.html` or `docs/PRD.md`) and renders it as a
**compliance panel between the progress bar and the raw body** — that
sentinel-delimited block written in step 7 is the **source of truth** for that
panel. HQ never writes back.

## Worked dry-run example (against THIS repo)

Run from the repo root in claude+:

```
/hq-update-progress
```

Expected behavior on this repo's current tree:

1. Locate the audited docs:
   - `docs/PRD.md` (Project Requirements headline, frontmatter `completion: 0`)
   - `docs/plans/command-hq-overview.md` (Detailed headline, if present)
   - `docs/plans/command-hq/01-plan-mapping.md` (frontmatter `completion: 35`)
   - `docs/plans/command-hq/02-weekly-update.md` (`completion: 45`)
   - `docs/plans/command-hq/05-agentforge.md` (`completion: 20`)
   - `docs/plans/2026-06-03-001-feat-command-hq-new-model-migration-plan.md`
2. Audit `05-agentforge.md`: it lists `/hq-startforge` and `/hq-endforge` as
   **Not built**. After this migration's skills land, the skill finds
   `.claude/skills/hq-startforge/SKILL.md` + `hq-endforge/SKILL.md` present →
   recomputes `05-agentforge.md` from `completion: 20` to, say, `completion: 30`
   (capture/register path documented; optimizer still deferred, fuzzy Forge
   still routed in backend).
3. Audit `01-plan-mapping.md`: `/hq-update-progress` itself now exists →
   nudges its number up; the prod-E2E hard gate is still deferred, so it stays
   well short of 100.
4. Fetch the org DoD (`GET /dod`). Say it returns
   `{ requiresUnitTests: true, requiresProdE2E: true, notes: "…" }`. Run the unit
   suites (`packages/*/npm test`) → green. Look for a prod E2E suite → none
   observed against the deployed env, so record it as unknown / not-yet-enforced.
5. Score `docs/PRD.md` against its high-level requirement bullets (the Project
   Requirements headline) — say its built surface lands it at `completion: 40`.
6. Edit those docs' `completion:` frontmatter in place (`docs/PRD.html` and the
   changed `docs/plans/**` docs).
7. Build + (re)insert the compliance breakdown block into the PRD doc. THIS repo
   ships `docs/PRD.html`, so emit the block as HTML and insert it right after the
   frontmatter, replacing any prior `<!--hq:compliance … --><!--/hq:compliance-->`
   block. Example block written into `docs/PRD.html`:

   ```
   <!--hq:compliance v1-->
   <h2>Compliance breakdown</h2>
   <table>
     <tr><th>Requirement</th><th>Status</th><th>Evidence</th></tr>
     <tr><td>Source Code</td><td>met</td><td>packages/* TS monorepo</td></tr>
     <tr><td>Technical Documentation</td><td>met</td><td>docs/PRD.html, docs/plans/**</td></tr>
     <tr><td>Demo Video</td><td>missing</td><td>no demo asset/link found in repo</td></tr>
     <tr><td>Test Results</td><td>partial</td><td>unit suites green; no prod E2E (gate deferred)</td></tr>
     <tr><td>AI Usage Log</td><td>partial</td><td>Co-Authored-By trailers in git log; no consolidated log</td></tr>
     <tr><td>Client-side SPA / no SSR</td><td>met</td><td>Vite SPA, no Next/Remix in deps</td></tr>
     <tr><td>No Redux Saga/Thunk fetching</td><td>met</td><td>no redux-saga/redux-thunk in package.json</td></tr>
     <tr><td>Spring Data JPA + Hibernate</td><td>n/a</td><td>n/a — TS/serverless monorepo, no Spring backend</td></tr>
     <tr><td>Lombok @Getter/@Setter/@Builder (not @Data)</td><td>n/a</td><td>n/a — no Java/Lombok in this stack</td></tr>
     <tr><td>Vite Module Federation remote (single route entry, shared deps, no hardcoded shell)</td><td>n/a</td><td>n/a — app is not a MF remote</td></tr>
   </table>
   <!--/hq:compliance-->
   ```
8. `git add docs/PRD.html docs/plans/... && git commit -m "chore(progress): update
   completion via /hq-update-progress" && git push`.
9. Print a compliance report, e.g.:

   ```
   Compliance report — 2026-06-03

   Definition of Done (org, advisory):
     [x] requiresUnitTests   PASS  — shared/backend/web suites green
     [?] requiresProdE2E     UNKNOWN — no prod E2E suite observed (hard gate deferred)
   Definition of Done is advisory — this push was not blocked by DoD conformance.

   doc                              prev  →  new   delta
   docs/PRD.md (Project Reqs)          0  →   40    +40
   command-hq/05-agentforge.md        20  →   30    +10
   command-hq/01-plan-mapping.md      35  →   38     +3
   command-hq/02-weekly-update.md     45  →   45      0

   Built:    /hq-startforge, /hq-endforge SKILL.md (.claude/skills/...)
   Partial:  /hq-update-progress (DoD prod-E2E hard gate — deferred)
   Missing:  fuzzy-Forge retirement (rest still references forge/propose.ts)

   Drift (Project Requirements docs/PRD.md vs Detailed docs/plans/):
     - "single-admin promote" listed high-level in docs/PRD.md; GitHub 05 has
       it as Not built.

   Caveat: v1 estimate; prod-E2E Definition-of-Done hard gate deferred
           (DoD is reported, not enforced).
   ```

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running
it: it edits the PRD doc's `completion:` (and at least one `docs/plans/` doc's),
(re)writes the single `<!--hq:compliance v1-->…<!--/hq:compliance-->` block
immediately after the PRD doc's frontmatter, pushes, and prints a compliance
report; after an HQ refresh the Project Requirements bar (from the PRD doc) and
the Detailed Requirements bar (from the `docs/plans/` headline) reflect the
pushed numbers, and HQ renders the sentinel block as the compliance panel.
Confirm only one compliance block exists after a re-run (regeneration replaces,
never stacks).
