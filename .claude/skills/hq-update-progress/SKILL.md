---
name: hq-update-progress
description: >-
  Run inside the claude+ PTY to audit a repo against its docs/plans/
  requirements, compute a code-vs-docs completion percentage per plan doc, write
  that number into each doc's `completion:` frontmatter, commit and push it to
  GitHub with the developer's own git/gh credentials, and emit a compliance
  report. Command HQ then READS the pushed number to move the Project
  Requirements + Detailed Requirements bars — HQ never writes to GitHub. Use
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

1. **Locate the plan tree.** Enumerate `docs/plans/**/*.md`. The top-of-folder
   doc (e.g. `docs/plans/command-hq-overview.md`, or the highest-level doc if no
   overview exists) is the **headline** doc — its `completion:` is the single
   number HQ shows on both the Project Requirements bar and the Detailed
   Requirements root. Every other doc gets its own per-doc `completion:`.
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
   structure, and roll up to a 0–100 integer. The headline doc's number is the
   weighted mean of its child docs (mirror the roll-up math in
   `packages/backend/src/projections/rollup.ts`: leaf = built-fraction,
   internal = mean of children) so the pushed number matches how HQ rolls up.
   Use the DoD evaluation (step 4) as a corroborating signal — e.g. don't claim
   a doc 100% done if the org DoD's unit-test floor is failing — but do not
   block on it.
6. **Write the frontmatter.** Edit each doc's YAML frontmatter, setting or
   updating `completion:` to the computed integer. Preserve all other
   frontmatter keys and the doc body byte-for-byte. If a doc has no frontmatter
   block, add a minimal one (`---\ncompletion: N\n---`).
7. **Commit + push.** Stage only the changed `docs/plans/**` files. Commit with
   a message like `chore(progress): update completion via /hq-update-progress`
   (include the Co-Authored-By trailer the repo uses). Push to the current
   branch's upstream with `git push` (use `gh` only if auth/PR is needed). Never
   call the GitHub Contents API to write — push with the dev's own git, so HQ's
   read sees the new SHA. The push happens **even if the DoD is not met** — the
   DoD is advisory and conformance is reported, not enforced.
8. **Emit the compliance report** (printed to the session, not posted anywhere):
   - **Definition-of-Done conformance** — restate the org DoD (from step 4) and,
     per criterion, report pass / fail / unknown with evidence:
     - `requiresUnitTests` → "unit tests passing" (which suite, green/red).
     - `requiresProdE2E` → "prod-E2E verified" (suite found? observed green? or
       "not-yet-enforced — hard gate deferred").
     End with the explicit line: **"Definition of Done is advisory — this push
     was not blocked by DoD conformance."**
   - **Requirements vs. built** — per doc: built / partial / missing, with the
     evidence file(s) for each "built."
   - **Drift** — discrepancies between the **HQ-owned Project Requirements**
     (the high-level list HQ holds canonically) and the **GitHub docs**: items
     present in one tier but not the other, or marked done in one but not built.
     This is the reconciliation surface; HQ owns Project Requirements, GitHub
     owns Detailed Requirements, and they can drift.
   - **Computed vs. previous `completion:`** per doc (delta).
   - A one-line caveat: "v1 estimate; prod-E2E Definition-of-Done hard gate
     deferred (DoD is reported, not enforced)."

## What HQ does after this

Nothing is posted. On the next project refresh (`POST /projects/:id/refresh` or
connect-time read), the backend fetches the repo via the read-only GitHub App,
parses the headline doc's `completion:`, stores `progressPct`, and the web
Project Requirements + Detailed Requirements bars reflect it. HQ never writes
back.

## Worked dry-run example (against THIS repo)

Run from the repo root in claude+:

```
/hq-update-progress
```

Expected behavior on this repo's current tree:

1. Enumerate `docs/plans/`:
   - `docs/plans/command-hq-overview.md` (headline, if present)
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
5. Edit those docs' `completion:` frontmatter in place.
6. `git add docs/plans/... && git commit -m "chore(progress): update
   completion via /hq-update-progress" && git push`.
7. Print a compliance report, e.g.:

   ```
   Compliance report — 2026-06-03

   Definition of Done (org, advisory):
     [x] requiresUnitTests   PASS  — shared/backend/web suites green
     [?] requiresProdE2E     UNKNOWN — no prod E2E suite observed (hard gate deferred)
   Definition of Done is advisory — this push was not blocked by DoD conformance.

   doc                              prev  →  new   delta
   command-hq/05-agentforge.md        20  →   30    +10
   command-hq/01-plan-mapping.md      35  →   38     +3
   command-hq/02-weekly-update.md     45  →   45      0

   Built:    /hq-startforge, /hq-endforge SKILL.md (.claude/skills/...)
   Partial:  /hq-update-progress (DoD prod-E2E hard gate — deferred)
   Missing:  fuzzy-Forge retirement (rest still references forge/propose.ts)

   Drift (HQ Project Requirements vs GitHub docs):
     - "single-admin promote" listed in HQ PR; GitHub 05 has it as Not built.

   Caveat: v1 estimate; prod-E2E Definition-of-Done hard gate deferred
           (DoD is reported, not enforced).
   ```

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. The skill is verified by running
it: it edits at least one `docs/plans/` doc's `completion:`, pushes, and prints
a compliance report; after an HQ refresh the bar reflects the pushed number.
