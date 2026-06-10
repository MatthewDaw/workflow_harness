"""Plan 001 U2: SQLite schema, store layer, snapshots, promotion queue (R1-R4).

Plan 002 U1: Phase 1 schema migration — runs, ticket lifecycle + audit, span
lifecycle/cost columns, failure records, tripwire events, ledger entries.
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import agent_families.store as store_mod
from agent_families.store import (
    FAILURE_KINDS,
    RUN_STATUSES,
    RUN_TERMINAL_STATUSES,
    SPAN_FINAL_STATUSES,
    STATUSES,
    TICKET_STATUSES,
    Store,
    StoreError,
)

EXPECTED_TABLES = {
    "schema_migrations",
    "families",
    "agents",
    "batches",
    "snapshots",
    "promotion_queue",
    "insights",
    "skills",
    "skill_members",
    "status_transitions",
    "contradictions",
    "merge_log",
    "meta",
    "trace_feat",
    "trace_msg",
    "trace_msg_mentions",
    "trace_req",
    "trace_tkt",
    "trace_tkt_covers",
    "trace_ac",
    "trace_span",
    "trace_chk",
    "trace_scen",
    # Phase 1 (plan 002 U1)
    "runs",
    "ticket_status_transitions",
    "failure_records",
    "tripwire_events",
    "ledger_entries",
}


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    yield s
    s.close()


def _table_names(s: Store) -> set[str]:
    rows = s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
        " AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r["name"] for r in rows}


def _count(s: Store, table: str) -> int:
    return s.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]


def _add_insight(s: Store, key: str, status: str = "quarantined", **kw) -> int:
    return s.insert_insight(
        precondition=f"pre-{key}",
        action=f"act-{key}",
        expected_outcome=f"out-{key}",
        content_hash=f"hash-{key}",
        status=status,
        **kw,
    )


# --- schema ------------------------------------------------------------------


def test_schema_creates_idempotently(tmp_path):
    db = tmp_path / "library.db"
    with Store(db) as s:
        s.migrate()
        s.migrate()  # second run applies nothing
        assert EXPECTED_TABLES <= _table_names(s)
        versions = s.conn.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [r["version"] for r in versions] == [1, 2]
    # a fresh connection over the same file is also a no-op
    with Store(db) as s2:
        s2.migrate()
        assert EXPECTED_TABLES <= _table_names(s2)


def test_foreign_keys_enforced(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.create_agent(family_id=999, name="orphan")


# --- insights ------------------------------------------------------------------


def test_insight_crud_phase0_statuses(store):
    ids = {}
    for status in ("quarantined", "active", "retired"):
        ids[status] = _add_insight(store, status, status=status)
    for status, insight_id in ids.items():
        row = store.get_insight(insight_id)
        assert row["status"] == status
        assert row["precondition"] == f"pre-{status}"
        assert row["action"] == f"act-{status}"
        assert row["expected_outcome"] == f"out-{status}"
        assert row["retrievals"] == row["wins"] == row["losses"] == 0
    # dormant is accepted by the enum (schema-only in Phase 0; nothing produces it)
    dormant_id = _add_insight(store, "dormant", status="dormant")
    assert store.get_insight(dormant_id)["status"] == "dormant"
    assert set(STATUSES) == {"quarantined", "active", "dormant", "retired"}


def test_invalid_status_rejected_by_enum(store):
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "bogus", status="bogus")


def test_content_hash_unique(store):
    _add_insight(store, "dup")
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "dup")


def test_insight_provenance_links_roundtrip(store):
    original = _add_insight(store, "orig")
    challenger = _add_insight(store, "chal", supersedes=original)
    dup = _add_insight(store, "dupl", duplicate_of=original)
    assert store.get_insight(challenger)["supersedes"] == original
    assert store.get_insight(dup)["duplicate_of"] == original
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "dangling", supersedes=99999)


def test_find_insight_by_hash(store):
    insight_id = _add_insight(store, "findme")
    assert store.find_insight_by_hash("hash-findme")["id"] == insight_id
    assert store.find_insight_by_hash("hash-missing") is None


# --- skills and membership -------------------------------------------------------


@pytest.fixture
def agent(store):
    family_id = store.create_family("worker", charter="builds things")
    return store.create_agent(family_id, "generic-worker")


def test_skill_membership_ordering_preserved(store, agent):
    skill_a = store.create_skill(agent, "skill-a")
    skill_b = store.create_skill(agent, "skill-b")
    i1, i2, i3, i4 = (_add_insight(store, f"m{n}") for n in range(4))
    # interleave appends across two skills; each skill keeps its own append order
    store.append_member(skill_a, i2)
    store.append_member(skill_b, i4)
    store.append_member(skill_a, i1)
    store.append_member(skill_a, i3)
    assert store.skill_members(skill_a) == [i2, i1, i3]
    assert store.skill_members(skill_b) == [i4]


def test_agent_skill_name_unique(store, agent):
    store.create_skill(agent, "elicitation")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_skill(agent, "elicitation")
    # same name under a different agent is fine
    family_id = store.create_family("verifier")
    other_agent = store.create_agent(family_id, "generic-verifier")
    store.create_skill(other_agent, "elicitation")


def test_duplicate_membership_rejected(store, agent):
    skill = store.create_skill(agent, "skill")
    insight = _add_insight(store, "once")
    store.append_member(skill, insight)
    with pytest.raises(sqlite3.IntegrityError):
        store.append_member(skill, insight)


# --- snapshots and the promotion queue ---------------------------------------------


def test_snapshot_minted_on_queue_op_not_on_insert(store):
    with store.transaction():
        _add_insight(store, "reg")
    assert _count(store, "snapshots") == 0  # registration never mints (R3)
    assert store.current_snapshot_id() == 0

    insight = _add_insight(store, "promotee")
    with store.queue_operation("promote", "batch-1") as snap:
        store.set_status(insight, "active", snap)
    assert _count(store, "snapshots") == 1
    assert store.current_snapshot_id() == snap
    audit = store.conn.execute("SELECT * FROM promotion_queue").fetchone()
    assert audit["operation"] == "promote"
    assert audit["snapshot_id"] == snap


def test_snapshot_parent_chain_linear(store):
    insight = _add_insight(store, "chained")
    snaps = []
    for status in ("active", "retired", "active"):
        with store.queue_operation("flip") as snap:
            store.set_status(insight, status, snap)
        snaps.append(snap)
    rows = store.conn.execute(
        "SELECT id, parent_id FROM snapshots ORDER BY id"
    ).fetchall()
    assert [r["id"] for r in rows] == snaps
    assert rows[0]["parent_id"] is None
    assert [r["parent_id"] for r in rows[1:]] == snaps[:-1]


def test_state_at_snapshot_reconstruction(store):
    insight = _add_insight(store, "lifecycle")  # born quarantined, no snapshot
    with store.queue_operation("promote") as s1:
        store.set_status(insight, "active", s1)
    with store.queue_operation("retire") as s2:
        store.set_status(insight, "retired", s2)
    assert store.status_at(insight, 0) == "quarantined"  # before any mutation
    assert store.status_at(insight, s1) == "active"
    assert store.status_at(insight, s2) == "retired"
    assert store.status_at(insight, s2 + 100) == "retired"
    assert store.get_insight(insight)["status"] == "retired"
    # an insight with no transitions reports its birth status at every snapshot
    untouched = _add_insight(store, "untouched")
    assert store.status_at(untouched, s2) == "quarantined"


def test_set_status_requires_transaction(store):
    insight = _add_insight(store, "guarded")
    with pytest.raises(StoreError, match="transaction"):
        store.set_status(insight, "active", 1)


def test_set_status_rejects_unknown_status_and_insight(store):
    insight = _add_insight(store, "checked")
    with store.queue_operation("op") as snap:
        with pytest.raises(StoreError, match="unknown status"):
            store.set_status(insight, "promoted", snap)
        store.set_status(insight, "active", snap)
    with store.queue_operation("op2") as snap2:
        with pytest.raises(StoreError, match="does not exist"):
            store.set_status(99999, "retired", snap2)
        store.set_status(insight, "retired", snap2)


def test_transaction_rolls_back_atomically(store, agent):
    skill = store.create_skill(agent, "atomic")
    before = {t: _count(store, t) for t in ("insights", "skill_members", "snapshots")}
    with pytest.raises(RuntimeError, match="judge failed"):
        with store.transaction():
            insight = _add_insight(store, "doomed")
            store.append_member(skill, insight)
            raise RuntimeError("judge failed")
    after = {t: _count(store, t) for t in ("insights", "skill_members", "snapshots")}
    assert after == before  # zero partial writes in any failure mode (R7 primitive)


def test_queue_operation_rolls_back_snapshot_on_error(store):
    insight = _add_insight(store, "unlucky")
    with pytest.raises(RuntimeError):
        with store.queue_operation("promote") as snap:
            store.set_status(insight, "active", snap)
            raise RuntimeError("validation failed")
    assert _count(store, "snapshots") == 0
    assert _count(store, "status_transitions") == 0
    assert store.get_insight(insight)["status"] == "quarantined"


def test_concurrent_immediate_writers_serialize(tmp_path):
    """Two BEGIN IMMEDIATE writers on the same DB: the second blocks, then succeeds."""
    db = tmp_path / "library.db"
    with Store(db) as setup:
        setup.migrate()
        insight = _add_insight(setup, "contended")
    a_in_txn = threading.Event()
    hold_seconds = 0.4

    def writer_a():
        # sqlite3 connections are thread-bound: each writer opens its own Store
        with Store(db, busy_timeout_ms=10_000) as s1:
            with s1.queue_operation("promote", "writer-a") as snap:
                s1.set_status(insight, "active", snap)
                a_in_txn.set()
                time.sleep(hold_seconds)

    t = threading.Thread(target=writer_a)
    t.start()
    try:
        assert a_in_txn.wait(timeout=10)
        with Store(db, busy_timeout_ms=10_000) as s2:
            start = time.monotonic()
            with s2.queue_operation("retire", "writer-b") as snap_b:
                s2.set_status(insight, "retired", snap_b)
            blocked_for = time.monotonic() - start
    finally:
        t.join()

    assert blocked_for >= hold_seconds * 0.5  # B actually waited for A's lock
    with Store(db) as check:
        rows = check.conn.execute(
            "SELECT id, parent_id, mutation_summary FROM snapshots ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["parent_id"] is None
        assert rows[1]["parent_id"] == rows[0]["id"]  # B chained after A, not forked
        assert check.get_insight(insight)["status"] == "retired"


# --- batches, merge log, contradictions, meta ----------------------------------------


def test_ensure_batch_idempotent(store):
    first = store.ensure_batch("session-2026-06-10")
    again = store.ensure_batch("session-2026-06-10")
    other = store.ensure_batch("session-other")
    assert first == again
    assert other != first


def test_merge_log_roundtrip(store):
    existing = _add_insight(store, "incumbent")
    batch = store.ensure_batch("merge-batch")
    store.insert_merge_log(
        content_hash="hash-discarded",
        structural_fields_json='{"precondition": "p", "action": "a", "expected_outcome": "o"}',
        duplicate_of=existing,
        batch_id=batch,
    )
    row = store.find_merge_log_by_hash("hash-discarded")
    assert row["duplicate_of"] == existing
    assert row["batch_id"] == batch
    assert "precondition" in row["structural_fields_json"]
    assert row["judged_at"]
    assert store.find_merge_log_by_hash("hash-never-merged") is None
    with pytest.raises(sqlite3.IntegrityError):  # duplicate_of must resolve
        store.insert_merge_log(
            content_hash="h",
            structural_fields_json="{}",
            duplicate_of=99999,
        )


def test_contradictions_accept_and_constrain(store):
    incumbent = _add_insight(store, "incumbent2", status="active")
    challenger = _add_insight(store, "challenger")
    store.conn.execute(
        "INSERT INTO contradictions (challenger_id, incumbent_id, opened_at)"
        " VALUES (?, ?, '2026-06-10T00:00:00+00:00')",
        (challenger, incumbent),
    )
    row = store.conn.execute("SELECT * FROM contradictions").fetchone()
    assert row["status"] == "open"
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE contradictions SET status = 'resolved' WHERE id = ?", (row["id"],)
        )


def test_meta_roundtrip(store):
    assert store.get_meta("embedding_model") is None
    store.set_meta("embedding_model", "nomic-ai/nomic-embed-text-v1.5")
    store.set_meta("embedding_dim", "768")
    store.set_meta("embedding_dim", "768")  # upsert is idempotent
    assert store.get_meta("embedding_model") == "nomic-ai/nomic-embed-text-v1.5"
    assert store.get_meta("embedding_dim") == "768"


# --- traceability (schema-only, R2) ----------------------------------------------


def test_traceability_tables_accept_valid_chain(store):
    c = store.conn
    with store.transaction():
        c.execute("INSERT INTO trace_feat VALUES ('FEAT-1', 'runtime-evidence-ref')")
        c.execute("INSERT INTO trace_msg VALUES ('MSG-1', 'asked about admin areas')")
        c.execute("INSERT INTO trace_msg_mentions VALUES ('MSG-1', 'FEAT-1')")
        c.execute("INSERT INTO trace_req VALUES ('REQ-1', 'MSG-1')")
        c.execute(
            "INSERT INTO trace_tkt (id, increment_id) VALUES ('TKT-1', 'INC-1')"
        )
        c.execute("INSERT INTO trace_tkt_covers VALUES ('TKT-1', 'REQ-1')")
        c.execute("INSERT INTO trace_ac VALUES ('AC-1', 'TKT-1', 'REQ-1')")
        c.execute(
            "INSERT INTO trace_span (id, ticket_id, files_json, status)"
            " VALUES ('SPAN-1', 'TKT-1', '[\"src/app.py\"]', 'completed')"
        )
        c.execute(
            "INSERT INTO trace_chk VALUES ('CHK-1', 'AC-1', 'pass',"
            " 'pytest tests/test_login.py', 'all green')"
        )
        c.execute("INSERT INTO trace_scen VALUES ('SCEN-1', 'FEAT-1', 'pass', '')")
    for table in ("trace_feat", "trace_msg", "trace_msg_mentions", "trace_req",
                  "trace_tkt", "trace_tkt_covers", "trace_ac", "trace_span",
                  "trace_chk", "trace_scen"):
        assert _count(store, table) == 1


def test_traceability_id_prefixes_constrained(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO trace_feat VALUES ('XFEAT-1', 'ref')")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_tkt (id, increment_id) VALUES ('TICKET-1', NULL)"
        )


def test_traceability_links_constrained(store):
    # mandatory links must resolve: a CHK without its AC is rejected
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_chk VALUES ('CHK-9', 'AC-404', 'fail', 'cmd', '')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO trace_req VALUES ('REQ-9', 'MSG-404')")
    # span index exists over the ticket link
    indexes = {
        r["name"]
        for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
    }
    assert "idx_trace_span_ticket" in indexes


# --- Phase 1 schema migration (plan 002 U1: R1, R2, R13, R16, R17, R20) -----------


def _add_ticket(s: Store, tkt_id: str = "TKT-1") -> str:
    s.conn.execute(
        "INSERT INTO trace_tkt (id, increment_id) VALUES (?, NULL)", (tkt_id,)
    )
    return tkt_id


def test_phase1_migration_applies_on_phase0_db_without_data_loss(tmp_path, monkeypatch):
    """Migration v2 upgrades a v1-only database in place, preserving every row."""
    db = tmp_path / "library.db"
    with Store(db) as s:
        monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS[:1])
        s.migrate()  # Phase 0 schema only
        versions = [
            r["version"]
            for r in s.conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        assert versions == [1]
        # seed Phase 0 data, including a trace_span row in the old (3-column) shape
        insight = _add_insight(s, "survivor")
        with s.queue_operation("promote") as snap:
            s.set_status(insight, "active", snap)
        s.conn.execute("INSERT INTO trace_tkt VALUES ('TKT-old', NULL)")
        s.conn.execute(
            "INSERT INTO trace_span VALUES ('SPAN-old', 'TKT-old', '[\"a.ts\"]')"
        )
    monkeypatch.undo()
    with Store(db) as s:
        s.migrate()  # applies v2 on top
        versions = [
            r["version"]
            for r in s.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2]
        assert EXPECTED_TABLES <= _table_names(s)
        # Phase 0 rows survive untouched
        assert s.get_insight(insight)["status"] == "active"
        assert s.current_snapshot_id() == snap
        span = s.conn.execute(
            "SELECT * FROM trace_span WHERE id = 'SPAN-old'"
        ).fetchone()
        assert span["ticket_id"] == "TKT-old"
        assert span["files_json"] == '["a.ts"]'
        # pre-lifecycle spans are backfilled 'completed', never flagged as orphans
        assert span["status"] == "completed"
        assert s.orphan_spans() == []
        # tickets that predate the migration carry the default status
        assert s.conn.execute(
            "SELECT status FROM trace_tkt WHERE id = 'TKT-old'"
        ).fetchone()["status"] == "pending"
        # Phase 0 integrity intact post-migration (foreign keys still resolve)
        assert s.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_run_row_roundtrip_all_terminals(store):
    assert set(RUN_TERMINAL_STATUSES) <= set(RUN_STATUSES)
    for terminal in RUN_TERMINAL_STATUSES:
        run_id = store.create_run("specs/01-trivial.md", store.current_snapshot_id())
        row = store.get_run(run_id)
        assert row["status"] == "created"
        assert row["spec_ref"] == "specs/01-trivial.md"
        assert row["snapshot_id"] == 0  # pre-mutation library snapshot is recorded
        for state in ("planning", "executing", terminal):
            store.set_run_status(run_id, state)
        assert store.get_run(run_id)["status"] == terminal
    # totals columns exist and round-trip at settlement
    store.conn.execute(
        "UPDATE runs SET total_cost_usd = 1.25, total_input_tokens = 1000,"
        " total_output_tokens = 500, total_turns = 12, total_duration_ms = 60000,"
        " cost_partial_spans = 1 WHERE id = ?",
        (run_id,),
    )
    settled = store.get_run(run_id)
    assert settled["total_cost_usd"] == 1.25
    assert settled["cost_partial_spans"] == 1


def test_run_status_enum_rejects_unknown_values(store):
    run_id = store.create_run("spec.md", 0)
    with pytest.raises(StoreError, match="unknown run status"):
        store.set_run_status(run_id, "finished")
    with pytest.raises(StoreError, match="does not exist"):
        store.set_run_status(99999, "success")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK backstops raw writes too
        store.conn.execute(
            "INSERT INTO runs (spec_ref, snapshot_id, status, created_at)"
            " VALUES ('s', 0, 'bogus', 't')"
        )


def test_ticket_status_transitions_audited(store):
    ticket = _add_ticket(store)
    run_id = store.create_run("spec.md", 0)
    assert store.conn.execute(
        "SELECT status FROM trace_tkt WHERE id = ?", (ticket,)
    ).fetchone()["status"] == "pending"
    with store.transaction():
        store.set_ticket_status(ticket, "in_progress", run_id=run_id)
        store.set_ticket_status(ticket, "escalated", run_id=run_id)
    rows = store.conn.execute(
        "SELECT * FROM ticket_status_transitions WHERE ticket_id = ? ORDER BY id",
        (ticket,),
    ).fetchall()
    assert [(r["from_status"], r["to_status"]) for r in rows] == [
        ("pending", "in_progress"),
        ("in_progress", "escalated"),
    ]
    assert all(r["run_id"] == run_id for r in rows)
    assert store.conn.execute(
        "SELECT status FROM trace_tkt WHERE id = ?", (ticket,)
    ).fetchone()["status"] == "escalated"


def test_ticket_status_enum_rejects_unknown_values(store):
    ticket = _add_ticket(store)
    assert set(TICKET_STATUSES) == {
        "pending", "in_progress", "done", "escalated", "blocked"
    }
    with store.transaction():
        with pytest.raises(StoreError, match="unknown ticket status"):
            store.set_ticket_status(ticket, "cancelled")
        with pytest.raises(StoreError, match="does not exist"):
            store.set_ticket_status("TKT-404", "done")
        store.set_ticket_status(ticket, "blocked")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK backstops raw writes too
        store.conn.execute(
            "UPDATE trace_tkt SET status = 'bogus' WHERE id = ?", (ticket,)
        )
    with pytest.raises(StoreError, match="transaction"):
        store.set_ticket_status(ticket, "done")  # multi-statement: txn required


def test_span_insert_running_then_finalize(store):
    ticket = _add_ticket(store)
    run_id = store.create_run("spec.md", 0)
    store.insert_span(
        "SPAN-w1",
        run_id=run_id,
        family="worker",
        agent="generic-worker",
        ticket_id=ticket,
        ralph_iteration=1,
        model_version="claude-opus-4-8",
        prompt_set_version="ps-abc123",
    )
    row = store.conn.execute(
        "SELECT * FROM trace_span WHERE id = 'SPAN-w1'"
    ).fetchone()
    assert row["status"] == "running"
    assert row["family"] == "worker"
    assert row["model_version"] == "claude-opus-4-8"
    assert row["prompt_set_version"] == "ps-abc123"
    assert row["episode"] is None and row["increment_id"] is None  # Phase 2 carve-out
    store.finalize_span(
        "SPAN-w1",
        "completed",
        num_turns=7,
        duration_ms=42_000,
        cost_usd=0.31,
        input_tokens=12_000,
        output_tokens=3_000,
        files_json='["src/app.tsx"]',
    )
    row = store.conn.execute(
        "SELECT * FROM trace_span WHERE id = 'SPAN-w1'"
    ).fetchone()
    assert row["status"] == "completed"
    assert row["num_turns"] == 7
    assert row["duration_ms"] == 42_000
    assert row["cost_usd"] == 0.31
    assert row["cost_partial"] == 0
    assert row["files_json"] == '["src/app.tsx"]'
    # a killed session finalizes with best-effort partial cost (R16)
    store.insert_span("SPAN-w2", run_id=run_id, ticket_id=ticket, ralph_iteration=2)
    store.finalize_span("SPAN-w2", "timeout", cost_usd=0.05, cost_partial=True)
    killed = store.conn.execute(
        "SELECT * FROM trace_span WHERE id = 'SPAN-w2'"
    ).fetchone()
    assert killed["status"] == "timeout"
    assert killed["cost_partial"] == 1
    # spans without a ticket are legal (judge and planner invocations)
    store.insert_span("SPAN-judge", run_id=run_id, family="judge")
    store.finalize_span("SPAN-judge", "completed")


def test_finalize_span_rejects_nonfinal_status_and_missing_span(store):
    store.insert_span("SPAN-x")
    with pytest.raises(StoreError, match="not a final span status"):
        store.finalize_span("SPAN-x", "running")
    with pytest.raises(StoreError, match="does not exist"):
        store.finalize_span("SPAN-404", "completed")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK backstops raw writes
        store.conn.execute(
            "UPDATE trace_span SET status = 'bogus' WHERE id = 'SPAN-x'"
        )
    assert set(SPAN_FINAL_STATUSES) == {"completed", "error", "timeout", "aborted"}


def test_orphan_query_returns_unfinalized_spans(store):
    run_a = store.create_run("spec-a.md", 0)
    run_b = store.create_run("spec-b.md", 0)
    store.insert_span("SPAN-1", run_id=run_a)
    store.insert_span("SPAN-2", run_id=run_a)
    store.insert_span("SPAN-3", run_id=run_b)
    store.finalize_span("SPAN-1", "completed")
    assert [r["id"] for r in store.orphan_spans()] == ["SPAN-2", "SPAN-3"]
    assert [r["id"] for r in store.orphan_spans(run_id=run_a)] == ["SPAN-2"]
    # resume marks orphans aborted; the query then comes back empty
    for orphan in store.orphan_spans():
        store.finalize_span(orphan["id"], "aborted")
    assert store.orphan_spans() == []


def test_failure_record_roundtrip_and_kind_enum(store):
    ticket = _add_ticket(store)
    run_id = store.create_run("spec.md", 0)
    store.insert_span("SPAN-f", run_id=run_id, ticket_id=ticket)
    record_id = store.insert_failure_record(
        failure_kind="gate_test",
        location="src/app.test.tsx:12",
        expected="login form renders",
        observed="TypeError: cannot read properties of undefined",
        repro_command="npx vitest run src/app.test.tsx",
        run_id=run_id,
        ticket_id=ticket,
        span_id="SPAN-f",
    )
    row = store.conn.execute(
        "SELECT * FROM failure_records WHERE id = ?", (record_id,)
    ).fetchone()
    assert row["failure_kind"] == "gate_test"
    assert row["repro_command"] == "npx vitest run src/app.test.tsx"
    assert row["span_id"] == "SPAN-f"
    with pytest.raises(StoreError, match="unknown failure kind"):
        store.insert_failure_record(failure_kind="vibes")
    with pytest.raises(sqlite3.IntegrityError):  # links must resolve
        store.insert_failure_record(failure_kind="gate_lint", span_id="SPAN-404")
    # the MAST taxonomy kinds the tripwire escalations emit are in the enum
    assert {"step_repetition", "incorrect_verification"} <= set(FAILURE_KINDS)


def test_tripwire_event_insert_with_model_and_dim(store):
    run_id = store.create_run("spec.md", 0)
    store.insert_span("SPAN-t", run_id=run_id)
    event_id = store.insert_tripwire_event(
        detector_kind="step_repetition",
        would_have_fired=True,
        span_id="SPAN-t",
        run_id=run_id,
        ralph_iteration=3,
        similarity=0.97,
        embedding_model="nomic-ai/nomic-embed-text-v1.5",
        embedding_dim=768,
    )
    row = store.conn.execute(
        "SELECT * FROM tripwire_events WHERE id = ?", (event_id,)
    ).fetchone()
    assert row["detector_kind"] == "step_repetition"
    assert row["would_have_fired"] == 1
    assert row["similarity"] == 0.97
    assert row["embedding_model"] == "nomic-ai/nomic-embed-text-v1.5"
    assert row["embedding_dim"] == 768
    # the no-progress detector logs a canonical failure-set hash, no embedding
    no_progress = store.insert_tripwire_event(
        detector_kind="no_progress",
        would_have_fired=False,
        run_id=run_id,
        ralph_iteration=3,
        failure_set_hash="sha256:deadbeef",
    )
    row = store.conn.execute(
        "SELECT * FROM tripwire_events WHERE id = ?", (no_progress,)
    ).fetchone()
    assert row["failure_set_hash"] == "sha256:deadbeef"
    assert row["embedding_model"] is None
    assert row["would_have_fired"] == 0


def test_ledger_entries_reference_spans_chks_and_failures(store):
    ticket = _add_ticket(store)
    run_id = store.create_run("spec.md", 0)
    store.insert_span("SPAN-l", run_id=run_id, ticket_id=ticket)
    with store.transaction():
        store.conn.execute("INSERT INTO trace_msg VALUES ('MSG-l', 'p')")
        store.conn.execute("INSERT INTO trace_req VALUES ('REQ-l', 'MSG-l')")
        store.conn.execute("INSERT INTO trace_ac VALUES ('AC-l', 'TKT-1', 'REQ-l')")
        store.conn.execute(
            "INSERT INTO trace_chk VALUES ('CHK-l', 'AC-l', 'pass', 'npm test', '')"
        )
    failure_id = store.insert_failure_record(
        failure_kind="gate_typecheck", ticket_id=ticket, run_id=run_id
    )
    entry_id = store.append_ledger_entry(
        ticket_id=ticket,
        entry_kind="gate_failure",
        run_id=run_id,
        ralph_iteration=1,
        span_id="SPAN-l",
        chk_id="CHK-l",
        failure_record_id=failure_id,
        content="tsc failed; see repro",
    )
    row = store.conn.execute(
        "SELECT * FROM ledger_entries WHERE id = ?", (entry_id,)
    ).fetchone()
    assert row["span_id"] == "SPAN-l"
    assert row["chk_id"] == "CHK-l"
    assert row["failure_record_id"] == failure_id
    # all three refs are FK-constrained
    with pytest.raises(sqlite3.IntegrityError):
        store.append_ledger_entry(
            ticket_id=ticket, entry_kind="x", span_id="SPAN-404"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.append_ledger_entry(ticket_id=ticket, entry_kind="x", chk_id="CHK-404")
    with pytest.raises(sqlite3.IntegrityError):
        store.append_ledger_entry(
            ticket_id=ticket, entry_kind="x", failure_record_id=99999
        )
    with pytest.raises(sqlite3.IntegrityError):  # and the ledger needs its ticket
        store.append_ledger_entry(ticket_id="TKT-404", entry_kind="x")


def test_msg_rows_accept_empty_mentions(store):
    """002 R20: orchestrator-synthesized MSG rows carry no FEAT mentions yet."""
    store.conn.execute(
        "INSERT INTO trace_msg VALUES ('MSG-bare', 'spec paragraph 1')"
    )
    row = store.conn.execute(
        "SELECT m.id, COUNT(mm.feat_id) AS mentions FROM trace_msg m"
        " LEFT JOIN trace_msg_mentions mm ON mm.msg_id = m.id"
        " WHERE m.id = 'MSG-bare' GROUP BY m.id"
    ).fetchone()
    assert row["mentions"] == 0  # persisted and queryable with zero mentions
