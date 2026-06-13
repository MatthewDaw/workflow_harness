"""SQLite store: schema, transactions, snapshots, promotion queue.

The complete Phase 0 data model (DESIGN §4, §12.1): insights are atomic rows that
are never dissolved; skills are ordered membership views; every active-set mutation
flows through the single-writer promotion queue, which is the only minter of
snapshots (R3/R4). Traceability tables (FEAT/MSG/REQ/TKT/AC/SPAN/CHK/SCEN) are
created and constrained here but unused until Phase 1+ (R2).

Phase 1 (Plan 002 U1, R20) adds the pipeline tables via migration v2: runs,
ticket status + audit trail, span lifecycle/cost columns (episode/increment
stay NULLable and unwritten until Phase 2), typed failure records, shadow
tripwire events, and the append-only ticket ledger. The orchestrator is the
sole writer of all of them (002 R6) — agents never see this database.

Phase 2 (Plan 003 U1) adds the episode layer via migration v3: episodes above
runs (an increment is exactly one Phase 1 run — 003 R1), run acceptance (R2),
the FEAT identity discipline (digest stamp, append-only triggers, `deprecated`
status — R9), the frontier ledger (R10), scenario manifests (R16 storage),
the Q&A log + human review queue (R12/R13 accounting), settlement reports
(R23 storage), SCEN episode/snapshot keys + judge columns (R22), and idea
provenance columns (R27). Backfill-safe over Phase 0/1 databases: every new
column is NULLable-or-defaulted and the SCEN key requirement is enforced by
an INSERT trigger so pre-existing rows survive untouched.

Phase 3a (Plan 004 U1) adds the learning-loop spine via migration v4: the
run-mode taxonomy (`training | trial | benchmark`) and nullable `epoch` on
episodes; the append-only `fitness_events` log (state-at-snapshot reconstructible
by COUNT over events with ``snapshot_id <= S`` in the queried channel, no
snapshot minted on writes); the episode-scoped `workflows` run-memory table
(rows die at settlement); `batch_validations` records (batch ↔ trial/benchmark
episode refs ↔ verdict); and the lineage seam columns (agents.routing_decisions /
lineage_status, skills.parent_skill_id / split_snapshot_id) the self-reorganization
lands on. Backfill-safe over Phase 0/1/2 databases.

Plan 008 U1 (R3 ingest gauntlet) adds the R3 schema spine via migration v6:
the deferred-supersede + altitude-audit columns on insights (negative_scope,
valid_at, invalid_at, rationale) with the provenance enum widened to include
`consolidated`; the module hierarchy `level` on skills with `name` relaxed to
nullable (lazy naming); the `corroborate` fitness-event kind (a snapshot-keyed
duplicate vote); and the typed `insight_edges` graph (corroborates / refines /
contradicts / generalizes_from). Three rename-copy-drop rebuilds carry the CHECK
widenings; nothing is dropped (demote, never drop — reversibility). Backfill-safe
over a Phase 0–007 database.

Transaction discipline: the connection runs in manual-commit mode; writers compose
inside :meth:`Store.transaction` (``BEGIN IMMEDIATE`` + busy-timeout backstop, R4)
so U5 can commit one atomic registration. Multi-statement mutators refuse to run
outside a transaction.

State-at-snapshot rule (R3): status at snapshot S = the latest status transition
with ``snapshot_id <= S``; if none, the insight's initial status (the first
transition's ``from_status``, else the current row status — registration never
mints snapshots, so a transition-free insight still carries its birth status).
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("quarantined", "active", "dormant", "retired")
DEFAULT_DB_FILENAME = "library.db"
DEFAULT_BUSY_TIMEOUT_MS = 5000

# --- Phase 1 (Plan 002) state vocabularies -----------------------------------

# Run state machine (002 R1): created → planning → executing → terminal.
RUN_STATUSES = (
    "created", "planning", "executing",
    "success", "partial", "plan_failed", "aborted_quota", "aborted_error",
)
RUN_TERMINAL_STATUSES = (
    "success", "partial", "plan_failed", "aborted_quota", "aborted_error",
)

# Ticket lifecycle (002 R2).
TICKET_STATUSES = ("pending", "in_progress", "done", "escalated", "blocked")

# Span lifecycle (002 R3/R16): inserted `running` at spawn, finalized on exit;
# orphans found at resume are marked `aborted`.
SPAN_STATUSES = ("running", "completed", "error", "timeout", "aborted")
SPAN_FINAL_STATUSES = ("completed", "error", "timeout", "aborted")

# Typed failure kinds (002 R13): Phase 1 producers (gate, plan lints, verifier,
# structured-output contract, timeouts, infra-charged retries) plus the MAST
# taxonomy kinds (DESIGN §7) the tripwire escalations emit.
FAILURE_KINDS = (
    "gate_typecheck", "gate_lint", "gate_test",
    "plan_lint",
    "verifier_check",
    "contract_violation",
    "timeout",
    "infra",
    "step_repetition", "reasoning_action_mismatch",
    "termination_unaware", "incorrect_verification",
)

# --- Phase 2 (Plan 003) state vocabularies -----------------------------------

# Episode state machine (003 R3): three terminals plus the resumable,
# non-terminal `suspended` (mid-increment quota exhaustion checkpoints the run
# and suspends the episode — it never counts as budget_spent).
EPISODE_STATUSES = (
    "created", "running", "suspended",
    "frontier_exhausted", "budget_spent", "aborted_error",
)
EPISODE_TERMINAL_STATUSES = ("frontier_exhausted", "budget_spent", "aborted_error")

# Explorer UAT verdict on a run's delivered subset (003 R2); NULL until UAT.
RUN_ACCEPTANCE = ("accepted", "rejected")

# FEAT registry rows exist only once runtime-confirmed ("source proposes,
# runtime confirms" — 003 R8); removed features deprecate, never delete (R9).
FEAT_STATUSES = ("confirmed", "deprecated")

# Frontier exploration states (003 R10).
FRONTIER_STATUSES = (
    "unexplored", "partially-explored", "explored", "newly-discovered",
)

# Scenario tolerance tiers (003 R16/R20).
SCENARIO_TIERS = ("must", "should", "free")

# Judge modes recorded on settled SCEN rows (003 R20/R22).
SCEN_JUDGE_MODES = ("deterministic", "single", "panel")

# Typed Q&A outcomes and checker verdicts (003 R12/R13).
QA_OUTCOMES = ("answered", "answer_unavailable", "budget_exhausted")
QA_CHECKER_VERDICTS = ("pass", "fail")

# --- Phase 3a (Plan 004) state vocabularies ----------------------------------

# Run-mode taxonomy (004 R1): the spine column gating quarantine visibility,
# fitness-channel routing, and SPC eligibility. An episode is exactly one mode;
# an increment (= one run) inherits its episode's mode, and standalone Phase 1
# runs are implicitly `training`.
RUN_MODES = ("training", "trial", "benchmark")

# Fitness-event kinds (004 R1/R19; 008 R4): retrieval = insight rendered into a
# prompt; win = the session's ticket reaches done AND is unimplicated; loss =
# causal blame only; corroborate = a snapshot-keyed append-only vote that a
# distinct insight restated this rule (008 R4/R12 — NOT a mutable insights
# counter, so it serves the §11 cross-target-recurrence signal). The append-only
# log is the substrate; insight counters are derived.
FITNESS_EVENT_KINDS = ("retrieval", "win", "loss", "corroborate")

# Run-memory workflow lifecycle (004 R1/R5/R6): a workflow row lives within its
# episode and dies at settlement (status flips live → dead).
WORKFLOW_STATUSES = ("live", "dead")

# Batch-validation verdicts (004 R1/R15-R17).
BATCH_VERDICTS = ("promote", "revert")

# --- Plan 007 (greenfield mode) state vocabularies ---------------------------

# DEC registry rows follow the FEAT identity discipline (007 KTD1): confirmed on
# runtime, deprecated never deleted.
DEC_STATUSES = ("confirmed", "deprecated")

# Assumption-ledger rows (007 KTD2): the planner's typed assumptions, promoted
# from plan-document strings to queryable rows keyed by run_id.
ASSUME_STATUSES = ("open", "confirmed", "invalidated")
ASSUME_RISKS = ("low", "med", "high")

# The episode `world` axis (007 KTD6) — orthogonal to `mode`. `greenfield_pure`
# is reserved (unused in this plan); default is `brownfield`.
WORLDS = ("brownfield", "greenfield_backtranslated", "greenfield_pure")

# Insight provenance (007 KTD5; 008 R2): manual hand-entry, reflector-mined,
# researched via the induction door, hand-seeded, or `consolidated` (the R3
# derive pass's batch writer — plan 009; the value is enum-legal at v6 so the
# widened CHECK ships with the ingest gauntlet). Backfilled from batch labels.
INSIGHT_PROVENANCES = ("manual", "reflector", "researched", "seeded", "consolidated")

# Batch validation-class (007 KTD5): the tag validate.py's substrate routing
# reads — code → frozen benchmark, elicitation → both, general → both.
VALIDATION_CLASSES = ("code", "elicitation", "general")

# Founder-model knowledge rows (007 KTD3): each registry ref the founder either
# knows plainly, knows vaguely (cached blur), or has never thought about.
FOUNDER_REF_KINDS = ("feat", "dec")
FOUNDER_KNOWLEDGE_STATES = ("intact", "blurred", "dropped")

# --- Plan 008 (R3 ingest gauntlet) state vocabularies ------------------------

# Typed semantic edges between insights (008 R3): the gauntlet writes
# corroborates / refines / contradicts at ingest, generalizes_from at
# consolidation (plan 009). `similarity` is enum-legal for forward-compat but is
# NEVER written at v1 — the KNN graph is recomputed from sqlite-vec each derive
# pass, not materialized (DESIGN §6). Append-only for the four semantic kinds.
INSIGHT_EDGE_KINDS = (
    "similarity", "corroborates", "refines", "contradicts", "generalizes_from",
)

_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in STATUSES)
_RUN_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in RUN_STATUSES)
_TICKET_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in TICKET_STATUSES)
_SPAN_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in SPAN_STATUSES)
_FAILURE_KIND_SQL_ENUM = ", ".join(f"'{k}'" for k in FAILURE_KINDS)
_EPISODE_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in EPISODE_STATUSES)
_RUN_ACCEPTANCE_SQL_ENUM = ", ".join(f"'{s}'" for s in RUN_ACCEPTANCE)
_FEAT_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in FEAT_STATUSES)
_FRONTIER_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in FRONTIER_STATUSES)
_SCENARIO_TIER_SQL_ENUM = ", ".join(f"'{s}'" for s in SCENARIO_TIERS)
_SCEN_JUDGE_MODE_SQL_ENUM = ", ".join(f"'{s}'" for s in SCEN_JUDGE_MODES)
_QA_OUTCOME_SQL_ENUM = ", ".join(f"'{s}'" for s in QA_OUTCOMES)
_QA_CHECKER_SQL_ENUM = ", ".join(f"'{s}'" for s in QA_CHECKER_VERDICTS)
_RUN_MODE_SQL_ENUM = ", ".join(f"'{s}'" for s in RUN_MODES)
_FITNESS_KIND_SQL_ENUM = ", ".join(f"'{s}'" for s in FITNESS_EVENT_KINDS)
_WORKFLOW_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in WORKFLOW_STATUSES)
_BATCH_VERDICT_SQL_ENUM = ", ".join(f"'{s}'" for s in BATCH_VERDICTS)
_DEC_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in DEC_STATUSES)
_ASSUME_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in ASSUME_STATUSES)
_ASSUME_RISK_SQL_ENUM = ", ".join(f"'{s}'" for s in ASSUME_RISKS)
_WORLD_SQL_ENUM = ", ".join(f"'{s}'" for s in WORLDS)
_INSIGHT_PROVENANCE_SQL_ENUM = ", ".join(f"'{s}'" for s in INSIGHT_PROVENANCES)
_VALIDATION_CLASS_SQL_ENUM = ", ".join(f"'{s}'" for s in VALIDATION_CLASSES)
_FOUNDER_REF_KIND_SQL_ENUM = ", ".join(f"'{s}'" for s in FOUNDER_REF_KINDS)
_FOUNDER_STATE_SQL_ENUM = ", ".join(f"'{s}'" for s in FOUNDER_KNOWLEDGE_STATES)
_INSIGHT_EDGE_KIND_SQL_ENUM = ", ".join(f"'{k}'" for k in INSIGHT_EDGE_KINDS)


class StoreError(Exception):
    """Raised on store misuse or integrity problems with an actionable message."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- schema migrations -------------------------------------------------------
# Append-only list of (version, ddl). Later phases (Plans 002+) append entries;
# never edit a shipped migration.

_SCHEMA_V1 = f"""
CREATE TABLE families (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,
    charter       TEXT NOT NULL DEFAULT '',
    router_prompt TEXT NOT NULL DEFAULT ''
);

CREATE TABLE agents (
    id                    INTEGER PRIMARY KEY,
    family_id             INTEGER NOT NULL REFERENCES families(id),
    parent_id             INTEGER REFERENCES agents(id),
    name                  TEXT NOT NULL,
    description           TEXT NOT NULL DEFAULT '',
    base_prompt_specialty TEXT NOT NULL DEFAULT '',
    permissions           TEXT NOT NULL DEFAULT '{{}}',
    active_cap            INTEGER NOT NULL DEFAULT 50 CHECK (active_cap > 0),
    UNIQUE (family_id, name)
);

CREATE TABLE batches (
    id         INTEGER PRIMARY KEY,
    label      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- Logical version rows, minted ONLY by promotion-queue operations (R3).
CREATE TABLE snapshots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_id        INTEGER REFERENCES snapshots(id),
    created_at       TEXT NOT NULL,
    mutation_summary TEXT NOT NULL
);

-- Audit trail of the single-writer promotion queue (R4).
CREATE TABLE promotion_queue (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    operation   TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id),
    created_at  TEXT NOT NULL
);

CREATE TABLE insights (
    id               INTEGER PRIMARY KEY,
    precondition     TEXT NOT NULL,
    action           TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    scope_tag        TEXT,
    content_hash     TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'quarantined'
                     CHECK (status IN ({_STATUS_SQL_ENUM})),
    batch_id         INTEGER REFERENCES batches(id),
    source_run_id    TEXT,
    embedding_model  TEXT,
    embedding_dim    INTEGER,
    duplicate_of     INTEGER REFERENCES insights(id),
    supersedes       INTEGER REFERENCES insights(id),
    retrievals       INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    causal_blames    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE skills (
    id               INTEGER PRIMARY KEY,
    agent_id         INTEGER NOT NULL REFERENCES agents(id),
    name             TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    created_batch_id INTEGER REFERENCES batches(id),
    token_count      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (agent_id, name)
);

-- Ordered membership: position is append order within a skill (R1, R15).
CREATE TABLE skill_members (
    skill_id   INTEGER NOT NULL REFERENCES skills(id),
    insight_id INTEGER NOT NULL REFERENCES insights(id),
    position   INTEGER NOT NULL,
    PRIMARY KEY (skill_id, insight_id),
    UNIQUE (skill_id, position)
);

-- Written by every queue operation; the substrate of state-at-snapshot (R3).
CREATE TABLE status_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id  INTEGER NOT NULL REFERENCES insights(id),
    from_status TEXT NOT NULL CHECK (from_status IN ({_STATUS_SQL_ENUM})),
    to_status   TEXT NOT NULL CHECK (to_status IN ({_STATUS_SQL_ENUM})),
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id)
);
CREATE INDEX idx_status_transitions_insight
    ON status_transitions(insight_id, snapshot_id);

CREATE TABLE contradictions (
    id                 INTEGER PRIMARY KEY,
    challenger_id      INTEGER NOT NULL REFERENCES insights(id),
    incumbent_id       INTEGER NOT NULL REFERENCES insights(id),
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'closed')),
    opened_at          TEXT NOT NULL,
    closed_snapshot_id INTEGER REFERENCES snapshots(id)
);

-- Discarded merge outcomes; consulted by the add_idea content-hash fast path (R5/R8).
CREATE TABLE merge_log (
    id                     INTEGER PRIMARY KEY,
    content_hash           TEXT NOT NULL,
    structural_fields_json TEXT NOT NULL,
    duplicate_of           INTEGER NOT NULL REFERENCES insights(id),
    batch_id               INTEGER REFERENCES batches(id),
    judged_at              TEXT NOT NULL
);
CREATE INDEX idx_merge_log_hash ON merge_log(content_hash);

-- Store-level pins and bookkeeping (embedding model/dim assertions, R22).
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Traceability (DESIGN §12.1): schema-only in Phase 0, populated by Phase 1+ (R2).
CREATE TABLE trace_feat (
    id           TEXT PRIMARY KEY CHECK (id LIKE 'FEAT-%'),
    evidence_ref TEXT NOT NULL
);

CREATE TABLE trace_msg (
    id      TEXT PRIMARY KEY CHECK (id LIKE 'MSG-%'),
    content TEXT NOT NULL DEFAULT ''
);

CREATE TABLE trace_msg_mentions (
    msg_id  TEXT NOT NULL REFERENCES trace_msg(id),
    feat_id TEXT NOT NULL REFERENCES trace_feat(id),
    PRIMARY KEY (msg_id, feat_id)
);

CREATE TABLE trace_req (
    id            TEXT PRIMARY KEY CHECK (id LIKE 'REQ-%'),
    source_msg_id TEXT NOT NULL REFERENCES trace_msg(id)
);

CREATE TABLE trace_tkt (
    id           TEXT PRIMARY KEY CHECK (id LIKE 'TKT-%'),
    increment_id TEXT
);

CREATE TABLE trace_tkt_covers (
    tkt_id TEXT NOT NULL REFERENCES trace_tkt(id),
    req_id TEXT NOT NULL REFERENCES trace_req(id),
    PRIMARY KEY (tkt_id, req_id)
);

CREATE TABLE trace_ac (
    id        TEXT PRIMARY KEY CHECK (id LIKE 'AC-%'),
    ticket_id TEXT NOT NULL REFERENCES trace_tkt(id),
    req_id    TEXT NOT NULL REFERENCES trace_req(id)
);

CREATE TABLE trace_span (
    id         TEXT PRIMARY KEY CHECK (id LIKE 'SPAN-%'),
    ticket_id  TEXT NOT NULL REFERENCES trace_tkt(id),
    files_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_trace_span_ticket ON trace_span(ticket_id);

CREATE TABLE trace_chk (
    id            TEXT PRIMARY KEY CHECK (id LIKE 'CHK-%'),
    ac_id         TEXT NOT NULL REFERENCES trace_ac(id),
    result        TEXT NOT NULL,
    repro_command TEXT NOT NULL,
    evidence      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE trace_scen (
    id       TEXT PRIMARY KEY CHECK (id LIKE 'SCEN-%'),
    feat_id  TEXT NOT NULL REFERENCES trace_feat(id),
    result   TEXT,
    evidence TEXT NOT NULL DEFAULT ''
);
"""

# Phase 1 (Plan 002 U1, R20): pipeline tables — runs, ticket status + audit,
# span lifecycle/cost columns, typed failure records, shadow-tripwire events,
# ledger refs. trace_msg_mentions already permits MSG rows with zero mention
# rows (join table; no relax needed) — asserted in tests, not re-constrained.
_SCHEMA_V2 = f"""
-- Run rows (R1): one per invocation, keyed by toy-spec ref and the library
-- snapshot in force (snapshot_id 0 = before any active-set mutation, so no FK).
-- Totals columns are written at settlement (R16); cost_partial_spans counts
-- spans whose cost fields were accumulated best-effort from a killed stream.
CREATE TABLE runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_ref            TEXT NOT NULL,
    snapshot_id         INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'created'
                        CHECK (status IN ({_RUN_STATUS_SQL_ENUM})),
    total_cost_usd      REAL,
    total_input_tokens  INTEGER,
    total_output_tokens INTEGER,
    total_turns         INTEGER,
    total_duration_ms   INTEGER,
    cost_partial_spans  INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL
);

-- Ticket lifecycle (R2) with an audit trail mirroring status_transitions.
ALTER TABLE trace_tkt ADD COLUMN status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ({_TICKET_STATUS_SQL_ENUM}));

CREATE TABLE ticket_status_transitions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   TEXT NOT NULL REFERENCES trace_tkt(id),
    from_status TEXT NOT NULL CHECK (from_status IN ({_TICKET_STATUS_SQL_ENUM})),
    to_status   TEXT NOT NULL CHECK (to_status IN ({_TICKET_STATUS_SQL_ENUM})),
    run_id      INTEGER REFERENCES runs(id),
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_ticket_status_transitions_ticket
    ON ticket_status_transitions(ticket_id);

-- Span rebuild (R16): the Phase 0 shape lacked lifecycle/cost columns and
-- required a ticket (judge and planner spans have none). SQLite cannot drop
-- NOT NULL in place, so rename-copy-drop; existing rows (pre-lifecycle) are
-- backfilled 'completed' so resume's orphan query never flags them.
ALTER TABLE trace_span RENAME TO trace_span_phase0;
DROP INDEX idx_trace_span_ticket;
CREATE TABLE trace_span (
    id                 TEXT PRIMARY KEY CHECK (id LIKE 'SPAN-%'),
    run_id             INTEGER REFERENCES runs(id),
    family             TEXT,
    agent              TEXT,
    ticket_id          TEXT REFERENCES trace_tkt(id),
    ralph_iteration    INTEGER,
    parent_span        TEXT REFERENCES trace_span(id),
    status             TEXT NOT NULL DEFAULT 'running'
                       CHECK (status IN ({_SPAN_STATUS_SQL_ENUM})),
    model_version      TEXT,
    prompt_set_version TEXT,
    files_json         TEXT NOT NULL DEFAULT '[]',
    artifact_refs_json TEXT NOT NULL DEFAULT '[]',
    episode            TEXT,    -- Phase 2 carve-out: NULLable, unwritten in Phase 1
    increment_id       TEXT,    -- Phase 2 carve-out: NULLable, unwritten in Phase 1
    num_turns          INTEGER,
    duration_ms        INTEGER,
    cost_usd           REAL,
    input_tokens       INTEGER,
    output_tokens      INTEGER,
    cost_partial       INTEGER NOT NULL DEFAULT 0 CHECK (cost_partial IN (0, 1))
);
INSERT INTO trace_span (id, ticket_id, files_json, status)
    SELECT id, ticket_id, files_json, 'completed' FROM trace_span_phase0;
DROP TABLE trace_span_phase0;
CREATE INDEX idx_trace_span_ticket ON trace_span(ticket_id);
CREATE INDEX idx_trace_span_run ON trace_span(run_id);
CREATE INDEX idx_trace_span_status ON trace_span(status);

-- Typed failure records (R13, DESIGN §7 schema).
CREATE TABLE failure_records (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER REFERENCES runs(id),
    ticket_id     TEXT REFERENCES trace_tkt(id),
    span_id       TEXT REFERENCES trace_span(id),
    failure_kind  TEXT NOT NULL CHECK (failure_kind IN ({_FAILURE_KIND_SQL_ENUM})),
    location      TEXT NOT NULL DEFAULT '',
    expected      TEXT NOT NULL DEFAULT '',
    observed      TEXT NOT NULL DEFAULT '',
    repro_command TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_failure_records_ticket ON failure_records(ticket_id);

-- Shadow-mode tripwire dataset (R17): embedder model+dim recorded per event
-- (thresholds are not portable across embedders); failure_set_hash carries the
-- no-progress detector's canonical failure-set hash. Nothing reads these to
-- kill anything in Phase 1.
CREATE TABLE tripwire_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    span_id          TEXT REFERENCES trace_span(id),
    run_id           INTEGER REFERENCES runs(id),
    ralph_iteration  INTEGER,
    detector_kind    TEXT NOT NULL,
    similarity       REAL,
    would_have_fired INTEGER NOT NULL CHECK (would_have_fired IN (0, 1)),
    embedding_model  TEXT,
    embedding_dim    INTEGER,
    failure_set_hash TEXT,
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_tripwire_events_run ON tripwire_events(run_id);

-- Append-only ticket ledger (R11): orchestrator-written entries referencing
-- spans, CHK rows, and failure records; rendered read-only to the worker.
CREATE TABLE ledger_entries (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id         TEXT NOT NULL REFERENCES trace_tkt(id),
    run_id            INTEGER REFERENCES runs(id),
    ralph_iteration   INTEGER,
    entry_kind        TEXT NOT NULL,
    span_id           TEXT REFERENCES trace_span(id),
    chk_id            TEXT REFERENCES trace_chk(id),
    failure_record_id INTEGER REFERENCES failure_records(id),
    content           TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_ledger_entries_ticket ON ledger_entries(ticket_id);
"""

# Phase 2 (Plan 003 U1): episode layer + grading-side tables. Backfill-safe:
# new columns are NULLable or defaulted; constraints that must not reject
# pre-existing rows (SCEN keys) are INSERT triggers, not rebuilds.
_SCHEMA_V3 = f"""
-- Episodes (003 R1/R3/R4): one target × one library snapshot × one fresh
-- workspace × one settlement, sitting above Phase 1 runs. snapshot_id 0 =
-- before any active-set mutation (no FK — same convention as runs). Budget
-- fields are stamped at creation from thresholds config and checked at
-- increment boundaries (R4); settled_at is written by settlement (003 U7).
CREATE TABLE episodes (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target           TEXT NOT NULL,
    digest           TEXT NOT NULL,
    snapshot_id      INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'created'
                     CHECK (status IN ({_EPISODE_STATUS_SQL_ENUM})),
    max_increments   INTEGER,
    cost_ceiling_usd REAL,
    created_at       TEXT NOT NULL,
    settled_at       TEXT
);

-- An increment is exactly one Phase 1 run (003 R1). acceptance is the explorer
-- UAT verdict on the delivered subset (R2): NULL until the acceptance stage runs.
ALTER TABLE runs ADD COLUMN episode_id INTEGER REFERENCES episodes(id);
ALTER TABLE runs ADD COLUMN increment_index INTEGER;
ALTER TABLE runs ADD COLUMN acceptance TEXT
    CHECK (acceptance IN ({_RUN_ACCEPTANCE_SQL_ENUM}));
CREATE INDEX idx_runs_episode ON runs(episode_id);

-- FEAT identity discipline (003 R8/R9): rows exist only once runtime-confirmed,
-- stamped with the target and its image digest; IDs are append-only and never
-- reused; removed features flip to 'deprecated', never delete. Pre-Phase-2
-- rows backfill 'confirmed' with NULL target/digest (no image pin existed yet).
ALTER TABLE trace_feat ADD COLUMN target TEXT;
ALTER TABLE trace_feat ADD COLUMN digest TEXT;
ALTER TABLE trace_feat ADD COLUMN status TEXT NOT NULL DEFAULT 'confirmed'
    CHECK (status IN ({_FEAT_STATUS_SQL_ENUM}));

CREATE TRIGGER trg_trace_feat_id_immutable
BEFORE UPDATE OF id ON trace_feat
BEGIN
    SELECT RAISE(ABORT,
        'FEAT ids are append-only: never renumbered or reused (003 R9)');
END;

CREATE TRIGGER trg_trace_feat_no_delete
BEFORE DELETE ON trace_feat
BEGIN
    SELECT RAISE(ABORT,
        'FEAT rows are never deleted: flip status to deprecated (003 R9)');
END;

-- Frontier ledger (003 R10): per-FEAT exploration status driving increment
-- requests (least-investigated first); force_scheduled marks rows the
-- mention-coverage audit pushes ahead of the normal ordering.
CREATE TABLE frontier (
    feat_id             TEXT PRIMARY KEY REFERENCES trace_feat(id),
    status              TEXT NOT NULL DEFAULT 'unexplored'
                        CHECK (status IN ({_FRONTIER_STATUS_SQL_ENUM})),
    investigation_count INTEGER NOT NULL DEFAULT 0,
    force_scheduled     INTEGER NOT NULL DEFAULT 0
                        CHECK (force_scheduled IN (0, 1))
);

-- Scenario manifests (003 R16): authored at FEAT-mint time; archived (never
-- deleted) when their FEAT deprecates (R9).
CREATE TABLE scenario_manifests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    feat_id       TEXT NOT NULL REFERENCES trace_feat(id),
    tier          TEXT NOT NULL CHECK (tier IN ({_SCENARIO_TIER_SQL_ENUM})),
    manifest_json TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'archived')),
    created_at    TEXT NOT NULL
);
CREATE INDEX idx_scenario_manifests_feat ON scenario_manifests(feat_id);

-- Q&A log (003 R12/R13): one row per question slot. Checker retries are
-- grader-side and live inside the row (retries counter — they consume no
-- budget); budget_counted is the accounting bit the elicitation-efficiency
-- metric and the per-increment cap read (budget-free rows: refunded
-- answer_unavailable slots; UAT feedback never lands here).
CREATE TABLE qa_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id         INTEGER NOT NULL REFERENCES episodes(id),
    run_id             INTEGER REFERENCES runs(id),
    question           TEXT NOT NULL,
    question_msg_id    TEXT REFERENCES trace_msg(id),
    answer             TEXT,
    answer_msg_id      TEXT REFERENCES trace_msg(id),
    checker_verdict    TEXT CHECK (checker_verdict IN ({_QA_CHECKER_SQL_ENUM})),
    contradiction_json TEXT,
    retries            INTEGER NOT NULL DEFAULT 0,
    outcome            TEXT CHECK (outcome IN ({_QA_OUTCOME_SQL_ENUM})),
    budget_counted     INTEGER NOT NULL DEFAULT 1
                       CHECK (budget_counted IN (0, 1)),
    created_at         TEXT NOT NULL
);
CREATE INDEX idx_qa_log_episode ON qa_log(episode_id);

-- Human review queue (003 R12): answer_unavailable tuples and other items
-- queued for the human reflector.
CREATE TABLE review_queue (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id   INTEGER REFERENCES episodes(id),
    qa_log_id    INTEGER REFERENCES qa_log(id),
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{{}}',
    status       TEXT NOT NULL DEFAULT 'open'
                 CHECK (status IN ('open', 'resolved')),
    created_at   TEXT NOT NULL
);
CREATE INDEX idx_review_queue_episode ON review_queue(episode_id);

-- Settlement reports (003 R23): one per episode — the human reflector's entry
-- point; report_json carries the assembled report (003 U7 writes it).
CREATE TABLE settlement_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id  INTEGER NOT NULL UNIQUE REFERENCES episodes(id),
    score       REAL,
    report_json TEXT NOT NULL DEFAULT '{{}}',
    created_at  TEXT NOT NULL
);

-- SCEN columns (003 R22): rows are keyed (episode, snapshot) and carry tier,
-- judge metadata, and the judge-input a11y-diff payloads (replay re-judging
-- needs the original inputs, not just screenshots). Columns stay NULLable so
-- pre-Phase-2 rows survive; the trigger requires keys on every NEW row.
ALTER TABLE trace_scen ADD COLUMN episode_id INTEGER REFERENCES episodes(id);
ALTER TABLE trace_scen ADD COLUMN snapshot_id INTEGER;
ALTER TABLE trace_scen ADD COLUMN tier TEXT
    CHECK (tier IN ({_SCENARIO_TIER_SQL_ENUM}));
ALTER TABLE trace_scen ADD COLUMN judge_mode TEXT
    CHECK (judge_mode IN ({_SCEN_JUDGE_MODE_SQL_ENUM}));
ALTER TABLE trace_scen ADD COLUMN judge_metadata_json TEXT NOT NULL DEFAULT '{{}}';
ALTER TABLE trace_scen ADD COLUMN judge_input_json TEXT NOT NULL DEFAULT '{{}}';
CREATE INDEX idx_trace_scen_episode ON trace_scen(episode_id);

CREATE TRIGGER trg_trace_scen_requires_keys
BEFORE INSERT ON trace_scen
WHEN NEW.episode_id IS NULL OR NEW.snapshot_id IS NULL
BEGIN
    SELECT RAISE(ABORT,
        'SCEN rows require episode_id and snapshot_id keys (003 R22)');
END;

-- Idea provenance (003 R27): required --episode for Phase 2 ideas, optional
-- --scenario/--ticket evidence refs — columns the Phase 3 reflector will
-- populate mechanically.
ALTER TABLE insights ADD COLUMN episode_id INTEGER REFERENCES episodes(id);
ALTER TABLE insights ADD COLUMN evidence_scenario_id TEXT REFERENCES trace_scen(id);
ALTER TABLE insights ADD COLUMN evidence_ticket_id TEXT REFERENCES trace_tkt(id);
"""

# Phase 3a (Plan 004 U1, R1): the learning-loop schema spine. Backfill-safe over
# a Phase 0/1/2 database — every change is an ADD COLUMN (nullable or defaulted)
# or a brand-new table; the append-only triggers add no constraint that could
# reject a pre-existing row.
_SCHEMA_V4 = f"""
-- Run-mode taxonomy (R1): every episode is training | trial | benchmark. The
-- mode gates quarantine visibility (retrieval), the fitness channel, and SPC
-- eligibility downstream. An increment is exactly one run inside an episode, so
-- a run's mode is its episode's; standalone Phase 1 runs are implicitly training.
-- Pre-Phase-3a episodes backfill 'training' (the default).
ALTER TABLE episodes ADD COLUMN mode TEXT NOT NULL DEFAULT 'training'
    CHECK (mode IN ({_RUN_MODE_SQL_ENUM}));

-- Epoch column (R1): one rotation through the target curriculum (DESIGN §11);
-- nullable here, populated by Plan 5's curriculum bookkeeping.
ALTER TABLE episodes ADD COLUMN epoch INTEGER;

-- Append-only fitness-event log (R1, R19): one row per fitness event, keyed
-- (insight, episode, run-mode, kind) and stamped with the library snapshot in
-- force. Fitness state at any snapshot is COUNT(*) over events with
-- snapshot_id <= S in the queried channel — no snapshot is minted on a fitness
-- write, and rows are immutable (triggers below). training events feed the
-- ratchet; trial/benchmark events land in the validation-only channel.
CREATE TABLE fitness_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id  INTEGER NOT NULL REFERENCES insights(id),
    episode_id  INTEGER REFERENCES episodes(id),
    mode        TEXT NOT NULL CHECK (mode IN ({_RUN_MODE_SQL_ENUM})),
    kind        TEXT NOT NULL CHECK (kind IN ({_FITNESS_KIND_SQL_ENUM})),
    snapshot_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_fitness_events_insight ON fitness_events(insight_id, snapshot_id);
CREATE INDEX idx_fitness_events_episode ON fitness_events(episode_id);

CREATE TRIGGER trg_fitness_events_no_update
BEFORE UPDATE ON fitness_events
BEGIN
    SELECT RAISE(ABORT,
        'fitness_events is append-only: state is reconstructed, never edited (004 R1)');
END;

CREATE TRIGGER trg_fitness_events_no_delete
BEFORE DELETE ON fitness_events
BEGIN
    SELECT RAISE(ABORT,
        'fitness_events is append-only: events are never deleted (004 R1)');
END;

-- Run-scoped working memory (R1, R5/R6): episode-scoped typed workflow rows,
-- induced on verifier-pass and injected into later increments' ledgers above
-- library skills. They die at episode settlement (status flips live → dead) so
-- the within-episode memory never leaks across episodes.
CREATE TABLE workflows (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id       INTEGER NOT NULL REFERENCES episodes(id),
    run_id           INTEGER REFERENCES runs(id),
    source_ticket_id TEXT REFERENCES trace_tkt(id),
    precondition     TEXT NOT NULL,
    action           TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'live'
                     CHECK (status IN ({_WORKFLOW_STATUS_SQL_ENUM})),
    created_at       TEXT NOT NULL
);
CREATE INDEX idx_workflows_episode ON workflows(episode_id);

-- Batch-validation records (R1, R15-R17): one per batch validation cycle, keyed
-- (batch, snapshot), linking the optional trial-replay episode and the required
-- benchmark episode to the verdict. replay_miss / bootstrap / cosigned_by carry
-- the R15/R16 telemetry. N=1 batches in this plan; the per-batch keying is the
-- Plan 5 parallel-merge seam.
CREATE TABLE batch_validations (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id             INTEGER NOT NULL REFERENCES batches(id),
    snapshot_id          INTEGER,
    trial_episode_id     INTEGER REFERENCES episodes(id),
    benchmark_episode_id INTEGER REFERENCES episodes(id),
    verdict              TEXT CHECK (verdict IN ({_BATCH_VERDICT_SQL_ENUM})),
    replay_miss          INTEGER NOT NULL DEFAULT 0 CHECK (replay_miss IN (0, 1)),
    bootstrap            INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap IN (0, 1)),
    cosigned_by          TEXT,
    detail               TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL
);
CREATE INDEX idx_batch_validations_batch ON batch_validations(batch_id);

-- Lineage seam (R1): columns the self-reorganization lands on. Agents already
-- carry parent_id (Phase 0); routing_decisions feeds Plan 5's
-- min_routing_decisions agent-split gate, and lineage_status marks a
-- split-pending or retired-by-split parent. Skills gain parent_skill_id +
-- split_snapshot_id so this plan's U8 skill split records provenance-correct
-- child membership. All nullable / defaulted — no writer exists yet.
ALTER TABLE agents ADD COLUMN routing_decisions INTEGER NOT NULL DEFAULT 0;
ALTER TABLE agents ADD COLUMN lineage_status TEXT;
ALTER TABLE skills ADD COLUMN parent_skill_id INTEGER REFERENCES skills(id);
ALTER TABLE skills ADD COLUMN split_snapshot_id INTEGER REFERENCES snapshots(id);
"""

# Plan 007 U1 (greenfield mode): the schema spine the founder simulator, decision
# registry, assumption ledger, typed proposals, provenance, and world axis hang
# off. Backfill-safe over a Phase 0-3b database — every change is a brand-new
# table, an ADD COLUMN (nullable or defaulted), or the documented trace_req
# rename-copy-drop rebuild (precedent: trace_span v2 at _SCHEMA_V2). trace_req is
# referenced by trace_tkt_covers and trace_ac (minted in v1); `legacy_alter_table`
# is toggled ON across the rename so the child FKs keep pointing at `trace_req`
# (the rebuilt table) rather than being rewritten to the dropped temp table.
_SCHEMA_V5 = f"""
-- Decision registry (007 KTD1): a sibling of trace_feat extracted in the same
-- pre-research pass. FEAT identity discipline as the template — digest stamp,
-- append-only id, never deleted (deprecate instead). `category` keys into the
-- probe-question taxonomy; `evidence_ref` is the runtime confirmation.
CREATE TABLE trace_dec (
    id           TEXT PRIMARY KEY CHECK (id LIKE 'DEC-%'),
    target       TEXT,
    digest       TEXT,
    category     TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    evidence_ref TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'confirmed'
                 CHECK (status IN ({_DEC_STATUS_SQL_ENUM}))
);

CREATE TRIGGER trg_trace_dec_id_immutable
BEFORE UPDATE OF id ON trace_dec
BEGIN
    SELECT RAISE(ABORT,
        'DEC ids are append-only: never renumbered or reused (007 KTD1)');
END;

CREATE TRIGGER trg_trace_dec_no_delete
BEFORE DELETE ON trace_dec
BEGIN
    SELECT RAISE(ABORT,
        'DEC rows are never deleted: flip status to deprecated (007 KTD1)');
END;

-- DEC mentions (007 KTD1): a sibling of trace_msg_mentions with its own FK, so
-- the per-kind FK integrity guarantee is preserved and existing consumers churn
-- zero. An unminted DEC is unmentionable mechanically (the FK rejects it).
CREATE TABLE trace_msg_dec_mentions (
    msg_id TEXT NOT NULL REFERENCES trace_msg(id),
    dec_id TEXT NOT NULL REFERENCES trace_dec(id),
    PRIMARY KEY (msg_id, dec_id)
);

-- Assumption ledger (007 KTD2): the planner's typed assumptions as queryable
-- rows, keyed by run_id (Phase-B toy-spec runs have no episode; episode is
-- derivable through runs.episode_id when present). `check_plan_assumptions` is
-- subsumed onto these rows in U2; the plan-document records become a view.
CREATE TABLE trace_assume (
    id              TEXT PRIMARY KEY CHECK (id LIKE 'ASSUME-%'),
    run_id          INTEGER REFERENCES runs(id),
    claim           TEXT NOT NULL,
    basis           TEXT NOT NULL DEFAULT '',
    risk_if_wrong   TEXT NOT NULL DEFAULT 'med'
                    CHECK (risk_if_wrong IN ({_ASSUME_RISK_SQL_ENUM})),
    cheapest_test   TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ({_ASSUME_STATUS_SQL_ENUM})),
    confirmed_by_msg TEXT REFERENCES trace_msg(id)
);
CREATE INDEX idx_trace_assume_run ON trace_assume(run_id);

-- Typed PROPOSAL artifact (007 KTD4/R10): the planner contract gains proposals;
-- they are legal but unexercised until Phase D. options_json holds the option
-- list; linked_assume_id optionally ties a proposal to the assumption it resolves.
CREATE TABLE trace_proposal (
    id               TEXT PRIMARY KEY CHECK (id LIKE 'PROP-%'),
    run_id           INTEGER REFERENCES runs(id),
    topic            TEXT NOT NULL DEFAULT '',
    options_json     TEXT NOT NULL DEFAULT '[]',
    recommended      TEXT NOT NULL DEFAULT '',
    linked_assume_id TEXT REFERENCES trace_assume(id)
);
CREATE INDEX idx_trace_proposal_run ON trace_proposal(run_id);

-- Stored adjudication verdicts (007 KTD4): the grader-side founder session maps
-- each proposal/question to registry refs as an explicit, stored micro-judgment
-- (oracle-check shape). Settlement and Stage A look these up; nothing
-- text-matches at settlement time. Low-confidence rows route to the review queue.
CREATE TABLE trace_proposal_adjudication (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_or_msg_id TEXT NOT NULL,
    ref_kind          TEXT NOT NULL CHECK (ref_kind IN ({_FOUNDER_REF_KIND_SQL_ENUM})),
    ref_id            TEXT NOT NULL,
    verdict           TEXT NOT NULL,
    confidence        REAL,
    checker_meta      TEXT NOT NULL DEFAULT '{{}}',
    created_at        TEXT NOT NULL
);
CREATE INDEX idx_proposal_adjudication_ref
    ON trace_proposal_adjudication(ref_kind, ref_id);

-- Founder blur cache (007 KTD3): blur prose is cached TARGET-side, not
-- episode-side — identical (registry digest, seed, params, prompt) yields
-- byte-identical blur forever, so prose variance stays out of benchmark and
-- validation runs. lint_verdict records the entailment-lint outcome.
CREATE TABLE founder_blur_cache (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    target             TEXT NOT NULL,
    ref_kind           TEXT NOT NULL CHECK (ref_kind IN ({_FOUNDER_REF_KIND_SQL_ENUM})),
    ref_id             TEXT NOT NULL,
    entry_digest       TEXT NOT NULL,
    seed               INTEGER NOT NULL,
    params_hash        TEXT NOT NULL,
    prompt_set_version TEXT NOT NULL DEFAULT '',
    blur_text          TEXT NOT NULL DEFAULT '',
    lint_verdict       TEXT NOT NULL DEFAULT '',
    UNIQUE (target, ref_kind, ref_id, entry_digest, seed, params_hash,
            prompt_set_version)
);

-- Founder model (007 KTD3): one per greenfield episode. goal_statement ("what I
-- want this product to do for me") derives from the JTBD-level registry summary,
-- is always intact, and now has a home. params_hash pins the degradation params.
CREATE TABLE founder_models (
    episode_id     INTEGER PRIMARY KEY REFERENCES episodes(id),
    target         TEXT NOT NULL,
    seed           INTEGER NOT NULL,
    params_hash    TEXT NOT NULL,
    goal_statement TEXT NOT NULL DEFAULT ''
);

-- Founder knowledge (007 KTD3): the seeded degradation log — every registry ref
-- the founder is intact / blurred / dropped on. blur_id points at the cached
-- prose when state = blurred. Knowledge degrades; truthfulness never does.
CREATE TABLE founder_knowledge (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    ref_kind   TEXT NOT NULL CHECK (ref_kind IN ({_FOUNDER_REF_KIND_SQL_ENUM})),
    ref_id     TEXT NOT NULL,
    state      TEXT NOT NULL CHECK (state IN ({_FOUNDER_STATE_SQL_ENUM})),
    blur_id    INTEGER REFERENCES founder_blur_cache(id)
);
CREATE INDEX idx_founder_knowledge_episode ON founder_knowledge(episode_id);

-- The episode `world` axis (007 KTD6) — NOT mode. Runs/spans inherit world
-- through episode_id (no new columns on them). Pre-007 episodes backfill
-- 'brownfield' via the default.
ALTER TABLE episodes ADD COLUMN world TEXT NOT NULL DEFAULT 'brownfield'
    CHECK (world IN ({_WORLD_SQL_ENUM}));

-- Insight provenance (007 KTD5): backfill predicate is explicit — batches
-- labelled 'reflect-ep%' are the reflector's (stage_b convention); everything
-- else is manual. researched/seeded are set going forward by af induct / the
-- seed loader.
ALTER TABLE insights ADD COLUMN provenance TEXT NOT NULL DEFAULT 'manual'
    CHECK (provenance IN ({_INSIGHT_PROVENANCE_SQL_ENUM}));
UPDATE insights SET provenance = 'reflector'
    WHERE batch_id IN (SELECT id FROM batches WHERE label LIKE 'reflect-ep%');

-- Batch validation-class (007 KTD5): the substrate-routing tag. Pre-007 batches
-- backfill 'general' (both substrates).
ALTER TABLE batches ADD COLUMN validation_class TEXT NOT NULL DEFAULT 'general'
    CHECK (validation_class IN ({_VALIDATION_CLASS_SQL_ENUM}));

-- Elicitation metrics block (007 KTD4/R5): settlement's world-keyed metrics ride
-- alongside the existing report_json.
ALTER TABLE settlement_reports
    ADD COLUMN elicitation_metrics_json TEXT NOT NULL DEFAULT '{{}}';

-- trace_req rebuild (007 KTD2): relax source_msg_id NOT NULL, add source_assume_id,
-- and enforce exactly-one source at insert time (DB CHECK — earlier, simpler
-- failure than a lint; the U3 lint catches it at the planner-output level first).
-- legacy_alter_table keeps trace_tkt_covers/trace_ac FKs bound to `trace_req`.
PRAGMA legacy_alter_table=ON;
ALTER TABLE trace_req RENAME TO trace_req_phase4;
CREATE TABLE trace_req (
    id               TEXT PRIMARY KEY CHECK (id LIKE 'REQ-%'),
    source_msg_id    TEXT REFERENCES trace_msg(id),
    source_assume_id TEXT REFERENCES trace_assume(id),
    CHECK ((source_msg_id IS NOT NULL) + (source_assume_id IS NOT NULL) = 1)
);
INSERT INTO trace_req (id, source_msg_id)
    SELECT id, source_msg_id FROM trace_req_phase4;
DROP TABLE trace_req_phase4;
PRAGMA legacy_alter_table=OFF;
"""

# Plan 008 U1 (R3 ingest gauntlet): the R3 schema spine. Backfill-safe over a
# Phase 0–007 (v1..v5) database — every change is a brand-new table, an ADD
# COLUMN (nullable), or a documented rename-copy-drop rebuild (precedent:
# trace_span at v2, trace_req at v5). NO column or table is dropped (R6: demote,
# never drop — migration reversibility). Three rebuilds, all referenced tables,
# so `legacy_alter_table=ON` keeps every child FK bound to the rebuilt table name
# (without it the modern RENAME rewrites child FKs to the dropped temp table —
# the v5 lesson). The migrate loop already holds `foreign_keys=OFF` for the whole
# pass; `legacy_alter_table` (unlike `foreign_keys`) is NOT a no-op inside a
# transaction, so it is toggled here in the DDL and always restored to OFF.
#
#   - insights rebuild: widen the `provenance` CHECK to include `consolidated`
#     (R2) and add the four nullable R1 columns (negative_scope, valid_at,
#     invalid_at, rationale). Self-FKs (duplicate_of/supersedes) and every inbound
#     child FK survive; supersedes/duplicate_of are DEMOTED, not dropped (R6).
#   - skills rebuild: add `level` (R1) and relax `name` to nullable for lazy
#     naming (KTD — derived modules exist unnamed; combined into the one rebuild
#     skills already takes for `level`).
#   - fitness_events rebuild: widen the `kind` CHECK to include `corroborate`
#     (R4). The append-only triggers carry FIXED names; a rename keeps them
#     attached to the renamed table, so the same-named triggers cannot be
#     recreated on the new table until the old ones are gone (else
#     'trigger already exists'), and the renamed _old table must shed them before
#     it is dropped. Dropping both triggers FIRST makes that ordering explicit
#     (KTD: the sharpest migration risk — see test_fitness_trigger_dropped_*).
#   - insight_edges: the new typed semantic graph (R3), append-only at v1.
_SCHEMA_V6 = f"""
PRAGMA legacy_alter_table=ON;

-- insights rebuild (R1 columns + R2 widened provenance CHECK).
ALTER TABLE insights RENAME TO insights_v5;
CREATE TABLE insights (
    id               INTEGER PRIMARY KEY,
    precondition     TEXT NOT NULL,
    action           TEXT NOT NULL,
    expected_outcome TEXT NOT NULL,
    scope_tag        TEXT,
    content_hash     TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL DEFAULT 'quarantined'
                     CHECK (status IN ({_STATUS_SQL_ENUM})),
    batch_id         INTEGER REFERENCES batches(id),
    source_run_id    TEXT,
    embedding_model  TEXT,
    embedding_dim    INTEGER,
    duplicate_of     INTEGER REFERENCES insights(id),
    supersedes       INTEGER REFERENCES insights(id),
    retrievals       INTEGER NOT NULL DEFAULT 0,
    wins             INTEGER NOT NULL DEFAULT 0,
    losses           INTEGER NOT NULL DEFAULT 0,
    causal_blames    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    episode_id           INTEGER REFERENCES episodes(id),
    evidence_scenario_id TEXT REFERENCES trace_scen(id),
    evidence_ticket_id   TEXT REFERENCES trace_tkt(id),
    provenance       TEXT NOT NULL DEFAULT 'manual'
                     CHECK (provenance IN ({_INSIGHT_PROVENANCE_SQL_ENUM})),
    negative_scope   TEXT,
    valid_at         TEXT,
    invalid_at       TEXT,
    rationale        TEXT
);
INSERT INTO insights (
    id, precondition, action, expected_outcome, scope_tag, content_hash, status,
    batch_id, source_run_id, embedding_model, embedding_dim, duplicate_of,
    supersedes, retrievals, wins, losses, causal_blames, created_at, episode_id,
    evidence_scenario_id, evidence_ticket_id, provenance)
SELECT
    id, precondition, action, expected_outcome, scope_tag, content_hash, status,
    batch_id, source_run_id, embedding_model, embedding_dim, duplicate_of,
    supersedes, retrievals, wins, losses, causal_blames, created_at, episode_id,
    evidence_scenario_id, evidence_ticket_id, provenance
FROM insights_v5;
DROP TABLE insights_v5;

-- skills rebuild (R1 `level` + KTD lazy naming: `name` relaxed to nullable).
ALTER TABLE skills RENAME TO skills_v5;
CREATE TABLE skills (
    id                INTEGER PRIMARY KEY,
    agent_id          INTEGER NOT NULL REFERENCES agents(id),
    name              TEXT,
    description       TEXT NOT NULL DEFAULT '',
    created_batch_id  INTEGER REFERENCES batches(id),
    token_count       INTEGER NOT NULL DEFAULT 0,
    parent_skill_id   INTEGER REFERENCES skills(id),
    split_snapshot_id INTEGER REFERENCES snapshots(id),
    level             INTEGER,
    UNIQUE (agent_id, name)
);
INSERT INTO skills (
    id, agent_id, name, description, created_batch_id, token_count,
    parent_skill_id, split_snapshot_id)
SELECT
    id, agent_id, name, description, created_batch_id, token_count,
    parent_skill_id, split_snapshot_id
FROM skills_v5;
DROP TABLE skills_v5;

-- fitness_events rebuild (R4 widened `kind` CHECK). Drop the append-only
-- triggers FIRST (fixed names; the sharpest migration risk, KTD), then rebuild
-- and recreate them on the new table.
DROP TRIGGER trg_fitness_events_no_update;
DROP TRIGGER trg_fitness_events_no_delete;
ALTER TABLE fitness_events RENAME TO fitness_events_old;
CREATE TABLE fitness_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    insight_id  INTEGER NOT NULL REFERENCES insights(id),
    episode_id  INTEGER REFERENCES episodes(id),
    mode        TEXT NOT NULL CHECK (mode IN ({_RUN_MODE_SQL_ENUM})),
    kind        TEXT NOT NULL CHECK (kind IN ({_FITNESS_KIND_SQL_ENUM})),
    snapshot_id INTEGER NOT NULL,
    created_at  TEXT NOT NULL
);
INSERT INTO fitness_events
    (id, insight_id, episode_id, mode, kind, snapshot_id, created_at)
    SELECT id, insight_id, episode_id, mode, kind, snapshot_id, created_at
    FROM fitness_events_old;
DROP TABLE fitness_events_old;
CREATE INDEX idx_fitness_events_insight ON fitness_events(insight_id, snapshot_id);
CREATE INDEX idx_fitness_events_episode ON fitness_events(episode_id);
CREATE TRIGGER trg_fitness_events_no_update
BEFORE UPDATE ON fitness_events
BEGIN
    SELECT RAISE(ABORT,
        'fitness_events is append-only: state is reconstructed, never edited (004 R1)');
END;
CREATE TRIGGER trg_fitness_events_no_delete
BEFORE DELETE ON fitness_events
BEGIN
    SELECT RAISE(ABORT,
        'fitness_events is append-only: events are never deleted (004 R1)');
END;

PRAGMA legacy_alter_table=OFF;

-- insight_edges (R3): the typed semantic graph. Self-edges between insights;
-- src/kind and dst/kind indexes serve the lifecycle's open-edge lookups.
CREATE TABLE insight_edges (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    src        INTEGER NOT NULL REFERENCES insights(id),
    dst        INTEGER NOT NULL REFERENCES insights(id),
    weight     REAL,
    kind       TEXT NOT NULL CHECK (kind IN ({_INSIGHT_EDGE_KIND_SQL_ENUM})),
    created_at TEXT NOT NULL
);
CREATE INDEX idx_insight_edges_src ON insight_edges(src, kind);
CREATE INDEX idx_insight_edges_dst ON insight_edges(dst, kind);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, _SCHEMA_V1),
    (2, _SCHEMA_V2),
    (3, _SCHEMA_V3),
    (4, _SCHEMA_V4),
    (5, _SCHEMA_V5),
    (6, _SCHEMA_V6),
)


class Store:
    """One connection to the library database, with transactional discipline.

    Single-process by declaration (Phase 0); ``BEGIN IMMEDIATE`` plus
    ``busy_timeout`` is the backstop if a second writer appears anyway (R4).
    """

    def __init__(
        self,
        path: str | Path,
        busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
    ) -> None:
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._txn_depth = 0

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # --- migrations ----------------------------------------------------------

    def migrate(self) -> None:
        """Apply pending schema migrations, idempotently."""
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TEXT NOT NULL)"
        )
        row = self.conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM schema_migrations"
        ).fetchone()
        current = row["v"]
        # Foreign keys OFF for the duration of the migration loop, per SQLite's
        # documented table-rebuild procedure (lang_altertable.html §7). A
        # rename-copy-drop rebuild of a *referenced* table (trace_req in v5, with
        # trace_tkt_covers/trace_ac children) otherwise has its child FK
        # references rewritten to the dropped temp table when foreign_keys is ON
        # — even with legacy_alter_table. The pragma is a no-op inside a
        # transaction, so it must be toggled here, between the per-migration
        # executescripts (the connection is autocommit). Always restored to ON.
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            for version, ddl in MIGRATIONS:
                if version <= current:
                    continue
                # executescript would implicitly COMMIT a transaction opened via
                # transaction(), so the DDL + version stamp travel as one script
                # with the transaction inside it.
                self.conn.executescript(
                    "BEGIN IMMEDIATE;\n"
                    f"{ddl}\n"
                    "INSERT INTO schema_migrations (version, applied_at)"
                    f" VALUES ({int(version)}, '{_utcnow()}');\n"
                    "COMMIT;"
                )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    # --- transactions --------------------------------------------------------

    @property
    def in_transaction(self) -> bool:
        return self._txn_depth > 0

    @contextmanager
    def transaction(self, immediate: bool = True):
        """Context-managed transaction; nested entry joins the outer transaction.

        ``immediate`` (default) takes the write lock up front so concurrent
        writers serialize at BEGIN rather than deadlocking at COMMIT (R4).
        """
        if self._txn_depth > 0:
            self._txn_depth += 1
            try:
                yield
            finally:
                self._txn_depth -= 1
            return
        self.conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        self._txn_depth = 1
        try:
            yield
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")
        finally:
            self._txn_depth = 0

    def _require_transaction(self, what: str) -> None:
        if not self.in_transaction:
            raise StoreError(
                f"{what} must run inside a Store.transaction() block "
                "(multi-statement mutation; atomicity per R7)."
            )

    # --- snapshots and the promotion queue ------------------------------------

    def current_snapshot_id(self) -> int:
        """Latest snapshot ID; 0 means 'before any active-set mutation'."""
        row = self.conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS v FROM snapshots"
        ).fetchone()
        return row["v"]

    @contextmanager
    def queue_operation(self, operation: str, detail: str = ""):
        """Serialized active-set mutation: one transaction, one minted snapshot.

        The ONLY way snapshots are created (R3). Yields the new snapshot ID;
        callers write their status changes via :meth:`set_status` inside the block.
        """
        with self.transaction(immediate=True):
            parent = self.current_snapshot_id()
            cur = self.conn.execute(
                "INSERT INTO snapshots (parent_id, created_at, mutation_summary)"
                " VALUES (?, ?, ?)",
                (parent or None, _utcnow(), f"{operation}: {detail}" if detail else operation),
            )
            snapshot_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO promotion_queue (operation, detail, snapshot_id, created_at)"
                " VALUES (?, ?, ?, ?)",
                (operation, detail, snapshot_id, _utcnow()),
            )
            yield snapshot_id

    def set_status(self, insight_id: int, to_status: str, snapshot_id: int) -> None:
        """Flip an insight's status, recording the transition under ``snapshot_id``."""
        if to_status not in STATUSES:
            raise StoreError(f"unknown status '{to_status}' (expected one of {STATUSES})")
        self._require_transaction("set_status")
        row = self.conn.execute(
            "SELECT status FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"insight {insight_id} does not exist")
        self.conn.execute(
            "INSERT INTO status_transitions (insight_id, from_status, to_status, snapshot_id)"
            " VALUES (?, ?, ?, ?)",
            (insight_id, row["status"], to_status, snapshot_id),
        )
        self.conn.execute(
            "UPDATE insights SET status = ? WHERE id = ?", (to_status, insight_id)
        )

    def status_at(self, insight_id: int, snapshot_id: int) -> str:
        """Reconstruct an insight's status as of ``snapshot_id`` (R3)."""
        row = self.conn.execute(
            "SELECT to_status FROM status_transitions"
            " WHERE insight_id = ? AND snapshot_id <= ?"
            " ORDER BY snapshot_id DESC, id DESC LIMIT 1",
            (insight_id, snapshot_id),
        ).fetchone()
        if row is not None:
            return row["to_status"]
        first = self.conn.execute(
            "SELECT from_status FROM status_transitions"
            " WHERE insight_id = ? ORDER BY snapshot_id ASC, id ASC LIMIT 1",
            (insight_id,),
        ).fetchone()
        if first is not None:
            return first["from_status"]
        current = self.conn.execute(
            "SELECT status FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if current is None:
            raise StoreError(f"insight {insight_id} does not exist")
        return current["status"]

    # --- taxonomy: families, agents, batches ----------------------------------

    def create_family(self, name: str, charter: str = "", router_prompt: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO families (name, charter, router_prompt) VALUES (?, ?, ?)",
            (name, charter, router_prompt),
        )
        return cur.lastrowid

    def create_agent(
        self,
        family_id: int,
        name: str,
        *,
        description: str = "",
        base_prompt_specialty: str = "",
        permissions: str = "{}",
        active_cap: int = 50,
        parent_id: int | None = None,
    ) -> int:
        # plan-010 R7: `base_prompt_specialty` is WRITE-ONLY at runtime. Personas
        # are removed under R3 (§3) — nothing assembles a specialty section into a
        # prompt, and no SELECT reads this column on any runtime path. The column
        # and any written value survive for reversibility (§4: demote, never drop);
        # the only reader (`reflector/agent_split.check_base_prompt_residue`) was
        # deleted in plan-009. test_demotion_finish guards the no-read invariant.
        cur = self.conn.execute(
            "INSERT INTO agents (family_id, parent_id, name, description,"
            " base_prompt_specialty, permissions, active_cap)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (family_id, parent_id, name, description, base_prompt_specialty,
             permissions, active_cap),
        )
        return cur.lastrowid

    def ensure_batch(self, label: str) -> int:
        """Return the batch ID for ``label``, creating the row if new (R11)."""
        row = self.conn.execute(
            "SELECT id FROM batches WHERE label = ?", (label,)
        ).fetchone()
        if row is not None:
            return row["id"]
        cur = self.conn.execute(
            "INSERT INTO batches (label, created_at) VALUES (?, ?)",
            (label, _utcnow()),
        )
        return cur.lastrowid

    # --- insights -------------------------------------------------------------

    def insert_insight(
        self,
        *,
        precondition: str,
        action: str,
        expected_outcome: str,
        content_hash: str,
        scope_tag: str | None = None,
        status: str = "quarantined",
        batch_id: int | None = None,
        source_run_id: str | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        duplicate_of: int | None = None,
        supersedes: int | None = None,
        episode_id: int | None = None,
        evidence_scenario_id: str | None = None,
        evidence_ticket_id: str | None = None,
        provenance: str = "manual",
        negative_scope: str | None = None,
        valid_at: str | None = None,
        invalid_at: str | None = None,
        rationale: str | None = None,
    ) -> int:
        """Register a quarantined insight (008 R1/R10/R13).

        The R3 ingest gauntlet authors the generalized atom (precondition/action/
        expected_outcome/rationale), its ``negative_scope`` ("when NOT to apply",
        R10 altitude audit), and ``provenance``. ``valid_at``/``invalid_at`` are
        the append-only temporal-validity stamps (§5); ``invalid_at`` is left
        NULL at ingest and stamped only at promotion (deferred-supersede, R12/R16
        — see :meth:`set_invalid_at`). The provenance/edge-kind enums are enforced
        by the column CHECKs (raw writes are backstopped the same way).
        """
        cur = self.conn.execute(
            "INSERT INTO insights (precondition, action, expected_outcome, scope_tag,"
            " content_hash, status, batch_id, source_run_id, embedding_model,"
            " embedding_dim, duplicate_of, supersedes, episode_id,"
            " evidence_scenario_id, evidence_ticket_id, provenance, negative_scope,"
            " valid_at, invalid_at, rationale, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (precondition, action, expected_outcome, scope_tag, content_hash, status,
             batch_id, source_run_id, embedding_model, embedding_dim, duplicate_of,
             supersedes, episode_id, evidence_scenario_id, evidence_ticket_id,
             provenance, negative_scope, valid_at, invalid_at, rationale,
             _utcnow()),
        )
        return cur.lastrowid

    def set_invalid_at(
        self, insight_id: int, value: str, snapshot_id: int
    ) -> None:
        """Stamp an insight's ``invalid_at`` (append-only temporal validity, §5).

        The deferred-supersede write (008 R12/R16): a contradiction is detected at
        ingest (recorded as a ``contradicts`` edge) but the loser is invalidated
        only at promotion, under the minted snapshot, alongside
        ``set_status(loser, "retired", snapshot_id)``. ``snapshot_id`` pins the
        caller to a promotion-queue context; this writer only sets the column.
        Must run inside a transaction (the queue block, R7).
        """
        self._require_transaction("set_invalid_at")
        row = self.conn.execute(
            "SELECT id FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"insight {insight_id} does not exist")
        self.conn.execute(
            "UPDATE insights SET invalid_at = ? WHERE id = ?", (value, insight_id)
        )

    def add_insight_edge(
        self,
        src: int,
        dst: int,
        kind: str,
        weight: float | None = None,
    ) -> int:
        """Write a typed semantic edge between two insights (008 R3).

        Append-only for the four semantic kinds (corroborates/refines/contradicts/
        generalizes_from); ``similarity`` is enum-legal for forward-compat but is
        never written at v1 (the KNN graph is recomputed each derive pass — plan
        009). The src/dst insight FKs and the ``kind`` CHECK are enforced by the
        table.
        """
        if kind not in INSIGHT_EDGE_KINDS:
            raise StoreError(
                f"unknown insight edge kind '{kind}'"
                f" (expected one of {INSIGHT_EDGE_KINDS})"
            )
        cur = self.conn.execute(
            "INSERT INTO insight_edges (src, dst, weight, kind, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (src, dst, weight, kind, _utcnow()),
        )
        return cur.lastrowid

    def get_insight(self, insight_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM insights WHERE id = ?", (insight_id,)
        ).fetchone()

    def find_insight_by_hash(self, content_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM insights WHERE content_hash = ?", (content_hash,)
        ).fetchone()

    # --- skills and membership --------------------------------------------------
    #
    # DEMOTED at 008 U1 (R13): the R3 ingest gauntlet authors NO group — grouping
    # is deferred to plan 009's derive pass. ``create_skill``/``append_member``
    # are kept (not dropped) as the batch writer that derive pass will call; no
    # ingest path invokes them after the add_idea rewrite (008 U6).

    def create_skill(
        self,
        agent_id: int,
        name: str,
        description: str = "",
        created_batch_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO skills (agent_id, name, description, created_batch_id)"
            " VALUES (?, ?, ?, ?)",
            (agent_id, name, description, created_batch_id),
        )
        return cur.lastrowid

    def append_member(self, skill_id: int, insight_id: int) -> int:
        """Append an insight to a skill; membership order is append order (R1/R15)."""
        cur = self.conn.execute(
            "INSERT INTO skill_members (skill_id, insight_id, position)"
            " SELECT ?, ?, COALESCE(MAX(position), 0) + 1"
            " FROM skill_members WHERE skill_id = ?",
            (skill_id, insight_id, skill_id),
        )
        return cur.lastrowid

    def skill_members(self, skill_id: int) -> list[int]:
        """Member insight IDs in append order."""
        rows = self.conn.execute(
            "SELECT insight_id FROM skill_members WHERE skill_id = ?"
            " ORDER BY position ASC",
            (skill_id,),
        ).fetchall()
        return [r["insight_id"] for r in rows]

    # --- merge log ---------------------------------------------------------------

    def insert_merge_log(
        self,
        *,
        content_hash: str,
        structural_fields_json: str,
        duplicate_of: int,
        batch_id: int | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO merge_log (content_hash, structural_fields_json,"
            " duplicate_of, batch_id, judged_at) VALUES (?, ?, ?, ?, ?)",
            (content_hash, structural_fields_json, duplicate_of, batch_id, _utcnow()),
        )
        return cur.lastrowid

    def find_merge_log_by_hash(self, content_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM merge_log WHERE content_hash = ?"
            " ORDER BY id DESC LIMIT 1",
            (content_hash,),
        ).fetchone()

    # --- meta ----------------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row is not None else None

    # --- Phase 1: runs (002 R1) -----------------------------------------------

    def create_run(
        self,
        spec_ref: str,
        snapshot_id: int,
        *,
        episode_id: int | None = None,
        increment_index: int | None = None,
    ) -> int:
        """Mint a run row keyed by spec ref + library snapshot (0 = pre-mutation).

        Episode mode (003 R1): an increment is exactly one run, so episode runs
        carry ``episode_id`` + ``increment_index``; standalone toy-spec runs
        leave both NULL (Phase 1 callers are unchanged).
        """
        cur = self.conn.execute(
            "INSERT INTO runs (spec_ref, snapshot_id, status, episode_id,"
            " increment_index, created_at) VALUES (?, ?, 'created', ?, ?, ?)",
            (spec_ref, int(snapshot_id), episode_id, increment_index, _utcnow()),
        )
        return cur.lastrowid

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()

    def set_run_status(self, run_id: int, status: str) -> None:
        if status not in RUN_STATUSES:
            raise StoreError(
                f"unknown run status '{status}' (expected one of {RUN_STATUSES})"
            )
        cur = self.conn.execute(
            "UPDATE runs SET status = ? WHERE id = ?", (status, run_id)
        )
        if cur.rowcount == 0:
            raise StoreError(f"run {run_id} does not exist")

    # --- Phase 1: ticket lifecycle (002 R2) -------------------------------------

    def set_ticket_status(
        self, ticket_id: str, to_status: str, run_id: int | None = None
    ) -> None:
        """Flip a ticket's status, recording the transition in the audit table."""
        if to_status not in TICKET_STATUSES:
            raise StoreError(
                f"unknown ticket status '{to_status}'"
                f" (expected one of {TICKET_STATUSES})"
            )
        self._require_transaction("set_ticket_status")
        row = self.conn.execute(
            "SELECT status FROM trace_tkt WHERE id = ?", (ticket_id,)
        ).fetchone()
        if row is None:
            raise StoreError(f"ticket {ticket_id} does not exist")
        self.conn.execute(
            "INSERT INTO ticket_status_transitions"
            " (ticket_id, from_status, to_status, run_id, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (ticket_id, row["status"], to_status, run_id, _utcnow()),
        )
        self.conn.execute(
            "UPDATE trace_tkt SET status = ? WHERE id = ?", (to_status, ticket_id)
        )

    # --- Phase 1: span lifecycle (002 R3/R16) ------------------------------------

    def insert_span(
        self,
        span_id: str,
        *,
        run_id: int | None = None,
        family: str | None = None,
        agent: str | None = None,
        ticket_id: str | None = None,
        ralph_iteration: int | None = None,
        parent_span: str | None = None,
        model_version: str | None = None,
        prompt_set_version: str | None = None,
    ) -> str:
        """Register a span as ``running`` at spawn (orphan detection's substrate)."""
        self.conn.execute(
            "INSERT INTO trace_span (id, run_id, family, agent, ticket_id,"
            " ralph_iteration, parent_span, status, model_version,"
            " prompt_set_version) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)",
            (span_id, run_id, family, agent, ticket_id, ralph_iteration,
             parent_span, model_version, prompt_set_version),
        )
        return span_id

    def finalize_span(
        self,
        span_id: str,
        status: str,
        *,
        num_turns: int | None = None,
        duration_ms: int | None = None,
        cost_usd: float | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_partial: bool = False,
        files_json: str | None = None,
        artifact_refs_json: str | None = None,
    ) -> None:
        """Finalize a span on session exit (or mark it aborted/timeout on kill)."""
        if status not in SPAN_FINAL_STATUSES:
            raise StoreError(
                f"'{status}' is not a final span status"
                f" (expected one of {SPAN_FINAL_STATUSES})"
            )
        cur = self.conn.execute(
            "UPDATE trace_span SET status = ?, num_turns = ?, duration_ms = ?,"
            " cost_usd = ?, input_tokens = ?, output_tokens = ?, cost_partial = ?,"
            " files_json = COALESCE(?, files_json),"
            " artifact_refs_json = COALESCE(?, artifact_refs_json)"
            " WHERE id = ?",
            (status, num_turns, duration_ms, cost_usd, input_tokens, output_tokens,
             1 if cost_partial else 0, files_json, artifact_refs_json, span_id),
        )
        if cur.rowcount == 0:
            raise StoreError(f"span {span_id} does not exist")

    def orphan_spans(self, run_id: int | None = None) -> list[sqlite3.Row]:
        """Spans still ``running`` — at resume these are dead sessions to abort (R3)."""
        if run_id is None:
            return self.conn.execute(
                "SELECT * FROM trace_span WHERE status = 'running' ORDER BY id"
            ).fetchall()
        return self.conn.execute(
            "SELECT * FROM trace_span WHERE status = 'running' AND run_id = ?"
            " ORDER BY id",
            (run_id,),
        ).fetchall()

    # --- Phase 1: failures, tripwires, ledger (002 R13/R17/R11) -------------------

    def insert_failure_record(
        self,
        *,
        failure_kind: str,
        location: str = "",
        expected: str = "",
        observed: str = "",
        repro_command: str = "",
        run_id: int | None = None,
        ticket_id: str | None = None,
        span_id: str | None = None,
    ) -> int:
        if failure_kind not in FAILURE_KINDS:
            raise StoreError(
                f"unknown failure kind '{failure_kind}'"
                f" (expected one of {FAILURE_KINDS})"
            )
        cur = self.conn.execute(
            "INSERT INTO failure_records (run_id, ticket_id, span_id, failure_kind,"
            " location, expected, observed, repro_command, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, ticket_id, span_id, failure_kind, location, expected,
             observed, repro_command, _utcnow()),
        )
        return cur.lastrowid

    def insert_tripwire_event(
        self,
        *,
        detector_kind: str,
        would_have_fired: bool,
        span_id: str | None = None,
        run_id: int | None = None,
        ralph_iteration: int | None = None,
        similarity: float | None = None,
        embedding_model: str | None = None,
        embedding_dim: int | None = None,
        failure_set_hash: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO tripwire_events (span_id, run_id, ralph_iteration,"
            " detector_kind, similarity, would_have_fired, embedding_model,"
            " embedding_dim, failure_set_hash, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (span_id, run_id, ralph_iteration, detector_kind, similarity,
             1 if would_have_fired else 0, embedding_model, embedding_dim,
             failure_set_hash, _utcnow()),
        )
        return cur.lastrowid

    def append_ledger_entry(
        self,
        *,
        ticket_id: str,
        entry_kind: str,
        run_id: int | None = None,
        ralph_iteration: int | None = None,
        span_id: str | None = None,
        chk_id: str | None = None,
        failure_record_id: int | None = None,
        content: str = "",
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO ledger_entries (ticket_id, run_id, ralph_iteration,"
            " entry_kind, span_id, chk_id, failure_record_id, content, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ticket_id, run_id, ralph_iteration, entry_kind, span_id, chk_id,
             failure_record_id, content, _utcnow()),
        )
        return cur.lastrowid

    # --- Phase 2: episodes and acceptance (003 R1-R4) ----------------------------

    def create_episode(
        self,
        target: str,
        digest: str,
        snapshot_id: int,
        *,
        max_increments: int | None = None,
        cost_ceiling_usd: float | None = None,
        mode: str = "training",
        epoch: int | None = None,
    ) -> int:
        """Mint an episode: one target × one library snapshot × one settlement.

        Budget fields come from thresholds config (003 R4) and are stamped here
        so the values that governed the episode are queryable forever. ``mode``
        is the run-mode taxonomy (004 R1) and ``epoch`` the curriculum rotation
        (nullable; Plan 5 populates).
        """
        if mode not in RUN_MODES:
            raise StoreError(
                f"unknown run mode '{mode}' (expected one of {RUN_MODES})"
            )
        cur = self.conn.execute(
            "INSERT INTO episodes (target, digest, snapshot_id, status,"
            " max_increments, cost_ceiling_usd, mode, epoch, created_at)"
            " VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?)",
            (target, digest, int(snapshot_id), max_increments, cost_ceiling_usd,
             mode, epoch, _utcnow()),
        )
        return cur.lastrowid

    def get_episode(self, episode_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM episodes WHERE id = ?", (episode_id,)
        ).fetchone()

    def set_episode_status(self, episode_id: int, status: str) -> None:
        """Flip episode status; `suspended` is resumable, terminals per 003 R3."""
        if status not in EPISODE_STATUSES:
            raise StoreError(
                f"unknown episode status '{status}'"
                f" (expected one of {EPISODE_STATUSES})"
            )
        cur = self.conn.execute(
            "UPDATE episodes SET status = ? WHERE id = ?", (status, episode_id)
        )
        if cur.rowcount == 0:
            raise StoreError(f"episode {episode_id} does not exist")

    def set_run_acceptance(self, run_id: int, acceptance: str) -> None:
        """Record the explorer's UAT verdict on a settled run's delivered subset."""
        if acceptance not in RUN_ACCEPTANCE:
            raise StoreError(
                f"unknown acceptance '{acceptance}'"
                f" (expected one of {RUN_ACCEPTANCE})"
            )
        cur = self.conn.execute(
            "UPDATE runs SET acceptance = ? WHERE id = ?", (acceptance, run_id)
        )
        if cur.rowcount == 0:
            raise StoreError(f"run {run_id} does not exist")

    # --- Phase 3a: fitness-event log (004 R1/R19) -----------------------------

    def record_fitness_event(
        self,
        insight_id: int,
        kind: str,
        mode: str,
        snapshot_id: int,
        *,
        episode_id: int | None = None,
    ) -> int:
        """Append one fitness event (immutable). Mints no snapshot (004 R1).

        The log is the substrate of state-at-snapshot: fitness at S in a channel
        is the COUNT over events with ``snapshot_id <= S``. training events feed
        the ratchet; trial/benchmark land in the validation-only channel (R19).
        """
        if kind not in FITNESS_EVENT_KINDS:
            raise StoreError(
                f"unknown fitness event kind '{kind}'"
                f" (expected one of {FITNESS_EVENT_KINDS})"
            )
        if mode not in RUN_MODES:
            raise StoreError(
                f"unknown run mode '{mode}' (expected one of {RUN_MODES})"
            )
        cur = self.conn.execute(
            "INSERT INTO fitness_events (insight_id, episode_id, mode, kind,"
            " snapshot_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (insight_id, episode_id, mode, kind, int(snapshot_id), _utcnow()),
        )
        return cur.lastrowid

    def fitness_counts(
        self,
        insight_id: int,
        *,
        snapshot_id: int | None = None,
        mode: str = "training",
    ) -> dict[str, int]:
        """Reconstruct an insight's fitness in one channel as of a snapshot.

        Returns ``{kind: count}`` for every kind (zeros included). ``mode``
        selects the channel: `training` is the ratchet channel; trial/benchmark
        events are excluded from it (004 R19). ``snapshot_id=None`` counts all
        events; otherwise only those with ``snapshot_id <= snapshot_id``.
        """
        if mode not in RUN_MODES:
            raise StoreError(
                f"unknown run mode '{mode}' (expected one of {RUN_MODES})"
            )
        sql = (
            "SELECT kind, COUNT(*) AS n FROM fitness_events"
            " WHERE insight_id = ? AND mode = ?"
        )
        params: list[object] = [insight_id, mode]
        if snapshot_id is not None:
            sql += " AND snapshot_id <= ?"
            params.append(int(snapshot_id))
        sql += " GROUP BY kind"
        counts = {kind: 0 for kind in FITNESS_EVENT_KINDS}
        for row in self.conn.execute(sql, params).fetchall():
            counts[row["kind"]] = row["n"]
        return counts

    # --- Phase 3a: run-scoped working memory (004 R1/R5/R6) -------------------

    def insert_workflow(
        self,
        episode_id: int,
        *,
        precondition: str,
        action: str,
        expected_outcome: str,
        run_id: int | None = None,
        source_ticket_id: str | None = None,
    ) -> int:
        """Induce a `live` episode-scoped workflow row (dies at settlement)."""
        cur = self.conn.execute(
            "INSERT INTO workflows (episode_id, run_id, source_ticket_id,"
            " precondition, action, expected_outcome, status, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, 'live', ?)",
            (episode_id, run_id, source_ticket_id, precondition, action,
             expected_outcome, _utcnow()),
        )
        return cur.lastrowid

    def live_workflows(self, episode_id: int) -> list[sqlite3.Row]:
        """Workflows still alive for an episode (injected above library skills)."""
        return self.conn.execute(
            "SELECT * FROM workflows WHERE episode_id = ? AND status = 'live'"
            " ORDER BY id",
            (episode_id,),
        ).fetchall()

    def settle_workflows(self, episode_id: int) -> int:
        """Kill an episode's run memory at settlement; returns rows retired."""
        cur = self.conn.execute(
            "UPDATE workflows SET status = 'dead'"
            " WHERE episode_id = ? AND status = 'live'",
            (episode_id,),
        )
        return cur.rowcount

    # --- Phase 3a: batch-validation records (004 R1/R15-R17) ------------------

    def insert_batch_validation(
        self,
        batch_id: int,
        *,
        snapshot_id: int | None = None,
        trial_episode_id: int | None = None,
        benchmark_episode_id: int | None = None,
        verdict: str | None = None,
        replay_miss: bool = False,
        bootstrap: bool = False,
        cosigned_by: str | None = None,
        detail: str = "",
    ) -> int:
        """Open a batch-validation record keyed (batch, snapshot) (004 R17)."""
        if verdict is not None and verdict not in BATCH_VERDICTS:
            raise StoreError(
                f"unknown batch verdict '{verdict}'"
                f" (expected one of {BATCH_VERDICTS})"
            )
        cur = self.conn.execute(
            "INSERT INTO batch_validations (batch_id, snapshot_id,"
            " trial_episode_id, benchmark_episode_id, verdict, replay_miss,"
            " bootstrap, cosigned_by, detail, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (batch_id, snapshot_id, trial_episode_id, benchmark_episode_id,
             verdict, 1 if replay_miss else 0, 1 if bootstrap else 0,
             cosigned_by, detail, _utcnow()),
        )
        return cur.lastrowid

    def get_batch_validation(self, validation_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM batch_validations WHERE id = ?", (validation_id,)
        ).fetchone()

    def set_batch_verdict(
        self,
        validation_id: int,
        verdict: str,
        *,
        replay_miss: bool | None = None,
        cosigned_by: str | None = None,
    ) -> None:
        """Record the promote/revert decision on a batch-validation record."""
        if verdict not in BATCH_VERDICTS:
            raise StoreError(
                f"unknown batch verdict '{verdict}'"
                f" (expected one of {BATCH_VERDICTS})"
            )
        sets = ["verdict = ?"]
        params: list[object] = [verdict]
        if replay_miss is not None:
            sets.append("replay_miss = ?")
            params.append(1 if replay_miss else 0)
        if cosigned_by is not None:
            sets.append("cosigned_by = ?")
            params.append(cosigned_by)
        params.append(validation_id)
        cur = self.conn.execute(
            f"UPDATE batch_validations SET {', '.join(sets)} WHERE id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise StoreError(f"batch validation {validation_id} does not exist")
