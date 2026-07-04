# Interaction Watcher

**Type:** Self-improvement — step 1 (observation substrate)

**Form:** A prompt command + its associated skills (see [What every agent *is*](../README.md#what-every-agent-is-definitional)). Each sub-agent below is itself a prompt command + skills.

## Purpose

Watch **all** Claude interactions across the org and turn them into a queryable stream
the rest of the self-improvement loop learns from. This is the CommandHQ observation layer.

## Input

- The raw firehose: Claude Code sessions, transcripts, tool calls + results, which
  prompt command / skill was invoked, outcomes (success/failure), errors, retries, user
  corrections, latency/cost, and any explicit user feedback (thumbs, edits, re-prompts).

## Output

- A normalized, queryable **interaction record** per session/turn, tagged with: which
  agent (prompt command + skills) ran, what it was asked, what it did, how it ended, and
  the signals that matter for learning (failures, corrections, abandonment, praise).
- Privacy-scrubbed and consent-respecting (see open questions).

## Sub-agents (to flesh out)

- **Collector** — ingests the telemetry firehose into a durable store.
- **Normalizer** — maps heterogeneous session formats to one interaction schema.
- **Tagger** — labels each record with agent identity + outcome + signal type.
- **Redactor** — strips secrets/PII before anything downstream reads it.

## Signals worth capturing (first cut)

- **Failure / error** — tool errors, test failures, the agent gave up.
- **Correction** — user edited the agent's output, re-prompted, or undid it.
- **Friction** — many turns to do a small thing; repeated clarifying questions.
- **Win** — clean one-shot success, explicit praise, fast completion.
- **Drift** — agent ignored a convention / repeated a known-bad pattern.

## Decisions

### Consent & privacy
**Org-wide opt-in by admin policy, with per-repo/per-user opt-out**, consent recorded
with timestamp + policy version. Redact **before storage, not at query time**: run a
self-hosted **OpenTelemetry Collector as a redaction gateway** that drops/hashes raw
prompt+completion content for non-sampled traces and keeps only metadata (token counts,
latency, scores, outcome). Per OTel GenAI conventions, store any retained content as span
*events* (filterable/droppable at the Collector), never as indexed span *attributes*.
Sign DPAs with every vendor; prefer self-hosted backends (Langfuse / Phoenix) for
regulated data. Map each field to a purpose + retention clock (GDPR Art. 5); deletion
must cascade across raw logs, embeddings, and eval datasets (Art. 17).

### Retention & sampling
**Tail-based sampling** (decide after the trace completes, so outcome is known):
- **100%** of errors, tool failures, low eval scores, user thumbs-down, budget overruns.
- **~10%** random sample of clean successes.
- Tiers: **hot 7d** (full content) → **warm 90d** (metadata + eval scores) → **cold ~2y**
  (aggregates only). Dev/staging capture 100%. Never head-sample only — you'd miss silent
  failures, which are the most valuable learning signal.

### Stream vs. batch
**Batched roll-up for the learning pipeline.** A self-improvement loop doesn't need
sub-minute latency (prompt changes take human-review + deploy cycles anyway). Export via
the OTel batch processor (flush ~30s/100 spans); run insight-mining on a **daily schedule
or per-deploy trigger**. Keep only a lightweight real-time *counter* stream (error rate,
cost) for alerting — no content.

### Version attribution
A record must pin to the **exact prompt-command + skill version** that produced it, or a
fix can't target the right artifact. Use a **prompt/skill registry** (self-hosted Langfuse
or LangSmith) with **git as ground truth** (templates in `prompts/…`, commit SHA = canonical
version). On every root span, stamp custom attributes: `prompt_name`, `prompt_version`,
`prompt_commit_sha`, `skill_name`, `skill_version`, `model_id`, `model_config_hash`. The
`(prompt_name, version)` pair is the join key the rest of the loop queries on.

> Sources: OTel GenAI semantic conventions; LLM-observability practice (Langfuse / LangSmith
> / Phoenix / Helicone); GDPR Art. 5/17 + SOC 2; tail-sampling guidance.
