---
status: active
type: feature
created: 2026-06-02
completion: 85
feature: weekly-update
---

# Feature 2 — Weekly Plan (`/weekly-update`)

A Claude command/skill that, when run, interviews the user about their coming
goals and then auto-generates a weekly update with two halves: **what was
accomplished last week** and **what's coming next**. This is the st6 weekly
lifecycle, built natively and automated by the harness.

## Flow

1. **Interview.** The skill asks the user what they want to do next week (in
   plain language).
2. **Conformity score (never blocks).** It scores how well each stated goal ladders
   up to the repo's **fixed high-level goals** (owned Supporting Outcomes / Project
   Requirements) and surfaces the score so a **manager can see when someone is
   drifting too far off**. It nudges the user to re-anchor but never blocks publish.
   *Now:* the conformity score is computed client-side in the `/weekly-update`
   skill (Claude Code); the old `weekly/agent.ts` challenger + `weekly/align.ts` were removed.
3. **Reconcile last week (auto, from git).** It reads the **git history since the
   last weekly report** and summarizes the week's commits/PRs as the "done"
   actuals (a git-history summary — no ticket linkage), and writes a checklist of:
   - what was completed,
   - which **high-level requirement items got checked off** this week,
   - how that **moved overall progress** (per-objective `priorPct → reportedPct`
     deltas), and
   - how that **compared to last week's plan** (planned vs. actual = the
     *reconciliation*).
   *Code today:* `weekly/align.ts` (`summarizeAlignment`, `computeDeltas`);
   `github/history.ts`. (`attributeDone` is ticket-based and needs rework for the
   no-ticket git-summary model.)
4. **Forward plan.** It fills the "what's coming next" section from the validated
   plan, each item linked to the outcome it advances, with projected deltas.
5. **Publish.** Writing the update rolls its completion up into Company
   Objectives.
   *Code today:* `rest/weekly.ts` (PUT draft, POST publish → `recomputeOrgRollup`).

## Native st6 framing (surface, don't hide)

Because st6 is the inspiration, the Weekly Update is labeled in its vocabulary:

- **Reconciliation** — the Done-from-git vs. plan comparison is literally called
  reconciliation.
- **Weekly lifecycle states** — `DRAFT → LOCKED → RECONCILING → RECONCILED`
  (+ carry-forward) as a first-class status on the weekly record. **No state blocks
  publish** (conformity is score-only). Transition triggers, the interview's
  accept/refuse/error states, and each state's failure path are design-phase work.

## Where it runs

A registered **Command HQ skill** (see [feature 3](./03-claude-code-integration.md)).
The interview + conformity loop run in the developer's session; the report and
publish hit the HQ REST API. The Weekly screen in HQ
(`screens/ProjectDetail/ProjectWeekly.tsx`, wireframe "HQ — Project › Weekly
Update") renders the *output*.

## Open questions

- "Since the last weekly report" boundary when reports are skipped or backfilled.
- Carry-forward semantics for unfinished plan items.
- Conformity is **score-only, never blocking** (resolved 2026-06-03); open: how the
  score is computed and surfaced to managers.

## Status

- **Built:** the agent logic (validate/assemble/deltas) and draft/publish
  endpoints.
- **Not built:** the `/weekly-update` skill itself; the interview/conformity UX;
  the explicit lifecycle states; the Weekly screen is currently display-only with
  placeholder data.
