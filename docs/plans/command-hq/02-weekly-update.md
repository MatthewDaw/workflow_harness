---
status: active
type: feature
created: 2026-06-02
completion: 88
feature: weekly-update
---

# Feature 2 — Weekly Plan (`/hq-weekly-update`)

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
   *Now:* the conformity score is computed client-side in the `/hq-weekly-update`
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
   *Code today (built):* this reconciliation is assembled **client-side** by the
   `/hq-weekly-update` skill from the git diff today→−7d. The old server-side
   `weekly/align.ts` (`summarizeAlignment`/`computeDeltas`/`attributeDone`) and
   `weekly/agent.ts` were **deleted** in the migration — the whole `weekly/` module
   is gone; the backend no longer generates weekly content. `github/history.ts`
   remains for git-history reads.
4. **Forward plan.** It fills the "what's coming next" section from the validated
   plan, each item linked to the outcome it advances, with projected deltas.
5. **Publish.** The skill POSTs the assembled report (`done` summary, `plan`,
   `conformityScore`) to HQ; publish recomputes the Company Objectives roll-up.
   *Code today (built):* `rest/weekly.ts` is now **store/serve only** — it validates
   and stores the client-posted report (PUT draft / POST publish →
   `recomputeOrgRollup`) and serves it; `conformityScore` round-trips and is never a
   gate.

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

- **Built:** the `/hq-weekly-update` skill (`.claude/skills/hq-weekly-update/`) with the
  interview + client-side never-blocking conformity score; the store/serve
  `rest/weekly.ts` (draft/publish, `conformityScore` round-trip); the Weekly screen
  (`ProjectWeekly.tsx`) renders the posted report.
- **Not built / deferred:** the explicit `DRAFT → LOCKED → RECONCILING →
  RECONCILED` lifecycle states as first-class status; the manager-visibility
  conformity surfacing UX is minimal.
