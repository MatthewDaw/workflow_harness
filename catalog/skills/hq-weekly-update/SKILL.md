---
name: hq-weekly-update
description: >-
  Run inside the claude+ PTY to drive the itemized weekly commit lifecycle
  (DRAFT → LOCKED → RECONCILING → RECONCILED). It is PLAN-ANCHORED: it reads the
  active `docs/plans/*.md`, projects the implementation-units (U-IDs) the user
  intends into itemized `WeeklyCommit` proposals — each carrying EITHER a hard
  Supporting-Outcome link (taken from the plan's Requirements & Traceability
  table) OR a typed `orphanReason` — and POSTs one commit per unit. The human
  ratifies/edits the SO links in the UI; the agent then LOCKs the week. At week
  end it reconciles each commit's actual status + outcome from git and plan-unit
  progress, then completes reconciliation (which carries incomplete items into
  next week's DRAFT). It no longer sends a derived `category`/`priority` (the
  server derives them) or a conformity score (deleted — replaced by the
  concentration / starvation / carry-aging metrics). Use when the user says
  "/hq-weekly-update", "weekly update", "do my weekly", "weekly commit", "plan my
  week", or asks to commit next week's work and/or reconcile last week.
---

# /hq-weekly-update

Plan-anchored weekly commit lifecycle (the st6 weekly, native + automated). The
skill PROPOSES itemized commits in the developer's session from the active
plan's U-IDs, the human RATIFIES the Supporting-Outcome links in the UI, the
agent LOCKs the week, and at week end RECONCILES each commit from git. HQ
**stores and serves** the itemized commits and enforces every transition's
legality; the backend never generates weekly content.

This skill replaces the legacy prose-blob weekly (`done`/`plan` strings +
`conformityScore` + `PUT`/`POST …/publish`). Those are gone: a week is now a
`weekly_plan` plus itemized `weekly_commits`, LOCK is the new "publish", and the
chess layer (`category` + `priorityNumeric`) is **derived server-side** — the
skill must not author it.

## The lifecycle (what the agent drives)

```
DRAFT  ──POST …/lock──▶  LOCKED  ──POST …/reconcile/start──▶  RECONCILING  ──POST …/reconcile/complete──▶  RECONCILED
  ▲                                                                                                              │
  └──────────────── carry-forward: incomplete (planned/partial) commits clone into NEXT week's DRAFT ───────────┘
```

- **DRAFT** — the agent adds/edits/deletes commits (`POST/PUT/DELETE …/commits`).
  Planned fields are editable only here; they freeze at LOCK.
- **LOCKED** — the new "publish." The server re-checks the SO-or-orphan invariant
  and refuses an empty week; the response carries a structured `blockers[]` so the
  agent can self-correct.
- **RECONCILING** — per-commit actuals are recorded (`PUT …/commits/:cid` setting
  `status` + `actualOutcome`).
- **RECONCILED** — terminal. Completing reconciliation runs ONE transaction that
  stamps the sources, clones every `planned`/`partial` commit into next week's
  DRAFT (`carryDepth + 1`), and recomputes the org roll-up.

## Hard rules (BLOCKING)

1. **Every commit carries an SO link OR a typed `orphanReason` — never neither.**
   The SO link is **proposed from the plan's Requirements & Traceability table**
   (the U-ID → SO mapping), not guessed. When a unit ladders up to nothing real,
   send a typed `orphanReason` (`KTLO | Incident | Exploration | ExternalAsk`)
   instead. "Neither" is rejected by the Zod refinement, the app-layer check, the
   DB CHECK, AND the lock guard — so a commit with neither will simply fail.
2. **The agent does NOT self-lock past unratified SO links.** Proposed links are a
   suggestion. After POSTing the commits, hand off to the human to ratify/edit the
   links in the U10 editor; only LOCK once the user has confirmed (enforcement at
   the human boundary). If running fully unattended with no human to ratify, stop
   after proposing the commits and report the week is in DRAFT awaiting ratify.
3. **Do NOT send `category`, `priorityNumeric`, or any `conformityScore`.** The
   server DERIVES `category` + `priorityNumeric` (KTD4) and ignores client-sent
   values; the conformity score is deleted. Sending them is wasted noise.
4. **Reconcile actuals are OBSERVED, not assumed.** Each commit's reconciled
   `status` + `actualOutcome` come from git (commits/PRs in the window) and the
   plan-unit's real progress — never optimistic guesses.

## When this runs

In the developer's claude+ session, inside a connected repo. It uses `git` for
the window diff/log and the plan's U-IDs for the proposal, and authenticated HTTP
calls to the HQ REST API for every transition.

**Getting the `projectId` — never guess it.** claude+ injects the authoritative HQ
project id into the session as `$CLAUDE_PLUS_PROJECT_ID` (alongside `$CLAUDE_PLUS_REPO`
and `$CLAUDE_PLUS_API_URL`), mirrored in `$CLAUDE_CONFIG_DIR/hq-project.json`. Use
`$CLAUDE_PLUS_PROJECT_ID` directly as the `:pid` in any `/projects/<projectId>/…`
call. Do NOT derive, slug, or probe candidate ids. If `$CLAUDE_PLUS_PROJECT_ID` is
empty, this session is not running under claude+ — say so and stop; do not guess.

## Steps

### Propose (DRAFT)

1. **Resolve the window + ISO week.** Today back 7 days; the target week is the
   current ISO week, e.g. `2026-W23`. **Then `GET /projects/:pid/weekly` and read
   the `calibration` field** (U19) — the caller's trailing locked-vs-done rate. Use
   it to RIGHT-SIZE the proposal: if `calibration.rate ≈ 0.6`, propose ~6 of the 10
   candidate units (the 6 highest-leverage), not all 10 — a person who finishes 60%
   of what they lock should not lock a wish list. When `calibration` is **absent**
   (a first-ever week, no history), propose an unscaled set and let reconciliation
   teach the rate. The calibration is advisory — it never blocks; it just sizes the
   ask. (It also carries `highPriorityFirstCount`/`highPriorityTotal`: if the
   higher-leverage half routinely slips, lead with fewer, higher-leverage units.)
2. **Read the active plan + its Requirements & Traceability table.** Open the
   active `docs/plans/*.md`. Each plan maps its implementation-units (U-IDs) to
   requirements (R-IDs), and the requirements ladder up to the repo's owned
   **Supporting Outcomes** (the RCDO leaves visible via `GET /objectives`, level
   `supporting_outcome`). Build, per intended U-ID, the **proposed** SO link by
   following U-ID → R-ID → the Supporting Outcome that requirement serves. Keep it
   a *proposal* — the human ratifies it in step 4.
3. **Project the intended U-IDs into commit proposals + POST each.** With the user,
   pick the units they intend to do next week. For each, `POST …/commits` a
   `WeeklyCommit` carrying:
   - a `title` (the unit's intent, terse),
   - EITHER a `supportingOutcomeId` (the proposed SO from step 2) OR an
     `orphanReason` (when the unit ladders to nothing real),
   - optional `alsoAdvances: string[]` (informational secondary SO ids — they
     surface in the leverage view but never split roll-up credit).
   Do NOT send `category`/`priorityNumeric` (derived server-side). The week's
   DRAFT plan is auto-created on the first commit POST. Right-size the set to what
   the user can realistically finish — fewer high-leverage units beats a long wish
   list.
4. **Hand off for ratification (human boundary).** Tell the user the week is in
   DRAFT with N proposed commits, and that they should open the Weekly editor to
   ratify/edit each SO link (the derived `category`/`priority` render read-only).
   **Do not LOCK until the user confirms the links are right.**

### Lock (DRAFT → LOCKED)

5. **`POST …/lock`.** Once the user has ratified, lock the week. If the response is
   a `409` with `blockers[]`, FIX each blocker (add the missing SO/orphan to the
   named `commitId`, or add a commit to an empty week) and retry — never report
   success on a 409.

### Reconcile (LOCKED → RECONCILING → RECONCILED)

6. **`POST …/reconcile/start`** to open per-commit actual recording.
7. **Record each commit's actuals from git + plan progress.** For every commit,
   `PUT …/commits/:cid` setting its reconciled `status`
   (`done | partial | dropped`) and a free-form `actualOutcome` — derived from the
   git window (commit subjects, merged PRs) and the plan-unit's observed progress.
   Every commit must end terminal (not `planned`) before completing.
8. **`POST …/reconcile/complete`.** Refuses (`409` + the still-`planned` list) if
   any commit is still `planned`; otherwise it stamps `RECONCILED`, carries the
   incomplete (`planned`/`partial`) items into next week's DRAFT
   (`carryDepth + 1`), and recomputes the org roll-up in one transaction. The
   response reports `carriedTo`, `carriedCount`, and (when any clone reaches
   `carryDepth ≥ 3`) a `deepCarryNudge` — surface it to the user ("3 items carried
   to 2026-W24; one is now at depth 3 — consider decomposing or killing it").
9. **Confirm.** Print the HQ Weekly screen URL, the week's final status, and the
   carry-forward summary (count + any depth nudge).

## HQ REST contract (what the backend already accepts)

The skill targets the project-scoped weekly routes
(`packages/backend/src/rest/weekly.ts` for CRUD, `weeklyTransitions.ts` for the
transitions). Auth: an `Authorization: Bearer <token>` header — the routes accept
EITHER the claude+ wrapper **device token** OR a **Cognito ID token** (verified by
`rest/bearerAuth.ts`; the routes use `HttpNoneAuthorizer` so the gateway doesn't
pre-reject the device token). The project must be owned by the caller — HQ
enforces `ownerUserId === principal.userId` (a non-owner gets `404`).

`:pid` is `$CLAUDE_PLUS_PROJECT_ID`; `:week` is the ISO week (e.g. `2026-W23`);
`:cid` is the commit id returned by the create call. `projectId` and `isoWeek`
come from the **path**, not the body — the server injects them.

| Step | Method + path | Purpose |
| --- | --- | --- |
| read | `GET /projects/:pid/weekly` | every week (plan + commits) + the caller's `calibration` |
| read | `GET /projects/:pid/weekly/:week` | one week (plan + commits) |
| propose | `POST /projects/:pid/weekly/:week/commits` | add a commit (DRAFT only) |
| edit | `PUT /projects/:pid/weekly/:week/commits/:cid` | edit / set actuals on a commit |
| remove | `DELETE /projects/:pid/weekly/:week/commits/:cid` | drop a commit (DRAFT only) |
| lock | `POST /projects/:pid/weekly/:week/lock` | DRAFT → LOCKED |
| reconcile start | `POST /projects/:pid/weekly/:week/reconcile/start` | LOCKED → RECONCILING |
| reconcile complete | `POST /projects/:pid/weekly/:week/reconcile/complete` | RECONCILING → RECONCILED |

### Commit create body (`POST …/commits`)

```jsonc
{
  // terse statement of the unit's intent
  "title": "U6 — single-source roll-up (progressPct removed)",

  // EITHER a primary SO link (the proposed SO from the plan's R-table) ...
  "supportingOutcomeId": "so_reconciled_rollup",

  // ... OR a typed orphan reason when the unit ladders to nothing real:
  //   one of KTLO | Incident | Exploration | ExternalAsk  (omit when SO is set)
  // "orphanReason": "Exploration",

  // optional informational secondaries — surfaced, but never split roll-up credit
  "alsoAdvances": [],
}
```

Send EXACTLY one of `supportingOutcomeId` or `orphanReason`. Do **not** send
`category`, `priorityNumeric`, `status`, or any `conformityScore` — the server
derives the chess fields and defaults `status` to `planned`. A dangling
`supportingOutcomeId` (not an SO in the caller's org) is a `400`. The response
echoes the stored `commit` (with its server-assigned `id` + derived
`category`/`priorityNumeric`).

### Reconcile body (`PUT …/commits/:cid`, during RECONCILING)

```jsonc
{
  // reconciled actual: done | partial | dropped  (planned never completes)
  "status": "done",
  // free-form observed result, cited from git / plan-unit progress
  "actualOutcome": "Shipped in #482; leafPct now derives from reconciled commits."
}
```

### Lock / complete responses to handle

- `lock` 409 → `{ error, blockers: [{ commitId?, reason }] }`. Fix each blocker
  (missing SO/orphan, or empty week) and retry.
- `reconcile/complete` 409 → `{ error, blockers: [{ commitId, reason }] }` listing
  the still-`planned` commits. Set their actuals, then retry.
- `reconcile/complete` 200 → `{ plan, carriedTo, carriedCount, deepCarryNudge? }`.
  Report the carry summary + any depth nudge.

### Calibration on the read (`GET …/weekly`, U19)

The `GET /projects/:pid/weekly` response carries an optional `calibration` the
propose step reads to size the ask:

```jsonc
{
  "weeks": [ /* … */ ],
  "calibration": {
    "rate": 0.6,                  // trailing done / locked — propose ~rate of the candidates
    "lockedCount": 10,            // terminal commits reconciled so far
    "doneCount": 6,
    "highPriorityFirstCount": 4,  // of the higher-leverage half, how many shipped
    "highPriorityTotal": 5
  }
}
```

`calibration` is **absent** until the caller has reconciled at least one week (a
first-ever week) — propose an unscaled set then. It is recomputed automatically on
every `reconcile/complete`; the skill never writes it.

## Worked dry-run example (against THIS repo)

```
/hq-weekly-update
```

1. Window: 2026-06-03 → 2026-06-10; ISO week `2026-W24`. `GET …/weekly` returns
   `calibration.rate ≈ 0.6` from prior weeks — so of ~5 candidate units, propose
   the 3 highest-leverage (not all 5).
2. Read `docs/plans/2026-06-10-006-feat-weekly-commit-lifecycle-plan.md` and its
   R-table. The user intends U6 and U17 next week. U6 → R6 → the "single roll-up
   source" Supporting Outcome; U17 → R12 → the "alignment metrics" Supporting
   Outcome. A spike on driver bundling ladders to nothing → `orphanReason:
   "Exploration"`.
3. POST three commits (proposed links, no category/priority):
   ```jsonc
   { "title": "U6 — single-source roll-up", "supportingOutcomeId": "so_rollup" }
   { "title": "U17 — concentration / starvation / carry-aging", "supportingOutcomeId": "so_metrics" }
   { "title": "Spike: Neon driver bundle size", "orphanReason": "Exploration" }
   ```
   The DRAFT plan auto-creates on the first POST.
4. Tell the user: "Week 2026-W24 is in DRAFT with 3 proposed commits — open the
   Weekly editor and ratify the SO links (category/priority are derived), then
   I'll lock." Wait for confirmation.
5. `POST /projects/$CLAUDE_PLUS_PROJECT_ID/weekly/2026-W24/lock`. If a `409` names
   a commit missing its link, fix it and retry.
6. At week end: `POST …/reconcile/start`; for each commit `PUT …/commits/:cid`
   with the observed `status` + `actualOutcome` from git (e.g. U6 `done`, U17
   `partial`, the spike `dropped`); then `POST …/reconcile/complete`.
7. Complete returns `{ carriedTo: "2026-W25", carriedCount: 1, ... }` — the
   `partial` U17 commit carried into next week's DRAFT at `carryDepth 1`. Print:
   "Reconciled 2026-W24; 1 item carried to 2026-W25. See Weekly screen:
   https://d13sqkbwzqe38l.cloudfront.net/ → Project › Weekly."

## Verification (this is a doc, not code)

Test expectation: none — SKILL.md authoring. Verified by running it: it creates
plan-anchored, SO-linked itemized commits visible in the U10 Weekly editor, the
week is lockable once the links are ratified, and reconcile-complete carries the
incomplete items into next week's DRAFT. The backend contract (commit CRUD +
transitions + carry-forward + roll-up) is covered by the U3/U4/U5/U6 backend
tests, not by this file.
