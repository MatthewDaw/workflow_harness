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

_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in STATUSES)
_RUN_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in RUN_STATUSES)
_TICKET_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in TICKET_STATUSES)
_SPAN_STATUS_SQL_ENUM = ", ".join(f"'{s}'" for s in SPAN_STATUSES)
_FAILURE_KIND_SQL_ENUM = ", ".join(f"'{k}'" for k in FAILURE_KINDS)


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

MIGRATIONS: tuple[tuple[int, str], ...] = ((1, _SCHEMA_V1), (2, _SCHEMA_V2))


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
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO insights (precondition, action, expected_outcome, scope_tag,"
            " content_hash, status, batch_id, source_run_id, embedding_model,"
            " embedding_dim, duplicate_of, supersedes, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (precondition, action, expected_outcome, scope_tag, content_hash, status,
             batch_id, source_run_id, embedding_model, embedding_dim, duplicate_of,
             supersedes, _utcnow()),
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

    def create_run(self, spec_ref: str, snapshot_id: int) -> int:
        """Mint a run row keyed by spec ref + library snapshot (0 = pre-mutation)."""
        cur = self.conn.execute(
            "INSERT INTO runs (spec_ref, snapshot_id, status, created_at)"
            " VALUES (?, ?, 'created', ?)",
            (spec_ref, int(snapshot_id), _utcnow()),
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
