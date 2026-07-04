# Registry Health — Skill Search & Agent Disambiguation

**Type:** Self-improvement — structural healing (the catalog side of self-healing)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)).

## Purpose

Self-healing has two halves. The rest of this folder heals **content** (skills learn
from interactions). This component heals **structure**: the catalog of agents and skills
itself stays deduplicated, indexable, right-sized, and searchable — so the organization
can grow to *a truly ton of agents* without ever overloading context.

## The four disciplines

### 1. Skills never repeat themselves (dedup)

One concern, one home. Before any skill is created or edited (by a human or by this
loop), a nearest-neighbor check runs against the skill index — same thresholds as the
[insight ledger](./insight-extractor.md):

- cosine **≥ 0.90** → duplicate: route the addition into the *existing* skill;
- **0.85–0.90** → overlap: merge/extend decision (consolidate or sharpen the boundary);
- **< 0.85** → genuinely new: register it.

### 2. Everything is indexable (clear ingestion targets)

Every agent and skill carries structured frontmatter: `name`, `family`, one-line
`description`, capability tags, and an embedding over (description + headings). The
index is the **routing table for learning**: when an ingestion lands, *"which skill does
this belong to?"* is a top-k query, not a guess. Same for humans adding ideas.

### 3. Auto-split when too large

Size is a registry invariant, enforced like a lint rule:

- **Skill too large** (≳ size threshold, or its sections cluster into >1 distinct
  concern) → split into child skills with cross-links; the parent becomes a thin router
  or is retired.
- **Agent too large** (its description can't stay one honest line, or its skill set
  outgrows one coherent job) → split into sub-agents within its family.

Splits run through the [skill-editor](./skill-editor.md) under its normal gates
(validated, versioned, canaried). Precedent: Voyager-style skill libraries grow by small
verified units; our spec rules already demand one-concern units — this applies the same
rule to the factory's own parts.

### 4. Top-k retrieval — never full-catalog loading

**Nobody ever loads the whole registry into context.** The orchestrator (and any agent
needing a capability) queries the vector index and receives the **top-k candidates
(k ≈ 5–10)**; only those definitions load. Context cost stays **O(k) while the catalog
grows O(n)** — thousands of agents, constant context footprint.

Proven pattern, not a bet: Claude Code itself ships deferred tools + ToolSearch (schemas
load on demand from a name-only list); tool-retrieval / RAG-over-tools is the standard
answer to large catalogs.

## Agent disambiguation

When a task could plausibly go to more than one agent:

1. **Index ranking** — top-k similarity between task and agent descriptions;
2. **Family ownership** — taxonomy breaks most ties (verify-family never takes build work);
3. **Cheap router call** — a small/fast model picks among survivors;
4. **Still ambiguous** → ask the user one clarifying question rather than guess — and
   log the collision as a **registry smell**: two agents whose descriptions collide is
   itself a dedup/split insight for this loop.

## Relationship to the rest of the factory

- **Orchestrator** ([supervisor](../groups/supervisor.md)) resolves every dispatch
  through the index — it composes agents it *retrieved*, not agents it memorized.
- **Content-healing** ([insight-extractor](./insight-extractor.md) →
  [skill-editor](./skill-editor.md)) uses the index to aim every ingestion; the
  dedup/split disciplines keep those edits from bloating or duplicating skills.
- **Versioning** rides the decided substrate: git as ground truth, content-addressable
  hashes, label-pointer rollback ([skill-editor](./skill-editor.md)).

## Open questions

- Embedding refresh policy on skill edits (re-embed per merge vs. batched).
- k tuning per call site (orchestrator routing vs. ingestion targeting).
- Can the family taxonomy grow new families automatically, or only by human decision?
