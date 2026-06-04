---
name: hq-weekly-update
description: >-
  Run inside the claude+ PTY to generate and publish a weekly update. It reads
  the full git diff from today back 7 days as the "done" actuals, interviews the
  user on next-week goals, computes a never-blocking, manager-visible conformity
  score (how well the stated goals ladder up to the repo's fixed high-level
  goals), assembles a two-part report (done + plan), and POSTs it to Command
  HQ's weekly REST endpoint. Publishing is GATED on planning next week: it will
  not publish until you have entered at least one concrete next-week goal (the
  conformity score itself stays advisory and never blocks). Use when the user says
  "/hq-weekly-update", "weekly update", "do my weekly", "weekly report", or asks to
  reconcile last week and plan next week.
---

# /hq-weekly-update

Client-side weekly lifecycle (the st6 weekly, native + automated). The skill
generates the report in the developer's session and POSTs it to HQ; HQ
**stores and serves** it (the backend no longer generates weekly content — see
U4). Conformity is a stored, surfaced number, **never a gate**.

## Publish gate — you MUST plan next week first (BLOCKING)

The whole point of the weekly is to force planning, not just report what shipped.
So publishing is **hard-gated on the plan**, separate from the advisory conformity
score:

- You MUST conduct the next-week interview (step 3) and capture a **non-empty,
  concrete `plan`** — a prose summary naming at least one real next-week goal —
  before any `PUT` draft or `POST publish`.
- If the user skips it, gives an empty answer, or says "just publish / no plan",
  **do NOT publish.** Re-ask: "What are you going to work on next week? I can't
  publish the weekly without a plan." Keep asking until there is ≥1 real goal, or
  the user explicitly aborts the whole command (then publish nothing).
- "Done"-only reports are not allowed. A report with an empty `plan` (`""`) must
  never be PUT or published.
- This gate is on the **existence of a plan**, not its quality. A low conformity
  score still publishes (it only nudges); but *no plan at all* blocks. Vague
  filler ("misc work", "stuff") doesn't count — push for a concrete goal.

## When this runs

In the developer's claude+ session, inside a connected repo. It uses `git` for
the diff and an authenticated HTTP POST to the HQ REST API for publish.

## Inputs it gathers

- **Done (auto, from git).** `git log --since="7 days ago" --until=now` plus
  `git diff` stat for the window — the week's commits/PRs summarized as the
  actuals. No ticket linkage (tickets are gone); this is a git-history summary.
- **Plan (interview).** Ask the user, in plain language, what they want to do
  next week. Capture their goals as a single free-form `plan` **string** (a short
  prose summary; list the goals as lines/bullets within it). You may note inline
  which high-level goal each advances, but the stored shape is plain text — there
  is no structured `objectiveId` field on the weekly.
- **High-level goals (anchor).** The repo's fixed high-level goals — the owned
  Supporting Outcomes / Project Requirements (the headline `docs/plans/`
  doc + the HQ-owned Project Requirements). Used only to score conformity.

## Conformity score (never blocks)

Score, per stated goal and overall, how well the plan ladders up to the fixed
high-level goals — a 0–100 integer. A low score means the user is drifting; the
skill **nudges** them to re-anchor and surfaces the number for **manager
visibility**, but it always lets publish proceed. There is no state that blocks
publish (mirrors `02-weekly-update.md`: conformity is score-only).

Heuristic (v1): for each goal stated in the plan, judge semantic alignment to the
nearest high-level goal (full / partial / none → 1.0 / 0.5 / 0.0), average across
goals, scale to 0–100. Print the per-goal rationale so the manager sees *why*.

## Steps

1. **Resolve the window + ISO week.** Today back 7 days; the target week is the
   current ISO week, e.g. `2026-W23`.
2. **Summarize done from git.** Read the diff/log for the window; produce a
   concise `done` summary (what shipped, which high-level items moved).
3. **Interview for the plan (REQUIRED — gate).** Ask the user what they will work
   on next week; record their goals into the `plan` **string**. **You may not
   advance to PUT/POST until the `plan` names ≥1 concrete goal.** If they decline
   or stall, re-ask and explain you can't publish a weekly without a plan. Only an
   explicit abort of the whole command ends it here (publishing nothing).
4. **Compute the conformity score** (above). Show it; nudge if low; never block.
5. **Assemble the report.** `done` summary text + the **non-empty** `plan` summary +
   `conformityScore`.
6. **POST to HQ — only if the gate passed.** Re-check the `plan` string is
   non-empty; if it is blank, STOP (do not PUT, do not publish) and return to
   step 3. Otherwise PUT the draft, then POST publish (contract below). Publish
   triggers HQ's org roll-up recompute (U3/U4).
7. **Confirm.** Print the HQ Weekly screen URL and the stored conformity score.

## HQ POST contract (what the backend weekly unit must accept)

The skill targets the existing project-scoped weekly routes
(`packages/backend/src/rest/weekly.ts`), extended by U4 to a client-posted
report. Auth: an `Authorization: Bearer <token>` header — the weekly routes
accept EITHER the claude+ wrapper **device token** OR a **Cognito ID token** (the
handler verifies both via `rest/bearerAuth.ts`; the routes use
`HttpNoneAuthorizer` so the gateway doesn't pre-reject the device token). The
project must be owned by the caller — HQ enforces `ownerUserId === principal.userId`.

- **Draft:** `PUT /projects/:pid/weekly/:week`
- **Publish:** `POST /projects/:pid/weekly/:week/publish`

Request body (JSON) the skill sends and the backend must validate + store:

```jsonc
{
  // free-form prose summary of the week's done actuals (from the git diff).
  // U4 makes `done` a free-form summary string (the old ticket-derived
  // WeeklyItem[] shape is dropped).
  "done": "Landed U13–U15 client skills; wired the weekly POST contract; ...",

  // next-week goals as a free-form prose summary (a STRING, not an array). List
  // the goals as lines/bullets in the string; there is no structured objectiveId
  // field on the weekly.
  "plan": "1. Wire the prod-E2E gate into /hq-update-progress.\n2. Promote the forged agent org-wide.",

  // never-blocking conformity score, 0–100 integer. Stored + surfaced for
  // manager visibility; HQ must NOT gate publish on it.
  "conformityScore": 82
}
```

`projectId` and `isoWeek` come from the path, not the body (the backend injects
them, as it does today). The response echoes the stored `update`.

**Assumptions the backend weekly unit (U4) must match:**

1. `weeklyUpdateSchema`: BOTH `done` and `plan` are free-form **string** summaries
   (the old ticket-derived `WeeklyItem[]` and `{text, objectiveId}[]` shapes are
   dropped); `conformityScore?: number` (0–100) is optional. The ticket-derived
   `completionPct`/attribution fields are gone.
2. `PUT` stores the posted body as the draft (`validated: false`); `POST
   .../publish` flips `validated: true` and recomputes the org roll-up. No
   server-side weekly content generation remains (`assembleWeekly` /
   `attributeDone` deleted).
3. `conformityScore` round-trips through store + serve and is purely
   informational — there is no code path where a low score rejects the write.

## Worked dry-run example (against THIS repo)

```
/hq-weekly-update
```

1. Window: 2026-05-27 → 2026-06-03; ISO week `2026-W23`.
2. From git: "Authored the four client skills (/hq-update-progress, /hq-weekly-update,
   /hq-startforge, /hq-endforge); began the de-ticket migration units."
3. Interview → plan (a prose summary naming the goals), e.g. two goals:
   "Wire the prod-E2E Definition-of-Done gate" and "Refactor the Saturday side
   project".
4. Conformity: goal 1 ladders fully to the fixed goals (1.0); goal 2 doesn't
   (0.0) → overall ~50. Nudge: "1 of 2 goals is off-roadmap"; still publishes.
5. Body:
   ```jsonc
   { "done": "Authored 4 client skills; started de-ticket migration.",
     "plan": "1. Wire the prod-E2E gate.\n2. Refactor the Saturday side project.",
     "conformityScore": 50 }
   ```
6. `PUT /projects/workflow-harness/weekly/2026-W23` then
   `POST /projects/workflow-harness/weekly/2026-W23/publish`.
7. Print: "Published 2026-W23 to HQ (conformity 50). See Weekly screen:
   https://d13sqkbwzqe38l.cloudfront.net/ → Project › Weekly."

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: it posts a
report that appears on the HQ Weekly screen with a conformity score, and publish
never blocks. The backend contract (POST report → stored + served, conformity
round-trips) is covered by the U4/U14 backend tests, not by this file.
