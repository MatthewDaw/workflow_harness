---
title: 'feat: Verified Learning — PR-merge-gated fold-forward, code-anchor supersession, and the necessity gate (claude+ / Command HQ)'
type: feat
status: ready-to-implement
date: 2026-06-12
revised: 2026-06-12 — PIVOT to PR-merge-gated learning. The merged PR is the unit; main''s merge history is an ordered event log we fold forward. This deletes the session→commit→PR join, the async pending-vote store, and the 4-state vote machine. See "What the pivot deletes."
origin: design session 2026-06-12; requirements doc docs/brainstorms/2026-06-12-verified-learning-requirements.md
target-codebases: NEW Python learning service — the ENTIRE loop incl. merge-webhook receipt, distillation/NLI/corroboration/supersession/necessity, fold/un-fold skill-revision writes, and the learning read API (container Lambda, reusing agent-families'' core); packages/backend (TS — non-learning catalog authoring + web read-contract only); wrapper (Go — U7 capture + existing skill materialization)
note: READY TO IMPLEMENT (reviewed 2026-06-12 — multi-persona doc review + grounded research sweep + independent verification pass; all findings folded in). The principled design, not the minimal flag — the pivot makes it SMALLER than the prior draft, not larger.
---

# Verified Learning (PR-merge-gated)

## Summary

The live platform learns insights ("ideas") and corroborates them — but **corroboration is recurrence-across-sessions, monotonic, no-decay, and negation-blind** (`ideas/corroborate.ts` MERGE-REWRITEs anything ≥ 0.9 cosine, so a *contradiction* on the same topic is blended into the incumbent instead of retiring it). So a wrong lesson that recurs in two sessions folds into a skill, and a later decision that reverses a bad one gets mushed into the old idea. That is the co-occurrence / bloat failure.

**The pivot:** stop trying to learn from sessions-in-general and gate learning *entirely on merged pull requests.* The merged PR is the atomic unit of verified knowledge — it carries a diff, a description, review approvals, CI status, and a merge event, all of it ground truth that already exists. **Main's merge history is an ordered event log; we fold it forward.** Each merge → distill candidate insight(s) from `(diff, PR description, review comments)` → reconcile against the standing library. Transfer-learning an existing codebase is the *same operation* replayed over history; live learning is the same handler driven by a webhook. One mechanism, two triggers.

This replaces three hard problems with one each:
- **Verification** was a fuzzy session→commit→PR join. Now it's intrinsic: the thing we distill from *already merged.*
- **Retraction** was a "watch for reverts" subsystem. Now it's **code locality**: a PR that rewrites the lines an insight was learned from is, by construction, the contradiction candidate. The codebase indexes the insights.
- **Corroboration** stays semantic (two PRs teaching the same lesson in different places), but now over *verified* units only.

The genuinely-new dependency is the contradiction classifier (NLI), which the TS side lacks and R3 already built in Python — we port it, but its role is now narrow (classify a new PR-insight against the 0–3 insights anchored to the same code).

## Two ingestion lanes (inferred vs. authored)

There is exactly one place a **human verifies** knowledge: the **authored** lane, where a person declares an idea directly. Everything else is **automated** — inferred lessons stream in off merged PRs and fold by hit count, with no human reviewing each one. PR-gating governs only that inferred lane, and the merge is the *automated* gate (not a human-verification step). Two lanes, one memory graph:

- **Lane A — inferred (automated, PR-gated).** Observe → distill → merged-PR hits accumulate → fold once they cross `verified_K`. No per-idea human review; the merge is the automated gate. Everything else in this plan.
- **Lane B — authored (direct, ungated).** A user just *says* *"remember: no dashes"* in-session, or pastes arbitrary text (a good skill, a doc) that is decomposed into atomic insights. Enters the graph **immediately, at top authority** — no `verified_K`, no golden case, no merge. The human *is* the verification.

Both lanes write the same store and share the anchor index, supersession, and necessity machinery — so an authored directive can supersede a colliding inferred idea (top authority wins), a later directive supersedes an earlier one (recency within the top tier), and an inferred PR idea can **never** supersede a standing user directive. This is **U8**. It is why "entirely PR-gated" means *inferred learning is PR-gated* — **not** *the only way in is a PR*. (Today neither sub-path exists: `rest/ideas.ts` is read-only; this lane is net-new.)

## What the pivot deletes (the simplification sweep)

The prior draft's complexity existed almost entirely to solve "map an in-flight session to the PR it eventually becomes, and hold a vote in limbo until then." PR-gating dissolves that. **Deleted or demoted:**

| Prior-draft machinery | Status under PR-gating | Why it''s changed |
|---|---|---|
| Wrapper Go push-capture hook (old U1) | **Simplified to a branch→session map** (U7), for distill-time enrichment only | The async *verification* use is gone; what remains is a synchronous "which session authored this branch?" lookup, **off the gating path**, that degrades gracefully when absent. |
| Session→commit→branch provenance as a *verification* join (old U2) | **Deleted from the verification path** | The PR is the verification; the session link is now distillation *context*, never a vote. |
| `pending` vote state + async pending-vote store (old U5) | **Deleted** | We distill **on merge**, not on push. Nothing is ever "waiting." |
| `BRANCHVOTE#` reverse index, `MERGED#` markers, out-of-order tolerance, TTL→recurrence | **Deleted** | All of it was reconciliation for the pending store. |
| 4-state vote machine (`recurrence\|pending\|verified\|superseded`) | **Collapsed to 2** (`verified \| superseded`) | Everything we ingest came from a merge → it's verified on arrival. |
| Branch-join, squash-SHA-rewrite handling, force-push edges, `patch-id` backup | **Deleted** | We never join on commit identity; we act on the PR object GitHub hands us. |
| Secret/PII scrub *on the DynamoDB session read* | **Replaced by scrub on PR content** | Smaller, well-defined surface (diff + description), not arbitrary conversation. |
| "recurrence as a weak candidate prior" + dual `corroborationCount`/`candidateCount` | **Deleted** | There are no recurrence-only candidates anymore; the input is merges. |
| Cosine-prefilter-against-whole-library *for contradiction* | **Replaced by code-anchor locality** | Locality is cheaper and far more precise for supersession. |

**Net on the inferred spine:** the gnarliest machinery is *deleted* (async reconciliation, branch-join fragility, the 4-state vote machine), replaced by two small deterministic pieces (the merge-replay driver, the code-anchor index) — the PR-gated core is simpler than the prior draft. The total unit count is 10; three are **genuinely-new capabilities** the prior draft lacked, not carried-over complexity: U7 (session-enrichment link), U8 (the authored lane — user directives + pasted-text ingestion), and U10 (the Python↔TS write contract). They earn their place; they aren''t the bloat the pivot was removing.

## The spine: fold-forward over the merge log

```
merged PR  ──►  distill candidate insight(s)        ──►  anchor to (file, symbol)
(diff +         from diff + description + reviews         from the diff hunks
 description)                                                    │
      │                                                          ▼
      │                                              ┌──► locality join: insights anchored to
      ▼                                              │     the same (file,symbol)?  → SUPERSEDE path
 semantic join (cosine + NLI):                       │
 same lesson elsewhere? → CORROBORATE ───────────────┘
```

- **Transfer mode = the primary, user-initiated trigger** (onboarding a repo): the user *points at a repo and hits run*; the job lists merged-to-main PRs in commit order and runs each through the handler. The library reconstructs deterministically, *with historical supersessions applied in the order they actually happened* — the controlled rebuild the design calls for. (See "Trigger model" below.)
- **Continuous mode (deferred follow-on, not v1):** a `pull_request` webhook (`closed` + `merged:true` + `base.ref == default branch`) drives the *same* handler — same code path, different trigger. Deferring it keeps v1 free of a public webhook endpoint + secret rotation.
- **Idempotency**: an org-scoped per-PR cursor (`PROCESSED#<owner/repo>#<prNumber>` under `SCOPE#org#<org>`) makes a re-run/​resume of the ingest job safe (and an already-ingested repo a cheap no-op). This is the only new bookkeeping record (replacing four deleted ones).

**Two joins, two distinct jobs** (this is the conceptual core):
- **Code-locality join → supersession.** Each insight remembers the `(file, symbol)` anchors of the PR it was learned from. A new PR that touches those same anchors is the contradiction candidate. Cheap (a key lookup), precise (the code itself says "we''re revisiting this decision"), and bounded (0–3 candidates, not the whole library).
- **Semantic join → corroboration.** A different PR teaching the *same* lesson in *different* code is found by cosine + NLI, as today — but now only over verified insights, and the cosine-≥0.9 *merge verdict* (the negation-blindness bug) is replaced by an NLI verdict.

## Trigger model (v1 = user-initiated)

**v1 is a user-run ingest job, not a live listener.** The user points at a repo and hits **Run**; the job lists merged-to-main PRs in commit order and folds each forward through the one handler — the controlled, deterministic rebuild. It is idempotent and resumable via the org-scoped `PROCESSED#<owner/repo>#<prNumber>` cursor: re-running an already-ingested repo is a cheap no-op, and an interrupted run resumes where it stopped.

**Source resolution (one trigger, graceful degradation):**
- **Command-HQ-known repo with captured sessions** → distillation is enriched with the authoring Claude conversation (the U7 branch→session links supply the agent-side "why") alongside the PR diff / description / reviews.
- **External / public repo (no session data)** → walk the git history via the unauthenticated `PublicGitHubReader`; distill from GitHub context only (diff + description + reviews + linked issues). No session enrichment, by construction.
- **Auth:** private / installed repos use the GitHub App installation token (needs `Pull requests: Read`); public repos use the unauthenticated reader.

**Continuous mode (webhook) is a deferred follow-on, not v1.** The same handler can later be driven by a `pull_request` webhook for always-on ingestion — deferring it keeps v1 free of a public webhook endpoint, secret rotation, and the per-installation permission re-consent. The webhook-hardening and ack-Lambda items in U1 belong to that deferred mode.

## Contradiction detection, made concrete (the part that was unclear)

"A PR contradicts what came before" becomes mechanical once insights are anchored to code:

1. **On fold**, store `anchors = [{file, symbol}]` derived from the source PR''s diff hunks via **tree-sitter AST** (symbol-level, because line numbers drift and squash rewrites them — `(file, symbol)` survives both; git''s own hunk headers are unreliable for TS/Go).
2. **On every merge**, compute the PR''s touched `(file, symbol)` set, look up standing insights in the **anchor index**, and for each collision classify the new PR''s change against the standing insight:
   - **supersede** (the change reverses/replaces the prior decision) → retire the incumbent (`invalid_at` + un-fold), authority-weighted.
   - **corroborate** (reinforces the same approach in the same place) → add a verified vote.
   - **refine** (scopes/adjusts it) → keep both, `refines` link.
   - **neutral** (a move/rename with no semantic change) → no-op.
3. A **literal `git revert` PR is the trivial subcase**: inverse diff, exact anchor match, "Revert…" title → high-confidence supersede.

What we deliberately give up: a PR that obsoletes an insight *without touching its lines* (a new util makes an old pattern moot elsewhere). That''s the long tail — lower recall, swept later by the semantic backstop. The **big** signal is local, and local is cheap and exact.

## Language & service boundary (architecture — maximal Python brain)

**Decision (taken out of TypeScript):** the **entire learning loop is Python**, top to bottom. The Python service runs the ingest job (a user-triggered history replay in v1; a `pull_request` webhook is a deferred follow-on), owns every learning record (ideas, anchors, golden cases, idea + skill vectors), **authors the fold/un-fold skill revisions itself**, and serves the learning read API. There is **no TypeScript inside the loop** — so the cross-language transaction questions dissolve rather than needing answers. It reuses `agent-families`'' **store-free** core (`nli.py` classify, `judge.py`) — the NLI and LLM-judge seams; the **distillation step is net-new** (there is no `distill` primitive in agent-families — `derive.py` is graph-clustering over existing insights, not PR-diff extraction), and the SQLite/`sqlite-vec` store + `derive_skills` are **not** reused (the live loop writes DynamoDB + S3 Vectors). So the loop and the sandbox share the NLI/judge algorithm, not the persistence layer. **Deploy: a container Lambda** (the model bundled in the image).

| Concern | Home | Notes |
|---|---|---|
| **The whole learning loop** — merge-webhook receipt, distillation, NLI, embeddings, anchor/locality join, corroboration + hit-counting, supersession, necessity, golden-judge, **and the fold/un-fold skill-revision writes** | **Python (container Lambda)** | Owns and writes every learning record directly (boto3) — the conditional/optimistic-concurrency writes *and* the `revisionKey`/`truePointerKey` versioning. The v1 trigger is a **user-run ingest job** (a new Python entrypoint); the `pull_request` webhook is a **deferred** follow-on (no TS migration — this code doesn''t exist yet). |
| **Learning read API** (candidate-learnings, all-ideas) + **authored-lane endpoint** (directive) | **Python** | Same service; the TS web app just points at it. |
| **Capture** (branch→session map) | **Go wrapper** (U7) | Unchanged; the Python loop reads the map. |
| **Skill materialization** | **Go wrapper** (existing) | Reads skill records (written by Python) → disk. A *read* contract on the skills shape, not a writer. |
| **Web display** of learning data | **TS frontend** (existing) | Reads via the Python API (JSON-DTO contract), not DynamoDB. |
| **Non-learning catalog authoring** (`hq-add-skill`, web editor, seeding) | **TS UI → Python writer** | The UIs stay TS, but the *write* calls the one Python author-revision API — **one** revision writer, no duplicated logic. |

**The contract in one line:** *The ingest run (user-triggered v1; webhook later) → the Python service does everything (distill, decide, write ideas, author fold/un-fold skill revisions) in one process.* TS/Go **read** the results; TS authoring UIs **call** the one Python writer. No duplicated write logic, no cross-language write transaction.

**DRY — one writer, one source of truth (the deduplication discipline):** rather than TS *and* Python both authoring skill revisions (the duplication trap), **Python is the sole writer** of every learning-owned record — ideas, anchors, golden cases, vectors, **and skill revisions**. The existing TS catalog-authoring UIs **call the Python author-revision API** instead of writing revisions themselves — so there is exactly **one** revision-author implementation, **one** golden judge (U6), **one** embedding path. The single-table key schema + record shapes are defined **once** and **code-generated** into the TS and Python types (no hand-mirrored copy to drift). Concurrent callers of the one writer still race-safely on the CAS `TRUE` pointer.

**Unit homes:** U1–U6 **Python** (the whole loop, incl. the user-run ingest entrypoint *and* the fold/un-fold writes; the webhook receiver is the deferred continuous-mode trigger) · U7 capture **Go** · U8 authored endpoint + write **Python** · U9 config + e2e **Python**. Cross-language only at the *read* contracts.

**How this resolves the integration questions:**
- **Cross-boundary fold/un-fold consistency — DISSOLVED.** One process authors the idea write *and* the skill revision; no two-language partial-failure.
- **Fold/un-fold command channel — GONE.** Python writes directly; no commands to ship.
- **Event transport — GONE.** The webhook lands in Python; nothing to forward.
- **Vector ownership — all Python.**
- **Deploy shape — container Lambda** (model in the image). v1 is a **user-run batch job** that invokes the model Lambda directly — cold-start is fine, there is no external delivery clock. The **deferred** continuous webhook, when added, must not sit on the model Lambda's cold-start path: a thin ack Lambda (no ML deps) HMAC-verifies, enqueues to SQS, and returns `202` inside GitHub's 10 s window; the model Lambda consumes from SQS (model load ~20–30 s off GitHub's clock; GitHub does not auto-redeliver).
- **Key-schema — one codegen''d source of truth:** keys + record shapes are defined once and generated into TS + Python (CI-checked for freshness), so TS/Go readers and the Python writer never drift — no hand-mirror, no byte-compat tests.

**The remaining cross-language surface (small, by design):** (1) the **codegen''d schema** — one source of truth for keys + record shapes, generated into both languages, CI-checked for freshness; (2) **re-pointing the existing TS catalog-authoring writes** to the one Python author-revision API — a deliberate, justified refactor of working code that *removes* the duplicate writer. No byte-compat-of-two-copies risk, because there is only one writer (U10).

## Codebase reality (grounded, verified against source)

- **Ideas** (`ideas/corroborate.ts`, `shared/dto.ts`): `Idea{ sources, postFoldSources, status: open|folded, foldedIntoRev, corroborationVersion }`; `IdeaSource{ sessionId, segmentId, seq, projectId?, repoId? }`; `corroborationCount = |distinct sessions|`, `K=2`; MERGE-REWRITE ≥ 0.9 cosine; optimistic concurrency via `corroborateIdeaConditional(corroborationVersion)`; cross-org guard.
- **Fold** = flip `status→folded` + `foldedIntoRev=<skill revision>`; folded ideas are immutable history (U20 resurrection rule: re-matches attach to `postFoldSources`, never reopen).
- **Golden cases** = `IDEAGOLD#<skill>#<caseId>` (before→after at fold) — the exercising test, already stored.
- **Skills are versioned** (`revisionKey`, `truePointerKey`) — the substrate for un-fold.
- **GitHub App** (`github/app.ts`) = read-only commit *polling* (`listCommits(since,until)`, "no writes to GitHub anywhere"), injectable `fetch` for fixtures, `PublicGitHubReader` for public repos. **No webhooks, no PR awareness, no NLI** — those are net-new.

**Strategic target:** the production claude+/Command-HQ platform. Under the maximal-Python decision the *brain* is a deployed **Python service that reuses agent-families'' store-free core** (`nli.py`, `judge.py`) but is its own production artifact pointed at real PRs + the live store; distillation and the DynamoDB/S3-Vectors persistence are net-new (the SQLite-backed `derive_skills` is not in the live loop); `agent-families` stays the offline training/experimentation sandbox. **Shared NLI/judge code, separate deployables — not a double-build.**

## Key Technical Decisions

- **The merged PR is the unit of learning.** Distillation input is `(diff, PR description, review comments)`. Direct-to-main pushes with no PR are **not learned** (deliberate scope — matches "un-PR''d work stays unlearned").
- **The existing session-recurrence pipeline is repointed, not run in parallel.** Today''s `topic-mining → associate → corroborate` (plan 008) mints ideas from raw session topics by recurrence. That input is **switched off** as an idea-creator: the inferred lane''s only idea source is merged-PR distillation (U1). Session topics survive solely as *enrichment context* (U7) — never idea-creating votes — otherwise un-verified session ideas would leak in beside the PR-gated ones.
- **Author-credibility weighting (single-author code still learns; reviewed/trusted code is prioritized).** Each merged-PR hit is multiplied by the **author's credibility** — a **user-curated** per-coder weight, settable during *or after* ingestion to boost a trusted contributor''s signal. Unknown / single-author code gets a modest default (so a solo repo still folds via recurrence, just needs more hits); a PR with a distinct non-author reviewer/contributor starts at a higher default; high-credibility coders fold faster. Credibility is **user-curated, not GitHub approval state** (we still don''t read approval clicks). Boosts are deliberate and may be applied **retroactively**; the system never *auto*-demotes already-folded ideas. (This replaces the self-build warm-start two-phase — simpler, and it makes "trusted reviewers matter" an explicit lever rather than an inferred transition.)
- **Transfer = replay (v1 trigger), continuous = webhook (deferred), one handler.** Merge history is an event log folded forward; the resulting library is deterministic given the log (→ replay fixtures are the test substrate).
- **Contradiction is code-locality-driven.** Insights anchor to `(file, symbol)`; a PR touching those anchors triggers the supersede classifier. Revert is the trivial subcase.
- **Corroboration is semantic over verified units, graded by verification rung × author credibility.** Each merged-PR hit carries a *weight*: a rung inferred **from the diff + PR metadata** (a test added/modified **or** a targeted bug-fix ⇒ 1.0; a substantive merge ⇒ 0.6; a trivial merge ⇒ 0.4 — **never 0, it got merged so it means something**), multiplied by the **author''s credibility** (user-curated; modest default for single-author, higher when the PR had a distinct non-author reviewer). So solo repos still learn (via recurrence) and reviewed/trusted code is prioritized. **Fold when accumulated weight reaches `verified_K`, not raw PR count;** repeated hits converge (a `recurrence_bonus` of 0.2 per repeat). The merge stays the *gate*; rung × credibility is the *grade*. The cosine-≥0.9 merge *verdict* is replaced by NLI.
- **Retraction = authority-weighted supersession**, `invalid_at` (valid-until-reversal, never deleted) + un-fold; revive on re-corroboration. `authority > evidence > recency`.
- **Authority precedence (two tiers, not a ladder of merge-types):** **authored** (`user_directive` / `authored_import` — a human vouched) outranks **inferred** (`merged` — automated, no per-item human review). Within inferred, more **weight** (rung × author credibility) then **recency** wins; an inferred idea can never supersede an authored one. We do **not** read the PR''s human-approval state — whether someone clicked "approve" is not a trust tier; the merge is the automated gate, and trust within the inferred tier is the **user-curated author credibility**, not approval clicks.
- **Necessity via the existing golden cases**, with vs. without, transfer-not-source, mis-retrieval = a *scoping* signal not an unnecessary verdict.
- **Grade on the merged outcome, never diff-match.**
- **DRY / single-source-of-truth (cross-language) — a hard principle.** No logic exists twice across TS/Python. **One** skill-revision writer (Python; the TS authoring UIs *call* it), **one** golden judge (Python; two triggers), **one** embedding path (Python), **one** NLI (Python), **one** key/record schema (code-generated into both languages, CI-checked for staleness). The aggressive-Python consolidation is justified *precisely* by this: wherever the split would otherwise produce two copies of the same logic, collapse to one writer + generated readers (U10). A hand-mirrored second copy is a bug, not a guardrail.

## Data model changes (exact, additive)

Following `shared/dto.ts` + `db/keys.ts`; every addition `.optional()` → back-compat.

**`IdeaSource` (additive):**
- `prRef?: { repo: string; number: number; mergedSha?: string }` — the merged PR this vote came from (replaces the deleted `branch`/`commitShas`/`verification`-state fields).
- `anchors?: { file: string; symbol: string }[]` — the code this insight was learned from (the supersession join key).
- `authorityKind?: 'user_directive' | 'authored_import' | 'merged'` — the provenance tier. The first two are the **authored lane** (human-verified, U8: a directive, or pasted text decomposed into nodes); `merged` is the **inferred lane** (automated — one tier, no human-review sub-distinction). No `recurrence`/`pending` kinds — they no longer exist.
- `verificationRung?: 'test' | 'normal' | 'bare'` — inferred **from the diff + PR metadata** (no Check-Runs dependency): a diff that adds/modifies a test **or** a targeted bug-fix ⇒ `test` (1.0); a substantive merge ⇒ `normal` (0.6); a trivial/low-signal merge ⇒ `bare` (0.4 floor — a merged PR is never 0). `authorId?: string` — the PR/commit author, for the author-credibility multiplier.
- The existing `sessionId`/`segmentId`/`seq` fields are retained as **best-effort enrichment provenance** (which session slice, if any, fed this distillation). The vote *unit* is now `prRef.number`, not `sessionId` — so `corroborationCount` counts distinct PRs even when sessions are absent or shared.

**`Idea` (additive):**
- `invalidAt?: number` — temporal retirement (valid-until-reversal); excluded from the *current* set, kept queryable as history.
- `supersededBy?: string` / `supersedes?: string[]` — the contradiction edge.
- `refines?: string` — the scoped-nuance edge (coexistence, not retirement).
- `revivedAt?: number`; `legacyRecurrenceFold?: boolean` (migration grandfather flag).
- `authored?: boolean`; `sourceRef?: { kind: 'user_directive' | 'authored_import'; label?: string; contentHash?: string }`; `scopeTag?: ScopeRef` — the **authored-lane** fields (U8); a directive / authored node carries no `prRef`, enters active immediately, and is necessity-exempt.
- `status`, `foldedIntoRev`, `corroborationVersion` unchanged. **Do NOT add a `superseded` status** — a superseded idea is `folded` + `invalidAt`, lesson removed from the current revision (keeps the U20 fold lifecycle intact).

**`corroborationWeight(idea)`** → `Σ (rungWeight(source) × authorCredibility(source.authorId)) over distinct prRef.number` (rung ∈ {test:1.0, normal:0.6, bare:0.4}, + a `recurrence_bonus` of 0.2 per repeat); **fold when it reaches `verified_K`.** (`corroborationCount` = distinct PRs is retained for telemetry.) The spine of U4.

**DynamoDB records (single-table `harness`) — two new, one kept, replacing the prior four:**
- **Anchor index** (the locality join): `PK=SCOPE#org#<org>, SK=ANCHOR#<owner/repo>#<file>#<symbol>#<ideaId>` (org-scoped, co-located with the idea records it references) → look up standing insights by the code a merge touches.
- **Processed-PR cursor** (replay/webhook idempotency): `PK=SCOPE#org#<org>, SK=PROCESSED#<owner/repo>#<prNumber>` (org-scoped, like the anchor index — the same repo under two orgs ingests independently) → never double-count a merge.
- **Verification/supersession audit** (append-only, under the idea): `SK=VERIFY#<seq>` → `(verdict, prRef, authority, at)`. (Kept — cheap and the supersede story needs it.)

## The idea lifecycle (state machine, collapsed)

No vote-state machine anymore (every ingested vote is verified-on-arrival). The lifecycle lives on the **idea**:

`open → folded` (accumulated `corroborationWeight` ≥ `verified_K`) `→ folded + invalidAt` (a verified PR supersedes it via locality) `→ revived` (a *new* verified PR re-corroborates the retired pattern). No terminal state; everything reversible.

## End-to-end flow (one explicit trace)

1. PR #412 merges to main (webhook, or replay reaches it in history order). Cursor check: not yet processed.
2. Distill candidate insight(s) from `(diff, description, review comments)`; scrub secrets from the PR content; derive `anchors` from the diff hunks. Authority = `merged` (the inferred tier — no human-review distinction).
3. **Locality join:** anchor-index lookup on the touched `(file, symbol)` set → standing insights I₁, I₂ in contention. NLI-classify the new insight vs. each: `I₁` = **supersede** → U5 (authority decision → `invalidAt` + un-fold I₁); `I₂` = **neutral** (a rename) → no-op.
4. **Semantic join:** cosine ≥ candidate-floor over verified ideas → NLI vs. top candidate → **corroborate** an existing idea J (add a verified vote — its rung × author-credibility weight) or **create** a new idea.
5. J''s `corroborationWeight` reaches `verified_K` → **fold** (golden case captured; anchors registered in the anchor index).
6. Later, PR #530 reverts the decision behind J (touches J''s anchors) → supersede → `invalidAt` + un-fold.
7. PR #604 re-introduces the pattern (touches the same anchors, NLI = corroborate of the retired J) → **revive** (`invalidAt` cleared, re-fold).
8. Periodically the **necessity gate** ablates ambiguous ideas against their golden cases; no-necessity → demote.

## Implementation Units

**Homes (per the boundary table):** U1–U6 and U8 are the **Python loop**; U7 is **Go**; U10 is the **Python↔TS contract**. The TS file paths inside each unit below are the **existing TS surface the Python loop reads/contracts-against or supersedes** (the symbol-level build reference) — *not* a claim the unit is implemented in TS. New Python modules mirror them.

### U1. PR-merge ingestion driver — user-triggered replay (v1) + webhook (deferred) (Python loop) — the spine
- **Goal:** one handler that turns a merged-to-main PR into distilled, anchored candidate insight(s); driven by a **user-run history replay (v1)** or, later, a webhook (deferred continuous mode). See "Trigger model."
- **Files:** `github/app.ts` (+ new `github/prs.ts`, `github/mergeHandler.ts`), a user-run ingest entrypoint (`rest/*` webhook receiver is the deferred continuous mode), `ideas/distill.ts` (or extend topic mining), tests (`github.test.ts`, fixtures).
- **Approach:**
  - **Transfer (v1, user-triggered):** `listMergedPullRequests(repo, sinceCursor)` (installation token for private repos; `PublicGitHubReader` + injectable `fetch` for public) yields merged PRs in commit order; feed each through the `mergeHandler`. This is the primary trigger (see "Trigger model").
  - **Continuous (deferred):** the *same* `mergeHandler` behind a `pull_request` webhook (`action:closed` + `merged:true` + `base.ref == default branch`, HMAC-verified, `repoProjectKey` resolves the project) — post-v1; defers the public endpoint + secret rotation.
  - **Distill from three sources** (richest available wins, graceful degradation): (a) **always** — the PR diff, title/description, and review comments/threads + linked issues (the GitHub-side "why"); (b) **when the branch was authored in a local Claude session** — the session''s conversation messages for the relevant turn-range (the agent-side "why"), looked up via the U7 branch→session map; (c) the `merged_changed` reviewer divergence as a labeled near-miss. **Derive `anchors`** from the diff hunk symbol headers. **Scrub** secrets/credentials from *all* distillation inputs (PR content *and* any pulled session slice) before an insight is created.
  - **Degradation:** no session link (human-authored PR, external repo, history replay of pre-capture commits) → distill from GitHub sources only. The session context strictly *enriches*; it never gates and is never a vote.
  - **Idempotency** via the `PROCESSED#<prNumber>` cursor (check-and-set).
- **Subtle features (explicit):**
  - **No approval-state read:** the merge is the gate; we do *not* read the PR''s human-approval state (it isn''t a trust tier). Drops the Reviews-API dependency entirely.
  - **`merged_changed` near-miss:** when review comments / requested changes diverged from the original diff, mine that divergence as a labeled near-miss candidate (re-enters distillation).
  - **Closed-unmerged** PRs are a *negative* signal (the approach was rejected) — record, don''t distill as positive.
  - **Curriculum filter (by signal, not size):** skip low-signal merges — dependency/version bumps, lockfile updates, formatting-only, generated-file churn, pure-mechanical renames — but **never filter on diff size**: the smallest PR can carry the strongest bug-fix signal (a one-line fix stays; a 500-line version bump drops). Log every drop so the filter is auditable and tunable.
  - **Bug-fix insights are verification-oriented (retrieval-shaped):** when the PR is a targeted bug-fix, phrase the distilled insight as a retrievable **error case / failure-mode to check** (what broke + the guard that prevents it) and tag it (e.g. `kind: bugfix-failure-mode`) so it is **preferentially retrieved when an agent is instructed to verify code** — so the same bug isn''t repeated. Keep this framing through U4''s corroborate re-synthesis.
  - **Multi-hop git-flow:** a PR merged into a *non-default* branch is **not** verification (the change hasn''t reached main); ignore until it lands on the default branch (the default-branch filter handles this; v1 ignores intermediate merges).
  - **Squash/rebase** rewrites the merged SHA — irrelevant now, we key on `prNumber`, not SHA.
  - **Replay determinism:** processing the same merge log twice yields the same library (cursor + ordered history).
  - **Rate-limit budget:** reuse the App''s SHA-keyed cache; replay paginates.
  - **Webhook hardening (continuous mode — deferred):** `hmac.compare_digest` (constant-time, per GitHub docs), a body-size cap *before* hashing (DoS), and `X-GitHub-Delivery` dedup (short-TTL) layered on the `PROCESSED#` cursor (HTTP-layer replay vs. business idempotency); precedent `auth/orgPassword.ts` uses `timingSafeEqual`.
  - **Scrub contract (Wave 1, not deferred):** port the six secret regexes in `wrapper/internal/topic/fold.go` (`scrubSecrets`: AWS AKIA, key/secret/token assignment, Bearer, JWT, PEM header, GitHub PAT) and apply to *all three* distillation inputs **before** any embed/write, truncating after scrub.
  - **Generalization pass (private-repo leak guard):** after scrub, replace repo-specific identifiers (repo/file/host/package names) with typed placeholders (`<REPO>`/`<FILE>`/`<HOST>`/`<PKG>`) before the idea body is written — reuse the admission-gate substitution in `agent-families` `pipeline/__init__.py`. Org partitioning blocks cross-org; this closes intra-org cross-repo leakage (companion §4).
  - **App-credential handling:** load the GitHub App PEM at cold start from AWS Secrets Manager (never env/image, never logged; mirrors the `api-stack.ts` secret pattern); cache the installation token in-process, refresh ~5 min before its 1 h expiry; rotation = new key → Secrets Manager → delete old.
  - **GitHub App permission upgrade (external dependency):** PR reads (private-repo transfer, and the deferred webhook) need a new `Pull requests: Read` permission → per-installation re-consent (inert until approved). Public repos use the unauthenticated reader (no permission). Un-consented private installs surface a clear "re-consent needed," not a silent skip.
- **Required tests:** `test_merge_to_main_distills_and_anchors`; `test_non_default_base_is_ignored`; `test_closed_unmerged_is_negative_not_positive`; `test_processed_cursor_prevents_double_count`; `test_replay_is_deterministic_over_fixed_log`; `test_webhook_hmac_rejected_on_bad_sig`; `test_version_bump_pr_is_skipped`; `test_one_line_bugfix_pr_is_not_skipped`; `test_webhook_replay_delivery_id_rejected`; `test_webhook_oversized_body_rejected_before_hashing`; `test_credential_in_diff_does_not_appear_in_insight_body`; `test_private_repo_insight_body_contains_no_repo_specifics`.
- **Verification:** recorded webhook + PR-list fixtures, offline (the App''s injectable `fetch` precedent). Zero live GitHub, zero quota.

### U2. Code-anchor model + locality join (Python) — NEW, small
- **Goal:** make the codebase the index of insights so contradiction detection is a key lookup.
- **Files:** `ideas/anchors.ts`, `db/keys.ts` (`anchorKey`), `db/repo.ts`, tests.
- **Approach:** parse `(file, symbol)` anchors from a PR''s diff hunks; on fold, write `anchorKey(repo, file, symbol, ideaId)`; on every merge, query `ANCHOR#<file>#<symbol>` over the touched set → the candidate incumbents for the supersede classifier.
- **Subtle features:**
  - **Symbol extraction via tree-sitter AST** (not git hunk headers — git ships no built-in TS diff driver and the TS driver PR was discarded; `@@` context is empty/wrong for TS arrow consts, JSX, Go closures). Parse the base-ref file with tree-sitter-{typescript,go,python}, walk to the smallest enclosing named symbol containing the hunk; fall back to file-level only when the grammar is absent or the parse fails. v1 = TS/Go/Python; log `anchor_resolution = symbol|file_fallback` + extension from day one, and gate the "0–3 candidates / local is exact" claim on the measured symbol-resolution rate before unfold-enforce.
  - **Org-scoped writes:** anchor records live in the `SCOPE#org#<org>` partition and the Python writer mirrors the non-blank-org guard (`corroborate.ts` ORG GUARD) before any write — no cross-org anchor collision.
  - **Anchor drift:** a renamed/moved symbol → the old anchor goes stale; a follow-up merge touching it still collides at file level. Accept file-level recall for the rename case.
  - **Anchor maintenance on un-fold:** retiring an idea removes its anchors from the current index (kept in history).
  - **Fan-out:** one PR touches many `(file, symbol)` → union the collisions, dedupe incumbents.
- **Required tests:** `test_anchor_written_on_fold_and_queryable`; `test_merge_touching_anchor_finds_incumbent`; `test_file_level_fallback_when_symbol_unparseable`; `test_unfold_removes_current_anchor_keeps_history`; `test_symbol_anchor_from_ts_arrow_const`; `test_symbol_anchor_from_go_anonymous_func`; `test_anchor_write_is_org_scoped`.
- **Verification:** seeded ideas + synthetic diffs.

### U3. NLI contradiction classifier — R3''s `nli.py`, in the Python loop (narrowed role) — NEW
- **Goal:** classify a new PR-insight against the few colliding/candidate incumbents → corroborate / refine / supersede / neutral.
- **Files:** the Python loop''s `nli` module (reuses agent-families'' `nli.py`); the corroboration step (replacing the cosine merge-verdict); tests + fixtures.
- **Approach:** **is** R3 008''s `nli.py` run in-process in the Python loop — a local cross-encoder (`cross-encoder/nli-deberta-v3-base`, CPU, offline), with record/replay fixtures and a 3-logit label-map asserted at load. Used in two spots: (a) the **locality** candidates from U2 (primarily supersede/neutral), and (b) the **semantic** corroboration top-candidate (replacing the cosine-≥0.9 merge verdict — the negation-blindness fix).
- **Subtle features:**
  - **The shipped negation-blindness fix:** a contradiction at cosine ~0.95 must classify `supersede`, never MERGE-REWRITE.
  - **Determinism:** CPU + fixed model + asserted label map (a silent model swap that reorders labels fails at load, not silently flips verdicts).
  - **Refine vs. supersede boundary** is the load-bearing judgment ("use Z for Y" reversal vs. "…in serverless" refinement) — fixtured both ways.
  - **Low-confidence fallback** → the existing idea-writer judge (`synth.ts`), never back to cosine.
  - **In-loop, not a separate service** (resolved): NLI runs in-process in the Python loop (reusing `nli.py`) — no onnxruntime-node bundle, no separate inference hop.
  - **Model artifact pinning:** load the cross-encoder with a pinned `revision=<commit-sha>` (in `[nli]` config) so a HuggingFace namespace-hijack / weight substitution that preserves label order can''t silently flip verdicts (the label-map assert guards order, not weights).
- **Required tests:** `test_contradiction_at_high_cosine_is_supersede_not_merge`; `test_nli_replay_byte_identical_offline`; `test_label_map_swap_fails_at_load`; `test_refine_not_supersede_for_scoped_nuance`; `test_neutral_for_pure_rename`; `test_low_confidence_falls_to_judge_not_cosine`; `test_model_revision_pinned`.
- **Verification:** offline suite, committed fixtures, one documented record smoke.

### U4. Verified corroboration (Python) — simplified spine
- **Goal:** corroboration counts distinct merged-PR hits; fold at `verified_K`.
- **Files:** `ideas/corroborate.ts`, `@harness/shared` `corroborationCount`, tests (`corroborate.test.ts`).
- **Approach:** each source carries its `prRef` + `verificationRung` + `authorId`; **fold when accumulated `corroborationWeight` reaches `verified_K`** (Σ rung × author-credibility, + recurrence bonus; rung test 1.0 / normal 0.6 / bare 0.4, never 0; default K=2), not raw PR count — a single test/bug-fix PR by a credible author is most of a fold, while repeated low-rung hits still converge. A single low-rung hit is a candidate, not yet folded. **No human-review sub-tier, no vote-state machine** — a source exists only because a PR merged. Preserve idempotency + optimistic concurrency (`corroborateIdeaConditional`) and the cross-org guard.
- **Subtle features:**
  - **Idempotency on `prRef.number`** — re-processing a PR (replay overlap) must not double-count a vote.
  - **Fold threshold** — `verified_K` distinct merged-PR hits; below it the idea is a surfaced candidate, not folded. (Authored ideas, U8, skip the threshold entirely — the only human-verified shortcut.)
  - **Author credibility** — each hit''s weight is × the author''s credibility: a **user-curated** per-coder weight (settable during *or after* ingest; `default_author_credibility` for unknown/single-author, a higher default when the PR had a distinct non-author reviewer/contributor). Solo repos still fold via recurrence; trusted coders fold faster. Boosts may be applied **retroactively** (deliberate, monotonic — recompute weight up); the system never *auto*-demotes folded ideas.
  - **Rung detection (from the diff + PR metadata, no Check-Runs dependency)** — diff adds/modifies a test, **or** the PR is a targeted bug-fix (small focused diff + fix signal: title/label `fix`/`bug`, linked bug issue) ⇒ `test` (1.0); a substantive merge ⇒ `normal` (0.6); a trivial/low-signal merge ⇒ `bare` (0.4). **Never 0.** Apply `recurrence_bonus = 0.2` per repeat hit so a lesson taught by many small PRs converges.
  - **Clean consolidation of overlapping paragraphs** — on a corroborate verdict the MERGE-REWRITE re-synthesizes the two findings into **one** paragraph; two ideas that say largely the same thing must consolidate, not accumulate as near-duplicates. This is the load-bearing requirement behind keeping ideas paragraph-level (Open Q2) — it must stay tight.
  - **MERGE-REWRITE only on corroborate** — never on a contradiction (that''s U5).
  - **Cross-org guard** on every write path.
- **Required tests:** `test_k_distinct_merged_prs_fold`; `test_single_merged_pr_is_candidate_not_folded`; `test_same_pr_reprocessed_does_not_double_count`; `test_overlapping_paragraphs_consolidate_not_duplicate`; `test_corroborate_resynthesizes_body`; `test_conditional_write_guards_corroboration_version`; `test_rung_weighted_fold_threshold`; `test_merged_pr_weight_never_zero`; `test_bugfix_diff_infers_test_rung`; `test_single_author_code_folds_via_recurrence`; `test_author_credibility_boost_raises_weight`; `test_credibility_boost_never_demotes_folded`; `test_recurrence_bonus_converges_low_rung_hits`.
- **Verification:** fixtures over K-hit folds + replay-overlap + overlapping-paragraph consolidation.

### U5. Supersession + temporal validity + un-fold (Python) — the retraction reframe (hardest unit)
- **Goal:** a verified PR that contradicts a standing insight (via locality) retires it, reversibly.
- **Files:** new `ideas/supersede.ts`, `ideas/corroborate.ts`, `db/repo.ts` (un-fold), `@harness/shared` `Idea`, skills versioning (`revisionKey`/`truePointerKey`), tests.
- **Approach:** when U3 classifies a colliding incumbent as `supersede`: decide the winner by `authority > evidence(distinct verified PRs) > recency`; stamp the loser''s `invalidAt` + `supersededBy`, **un-fold** if folded, surface the winner. Never delete. Re-corroboration (a new PR re-teaching the retired pattern) **revives**.
- **Subtle features (where most of the real work is):**
  - **Un-fold operation** — the lesson lives in skill revision `foldedIntoRev`; un-fold authors a *new* revision that drops the superseded lesson and repoints `TRUE`; the old revision survives as immutable history; **must not corrupt sibling lessons** in the same skill body.
  - **`invalidAt` is temporal, not deletion** — excluded from the current set, queryable as history.
  - **Authority** — **authored** (user directive / import) > **inferred** (merged); within inferred, more **hits** then **recency**. Pin the precedence.
  - **Supersede-of-folded vs. -open** — folded path triggers un-fold; open path just stamps + demotes.
  - **Anti-thrash** — bound supersede/revive oscillation per idea per window (a flip-flopping decision shouldn''t churn skills); revive only on a *new* verified PR, never on re-reading the old one.
  - **Cascade** — if I corroborated other ideas, supersession must not orphan dependents.
  - **Authority safety invariant** — an **inferred** contradiction can never supersede an **authored** directive (only a newer authored statement can); between two inferred ideas, hits then recency decide. The safety regression.
  - **Supersede false-positive calibration gate** — before `unfold_mode` flips to `enforce`, shadow telemetry must show the supersede FP rate below a ceiling (e.g. `supersede_fp_ceiling` <10% confirmed-false over ≥30 human-spot-checked locality-collision supersede verdicts), mirroring U6''s golden-judge ladder. File-level-fallback collisions are logged separately (structurally noisier than symbol-level). Conservative by design — a couple of bad verdicts must not bulk-retire the corpus.
  - **Symbol-deletion handling** — a PR that deletes an anchored symbol/file with no superseding change must clean up the stale anchor (remove from the current index, keep in history), not leave it to collide forever.
- **Required tests:** `test_verified_contradiction_supersedes_and_stamps_invalid_at`; `test_superseded_idea_queryable_as_history_not_deleted`; `test_unfold_authors_new_revision_without_corrupting_siblings`; `test_inferred_cannot_supersede_authored`; `test_more_hits_then_recency_wins_between_inferred`; `test_revive_only_on_new_verified_pr`; `test_anti_thrash_bounds_oscillation`; `test_supersede_fp_calibration_gate_blocks_enforce`; `test_deleted_symbol_cleans_stale_anchor`.
- **Verification:** a full supersede→un-fold→revive cycle + the authority safety regression, on fixtures.

### U6. Necessity gate over golden cases (Python)
- **Goal:** demote ideas nothing depends on; keep rare-but-necessary ones.
- **Files:** `ideas/necessity.ts`, golden-case runner, tests.
- **Approach:** an idea''s exercising signal is its `IDEAGOLD#` before→after case. **Correction (was overstated):** that case is a skill-body *drift-detector* (an LLM judges "does the candidate still satisfy the lesson?"), **not** an executable precondition-firing test, and it is keyed `caseId=ideaId` — so the K corroborating PRs refresh **one** case row, not K independent cases. The real transfer-necessity corpus is therefore the golden case **plus any later PR that fires the same anchors after fold** (genuinely independent instances); absent such a PR the gate yields `no_signal`, never a false unnecessary verdict. Necessity = does including the idea improve the case''s outcome (with vs. without). Coverage-aware; aggregate over firings for the un-pairable tail.
  - **Invocation (deliberately relaxes §8):** the executable ablation isn''t available at fold (the golden case is a drift-detector, not an executable test), so the fold-time check is reduced to a **triviality / dedup filter** (non-empty, has anchors, not a near-duplicate of a live idea); the **full necessity ablation runs on a scheduled scan** (EventBridge `rate(1 day)` → the Python necessity handler, scoped via `necessity_sample_rate`/`necessity_min_firings`), demoting a folded-but-useless idea within one cycle. Wrong-*but-merged* lessons are caught by the **contradiction-resolution engine (U5 supersession)**, not the fold-time check. This is the documented deviation from §8''s literal "gate at fold."
- **Subtle features:**
  - **No-signal ≠ unnecessary** — a non-precondition-firing case yields `no_signal`.
  - **Authored directives are exempt** — a `user_directive`/`authored_import` is never ablated for necessity (the human asserted it); mis-retrieval still tightens its scope, never demotes it.
  - **Golden-judge enforcement (RESOLVED via research — graduated, precision-keyed).** The golden judge is a **drift detector, not a correctness oracle**; the literature is clear that an LLM-judge verdict must *not* be a unilateral hard-block (documented judge self-inconsistency — Krippendorff α as low as 0.3 — and agreeableness/false-pass bias). So enforcement is a ladder, split by check type:
    - **(a) Deterministic components hard-block immediately** — a real PR-added test, or a structural assertion on a golden case (output must / must-not contain X, required tool-call shape). Cheap, reliable, no judge variance → block on regression now.
    - **(b) LLM-judged behavioral components graduate:** start **advisory + telemetry** (log every flag; record human-confirmed vs. false) → **soft-block-with-override** once the empirical false-positive rate is **<15% over ≥50 human-reviewed verdicts** *and* the flag replicates across 2–3 runs → **hard-block** only for **catastrophic-severity** cases (safety / irreversible-action contracts) or with **2-judge ensemble agreement at FP <5%**.
    - **Always** keep a logged override path (no-override gates get disabled — the broken-window trap); **auto-demote** a tier if judge calibration (α) drops below ~0.70. **One golden judge:** a single Python implementation of the drift-detector — the standalone TS `rerank/golden.ts` advisory judge is **superseded by it, not run in parallel**. Two triggers share the one judge: the learning fold path (in-process) and the TS manual catalog-promote path (via the loop''s API). Mirrors the plan''s shadow→enforce discipline, per check-type.
  - **Mis-retrieval → scope-miss** — an idea pulled into a case it doesn''t affect tightens scope / down-ranks; never "unnecessary."
  - **Idea-vs-skill granularity** (KTD) — v1 idea-level; note the skill-level rollup seam.
  - **Cost budget** — gate ablation to ambiguous-usage ideas + a sampled tail.
- **Required tests:** `test_unrelated_golden_case_is_no_signal`; `test_necessity_is_with_vs_without`; `test_rare_but_necessary_kept`; `test_misretrieval_is_scope_signal_not_unnecessary`; `test_triviality_dedup_filter_at_fold`; `test_scheduled_scan_demotes_folded_useless_within_one_cycle`.
- **Verification:** seeded idea + golden-case fixtures.

### U7. Session-link capture (Go wrapper) — distillation enrichment, off the verification path
- **Goal:** maintain a best-effort `branch → sessionId(s) + turn-range` map so U1 can pull the authoring Claude conversation into distillation when the change came through a local session. **Not** a verification signal, **not** a vote — a synchronous enrichment lookup that degrades to absent.
- **Files:** `wrapper/internal/capture/hooks.go`, `wrapper/internal/daemon/events.go`, `db/keys.ts` (`branchSessionKey`), `db/repo.ts`, tests.
- **Approach (net-new ingest path, not a field add):** `PostToolUse` is currently *dropped* by both `capture.MapHook` and `daemon.ingestHook` (and `HookEvent` lacks `tool_name`/`tool_input`), so U7 must (1) add `ToolName`/`ToolInput` to `HookEvent`, (2) add a `PostToolUse` branch in `ingestHook` *before* `MapHook` to detect `Bash` + `git push`, and (3) at push time distill the relevant turn-range slice (scrubbed) and write `branchSessionKey(repo, branch) → {sessionId, turnRange, distilledContext}` (last-writer-wins per branch; multi-session branches keep a small list). U1 reads `distilledContext` directly at distill time by `(repo, head.ref)` — no merge-time `EVT#` re-read.
- **Subtle features:**
  - **Best-effort, never blocking:** a missing/failed/forced/detached push simply yields no link → U1 distills from GitHub only. No pending state, no TTL, no async reconciliation (those were the deleted async spine).
  - **Turn-range, not whole session:** map the push to the turn-range that produced it (the `LEARN#<sessionId>#<turnId>` turn key is the bridge) so distillation pulls the *relevant* slice, not the entire transcript.
  - **Stable across `/resume`** (use `PinnedSessionID`).
  - **Eager-distill at capture (logs expire):** `EVT#` records carry a 90-day TTL and merge can lag longer, so the slice is distilled + scrubbed at push time and stored as `distilledContext`; U1 never re-reads `EVT#` at merge time (avoids the companion §4 expiry trap).
- **Required tests:** `test_git_push_records_branch_session_link`; `test_failed_or_detached_push_records_nothing`; `test_link_lookup_returns_turn_range_for_branch`; `test_missing_link_degrades_to_pr_only` (in U1).
- **Verification:** unit tests on the hook parser + the link round-trip; no live git.

### U8. Authored ingestion — user directives + pasted-text decomposition (Python endpoint + write) — the second lane
- **Goal:** let a human put knowledge into the memory graph directly, bypassing the PR gate: (a) declarative directives a user just states ("remember: no dashes"), (b) **authored text ingestion** — paste arbitrary text (a good skill, a doc); it is decomposed into atomic insights and inserted at top authority (no external service, no URL fetch).
- **Grounding (verified against source):** a **key-value memory store already exists** — `memoryKey → MEM#<userId>#<name>` under the project partition (`db/keys.ts`): the per-user, **name-keyed, overwrite-in-place** "Memories" tab synced from the developer''s machine (`rest/memories.ts`: `GET`/`PUT /projects/:pid/memories`; `repo.listMemories`/`replaceUserMemories`, per-author reconcile). **But it is disconnected from the graph** — a flat synced list, never corroborated, never retrieved into a working session, no authority/supersession. A directive *is* a named memory; the new work is **bridging** the KV store into the graph, not inventing a store. (`rest/ideas.ts` is read-only today — there is no idea-insert path either.)
- **Files:** `rest/memories.ts` (+ new `rest/ideas-author.ts` or extend), an in-session agent tool (`remember`), `ideas/authored.ts` (the bridge), `db/repo.ts`, a text-decomposition path (reusing distillation), tests.
- **Approach:**
  - **Directive path:** stated in-session (agent tool) or via the Memories tab (REST). It (1) persists to the existing `MEM#<userId>#<name>` KV store (durable, user-visible, machine-syncable, overwrite-in-place on `name`), **and** (2) **bridges into the graph** as an authored idea with `authorityKind:'user_directive'`, `authored:true`, **immediately active** (no `verified_K`, no golden case) so it is actually retrieved into sessions and can supersede inferred ideas. Agent-native parity: anything the user does in the tab, the agent does via the tool — the user can just *say* it.
  - **Authored text-ingestion path:** the user pastes arbitrary text (agent tool or REST); it is split into atomic paragraph-ideas (one paste → N nodes) by the **same decomposition step** the inferred lane uses, but with a **prose-tuned distillation prompt — not the PR-diff distiller** (pasted docs aren''t diffs: there''s no hunk/anchor signal, so the prompt extracts standalone claims from prose). Shared splitter, separate extraction prompt. Each node is inserted as `authorityKind:'authored_import'`, **authored, top authority, immediately active** (necessity-exempt), with a `sourceRef` (label + per-node `contentHash`). Input hygiene only: a size cap + the U1 secret-scrub on the pasted text. **No URL fetch, no external service, no SSRF surface.**
  - **Authored-source persistence & edit (manage it like a skill):** the paste persists as a **named, editable source** in the existing `MEM#<userId>#<name>` store (visible/editable/deletable in the Memories tab, machine-syncable) — the same manage-your-skills experience as Claude Code, different platform. Its N graph nodes are projections keyed by `(sourceName, contentHash)`; editing the source re-decomposes + reconciles them, deleting it removes them.
- **Subtle features (explicit):**
  - **Scope tag** — a directive''s scope (project / user / org / repo / skill); `MEM#` is already `(project, user)`-scoped (a sensible "this project, me" default), promotable to org. Surface it; ambiguous global style → org-level.
  - **Anchor-optional** — a stylistic directive ("no dashes") has no code anchor; directive supersession is by semantic NLI + scope, not code-locality (locality is the inferred-lane join).
  - **Necessity-exempt** — never demote a user directive as "unnecessary"; mis-retrieval still tightens its scope.
  - **Cross-lane supersession** — a directive supersedes a colliding inferred idea (authority); a later directive supersedes an earlier one (recency, top tier); an inferred PR idea can never supersede a directive. Reuses U5.
  - **Authored semantic-scan entry into U5 (anchor-less)** — a directive / authored node has no code anchor, so on write `authored.ts` runs a cosine+NLI scan over active inferred ideas (org+scope-filtered, `authorityKind:'merged'`); any `supersede` verdict routes into U5 with the authored idea as the predetermined winner (authority). This is the non-anchor entry U5 otherwise lacks.
  - **Input hygiene (text ingestion)** — size-cap the pasted text and run the U1 secret-scrub before decomposition; no server-side fetch means no SSRF/allowlist/content-pinning needed.
  - **Two-store coherence (directives *and* pasted sources)** — editing a source in the Memories tab (or a machine re-sync `PUT`) **re-decomposes and reconciles** its child nodes; deleting it un-bridges all of them; vice-versa too. `(userId, name)` stays the idempotency key. One source → one node for a directive, one source → N nodes for a paste.
  - **Re-ingest / dedupe** — re-pasting the same text dedupes against existing authored nodes by per-node `contentHash` (no duplicate); a node colliding with a local idea reconciles by authority + NLI.
  - **Retraction is itself a directive** — "actually, dashes are fine" supersedes the prior one (recency, top tier).
- **Required tests:** `test_user_directive_persists_to_mem_kv_and_bridges_to_graph`; `test_directive_enters_active_without_pr`; `test_directive_supersedes_colliding_inferred_idea`; `test_inferred_idea_cannot_supersede_directive`; `test_later_directive_supersedes_earlier`; `test_directive_is_necessity_exempt`; `test_memory_delete_unbridges_graph_projection`; `test_pasted_text_decomposes_into_atomic_authored_nodes`; `test_authored_text_enters_top_authority_necessity_exempt`; `test_repaste_dedupes_by_content_hash`; `test_authored_semantic_scan_supersedes_colliding_inferred_without_anchor`; `test_agent_tool_and_rest_write_same_authored_memory` (parity).
- **Verification:** offline fixtures; recorded import payload, no external fetch in tests.

### U9. Config + shadow rollout + e2e
- **Goal:** wire thresholds; prove the full cycle offline.
- **Approach:** wire all thresholds — `[nli]` (candidate-floor 0.80, confidence), `verified_K`, `rung_weights` + `recurrence_bonus`, `author_credibility` (+ `default_author_credibility`), `supersede_thrash_window`, `necessity_*` (incl. `necessity_scan_schedule`), `supersede_fp_ceiling`, mode flags. e2e over a **recorded merge log** (with and without an attached session slice, to exercise both distillation paths): replay → fold → a later contradiction PR supersedes (un-fold, `invalidAt`) → a re-introduction PR revives; necessity demotes a planted useless idea; a contradiction at cosine ~0.95 classifies supersede not merge.
- **Verification:** offline, deterministic, green.

### U10. Single sources of truth — schema codegen + single skill-revision writer (the DRY guardrail; Wave 1)
- **Goal:** eliminate cross-language duplication *by construction*: **one** key/record schema (code-generated into TS + Python), **one** writer of skill revisions (Python), **one** golden judge (U6), **one** embedding path. Nothing is hand-mirrored or byte-compared, because nothing is duplicated.
- **Files:** a shared schema/IDL (single source of truth for the single-table keys + record shapes) + codegen into `packages/shared` (TS) and the Python loop; the Python `skills_write` module (the sole revision author); re-point the TS authoring writes (`rest/skills.ts` fold + manual, seeding) to call the Python author-revision API.
- **Approach:**
  - **Schema as data, generated both ways:** define the single-table key formats (prefixes, pad widths, the variant infix, `IDEA#`/`SKILL#`/`IDEAGOLD#`/`ANCHOR#`/…) + record shapes **once**; generate the TS types/parsers and the Python types/builders. **CI fails if generated output is stale** (the one drift guard).
  - **Single skill-revision writer = Python:** append-immutable revision + CAS `TRUE` pointer + `IDEAGOLD#` capture lives in **one** Python module. The Python fold/un-fold path calls it in-process; the TS catalog-authoring UIs (`hq-add-skill`, web editor, seeding) call it over the author-revision API. No second implementation to keep in sync.
  - **One golden judge, one embedding path** — see U6 (judge) and U2/U4 (embeddings): each a single Python implementation with two triggers.
- **Subtle features:**
  - **Re-pointing existing TS writes is a real, justified refactor** — `hq-add-skill`, the web editor, and seeding author revisions in TS today; they become thin callers of the Python writer. The payoff: the duplicate writer and the entire byte-compat risk *vanish*.
  - **CAS still protects concurrent callers** — a learning fold and a manual edit both hit the *one* writer and race on the `TRUE` pointer; on a lost CAS the loser **re-reads the winning revision body and re-applies its lesson delta** before retrying (never a blind re-CAS of the stale body), so neither write silently loses. One writer, still concurrency-safe.
  - **Readers derive from the generated schema** — the Go wrapper''s materialization and the TS web reads parse from the generated code, so a format change propagates from a single edit.
- **Required tests:** `test_schema_codegen_is_fresh_or_ci_fails`; `test_ts_authoring_routes_through_python_writer`; `test_single_writer_cas_race_retries`; `test_wrapper_and_web_parse_generated_schema`; `test_cas_retry_preserves_concurrent_edit_body`.
- **Verification:** a round-trip over the *generated* schema — Python writes a revision → Go wrapper materializes it → TS web reads it — with no hand-authored mirror anywhere.

## Code touch-points (symbol-level build sheet)

Verified against source; line numbers are anchors (will drift), symbol names stable.

**Home caveat (all-Python loop):** the *entire* learning loop — distillation, corroboration, NLI, supersession, necessity, the merge-webhook receiver, **and the fold/un-fold skill-revision writes** — lives in the **Python service**, not these TS files. The TS symbols pinned below are the **data-contract surface** (the DynamoDB record shapes the Python loop reads/writes, defined once in the shared schema and code-generated into TS + Python) plus the **read** paths TS/Go keep (the web learning-read API now calls Python; the Go wrapper materializes Python-written skill records). What genuinely stays TS *code*: the existing **non-learning catalog authoring** (`rest/skills.ts` manual edits, seeding) and the existing read-only GitHub App (`github/app.ts`, untouched). Read the per-symbol pins as the generated-schema surface the Python loop writes and TS/Go read — not a hand-mirrored or byte-compat contract, and **not** TS implementation work.

**Data model — `packages/shared/src/dto.ts` (Zod, additions `.optional()`):**
- `ideaSourceSchema` (~1047): add `prRef?`, `anchors?`, `authorityKind?: z.enum(['user_directive','authored_import','merged'])`, `verificationRung?: z.enum(['test','normal','bare'])`, `authorId?`. (No `verification`/`branch`/`commitShas` — deleted by the pivot.)
- `ideaSchema` (~1072): add `invalidAt?`, `supersededBy?`, `supersedes?: string[]`, `refines?: string`, `revivedAt?`, `legacyRecurrenceFold?`.
- `corroborationCount` (~1109): retained for telemetry (`new Set(sources.map(s => s.prRef?.number).filter(Boolean)).size`); add `corroborationWeight` = `Σ rungWeight(s) × authorCredibility(s.authorId)` over distinct `prRef.number` + `recurrence_bonus` — the fold spine.
- `IDEA_STATUSES` (~1036): **unchanged** (`open|folded`).

**Ingestion — `packages/backend/src/github/app.ts` (+ new `github/prs.ts`, `github/mergeHandler.ts`):**
- `GitHubApp` (~79) is read-only commit polling (`listCommits` ~215, no PRs). Add `listMergedPullRequests` + a webhook handler reusing `installationToken` (~103) + injectable `fetch` (~82, fixtures). `repoProjectKey` (`REPO#/PROJ`) resolves project; `PublicGitHubReader` (~349) is the unauthenticated reader for **public repos** (un-consented private installs surface a "re-consent needed," not a silent fallback).

**Reconcile/NLI — `ideas/corroborate.ts` + new `ideas/nli.ts`:**
- Keep `vectors.queryTopK(...)` (~192) as the **candidate fetch** at `IDEA_CANDIDATE_FLOOR` (rename `IDEA_MERGE_THRESHOLD`=0.9 at ~66 → 0.80; repurpose `mergeThreshold()` at ~71).
- **Replace the merge *verdict* at ~199** (`if (top && top.score >= mergeThreshold())`): route candidates (semantic) and anchor-collisions (locality) through `nli.classify` → corroborate (merge branch ~242–273, minus body-rewrite-on-contradiction) / refine (keep both) / supersede (→ U5) / neutral.
- `ideas/nli.ts`: `classify(premise, hypothesis) → {label, confidence, requestHash}`, record/replay, label-map asserted at load.

**Anchor index + repo — `db/keys.ts`, `db/repo.ts`:**
- New keys: `anchorKey(org, repo, file, symbol, ideaId)`, `processedPrKey(org, repo, prNumber)`, `verifyEventKey(ideaId, seq)`. New methods: `writeAnchors`, `getIncumbentsByAnchor`, `markPrProcessed`/`isPrProcessed`, `stampInvalidAt`, all via `corroborateIdeaConditional` (~1593) where they touch an idea (never an unconditional `putIdea` ~1500 on a concurrent path).
- Reads `getIdea` (~1509), `listIdeasForSkill` (~1517), `listIdeasForOrg` (~1523) must **exclude `invalidAt`** ideas from the current set, keep them for history.

**Un-fold — new `ideas/supersede.ts` + the skills fold service (`foldIdea`, `rest/skills.ts`):**
- On a verified `supersede`: `getIdea` incumbent → authority decision → conditional `invalidAt`/`supersededBy` → un-fold: read `foldedIntoRev`, author a *new* skill revision dropping the superseded lesson (inverse of `foldIdea`), repoint `truePointerKey`; old revision + `postFoldSources` survive. The "matched a folded idea" branch (~204) is the precedent for touching a folded idea safely.

## Rollout: shadow-first

This changes live fold/corroboration behavior → ship behind flags in shadow mode (R3 tripwire discipline): compute distill/supersede decisions and **log** them while live behavior is unchanged, until shadow telemetry shows thresholds are calibrated, then flip to `enforce`. **Un-fold/supersede enforce last** (a false supersede edits a real skill body).

Sequence: (1) PR-ingestion + anchors + NLI in shadow (observe the distill/move distribution); (2) verified-corroboration in shadow (how many folds change); (3) enforce corroboration; (4) supersede/un-fold in shadow; (5) enforce supersede **only after the supersede FP-rate calibration gate passes (<10% confirmed-false over ≥30 spot-checked verdicts)**, with anti-thrash on.

## Unit DAG (for parallel build)

- **Wave 1** (independent): `U1` (ingestion driver) ∥ `U2` (anchor model) ∥ `U3` (NLI port) ∥ `U7` (session-link capture, Go) ∥ `U10` (single-source schema + writer) ∥ `cleanup` (the dead-code deletes above).
- **Wave 2:** `U4` (verified corroboration; needs U1, U3, **U10** for the fold write).
- **Wave 3:** `U5` (supersede/un-fold; needs U2, U3, U4, **U10** for the un-fold write) ∥ `U6` (necessity; needs U4) ∥ `U8` (authored ingestion; insert/bridge is independent, cross-lane supersession needs U5).
- **Wave 4:** `U9` (config + e2e; needs all).

`U1` only *soft*-depends on `U7` — build U1 against a stub link-lookup (returns none → PR-only distillation), wire U7''s map in when ready. The async-reconciliation subsystem is still gone; U7 is a synchronous map, not a pending store.

## Cleanup & deletions (do alongside — clears dead code this project would otherwise sit beside)

Delete before/while building, so the new session-link (U7) and authored-memory (U8) work isn''t confused with abandoned experiments. Each is verified dead by two independent code sweeps; **re-confirm zero non-test callers immediately before removing.**

- **SessionVector / "Forge" (U27) dead code** — `packages/shared/src/dto.ts` `sessionVectorSchema` (~985–998), `packages/backend/src/db/repo.ts` `putSessionVector`/`listSessionVectors` (~1427–1441), and `sessionVectorKey`/`sessionVectorPrefix` in `db/keys.ts` (~450–458). Write-only, **zero callers, no route, no stream consumer** — a deferred session→agent search that never shipped. Removing it avoids collision with U7''s (different) branch→session map. *(Guard: grep `putSessionVector`/`listSessionVectors`/`sessionVectorKey`, confirm 0 non-test refs first.)*
- **Retired `410 Gone` scope routes** — `rest/skills.ts` `POST /skills/:name/scope` (~450) and `rest/agents.ts` `POST /agents/:name/scope` (~77). Org-only catalog made tiers obsolete; unreachable no-ops. Delete the handlers + their dispatch + any `infra`/`devServer` route entries.

**Explicitly NOT in scope** (the sweep found these, but they belong to the weekly/strategic-execution track, not distillation): the weekly `wsjfPriority`/`deriveCategory` stubs (`weeklyLifecycle.ts`) and the Neon HTTP→WebSocket transaction driver (`weeklyTransitions.ts`). Real gaps — left untouched here.

## Migration / backfill

Existing `folded` ideas were corroborated by **recurrence** (no `prRef`). Recomputing under verified semantics would un-fold them en masse — wrong. Tag each `legacyRecurrenceFold = true` and **grandfather** as verified-equivalent. A grandfathered fold then either (a) earns a real merged-PR vote → confirmed (flag cleared), or (b) is superseded by a verified PR → retired normally. **Optional transfer backfill:** replay the repo''s historical merged PRs through U1 to reconstruct the library on verified footing from scratch (the controlled rebuild the design calls for) — run in shadow, diff against the legacy library, promote when calibrated. New ideas use verified semantics from day one.

## Config & thresholds

- `[nli] candidate_floor = 0.80`; `confidence_threshold` (below → LLM-judge fallback, never cosine).
- `verified_K = 2` — accumulated rung-weighted corroboration to fold an inferred idea (authored ideas skip the threshold).
- `rung_weights = {test: 1.0, normal: 0.6, bare: 0.4}` — inferred from the diff/PR (test-or-bugfix / substantive / trivial); a merged PR is **never 0**. `recurrence_bonus = 0.2` — added per repeat hit on one idea (capped).
- `author_credibility` — per-coder weight (user-curated, settable during *or after* ingest); `default_author_credibility = 0.5` for unknown/single-author, higher when a PR had a distinct non-author reviewer. Multiplies each hit''s weight; boosts may be retroactive, but the system never auto-demotes folded ideas.
- `supersede_thrash_window` — bound supersede/revive oscillation per idea.
- `necessity_sample_rate`, `necessity_min_firings`, `necessity_scan_schedule = rate(1 day)`.
- `supersede_fp_ceiling = 0.10` — the calibration gate for flipping `unfold_mode = enforce`.
- Flags: `verified_learning_mode = shadow|enforce`, `supersede_mode = shadow|enforce`, `unfold_mode = shadow|enforce`.

## Telemetry (shadow→enforce calibration inputs)

Per skill/org: distinct-PR corroboration distribution + **corroboration-weight (rung × credibility) distribution + per-author credibility distribution**; anchor-collision hit rate (locality coverage) + **symbol-vs-file-level anchor-resolution rate**; supersede events (with the contradiction pair) + un-fold count + **supersede false-positive rate (the U5 calibration-gate input)**; revive count; necessity demotions; **NLI move distribution** (corroborate/refine/supersede/neutral) + judge-fallback rate; authored-vs-inferred idea ratio. These decide when each mode flips to `enforce`.

## What we deliberately give up (honest scope)

- **The un-PR''d tail.** Work that never becomes a merged PR (local experiments, internal/un-CI''d repos, direct-to-main) is **not learned.** This is the precision-for-recall trade the pivot chooses on purpose.
- **Non-local contradictions.** A PR that obsoletes an insight without touching its anchors is missed by the locality join (semantic backstop only, lower recall).
- **Distillation richness on the un-linked tail.** When a PR has *no* attached Claude session (human-authored, external repo, history replay of pre-capture commits), distillation falls back to GitHub-only context (diff + description + reviews + linked issues) and loses the agent-side "why." This is graceful degradation, not a gap in the spine — the session enrichment (U7) is used whenever available.
- **The no-revert soak window.** §8 floated holding a fold for N days to catch a fast revert; v1 **folds immediately** and relies on supersession/un-fold (U5) as the reversal path instead — simpler, at the cost of a fold-then-unfold churn when a merge is reverted shortly after.

## Acceptance criteria (done = all true)

- A merged-to-main PR distills + anchors; a non-default-base or closed-unmerged PR does not produce a positive insight.
- An inferred idea folds when `corroborationWeight` (Σ rung × author-credibility, + recurrence bonus) reaches `verified_K` (default 2; rung test 1.0 / normal 0.6 / bare 0.4, never 0) — a single low-rung PR is a candidate, not folded; a single-author repo still folds via recurrence; reprocessing one PR does not double-count; two near-duplicate paragraphs consolidate into one idea.
- Author credibility: a user-set credibility boost raises a coder''s signal (retroactively if applied after the fact) and never auto-demotes already-folded ideas.
- A contradiction at cosine ~0.95 classifies `supersede`, never merges (negation-blindness regression).
- A verified PR touching an insight''s anchors supersedes + un-folds it (`invalidAt` stamped); the row survives as queryable history.
- An inferred contradiction cannot supersede an authored directive (authority safety regression).
- Re-introduction **revives** a superseded idea (no re-learn from scratch).
- A **user directive** enters the active set immediately (no PR), supersedes a colliding inferred idea, is exempt from the necessity gate, and persists in the `MEM#` memory store; **pasted authored text** is decomposed into atomic top-authority nodes (no PR, necessity-exempt), deduped by `contentHash`.
- The necessity gate demotes a planted useless idea (within one scheduled scan cycle) and keeps a rare-but-necessary one; a triviality/dedup filter runs in-process at fold (wrong-but-merged lessons are caught by U5 supersession, not the fold-time check).
- Existing folded ideas are grandfathered, not mass-churned.
- Replaying a fixed merge log is deterministic; the full suite runs offline (recorded webhooks/PR fixtures, NLI fixtures); zero live GitHub, zero quota.
- Shipped shadow-first; each mode enforces only after its telemetry is calibrated.

## Open Questions

1. ~~`verified_K`~~ **RESOLVED** — a single `verified_K` hit threshold (default **2** distinct merged PRs) for inferred ideas; **no human-review sub-tier** (the merge is automated gating, not human verification — human verification lives only in the authored lane). Authored ideas skip the threshold.
2. ~~Idea atomicity~~ **RESOLVED — paragraph-level.** Ideas stay synthesized paragraphs; we do **not** build a bespoke claim-decomposer. Adopt atomic-claim decomposition only if a **well-founded off-the-shelf tool** proves robust (research spike: evaluate an existing proposition/claim-extraction model — e.g. a "propositionizer" — against our ideas; adopt only if clean). Standing requirement either way: the **corroborate/merge machinery must cleanly consolidate two highly-overlapping paragraphs into one** (no near-duplicate ideas) — the existing MERGE-REWRITE re-synthesis on the corroborate verdict, which U4 must keep tight. NLI then operates paragraph-to-paragraph.
3. ~~NLI bundle vs. shared service~~ **RESOLVED** — moot under the maximal-Python decision: NLI lives inside the Python brain alongside the rest of the learning algorithm (reusing `nli.py`). No onnxruntime-node bundle.
4. ~~PR→session enrichment~~ **RESOLVED** — distillation pulls three sources: PR diff/description + GitHub comment/review history (always) + the authoring Claude session''s messages (when linked via U7). The session link is enrichment, off the verification path; it degrades to GitHub-only when absent.
5. ~~Cross-org generalization~~ **RESOLVED — org-local.** Verified lessons stay org-local for v1; scope-tagged cross-org promotion is deferred.

**Deferred — defaults set, revisit during shadow (not blocking):** secret/PII scrub allow-list exceptions (deny-list committed in U1) · anti-thrash window length · migration grandfather specifics · multi-hop git-flow (v1 ignores intermediate non-main merges) · golden-case re-runnability (precondition-signature + minimal repro) · necessity idea-vs-skill granularity (v1 idea-level) · golden-judge α / false-positive thresholds · authored-directive scope-resolution UX · distillation cost/throughput budget on busy repos.

## Risks & Dependencies

- **NLI** (U3) — the hard dependency, now in-loop in Python (reuses `nli.py`); its *role* is narrow (few candidates per merge).
- **Un-fold** (U5) — the sharpest behavioral change; reversing a fold without corrupting sibling lessons in a shared skill body.
- **Anchor precision** (U2) — symbol extraction quality drives supersession recall; file-level fallback bounds the downside.
- **GitHub access** (U1) — private-repo transfer needs the GitHub App installation token with **`Pull requests: Read`** (per-installation re-consent, inert until approved); public repos use the unauthenticated `PublicGitHubReader`. The **deferred** continuous webhook adds a public endpoint + secret rotation; v1 (user-run job) avoids both.
- **Migration** — existing folds were recurrence-corroborated; grandfather + optional transfer-replay rebuild, never silent churn.
- **Distillation quality** — PR-only context may be thin for terse PRs; the session-enrichment edge (U7, a Wave-1 unit — not deferred) is the mitigation.
