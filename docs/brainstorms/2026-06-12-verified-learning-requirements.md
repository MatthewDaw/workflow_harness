# Verified Learning — Requirements

**Date:** 2026-06-12 · **Status:** draft (refined design session) · **Tier:** Deep (product/architecture)
**Feeds:** `docs/plans/2026-06-12-011-feat-verified-learning-plan.md` (the HOW — now the architecture source of truth). This doc is the WHAT + the architectural subtleties.
**Depends on:** R3 reform (plans 008–010), Plan 007 probation/induct.

---

## 0. PIVOT (2026-06-12) — PR-merge-gated learning

After this doc was written, the design pivoted: **learning is gated entirely on merged pull requests.** The merged PR is the atomic unit; main''s merge history is an ordered event log we fold forward (transfer = replay it, live = a webhook drives the same handler). This **deletes** the session→commit→PR *verification* join, the async pending-vote store, and the 4-state vote machine. The wrapper push-capture survives in a much smaller form — a synchronous `branch→session` map used only to pull the authoring Claude conversation into distillation (alongside the PR diff/description and the GitHub comment/review history), **off the gating path** and degrading to GitHub-only when absent. **Contradiction detection becomes code-locality**: each insight is anchored to the `(file, symbol)` it was learned from, and a later PR touching those anchors is the supersession candidate (a literal revert is the trivial subcase) — which answers the "how does a PR contradict what came before" question §8 left open.

**Superseded by the plan:** §3''s "read from anywhere" framing (now PR-only), §4''s session⋈git join machinery (now PR-is-the-provenance), §8''s revert-watching (now locality supersession), and Q2/Q6''s session-capture and Dynamo-read-scrub (now PR-content scrub). **Still standing:** the necessity gate (§6), the verification ladder framing (§5, with the merged PR as the dominant rung), graded-not-binary outcomes (§9), and the non-goals (§2). The plan''s "Open Questions" supersede §11. Read the plan for the current architecture; this doc is retained for the WHAT and the rationale trail.

---

## 1. Problem & Context

The agent-families library accumulates insights. R3 gives it organizational pressure (`cost(G)`, 009) and regression-down batch validation (004/U7) — but **nothing checks whether an individual insight is *necessary*.** Harmless-but-useless insights pass every gate (they don't disorganize, they don't regress) and accumulate; at thousands of insights — an empirically uncharacterized regime — retrieval precision degrades. That is the bloat risk.

Separately, we want the system to **learn from any repo, any PR, and ordinary vibe-coding sessions** — not just its own graded build episodes. The research is unambiguous: verified test-pass signals produce transferable knowledge, while read-derived distillation injects hallucinated generalizations (ExpeL, AWM) and SFT/imitation degrades out-of-domain generalization (SWE-RL, DeepSWE, SWE-Gym). So external learning has to be **gated on verification, not trusted on read.**

The two problems share one solution: **verified counterfactual ablation** — an insight earns and keeps trust only by measurably improving a *verified outcome* on a task that *exercises its precondition*. The cheapest, strongest verification already exists for free: teams review, CI, and merge code. If we map the agent's own conversation logs (in DynamoDB) to the PRs they become (in git), the **merge is the verification and the revert is the retraction.**

## 1.5 Grounding in the existing platform (`packages/backend`) — resolves the Dynamo unknown

The session logs and an insight library **already exist** in the `harness` TypeScript backend. Verified before claiming, from source:

- **Session logs = the `harness` single-table DynamoDB** (`packages/backend/src/db/keys.ts`):
  - **Events (the messages):** `PK=SESS#<sessionId>, SK=EVT#<seq>` — ordered, monotonic per session; `(sessionId, seq)` is the dedupe key.
  - **Correction learnings (already mined, per turn):** `PK=PROJ#<projectId>, SK=LEARN#<sessionId>#<turnId>` — *"a learning mined from a correction turn,"* idempotent on `(sessionId, turnId)`. This is the message-range attribution, already first-class.
  - **Resolvers:** session pointer `SESS#<sessionId>/PTR` (→ projectId); **repo→project `REPO#<owner/repo>/PROJ`** with GitHub webhooks (U26); session vectors `USERVEC#<userId>/VEC#<sessionId>` (U27 Forge).
- **The insight library = the "ideas" system** (`packages/backend/src/ideas/corroborate.ts`): `Idea{ sources: IdeaSource[], postFoldSources, status: open|folded, corroborationVersion }`; **`IdeaSource{ sessionId, segmentId, seq, snippet, projectId?, repoId? }`** — so each corroboration vote *already carries its repo/project provenance*. MERGE-REWRITE on ≥0.9 cosine (sharpens text, not just count).
- **The exercising test already exists** as **golden cases**: `IDEAGOLD#<skillBaseName>#<caseId>` = *"the fold's before→after expectation"* — captured at fold time. That is the regression oracle I called "the exercising test."
- **The PR stream exists**: `github/app.ts` + `github/history.ts` (the GitHub App).

### The load-bearing realization

**Current corroboration is recurrence-across-sessions — i.e., co-occurrence — and it is monotonic with no decay** (`corroborate.ts`: corroboration = `|distinct sessionId| ≥ K=2`; *"a session, once counted, stays counted; corroboration only ever grows"*). It does **not** check whether those sessions' changes ever **merged** or were **reverted**. So a *wrong* lesson that recurs in two sessions corroborates and folds — exactly the co-occurrence trap and the bloat risk we've been circling.

**Therefore the verified-learning reform, grounded in this platform, is precisely:**
1. **Gate corroboration on verification** — a session's vote should count (or count more) only when that session's change reached a **merged PR**, joined via the `IdeaSource.repoId/projectId` already on the source + the GitHub App. Recurrence alone (today's signal) is downgraded from "corroborated" to "candidate."
2. **Add retraction** — when a source change is **reverted**, remove/down-weight that vote. This **breaks the current monotonic-no-decay invariant on purpose** (sources must be able to shrink/demote), and is the genuinely-new mechanism the rest of this doc specifies.
3. **Reuse the golden case (`IDEAGOLD#`) as the exercising test** for the necessity gate, rather than inventing a new artifact.

This is mostly **wiring verification into existing primitives** (ideas + corroboration + golden cases + the GitHub App + `IdeaSource` provenance) plus the new **decay/retraction path** — not a from-scratch build. (Outstanding Q1 is resolved.)

## 2. Goals / Non-Goals

**Goals**
- Add a per-insight **necessity gate** that removes insights nothing depends on.
- Let the system **distill verified insights from any repo / PR / vibe-coding session**, decoupled from whether the *source* has tests.
- Make learning **symmetric**: promote on a fix being merged, retract on it being reverted.
- Keep the library's active set lean without deleting reversible knowledge.

**Non-goals**
- No RL/policy training on the data — this is *library* learning, not model training.
- No diff-similarity reward anywhere.
- No deletion of an insight on a single negative signal (demote, don't drop).
- No change to R3's ingest gauntlet, retrieval, or objective — this composes on top.
- No syncing of conversation content into git (see §4 — content stays in DynamoDB).

## 3. The Core Mechanism (the throughline)

**Read from anywhere → enter probationary → earn trust only on a verified outcome from a task that exercises the insight → promote on durable reality (merge), retract on reversal (revert).**

- **Grade on verified behavior, never diff-match.** Diff-similarity punishes correct-but-different solutions (SWE-RL's own caveat); test-pass is immune.
- **Trust scales with the verification rung** (reuses Plan 007 probation + 004 validation-class).
- **Pair with the exercising task** — never ablate against a random/unrelated task; an unrelated trivially-passing test yields *no signal*, never "unnecessary."
- **Verify transfer, not the source instance** — full trust requires helping a *different, similar* case.

## 4. Provenance: DynamoDB session-log ⋈ git/PR (the corrected design)

**Conversation content stays in DynamoDB; git carries only a pointer.** Do **not** sync session messages into git history. The git side carries a `Session-Id + turn-range` stamp on the commit; distillation fetches the relevant message slice from DynamoDB by that pointer.

**The join is many-to-many-to-many** (`session : commit : PR`):
- One session produces many commits across many bugs → attribute the *specific turn-range* that produced each commit, not the whole session.
- One commit/PR can involve many sessions (multiple agent sessions, plus human edits).
- The PR is the unit at which the *verification* (review + CI + merge) lands.

**Capture the session→commit edge AT THE SOURCE (decided simplification).** Assume the user's commits/pushes go through Claude (enforceable — the daemon/wrapper hosts the session and can mediate or observe the git call). Then the session *witnesses its own push*: at push time, record `(sessionId, turnId, commitSha, branch, repo)` into the session log. This **solves Q2 with no fuzzy reconstruction** — the session→commit→branch mapping is captured, not inferred. It also anchors the exact commit to the exact correction turn, which is ideal distillation provenance.

**But verification stays GitHub-side — the two halves are complementary, not a replacement:**
- The push-capture gives you *authorship* (session → commit → **branch**).
- **Merge and revert are not "pushes in Claude"** — a merge happens via review/CI/GitHub UI; a revert is a *future* PR, often by someone else. So the **GitHub App** (`github/app.ts`) still owns the verification signal.
- **Join on the branch, not the SHA.** The captured SHA is rewritten by squash/rebase at merge; the **branch is the stable join key**. Flow: session pushes to branch B → GitHub App watches B → its PR → merge (promote, graded) / revert (retract). `patch-id` is the belt-and-suspenders backup; the trailer is optional now that capture is at-source.

So: **in-session push capture (authorship) ⋈ GitHub App (verification) on the branch.** Cleaner and more reliable than the fuzzy session↔commit join.

**Graded merge outcome** (not binary):
- `merged_clean` — agent's change shipped as-is → strong positive.
- `merged_changed` — the merged version diverged from the agent's; **the reviewer's diff is a labeled near-miss** (mine it as a corrected candidate).
- `closed / rejected` — negative.

**Retraction on revert is vote-removal, not a kill switch:** a revert resolves the reverted commit → its provenanced insight(s) → removes that corroboration vote; the insight demotes to dormant only if net support drops below threshold. An insight corroborated by other live PRs survives one revert.

**Subtleties carried by this design (all must be handled):**
- **Attribution at commit time.** The `Session-Id + turn-range` must be injected when the commit is made — by a git hook or the agent's commit path. The agent is frequently *not* the committer (the human commits/edits the change), so coverage is partial; human-amended commits may strip the stamp. The lagging churn/corroboration signal backstops the gaps.
- **Async, out-of-order events.** session → commit → PR → merge → (later) revert is a temporal chain with lag at each hop. A durable **pending-candidate store** keyed by `session+commit` holds the candidate; merge/revert events (poller or webhook, recorded for tests) drive promotion/retraction and must tolerate out-of-order arrival.
- **DynamoDB retention.** Session logs expire; if a log is gone, the insight loses its provenance context (distill eagerly at ingest; don't depend on the log being re-readable later).
- **Secret/PII scrub on the Dynamo read.** Session logs contain secrets, credentials, and internal code. Distillation must scrub *before* an insight enters a shareable library; the admission gate's generalization (strip trivia → typed placeholders) is the second pass, not the only one.
- **Privacy boundary.** An insight distilled from a private repo's session must not leak that repo's specifics into a cross-target/shared library — generalization + scope tags enforce this.

## 5. The Verification Ladder

Strongest available executable signal, never diff-match:
1. **Real fail-to-pass test** (the PR's regression test) — strongest.
2. **Synthesized regression test** — generated when the PR added none; **self-checked** (must FAIL on base, PASS on the known-good state, or it's discarded).
3. **Metamorphic / behavioral check** — the bug's reproduction as oracle (reuse Plan 007 U8 AC-derived machinery).
4. **Build / typecheck / lint floor** — universal, weak.
5. **Human correction / acceptance** — see §7; above the judge rung.
6. **LLM-judge on behavior** — universal, noisiest, last resort.

Confidence is fixed per rung. **"Any repo" is bounded by "any repo you can run":** un-buildable repos degrade to the judge rung at low confidence — distilled candidates from them stay deeply probationary.

## 6. The Necessity Gate

`assess(insight)` runs the insight's exercising task **with vs. without** it through the ladder and compares verified outcomes.
- **Coverage-aware:** measured only against a task that fires the precondition. No such task → `no_signal`, not `unnecessary`.
- **Transfer, not source:** trust counts only from exercising cases *other than* the source instance.
- **Counterfactual, not co-occurrence:** "in-context when it passed" earns nothing; only a with/without delta does.
- **Aggregate for the un-pairable tail:** advantage over many recorded firings + cross-target corroboration + the ratchet — never a single ablation.
- **Mis-retrieval is a scoping signal, not a necessity failure:** an insight retrieved into a task it doesn't affect → tighten precondition / down-rank (NOT demote-as-unnecessary). Scope/precision is the *primary* bloat defense; the necessity gate is secondary cleanup.

**Re-runnability subtlety:** verifying transfer requires re-firing the precondition later, but the original test env may be gone. Transfer therefore leans on *new* cases that exercise the precondition (the system's own future episodes, or new PRs), with at most a preserved minimal repro — not on re-running arbitrary historical repos.

## 7. Human-in-the-Loop Verification

- **Human correction/acceptance is a first-class verification rung.** Entry needs *some* verification (human counts); full trust needs a test or proven transfer.
- **Explicit positive act only** — commit/keep, explicit affirmation, observed-it-work, survives-N-days. **Silence is not an event** (the co-occurrence trap).
- **Corrections rank above acceptances** — a user correcting the agent is a clean labeled near-miss (the richest signal); the agent-guessed-and-user-said-ok case is weaker and fallible ("believed fixed" ≠ "is fixed").

## 8. Promote / Retract Lifecycle

- **Promote on merge**, gated by the necessity gate (merge alone is co-occurrence; necessity must confirm). Top-tier trust additionally requires surviving an **N-day no-revert window**.
- **Retract on revert** → `invalid_at` + `demote_to_dormant` (Plan 008 machinery), as vote-removal (§4). **Demote, don't delete** — revivable.
- **Revive on re-corroboration** — a dormant/reverted insight whose pattern re-merges on a different target revives rather than being re-learned.
- **The revert is itself a lesson** — its diff/comments seed the corrected insight.

## 9. Cross-Cutting Subtleties / Design Notes

- **Self-referential bootstrapping.** The system will learn from its *own* build sessions (it is building itself now). Compounding, but it can amplify its own biases — needs a guardrail (e.g., down-weight or quarantine insights whose only provenance is the system's own self-build until corroborated by an external target).
- **The exercising test as a stored artifact** is heavy (env + deps). Prefer storing the *precondition signature* + a minimal repro, and re-exercise on new matching cases.
- **`cost(G)` vs necessity** are orthogonal: organization vs need. Both stay; neither subsumes the other.
- **Graded, not binary, throughout** — merge outcome, verification rung, corroboration count are all graded; binary pass/fail is too coarse to assign single-insight credit.
- **Curriculum** — prefer bug-fix PRs with fail-to-pass tests, heavy review, and near-misses; skip trivial/mechanical; log every drop.

## 10. Scope Boundaries

**Deferred for later**
- Multi-repo concurrent PR-sync at scale (single-stream v1).
- Live GitHub credentials in the test path (recorded streams only).
- A standalone "exercising-environment snapshot" service (use precondition-signature + minimal repro first).

**Outside this product's identity**
- Training/RL on the harvested data (this is library learning).
- Any diff-similarity reward.
- Storing conversation content in git.

## 11. Outstanding Questions

1. ~~DynamoDB session-log schema/keys~~ **RESOLVED** (§1.5) — `harness` single-table: `SESS#/EVT#` events, `PROJ#/LEARN#<sessionId>#<turnId>`, `REPO#/PROJ`, `IdeaSource{sessionId,segmentId,seq,projectId,repoId}`.
2. ~~Commit-time attribution / where the hook lives~~ **RESOLVED** — extend the wrapper's existing `PostToolUse` hook (`wrapper/internal/capture/hooks.go`, already installed and forwarding to the daemon socket) to detect `Bash` + `git push`/`git commit`, parse `(commitSha, branch, repo)`, and emit a `git.push` capture event `(sessionId, turnId, ...)` through the existing daemon→backend→session-log pipeline. Medium-effort (new PostToolUse ingest branch in `ingestHook` — currently dropped — plus `tool_name`/`tool_input` fields on `HookEvent` and a git-push parser). Primary path = the agent's Bash tool-call; secondary best-effort = the wrapped terminal pane (PTY/vt10x). Join on **branch**; the GitHub App owns merge/revert.
3. **Net-support threshold for retraction** — with corroboration = `|distinct sessions|` and `K=2`, how many *verified* votes must remain for an insight to survive a revert, and do verified votes outweigh recurrence-only votes?
4. **No-revert window length (N days)** for top-tier trust — fixed, or per-repo cadence?
5. **Self-build guardrail strength** — quarantine self-provenanced insights until external corroboration, or merely down-weight?
6. **Scrub policy** — allow-list vs. deny-list for what may leave a session log into the shared library.

## 12. Assumptions

- Session logs are durably in DynamoDB, keyed by a session-id, with per-message/turn timestamps. *(To verify — Q1.)*
- The agent's commit path (or a hook) can stamp commits at creation time.
- The team's normal review/CI/merge flow is the verification harness — no separate sandbox reproduction needed for the vibe-coding path.
- Most learnable fixes eventually reach a PR; un-PR'd fixes stay probationary forever (correct).

## 13. Success Criteria

- A read-only/vibe-coded candidate **never reaches the active set** without a verified outcome on an exercising task.
- A merged-then-reverted insight is **out of the active set** (dormant, revivable) without being deleted.
- An insight corroborated by multiple PRs **survives a single revert**.
- The necessity gate **demotes** a planted useless-but-harmless insight and **keeps** a rare-but-necessary one (verified on a precondition-firing case).
- No code path scores against textual similarity to an oracle patch.
- Conversation content is read from DynamoDB; git carries only the pointer.

## 14. Relationship to Existing Plans

This refines and *supersedes the provenance design* in `2026-06-12-011-...-plan.md` U6/U7: the join is DynamoDB-session ⋈ git/PR (pointer-in-git, content-in-Dynamo), retraction is vote-removal, and the message-range attribution + secret-scrub + self-build guardrail are added. The 011 plan's units (schema, verification ladder, repo/PR target, PR ingestion, necessity gate, lifecycle, human-rung) stand; their requirements should be updated to match this doc before queuing.
