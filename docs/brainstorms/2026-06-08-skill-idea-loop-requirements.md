---
date: 2026-06-08
topic: skill-idea-loop
---

# Ideas: a session → skill improvement loop

## Summary

A background loop turns each auto-generated session topic into an "idea" attached to its best-matching skill, found by embedding search then an LLM rerank. Ideas corroborated across multiple independent sessions surface read-only to the live agent when a skill loads; every skill lists its ideas in a Command HQ dropdown; and a new `/skill-idea-iterate` skill lets a person merge the best ideas into the skill itself. Topics that match no skill collect in an unassigned bin that doubles as a backlog of skills worth creating.

## Problem Frame

Skills only improve when a person notices something and hand-edits them. The corrections and conventions that surface naturally during real sessions — "this skill should have told me X," "the right move here was Y" — evaporate when the session ends. The 2026-06-06 topic-capture design already mines those corrections into structured records but stops there: it explicitly deferred feeding them back into the skills. So today the signal is captured and then sits, and the skill a hundred sessions later is no better for it.

The cost is twofold: knowledge that was paid for once (a real debugging detour, a correction the user typed) is never reused, and the skill catalog drifts further from how work actually goes. As the catalog grows, the gap compounds — more skills, none of them learning.

## Key Decisions

- **Read-side flywheel before write-back.** Most of the value is delivered the moment corroborated learnings surface at point-of-use; folding into the canonical skill is deliberate consolidation, not the value driver. This lets the cheap, low-risk part run automatically while the one risky operation stays human-gated.

- **Corroborated-only into working context.** Humans see every idea in Command HQ and `/skill-idea-iterate`; a live coding session only ever has *corroborated* ideas injected. Anthropic's own skill selection degrades past ~30–50 in-context items, and research shows dumping low-signal notes into a skill's context lowers task success — so point-of-use gets the filtered set, the human gets the firehose.

- **Two-stage association: embed, then rerank.** A topic finds its skill by embedding vector search for the top-k most similar skills, then an LLM judge picks the single best (or rejects all). Claude Code does *not* do this under the hood — it loads all skill descriptions into the prompt and reasons over them, which is exactly what degrades at scale. Stage 1 is the pre-filter the platform lacks; stage 2 mirrors how Claude natively selects a skill, but over a focused set, keeping judge cost flat as the catalog grows.

- **Skill embeddings are a maintained derived index.** Because selection rides on the description, each skill carries an embedding built from its description (and body), regenerated on every mutation — create, edit, fold, and seed. Folding therefore sharpens future associations: improving a skill re-indexes it.

- **Fold through the existing versioning, human-gated.** Merging an idea writes a new skill revision and a human promotes it; no blind overwrite, no auto-fold. Prior research found curated skills help (+16.2pp) while unverified self-generated ones hurt (−1.3pp), so a person stays the quality gate.

- **Corroboration is a distinct-session set.** An idea records the set of sessions that produced it; its size is the corroboration count. The same structure does dedup and quality-gating at once, and counting *distinct sessions* keeps one long session from self-corroborating.

- **The unassigned bin is the new-skill backlog.** A homeless topic is not dropped onto a random skill; it lands in a bin, and recurring entries there are the signal for which skills to create next.

## Actors

- A1. **Session miner** — runs wrapper-side on the developer's own model; turns auto-generated topics into findings automatically, no human in the loop.
- A2. **Skill indexer** — (re)embeds skills on every write and runs the topic→skill vector search plus judge rerank.
- A3. **Live coding agent** — receives corroborated ideas, read-only, when it loads a skill.
- A4. **Human curator** — reviews and folds ideas via `/skill-idea-iterate` and Command HQ.
- A5. **Command HQ (web)** — surfaces each skill's ideas and the unassigned bin.

## Key Flows

- F1. **Background idea generation**
  - **Trigger:** a session topic is generated/updated.
  - **Steps:** embed the topic → vector-search the catalog for top-k skills → judge picks the best or rejects all → merge into the matching skill's idea (or create one), else route to the unassigned bin → advance the session's reviewed watermark.
  - **Covers:** R1, R2, R4, R5, R6, R12, R13.

- F2. **Surface at skill-pull**
  - **Trigger:** a skill loads in a working session.
  - **Steps:** fetch that skill's corroborated ideas → inject them as a fenced, read-only "candidate learnings" block in the materialized skill; the stored skill body is untouched.
  - **Covers:** R16, R17.

- F3. **Human fold**
  - **Trigger:** a person runs `/skill-idea-iterate` (or acts from the HQ dropdown).
  - **Steps:** review the skill's ideas → merge chosen ones into a new skill revision → human promotes it → folded ideas are marked, drop from the live block, and the skill re-embeds.
  - **Covers:** R19, R20, R21, R9.

- F4. **Skill embedding maintenance**
  - **Trigger:** any skill write — create, edit, fold, or seed.
  - **Steps:** regenerate the skill's embedding and update the index, eventually-consistent with the write.
  - **Covers:** R8, R9, R10, R11.

## Requirements

**Extraction**
- R1. Ideas are generated automatically in the background from auto-generated session topics, with no human in the extraction loop.
- R2. Extraction advances a per-session "reviewed" watermark; "reviewed" means the miner has processed up to that point — there is no manual per-message review surface.
- R3. Extraction keeps up with ongoing session activity (firing as topics are generated/updated, debounced at turn boundaries) and runs wrapper-side on the developer's own model.

**Association (topic → skill)**
- R4. Each topic is associated to a skill in two stages: embedding vector search returns the top-k most similar skills, then an LLM judge selects the single best from those k or rejects all.
- R5. Association is by catalog-wide similarity, independent of which skills were active in the session — a strong match is captured even for a skill the session never used.
- R6. A topic the judge cannot confidently tie to any candidate skill is routed to an org-level unassigned bin.
- R7. The unassigned bin is the backlog for new skills; recurring unassigned topics indicate a missing skill.

**Skill embedding index**
- R8. Every skill carries an embedding built from its description and body.
- R9. The skill embedding is regenerated on every skill mutation: create, edit, fold, and seed.
- R10. The embedding is a derived index kept in sync with the canonical skill; re-indexing may lag the write (eventually consistent).
- R11. Seeding all orgs regenerates embeddings so freshly-seeded skills are immediately matchable.

**Idea store & corroboration**
- R12. An idea belongs to one skill and records the set of distinct sessions that produced it; corroboration is the size of that set.
- R13. A new finding that matches an existing idea on the same skill (by semantic similarity) merges into it rather than creating a duplicate; re-emission from a session already in the set is a no-op.
- R14. An idea is corroborated at K or more distinct sessions, with K defaulting to 2 and tunable.
- R15. Every idea carries durable evidence — its source session, event sequence position, and a snippet — because full transcripts are not retained server-side.

**Surfacing**
- R16. When a skill loads in a working session, only corroborated ideas surface, read-only, in a fenced "candidate learnings" block in the materialized skill; the stored skill body is never modified by surfacing.
- R17. Uncorroborated ideas never enter a working session's context; they are visible only to humans in Command HQ and `/skill-idea-iterate`.
- R18. The Command HQ full-screen skill view shows a dropdown listing all of that skill's ideas — corroborated, uncorroborated, and folded history.

**Folding (human-in-the-loop)**
- R19. A new hq-skill, `/skill-idea-iterate`, lets a person review a skill's ideas and merge the best into the skill body.
- R20. Folding writes a new skill revision through the existing revision-then-promote machinery and a human promotes it; it never blind-overwrites the live skill.
- R21. A folded idea is marked folded, drops out of the live candidate-learnings block, and remains visible in Command HQ as folded history.

## Acceptance Examples

- AE1. **Corroboration gate. Covers R14, R16, R17.**
  - **Given** an idea seen in only one session, **when** a skill loads in a working session, **then** the idea is not injected, but it is visible in the HQ dropdown.
  - **When** the same lesson recurs in a second independent session, **then** the idea becomes corroborated and surfaces at the next skill-pull.

- AE2. **Homeless topic. Covers R6, R5.** **Given** a topic whose top-k candidates all fall below the judge's confidence bar, **then** it lands in the unassigned bin and is not attached to any skill that merely happened to be active.

- AE3. **Folded lifecycle. Covers R21.** **Given** an idea folded via `/skill-idea-iterate`, **then** it no longer appears in the live candidate-learnings block but still appears in Command HQ marked as folded.

- AE4. **Re-emit idempotency. Covers R13.** **Given** a session that produces the same finding twice, **then** the idea's corroboration count does not increase from that session.

- AE5. **Seed re-index. Covers R9, R11.** **Given** an `hq-*` skill edited, pushed, and re-seeded to all orgs, **then** each org's copy is re-embedded and matchable by new topics.

## Scope Boundaries

**Deferred for later**
- Auto-folding without a human, and golden-set regression checks at fold time — v1 keeps a person as the gate.
- Variant-scoped folding (merging into a specific repo or person's skill fork); v1 folds the org-level skill.

**Outside this feature's identity**
- A general per-message annotation or feedback system. Ideas are specifically *candidate amendments to a skill*, not freeform notes on sessions.

## Dependencies / Assumptions

- **Hard dependency — topic capture.** This feature consumes auto-generated session topics (`session.topic` / segments) from the 2026-06-06 topic-capture design, which is settled but not yet built (today only the first prompt is cached as a summary). Topic capture must land first.
- **Existing vector machinery is the starting point.** The repo has a dormant `SessionVector` cosine path and a `SearchStack` that is synth-only and never deployed. Whether v1 runs brute-force cosine over an org's skills or stands up a real search tier is a planning decision.
- Assumes the single-table `harness` model and org-scoping continue, and that the daemon keeps materializing the declared enabled skill set at load.

## Outstanding Questions

**Deferred to planning**
- Vector store choice: brute-force cosine over the org's skills vs. deploying the search tier, and where skill/idea vectors live.
- Embedding generation: synchronous with the skill write vs. asynchronous off the table's DynamoDB stream (leaning async, eventually consistent).
- Tuning: the corroboration threshold K, and the judge's confidence bar for "no match → unassigned."
- Exactly what is embedded (description only vs. description + body) and any chunking.
- Permissions for who may fold and promote (default: whoever can edit skills today).

## Sources / Research

- **Ideation lineage** (`docs/ideation/`, this session): the K-independent-sessions corroboration gate derives from gene-regulatory coincident-activation and pharmacovigilance disproportionality; insertion-time scoring from Generative Agents; corroborated-only/fenced surfacing from AKU progressive disclosure and findings that dumping low-signal notes degrades task success; human-gated fold from the curated +16.2pp vs. unverified −1.3pp result.
- **Claude Code skill selection is model-driven, not embedding-based** — all enabled skill `name`+`description` load into the system prompt and the model reasons over them; selection degrades past ~30–50 items. Embedding/BM25 retrieval exists only as the separate API-level Tool Search, not in skill selection. This is why the skill embedding index must be built here, not inherited.
- **Codebase breadcrumbs:** `packages/shared/src/dto.ts` (`skillSchema`, `learningRecordSchema`), `packages/backend/src/db/keys.ts` (skill / revision / true-pointer / session-vector keys), `packages/backend/src/db/repo.ts` (`putNewVersion`, `listSessionVectors`), `infra/lib/api-stack.ts` (the `harness` table, its DynamoDB stream, and GSI1), the wrapper-side Stop-hook miner, and `docs/2026-06-06-conversation-topic-capture-design.md`.
