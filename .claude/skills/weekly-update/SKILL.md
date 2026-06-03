---
name: weekly-update
description: >-
  Run inside the claude+ PTY to generate and publish a weekly update. It reads
  the full git diff from today back 7 days as the "done" actuals, interviews the
  user on next-week goals, computes a never-blocking, manager-visible conformity
  score (how well the stated goals ladder up to the repo's fixed high-level
  goals), assembles a two-part report (done + plan), and POSTs it to Command
  HQ's weekly REST endpoint. It never blocks publish. Use when the user says
  "/weekly-update", "weekly update", "do my weekly", "weekly report", or asks to
  reconcile last week and plan next week.
---

# /weekly-update

Client-side weekly lifecycle (the st6 weekly, native + automated). The skill
generates the report in the developer's session and POSTs it to HQ; HQ
**stores and serves** it (the backend no longer generates weekly content — see
U4). Conformity is a stored, surfaced number, **never a gate**.

## When this runs

In the developer's claude+ session, inside a connected repo. It uses `git` for
the diff and an authenticated HTTP POST to the HQ REST API for publish.

## Inputs it gathers

- **Done (auto, from git).** `git log --since="7 days ago" --until=now` plus
  `git diff` stat for the window — the week's commits/PRs summarized as the
  actuals. No ticket linkage (tickets are gone); this is a git-history summary.
- **Plan (interview).** Ask the user, in plain language, what they want to do
  next week. Capture each goal as a free-text item; optionally tag it with the
  Supporting Outcome / objective it advances (`objectiveId`).
- **High-level goals (anchor).** The repo's fixed high-level goals — the owned
  Supporting Outcomes / Project Requirements (the headline `docs/plans/`
  doc + the HQ-owned Project Requirements). Used only to score conformity.

## Conformity score (never blocks)

Score, per plan item and overall, how well the stated goals ladder up to the
fixed high-level goals — a 0–100 integer. A low score means the user is
drifting; the skill **nudges** them to re-anchor and surfaces the number for
**manager visibility**, but it always lets publish proceed. There is no state
that blocks publish (mirrors `02-weekly-update.md`: conformity is score-only).

Heuristic (v1): for each plan item, judge semantic alignment to the nearest
high-level goal (full / partial / none → 1.0 / 0.5 / 0.0), average across items,
scale to 0–100. Print the per-item rationale so the manager sees *why*.

## Steps

1. **Resolve the window + ISO week.** Today back 7 days; the target week is the
   current ISO week, e.g. `2026-W23`.
2. **Summarize done from git.** Read the diff/log for the window; produce a
   concise `done` summary (what shipped, which high-level items moved).
3. **Interview for the plan.** Ask next-week goals; record them as `plan` items.
4. **Compute the conformity score** (above). Show it; nudge if low; never block.
5. **Assemble the report.** `done` summary text + `plan` items +
   `conformityScore`.
6. **POST to HQ.** PUT the draft, then POST publish (contract below). Publish
   triggers HQ's org roll-up recompute (U3/U4).
7. **Confirm.** Print the HQ Weekly screen URL and the stored conformity score.

## HQ POST contract (what the backend weekly unit must accept)

The skill targets the existing project-scoped weekly routes
(`packages/backend/src/rest/weekly.ts`), extended by U4 to a client-posted
report. Auth: the device/session bearer token (the project must be owned by the
caller — HQ enforces `ownerUserId === principal.userId`).

- **Draft:** `PUT /projects/:pid/weekly/:week`
- **Publish:** `POST /projects/:pid/weekly/:week/publish`

Request body (JSON) the skill sends and the backend must validate + store:

```jsonc
{
  // free-form prose summary of the week's done actuals (from the git diff).
  // U4 makes `done` a free-form summary string (the old ticket-derived
  // WeeklyItem[] shape is dropped).
  "done": "Landed U13–U15 client skills; wired the weekly POST contract; ...",

  // next-week goals. Each item is free text; objectiveId is optional and links
  // the item to the Supporting Outcome it advances.
  "plan": [
    { "text": "Wire prod-E2E gate into /update-progress", "objectiveId": "so-123" },
    { "text": "Promote forged agent org-wide", "objectiveId": null }
  ],

  // never-blocking conformity score, 0–100 integer. Stored + surfaced for
  // manager visibility; HQ must NOT gate publish on it.
  "conformityScore": 82
}
```

`projectId` and `isoWeek` come from the path, not the body (the backend injects
them, as it does today). The response echoes the stored `update`.

**Assumptions the backend weekly unit (U4) must match:**

1. `weeklyUpdateSchema` is extended: `done` becomes a free-form **string**
   summary (today it is `WeeklyItem[]`); `plan` stays an array of items with
   optional `objectiveId`; add `conformityScore?: number` (0–100). The
   ticket-derived `completionPct`/attribution fields are dropped.
2. `PUT` stores the posted body as the draft (`validated: false`); `POST
   .../publish` flips `validated: true` and recomputes the org roll-up. No
   server-side weekly content generation remains (`assembleWeekly` /
   `attributeDone` deleted).
3. `conformityScore` round-trips through store + serve and is purely
   informational — there is no code path where a low score rejects the write.

## Worked dry-run example (against THIS repo)

```
/weekly-update
```

1. Window: 2026-05-27 → 2026-06-03; ISO week `2026-W23`.
2. From git: "Authored the four client skills (/update-progress, /weekly-update,
   /startforge, /endforge); began the de-ticket migration units."
3. Interview → plan:
   - "Wire the prod-E2E Definition-of-Done gate" (objectiveId: the progress SO)
   - "Refactor the Saturday side project" (no objectiveId)
4. Conformity: item 1 ladders fully to the fixed goals (1.0); item 2 doesn't
   (0.0) → overall ~50. Nudge: "1 of 2 goals is off-roadmap"; still publishes.
5. Body:
   ```jsonc
   { "done": "Authored 4 client skills; started de-ticket migration.",
     "plan": [ {"text":"Wire prod-E2E gate","objectiveId":"so-progress"},
               {"text":"Refactor side project"} ],
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
