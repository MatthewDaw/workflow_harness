# Code Research

**Type:** Cross-cutting research group (called by other groups)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Answer questions about the codebase. The atomic "what's actually true in this repo"
service that other agent groups lean on.

## Input

A question about the codebase. Questions can include:

- What is the normal/idiomatic way to do something here?
- Where is something done?
- Does this feature already kind of exist?
- Should we reuse existing code, or centralize it for this new feature?
- What would be the best location to put this new code?
- ...and so on.

## Output

- **Code line references** (`file:line`) backing the answer.
- A direct answer to the question.

## Sub-agents (to flesh out)

Some questions warrant their own specialized sub-agent. Candidates:

- **Convention finder** — "what's the normal way to do X here" (idioms, patterns).
- **Locator** — "where is X done" (entry points, call sites).
- **Prior-art / dedup finder** — "does this already exist" / "where's the overlap".
- **Reuse-vs-centralize advisor** — judgment call on reuse vs. extraction.
- **Placement advisor** — "best location for new code" (module boundaries, layering).

## Infrastructure (decided)

### Shared retrieval context across sub-agents
Lead agent owns a **run-scoped retrieval registry** keyed by `file_path`:
`{file_path: {content, file_sha, retrieved_at, read_by}}`. Every sub-agent checks the
registry before any file read and writes back on a miss — so the locator, convention
finder, etc. never re-read the same file. Sub-agents return lightweight references
(path + line range), **not** full file text, up to the lead (the Anthropic multi-agent
pattern — avoids token explosion). No blackboard or vector index needed until >5
sub-agents have overlapping retrieval domains; add a shared embedding index only for
freeform semantic "where is X done" queries on a large repo.

### Confidence & freshness signaling
**Citations are the confidence signal** — verbalized numeric confidence is unreliable
(LLMs are systematically overconfident). Every answer is structured output with a
mandatory citation array; a claim with no `file:line` citation is flagged unverified.

```json
{
  "answer": "...",
  "citations": [{"file": "src/auth.ts", "line_start": 42, "line_end": 51, "quote": "..."}],
  "freshness": {"run_git_sha": "abc1234", "files_read_at_sha": {"src/auth.ts": "def5678"}, "stale": false}
}
```
Pin `git rev-parse HEAD` at run start; store per-file content hashes at read time. If a
file changes mid-run, invalidate and re-read, and mark the answer `stale: true`. Optional
escalation for high-stakes answers: sample the query 3–5× and measure agreement across
returned citations (consistency-based confidence).

### Caching repeated lookups
Run-scoped cache keyed by **`(sha256(query_text), run_git_sha)`** for exact repeats
(two sub-agents asking the same thing). Within a run, **never invalidate on time** — only
on file-hash change. Add a semantic layer (cosine ≥ 0.92 on query embeddings) only if
profiling shows >20% of cross-agent queries are paraphrases. Cache the static system
prompt + repo-structure overview at the LLM-provider level; never cache tool outputs in
the prompt prefix. Don't cache answers that touched uncommitted/branch-specific state.

> Sources: Anthropic multi-agent research system; citation-grounded RAG; verbalized-
> confidence calibration (arXiv 2412.14737); semantic caching / prompt caching
> (arXiv 2601.06007).
