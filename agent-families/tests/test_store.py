"""Plan 001 U2: SQLite schema, store layer, snapshots, promotion queue (R1-R4).

Plan 002 U1: Phase 1 schema migration — runs, ticket lifecycle + audit, span
lifecycle/cost columns, failure records, tripwire events, ledger entries.

Plan 003 U1: Phase 2 schema migration — episodes/increments, FEAT identity
discipline, frontier, scenario manifests, Q&A log, review queue, settlement
reports, SCEN keys, idea provenance.

Plan 004 U1: Phase 3a schema spine — run-mode taxonomy + epoch, append-only
fitness-event log, episode-scoped workflows (run memory), batch-validation
records, lineage seam columns.

## Conformance (plan 003 U1 test scenarios → tests)

- migration idempotent over a Phase 1 database →
  ``test_phase2_migration_applies_on_phase1_db_without_data_loss``
- FEAT append-only constraint (update of ID rejected, status flip allowed) →
  ``test_feat_append_only_id_immutable_status_flippable``
- mentions FK rejects unconfirmed FEAT →
  ``test_mentions_fk_rejects_unconfirmed_feat``
- run acceptance enum → ``test_run_acceptance_enum``
- episode suspension round-trip → ``test_episode_suspension_roundtrip``
- SCEN row requires episode + snapshot keys →
  ``test_scen_row_requires_episode_and_snapshot_keys``

## Conformance (plan 004 U1 test scenarios → tests)

- mode enum rejects unknowns →
  ``test_episode_mode_enum_rejects_unknowns``
- migration idempotent over a Phase 2 database →
  ``test_phase3a_migration_applies_on_phase2_db_without_data_loss``
- fitness events append-only and reconstructible per (insight, snapshot) →
  ``test_fitness_events_append_only_and_reconstructible``
- trial-mode events excluded from a training-mode fitness query →
  ``test_trial_mode_fitness_excluded_from_training_query``
- workflows rows die with episode settlement →
  ``test_workflows_die_with_episode_settlement``
- batch-validation record round-trip → ``test_batch_validation_roundtrip``
- epoch nullable → ``test_episode_epoch_nullable``
- lineage seam columns present → ``test_lineage_seam_columns_present``
"""

from __future__ import annotations

import sqlite3
import threading
import time

import pytest

import agent_families.store as store_mod
from agent_families.store import (
    ASSUME_RISKS,
    ASSUME_STATUSES,
    BATCH_VERDICTS,
    DEC_STATUSES,
    EPISODE_STATUSES,
    EPISODE_TERMINAL_STATUSES,
    FAILURE_KINDS,
    FEAT_STATUSES,
    FITNESS_EVENT_KINDS,
    FOUNDER_KNOWLEDGE_STATES,
    FRONTIER_STATUSES,
    INSIGHT_PROVENANCES,
    QA_OUTCOMES,
    RUN_ACCEPTANCE,
    RUN_MODES,
    RUN_STATUSES,
    RUN_TERMINAL_STATUSES,
    SCEN_JUDGE_MODES,
    SCENARIO_TIERS,
    SPAN_FINAL_STATUSES,
    STATUSES,
    TICKET_STATUSES,
    VALIDATION_CLASSES,
    WORKFLOW_STATUSES,
    WORLDS,
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
    # Phase 2 (plan 003 U1)
    "episodes",
    "frontier",
    "scenario_manifests",
    "qa_log",
    "review_queue",
    "settlement_reports",
    # Phase 3a (plan 004 U1)
    "fitness_events",
    "workflows",
    "batch_validations",
    # Greenfield mode (plan 007 U1)
    "trace_dec",
    "trace_msg_dec_mentions",
    "trace_assume",
    "trace_proposal",
    "trace_proposal_adjudication",
    "founder_blur_cache",
    "founder_models",
    "founder_knowledge",
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
        assert [r["version"] for r in versions] == [1, 2, 3, 4, 5]
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
    episode = store.create_episode("linkding", "sha256:abc", 0)
    with store.transaction():
        c.execute(
            "INSERT INTO trace_feat (id, evidence_ref)"
            " VALUES ('FEAT-1', 'runtime-evidence-ref')"
        )
        c.execute("INSERT INTO trace_msg VALUES ('MSG-1', 'asked about admin areas')")
        c.execute("INSERT INTO trace_msg_mentions VALUES ('MSG-1', 'FEAT-1')")
        c.execute(
            "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-1', 'MSG-1')"
        )
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
        c.execute(
            "INSERT INTO trace_scen (id, feat_id, result, evidence,"
            " episode_id, snapshot_id) VALUES ('SCEN-1', 'FEAT-1', 'pass', '', ?, 0)",
            (episode,),
        )
    for table in ("trace_feat", "trace_msg", "trace_msg_mentions", "trace_req",
                  "trace_tkt", "trace_tkt_covers", "trace_ac", "trace_span",
                  "trace_chk", "trace_scen"):
        assert _count(store, table) == 1


def test_traceability_id_prefixes_constrained(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_feat (id, evidence_ref) VALUES ('XFEAT-1', 'ref')"
        )
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
        store.conn.execute(
            "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-9', 'MSG-404')"
        )
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
        # seed Phase 0 data in the v1 shape (insert_insight targets the current
        # schema, so the pre-migration row is written with the v1 column list)
        insight = s.conn.execute(
            "INSERT INTO insights (precondition, action, expected_outcome,"
            " content_hash, created_at) VALUES ('p', 'a', 'o', 'hash-survivor-v1',"
            " '2026-06-10T00:00:00+00:00')"
        ).lastrowid
        with s.queue_operation("promote") as snap:
            s.set_status(insight, "active", snap)
        s.conn.execute("INSERT INTO trace_tkt VALUES ('TKT-old', NULL)")
        s.conn.execute(
            "INSERT INTO trace_span VALUES ('SPAN-old', 'TKT-old', '[\"a.ts\"]')"
        )
    monkeypatch.undo()
    with Store(db) as s:
        s.migrate()  # applies v2 (and v3+) on top
        versions = [
            r["version"]
            for r in s.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5]
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
        store.conn.execute(
            "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-l', 'MSG-l')"
        )
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


# --- Phase 2 schema migration (plan 003 U1: R1, R3, R9, R10, R22, R23, R27) -------


def _add_feat(s: Store, feat_id: str = "FEAT-1", digest: str = "sha256:abc") -> str:
    s.conn.execute(
        "INSERT INTO trace_feat (id, evidence_ref, target, digest)"
        " VALUES (?, 'a11y-snapshot-ref', 'linkding', ?)",
        (feat_id, digest),
    )
    return feat_id


def test_phase2_migration_applies_on_phase1_db_without_data_loss(tmp_path, monkeypatch):
    """Migration v3 upgrades a v1+v2 database in place, idempotently."""
    db = tmp_path / "library.db"
    with Store(db) as s:
        monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS[:2])
        s.migrate()  # Phase 0 + 1 schema only
        # seed Phase 1 data, including FEAT/SCEN rows in the pre-Phase-2 shapes
        # (raw insert: insert_insight targets the current, post-v3 column list)
        insight = s.conn.execute(
            "INSERT INTO insights (precondition, action, expected_outcome,"
            " content_hash, created_at) VALUES ('p', 'a', 'o', 'hash-survivor-v2',"
            " '2026-06-10T00:00:00+00:00')"
        ).lastrowid
        run_id = s.conn.execute(
            "INSERT INTO runs (spec_ref, snapshot_id, status, created_at)"
            " VALUES ('specs/01-trivial.md', 0, 'created',"
            " '2026-06-10T00:00:00+00:00')"
        ).lastrowid
        s.insert_span("SPAN-old", run_id=run_id)
        s.finalize_span("SPAN-old", "completed")
        s.conn.execute(
            "INSERT INTO trace_feat VALUES ('FEAT-old', 'old-evidence')"
        )
        s.conn.execute(
            "INSERT INTO trace_scen VALUES ('SCEN-old', 'FEAT-old', 'pass', '')"
        )
    monkeypatch.undo()
    with Store(db) as s:
        s.migrate()  # applies v3 on top
        s.migrate()  # second run applies nothing
        versions = [
            r["version"]
            for r in s.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5]
        assert EXPECTED_TABLES <= _table_names(s)
        # Phase 0/1 rows survive untouched; new columns backfill their defaults
        assert s.get_insight(insight)["episode_id"] is None
        run = s.get_run(run_id)
        assert run["episode_id"] is None
        assert run["increment_index"] is None
        assert run["acceptance"] is None
        feat = s.conn.execute(
            "SELECT * FROM trace_feat WHERE id = 'FEAT-old'"
        ).fetchone()
        assert feat["status"] == "confirmed"  # pre-Phase-2 rows backfill confirmed
        assert feat["digest"] is None  # no image pin existed when it was minted
        scen = s.conn.execute(
            "SELECT * FROM trace_scen WHERE id = 'SCEN-old'"
        ).fetchone()
        assert scen["result"] == "pass"
        assert scen["episode_id"] is None  # legacy row survives the key trigger
        assert s.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_feat_append_only_id_immutable_status_flippable(store):
    feat = _add_feat(store)
    row = store.conn.execute(
        "SELECT * FROM trace_feat WHERE id = ?", (feat,)
    ).fetchone()
    assert row["status"] == "confirmed"
    assert row["digest"] == "sha256:abc"
    assert row["target"] == "linkding"
    # IDs are append-only: renumbering is rejected by trigger (003 R9)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(
            "UPDATE trace_feat SET id = 'FEAT-2' WHERE id = ?", (feat,)
        )
    # rows are never deleted: removed features deprecate instead
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.conn.execute("DELETE FROM trace_feat WHERE id = ?", (feat,))
    store.conn.execute(
        "UPDATE trace_feat SET status = 'deprecated' WHERE id = ?", (feat,)
    )
    assert store.conn.execute(
        "SELECT status FROM trace_feat WHERE id = ?", (feat,)
    ).fetchone()["status"] == "deprecated"
    with pytest.raises(sqlite3.IntegrityError):  # status enum backstops raw writes
        store.conn.execute(
            "UPDATE trace_feat SET status = 'removed' WHERE id = ?", (feat,)
        )
    assert set(FEAT_STATUSES) == {"confirmed", "deprecated"}


def test_mentions_fk_rejects_unconfirmed_feat(store):
    """003 R10: a feature is mentionable only after confirmation mints its row."""
    store.conn.execute("INSERT INTO trace_msg VALUES ('MSG-q', 'does tagging work?')")
    # unconfirmed features have no FEAT row at all, so the FK rejects the mention
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_msg_mentions VALUES ('MSG-q', 'FEAT-unconfirmed')"
        )
    # the grader-side confirmation step mints the row; only then is it mentionable
    _add_feat(store, "FEAT-unconfirmed")
    store.conn.execute(
        "INSERT INTO trace_msg_mentions VALUES ('MSG-q', 'FEAT-unconfirmed')"
    )
    assert _count(store, "trace_msg_mentions") == 1


def test_run_acceptance_enum(store):
    episode = store.create_episode("linkding", "sha256:abc", 0)
    run_id = store.create_run(
        "episode-increment", 0, episode_id=episode, increment_index=1
    )
    run = store.get_run(run_id)
    assert run["episode_id"] == episode
    assert run["increment_index"] == 1
    assert run["acceptance"] is None  # NULL until the UAT stage runs (003 R2)
    for verdict in RUN_ACCEPTANCE:
        store.set_run_acceptance(run_id, verdict)
        assert store.get_run(run_id)["acceptance"] == verdict
    assert set(RUN_ACCEPTANCE) == {"accepted", "rejected"}
    with pytest.raises(StoreError, match="unknown acceptance"):
        store.set_run_acceptance(run_id, "maybe")
    with pytest.raises(StoreError, match="does not exist"):
        store.set_run_acceptance(99999, "accepted")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK backstops raw writes too
        store.conn.execute(
            "UPDATE runs SET acceptance = 'shipped' WHERE id = ?", (run_id,)
        )
    with pytest.raises(sqlite3.IntegrityError):  # episode FK must resolve
        store.create_run("spec.md", 0, episode_id=99999)


def test_episode_suspension_roundtrip(store):
    episode = store.create_episode(
        "linkding", "sha256:abc", 0, max_increments=5, cost_ceiling_usd=20.0
    )
    row = store.get_episode(episode)
    assert row["status"] == "created"
    assert row["target"] == "linkding"
    assert row["digest"] == "sha256:abc"
    assert row["snapshot_id"] == 0
    assert row["max_increments"] == 5  # budget fields stamped at creation (003 R4)
    assert row["cost_ceiling_usd"] == 20.0
    assert row["settled_at"] is None
    # suspension is resumable, never terminal (003 R3): quota suspends, the
    # episode resumes mid-stream, and only then reaches a real terminal
    for status in ("running", "suspended", "running", "budget_spent"):
        store.set_episode_status(episode, status)
        assert store.get_episode(episode)["status"] == status
    assert set(EPISODE_TERMINAL_STATUSES) == {
        "frontier_exhausted", "budget_spent", "aborted_error"
    }
    assert "suspended" in EPISODE_STATUSES
    assert "suspended" not in EPISODE_TERMINAL_STATUSES
    with pytest.raises(StoreError, match="unknown episode status"):
        store.set_episode_status(episode, "finished")
    with pytest.raises(StoreError, match="does not exist"):
        store.set_episode_status(99999, "running")
    with pytest.raises(sqlite3.IntegrityError):  # CHECK backstops raw writes too
        store.conn.execute(
            "INSERT INTO episodes (target, digest, snapshot_id, status, created_at)"
            " VALUES ('t', 'd', 0, 'bogus', 'now')"
        )


def test_scen_row_requires_episode_and_snapshot_keys(store):
    feat = _add_feat(store)
    episode = store.create_episode("linkding", "sha256:abc", 0)
    # missing either key is rejected at insert (003 R22)
    with pytest.raises(sqlite3.IntegrityError, match="episode_id and snapshot_id"):
        store.conn.execute(
            "INSERT INTO trace_scen (id, feat_id, result, evidence)"
            " VALUES ('SCEN-nokeys', ?, 'pass', '')",
            (feat,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="episode_id and snapshot_id"):
        store.conn.execute(
            "INSERT INTO trace_scen (id, feat_id, result, evidence, episode_id)"
            " VALUES ('SCEN-nosnap', ?, 'pass', '', ?)",
            (feat, episode),
        )
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, result, evidence, episode_id,"
        " snapshot_id, tier, judge_mode, judge_metadata_json, judge_input_json)"
        " VALUES ('SCEN-keyed', ?, 'pass', 'evidence-ref', ?, 0, 'must', 'panel',"
        " '{\"votes\": 3}', '{\"a11y_diff\": \"...\"}')",
        (feat, episode),
    )
    row = store.conn.execute(
        "SELECT * FROM trace_scen WHERE id = 'SCEN-keyed'"
    ).fetchone()
    assert row["episode_id"] == episode
    assert row["snapshot_id"] == 0
    assert row["tier"] == "must"
    assert row["judge_mode"] == "panel"
    # the judge-input payload persists — replay re-judging needs the original
    # inputs, not just screenshots (003 R22)
    assert "a11y_diff" in row["judge_input_json"]
    assert set(SCENARIO_TIERS) == {"must", "should", "free"}
    assert set(SCEN_JUDGE_MODES) == {"deterministic", "single", "panel"}
    with pytest.raises(sqlite3.IntegrityError):  # tier enum backstops raw writes
        store.conn.execute(
            "INSERT INTO trace_scen (id, feat_id, episode_id, snapshot_id, tier)"
            " VALUES ('SCEN-badtier', ?, ?, 0, 'optional')",
            (feat, episode),
        )


def test_insight_provenance_columns_roundtrip(store):
    """003 R27: add_idea provenance — required episode, optional evidence refs."""
    feat = _add_feat(store)
    episode = store.create_episode("linkding", "sha256:abc", 0)
    _add_ticket(store, "TKT-ev")
    store.conn.execute(
        "INSERT INTO trace_scen (id, feat_id, episode_id, snapshot_id)"
        " VALUES ('SCEN-ev', ?, ?, 0)",
        (feat, episode),
    )
    insight = _add_insight(
        store,
        "provenanced",
        episode_id=episode,
        evidence_scenario_id="SCEN-ev",
        evidence_ticket_id="TKT-ev",
    )
    row = store.get_insight(insight)
    assert row["episode_id"] == episode
    assert row["evidence_scenario_id"] == "SCEN-ev"
    assert row["evidence_ticket_id"] == "TKT-ev"
    # provenance-free registration still works (Phase 0/1 ideas)
    bare = _add_insight(store, "bare")
    assert store.get_insight(bare)["episode_id"] is None
    # all three refs are FK-constrained
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "dangling-episode", episode_id=99999)
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "dangling-scen", evidence_scenario_id="SCEN-404")
    with pytest.raises(sqlite3.IntegrityError):
        _add_insight(store, "dangling-tkt", evidence_ticket_id="TKT-404")


def test_phase2_support_tables_roundtrip(store):
    """Frontier, scenario manifests, Q&A log, review queue, settlement reports."""
    feat = _add_feat(store)
    episode = store.create_episode("linkding", "sha256:abc", 0)
    # frontier rows default unexplored with zero investigations (003 R10)
    store.conn.execute("INSERT INTO frontier (feat_id) VALUES (?)", (feat,))
    row = store.conn.execute(
        "SELECT * FROM frontier WHERE feat_id = ?", (feat,)
    ).fetchone()
    assert row["status"] == "unexplored"
    assert row["investigation_count"] == 0
    assert row["force_scheduled"] == 0
    for status in FRONTIER_STATUSES:
        store.conn.execute(
            "UPDATE frontier SET status = ? WHERE feat_id = ?", (status, feat)
        )
    with pytest.raises(sqlite3.IntegrityError):  # status enum
        store.conn.execute(
            "UPDATE frontier SET status = 'done' WHERE feat_id = ?", (feat,)
        )
    with pytest.raises(sqlite3.IntegrityError):  # FEAT FK must resolve
        store.conn.execute("INSERT INTO frontier (feat_id) VALUES ('FEAT-404')")
    # scenario manifests carry tier + archive status (003 R16, R9 deprecation)
    store.conn.execute(
        "INSERT INTO scenario_manifests (feat_id, tier, manifest_json, created_at)"
        " VALUES (?, 'must', '{\"steps\": []}', 'now')",
        (feat,),
    )
    store.conn.execute(
        "UPDATE scenario_manifests SET status = 'archived' WHERE feat_id = ?", (feat,)
    )
    with pytest.raises(sqlite3.IntegrityError):  # tier enum
        store.conn.execute(
            "INSERT INTO scenario_manifests (feat_id, tier, manifest_json,"
            " created_at) VALUES (?, 'bonus', '{}', 'now')",
            (feat,),
        )
    # qa_log: typed outcomes + budget accounting (003 R12/R13)
    store.conn.execute(
        "INSERT INTO qa_log (episode_id, question, checker_verdict, retries,"
        " outcome, budget_counted, created_at)"
        " VALUES (?, 'is search fuzzy?', 'fail', 3, 'answer_unavailable', 0, 'now')",
        (episode,),
    )
    qa = store.conn.execute("SELECT * FROM qa_log").fetchone()
    assert qa["outcome"] == "answer_unavailable"
    assert qa["budget_counted"] == 0  # refunded slot (003 R12)
    assert set(QA_OUTCOMES) == {"answered", "answer_unavailable", "budget_exhausted"}
    with pytest.raises(sqlite3.IntegrityError):  # outcome enum
        store.conn.execute(
            "INSERT INTO qa_log (episode_id, question, outcome, created_at)"
            " VALUES (?, 'q', 'gave_up', 'now')",
            (episode,),
        )
    # review queue rows open against the failed Q&A tuple (003 R12)
    store.conn.execute(
        "INSERT INTO review_queue (episode_id, qa_log_id, kind, created_at)"
        " VALUES (?, ?, 'answer_unavailable', 'now')",
        (episode, qa["id"]),
    )
    assert store.conn.execute(
        "SELECT status FROM review_queue"
    ).fetchone()["status"] == "open"
    # settlement reports: exactly one per episode (003 R23)
    store.conn.execute(
        "INSERT INTO settlement_reports (episode_id, score, report_json, created_at)"
        " VALUES (?, 0.85, '{\"tiers\": {}}', 'now')",
        (episode,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO settlement_reports (episode_id, created_at)"
            " VALUES (?, 'now')",
            (episode,),
        )


# --- Phase 3a schema spine (plan 004 U1: R1) --------------------------------


def test_phase3a_migration_applies_on_phase2_db_without_data_loss(
    tmp_path, monkeypatch
):
    """Migration v4 upgrades a v1+v2+v3 database in place, idempotently."""
    db = tmp_path / "library.db"
    with Store(db) as s:
        monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS[:3])
        s.migrate()  # Phase 0 + 1 + 2 schema only
        versions = [
            r["version"]
            for r in s.conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        assert versions == [1, 2, 3]
        # seed a Phase 2 episode in the pre-Phase-3a shape (no mode/epoch columns)
        episode = s.conn.execute(
            "INSERT INTO episodes (target, digest, snapshot_id, status, created_at)"
            " VALUES ('linkding', 'sha256:abc', 0, 'frontier_exhausted',"
            " '2026-06-10T00:00:00+00:00')"
        ).lastrowid
        insight = s.conn.execute(
            "INSERT INTO insights (precondition, action, expected_outcome,"
            " content_hash, created_at) VALUES ('p', 'a', 'o', 'hash-survivor-v3',"
            " '2026-06-10T00:00:00+00:00')"
        ).lastrowid
        family = s.create_family("worker")
        agent = s.create_agent(family, "generic-worker")
        skill = s.create_skill(agent, "elicitation")
    monkeypatch.undo()
    with Store(db) as s:
        s.migrate()  # applies v4 on top
        s.migrate()  # second run applies nothing
        versions = [
            r["version"]
            for r in s.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5]
        assert EXPECTED_TABLES <= _table_names(s)
        # pre-Phase-3a episodes backfill mode='training' with a NULL epoch
        ep = s.get_episode(episode)
        assert ep["mode"] == "training"
        assert ep["epoch"] is None
        # lineage seam columns backfill their defaults on existing rows
        agent_row = s.conn.execute(
            "SELECT * FROM agents WHERE id = ?", (agent,)
        ).fetchone()
        assert agent_row["routing_decisions"] == 0
        assert agent_row["lineage_status"] is None
        skill_row = s.conn.execute(
            "SELECT * FROM skills WHERE id = ?", (skill,)
        ).fetchone()
        assert skill_row["parent_skill_id"] is None
        assert skill_row["split_snapshot_id"] is None
        assert s.get_insight(insight)["status"] == "quarantined"
        assert s.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_episode_mode_enum_rejects_unknowns(store):
    """004 R1: the run-mode taxonomy is `training | trial | benchmark`."""
    assert set(RUN_MODES) == {"training", "trial", "benchmark"}
    for mode in RUN_MODES:
        episode = store.create_episode("linkding", "sha256:abc", 0, mode=mode)
        assert store.get_episode(episode)["mode"] == mode
    # the helper rejects unknowns with an actionable message
    with pytest.raises(StoreError, match="unknown run mode"):
        store.create_episode("linkding", "sha256:abc", 0, mode="prod")
    # and the CHECK backstops raw writes too
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO episodes (target, digest, snapshot_id, status, mode,"
            " created_at) VALUES ('t', 'd', 0, 'created', 'shadow', 'now')"
        )


def test_episode_epoch_nullable(store):
    """004 R1: epoch is nullable here (Plan 5 populates the curriculum rotation)."""
    default = store.create_episode("linkding", "sha256:abc", 0)
    assert store.get_episode(default)["epoch"] is None
    stamped = store.create_episode("linkding", "sha256:abc", 0, epoch=3)
    assert store.get_episode(stamped)["epoch"] == 3


def test_fitness_events_append_only_and_reconstructible(store):
    """004 R1/R19: the log is append-only; state at S is COUNT over events <= S."""
    insight = _add_insight(store, "fit")
    e1 = store.create_episode("linkding", "sha256:abc", 0)
    # events accrue across two snapshots; no snapshot is minted on a fitness write
    snaps_before = _count(store, "snapshots")
    store.record_fitness_event(insight, "retrieval", "training", 1, episode_id=e1)
    store.record_fitness_event(insight, "retrieval", "training", 1, episode_id=e1)
    store.record_fitness_event(insight, "win", "training", 1, episode_id=e1)
    store.record_fitness_event(insight, "loss", "training", 3, episode_id=e1)
    assert _count(store, "snapshots") == snaps_before  # writes mint nothing (R1)
    # reconstruct fitness as of each snapshot
    at1 = store.fitness_counts(insight, snapshot_id=1)
    assert at1 == {"retrieval": 2, "win": 1, "loss": 0}
    at3 = store.fitness_counts(insight, snapshot_id=3)
    assert at3 == {"retrieval": 2, "win": 1, "loss": 1}
    # unbounded query counts everything in the channel
    assert store.fitness_counts(insight) == {"retrieval": 2, "win": 1, "loss": 1}
    # append-only: the row can never be edited or deleted (immutability triggers)
    event_id = store.conn.execute(
        "SELECT id FROM fitness_events LIMIT 1"
    ).fetchone()["id"]
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(
            "UPDATE fitness_events SET kind = 'win' WHERE id = ?", (event_id,)
        )
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("DELETE FROM fitness_events WHERE id = ?", (event_id,))
    # enum guards on both kind and mode
    assert set(FITNESS_EVENT_KINDS) == {"retrieval", "win", "loss"}
    with pytest.raises(StoreError, match="unknown fitness event kind"):
        store.record_fitness_event(insight, "draw", "training", 1)
    with pytest.raises(StoreError, match="unknown run mode"):
        store.record_fitness_event(insight, "win", "prod", 1)
    with pytest.raises(sqlite3.IntegrityError):  # insight FK must resolve
        store.record_fitness_event(99999, "win", "training", 1)


def test_trial_mode_fitness_excluded_from_training_query(store):
    """004 R19: trial/benchmark events land in a channel the ratchet never reads."""
    insight = _add_insight(store, "channels")
    store.record_fitness_event(insight, "win", "training", 1)
    store.record_fitness_event(insight, "win", "trial", 1)
    store.record_fitness_event(insight, "win", "benchmark", 1)
    store.record_fitness_event(insight, "loss", "trial", 1)
    # the training-mode (ratchet) query sees only training events
    assert store.fitness_counts(insight, mode="training") == {
        "retrieval": 0, "win": 1, "loss": 0
    }
    # the validation channel reads its own modes, kept separate from the ratchet
    assert store.fitness_counts(insight, mode="trial") == {
        "retrieval": 0, "win": 1, "loss": 1
    }
    assert store.fitness_counts(insight, mode="benchmark") == {
        "retrieval": 0, "win": 1, "loss": 0
    }
    with pytest.raises(StoreError, match="unknown run mode"):
        store.fitness_counts(insight, mode="prod")


def test_workflows_die_with_episode_settlement(store):
    """004 R6: run memory is episode-scoped — settlement kills the live rows."""
    e1 = store.create_episode("linkding", "sha256:abc", 0)
    e2 = store.create_episode("linkding", "sha256:abc", 0)
    run = store.create_run("inc", 0, episode_id=e1, increment_index=1)
    ticket = _add_ticket(store, "TKT-wf")
    wf = store.insert_workflow(
        e1,
        precondition="after creating a bookmark",
        action="assert it appears in the list without reload",
        expected_outcome="optimistic UI update verified",
        run_id=run,
        source_ticket_id=ticket,
    )
    other = store.insert_workflow(
        e2, precondition="p", action="a", expected_outcome="o"
    )
    row = store.conn.execute(
        "SELECT * FROM workflows WHERE id = ?", (wf,)
    ).fetchone()
    assert row["status"] == "live"
    assert row["source_ticket_id"] == ticket
    assert [r["id"] for r in store.live_workflows(e1)] == [wf]
    # settlement kills only this episode's run memory; siblings are untouched
    killed = store.settle_workflows(e1)
    assert killed == 1
    assert store.live_workflows(e1) == []
    assert [r["id"] for r in store.live_workflows(e2)] == [other]
    # the dead row persists (auditable) but is no longer injectable
    assert store.conn.execute(
        "SELECT status FROM workflows WHERE id = ?", (wf,)
    ).fetchone()["status"] == "dead"
    assert set(WORKFLOW_STATUSES) == {"live", "dead"}
    with pytest.raises(sqlite3.IntegrityError):  # status enum backstops raw writes
        store.conn.execute(
            "INSERT INTO workflows (episode_id, precondition, action,"
            " expected_outcome, status, created_at)"
            " VALUES (?, 'p', 'a', 'o', 'zombie', 'now')",
            (e1,),
        )
    with pytest.raises(sqlite3.IntegrityError):  # episode FK must resolve
        store.insert_workflow(99999, precondition="p", action="a",
                              expected_outcome="o")


def test_batch_validation_roundtrip(store):
    """004 R15-R17: batch ↔ trial/benchmark episode refs ↔ verdict."""
    batch = store.ensure_batch("batch-2026-06-10")
    trial = store.create_episode("linkding", "sha256:abc", 5, mode="trial")
    benchmark = store.create_episode("kanboard", "sha256:def", 5, mode="benchmark")
    vid = store.insert_batch_validation(
        batch,
        snapshot_id=5,
        trial_episode_id=trial,
        benchmark_episode_id=benchmark,
        bootstrap=True,
    )
    row = store.get_batch_validation(vid)
    assert row["batch_id"] == batch
    assert row["snapshot_id"] == 5
    assert row["trial_episode_id"] == trial
    assert row["benchmark_episode_id"] == benchmark
    assert row["verdict"] is None  # undecided until the decision rule runs
    assert row["bootstrap"] == 1
    assert row["replay_miss"] == 0
    # the decision rule writes the verdict (+ replay_miss flag + bootstrap co-sign)
    store.set_batch_verdict(vid, "promote", replay_miss=True, cosigned_by="human")
    decided = store.get_batch_validation(vid)
    assert decided["verdict"] == "promote"
    assert decided["replay_miss"] == 1
    assert decided["cosigned_by"] == "human"
    assert set(BATCH_VERDICTS) == {"promote", "revert"}
    with pytest.raises(StoreError, match="unknown batch verdict"):
        store.insert_batch_validation(batch, verdict="maybe")
    with pytest.raises(StoreError, match="unknown batch verdict"):
        store.set_batch_verdict(vid, "shipped")
    with pytest.raises(StoreError, match="does not exist"):
        store.set_batch_verdict(99999, "revert")
    with pytest.raises(sqlite3.IntegrityError):  # batch FK must resolve
        store.insert_batch_validation(99999)
    with pytest.raises(sqlite3.IntegrityError):  # verdict enum backstops raw writes
        store.conn.execute(
            "INSERT INTO batch_validations (batch_id, verdict, created_at)"
            " VALUES (?, 'rollback', 'now')",
            (batch,),
        )


def test_lineage_seam_columns_present(store):
    """004 R1: lineage seam columns exist (no writer yet — Plan 4 U8 / Plan 5)."""
    family = store.create_family("worker")
    agent = store.create_agent(family, "generic-worker")
    skill = store.create_skill(agent, "elicitation")
    agent_cols = {
        r["name"]
        for r in store.conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    assert {"routing_decisions", "lineage_status"} <= agent_cols
    skill_cols = {
        r["name"]
        for r in store.conn.execute("PRAGMA table_info(skills)").fetchall()
    }
    assert {"parent_skill_id", "split_snapshot_id"} <= skill_cols
    # the seam carries data: a split child references its parent + the snapshot
    child = store.create_skill(agent, "elicitation-probing")
    with store.queue_operation("split") as snap:
        store.conn.execute(
            "UPDATE skills SET parent_skill_id = ?, split_snapshot_id = ?"
            " WHERE id = ?",
            (skill, snap, child),
        )
    child_row = store.conn.execute(
        "SELECT * FROM skills WHERE id = ?", (child,)
    ).fetchone()
    assert child_row["parent_skill_id"] == skill
    assert child_row["split_snapshot_id"] == snap
    # routing_decisions feeds Plan 5's min_routing_decisions split gate
    store.conn.execute(
        "UPDATE agents SET routing_decisions = 128 WHERE id = ?", (agent,)
    )
    assert store.conn.execute(
        "SELECT routing_decisions FROM agents WHERE id = ?", (agent,)
    ).fetchone()["routing_decisions"] == 128


# --- Greenfield schema migration (plan 007 U1: R1, R2, R7, R10) --------------
#
# ## Conformance (plan 007 U1 test scenarios → tests)
#
# - fresh store has all greenfield tables; migration preserves counts and
#   backfills provenance/world →
#   ``test_greenfield_migration_applies_on_phase3b_db_without_data_loss``
# - re-running migrations is a no-op (idempotent) →
#   ``test_schema_creates_idempotently`` (versions == [1, 2, 3, 4, 5])
# - a REQ insert with both or neither source fails the CHECK; exactly one passes →
#   ``test_trace_req_source_xor_check``
# - trace_msg_dec_mentions FK rejects an unminted DEC →
#   ``test_msg_dec_mentions_fk_rejects_unminted_dec``
# - DEC identity discipline (id immutable, never deleted, status enum) →
#   ``test_trace_dec_append_only_discipline``
# - episodes.world enum + brownfield default →
#   ``test_episode_world_enum_and_default``
# - new greenfield tables round-trip →
#   ``test_greenfield_tables_roundtrip``
# - batches.validation_class enum + general default →
#   ``test_batch_validation_class_enum_and_default``


def test_greenfield_migration_applies_on_phase3b_db_without_data_loss(
    tmp_path, monkeypatch
):
    """Migration v5 upgrades a v1..v4 database in place, idempotently, and
    backfills insight provenance from batch labels + episode world to brownfield.
    """
    db = tmp_path / "library.db"
    with Store(db) as s:
        monkeypatch.setattr(store_mod, "MIGRATIONS", store_mod.MIGRATIONS[:4])
        s.migrate()  # Phase 0 + 1 + 2 + 3a schema only
        versions = [
            r["version"]
            for r in s.conn.execute("SELECT version FROM schema_migrations").fetchall()
        ]
        assert versions == [1, 2, 3, 4]
        episode = s.create_episode("linkding", "sha256:abc", 0)
        # a reflector-mined insight and a hand-entered one, distinguished only by
        # their batch label (the explicit backfill predicate)
        reflect_batch = s.ensure_batch("reflect-ep7")
        manual_batch = s.ensure_batch("session-2026-06-10")
        reflected = _add_insight(s, "mined", batch_id=reflect_batch)
        manual = _add_insight(s, "by-hand", batch_id=manual_batch)
        # the v1 trace_req shape (two positional columns) still holds pre-rebuild
        s.conn.execute("INSERT INTO trace_msg VALUES ('MSG-keep', 'keep me')")
        s.conn.execute(
            "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-keep', 'MSG-keep')"
        )
        req_count_before = _count(s, "trace_req")
    monkeypatch.undo()
    with Store(db) as s:
        s.migrate()  # applies v5 on top
        s.migrate()  # second run applies nothing
        versions = [
            r["version"]
            for r in s.conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        ]
        assert versions == [1, 2, 3, 4, 5]
        assert EXPECTED_TABLES <= _table_names(s)
        # provenance backfilled from the batch label predicate
        assert s.get_insight(reflected)["provenance"] == "reflector"
        assert s.get_insight(manual)["provenance"] == "manual"
        # episode world defaults to brownfield on pre-007 rows
        assert s.get_episode(episode)["world"] == "brownfield"
        # the trace_req rebuild preserved the row and its FK still resolves
        assert _count(s, "trace_req") == req_count_before
        kept = s.conn.execute(
            "SELECT * FROM trace_req WHERE id = 'REQ-keep'"
        ).fetchone()
        assert kept["source_msg_id"] == "MSG-keep"
        assert kept["source_assume_id"] is None
        # the inbound FKs (trace_tkt_covers, trace_ac → trace_req) survived intact
        assert s.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_trace_req_source_xor_check(store):
    """007 KTD2: every REQ traces to exactly one of a MSG or an ASSUME."""
    store.conn.execute("INSERT INTO trace_msg VALUES ('MSG-x', 'a question')")
    run_id = store.create_run("spec.md", 0)
    store.conn.execute(
        "INSERT INTO trace_assume (id, run_id, claim) VALUES ('ASSUME-x', ?, 'c')",
        (run_id,),
    )
    # MSG-sourced REQ passes
    store.conn.execute(
        "INSERT INTO trace_req (id, source_msg_id) VALUES ('REQ-msg', 'MSG-x')"
    )
    # ASSUME-sourced REQ passes
    store.conn.execute(
        "INSERT INTO trace_req (id, source_assume_id) VALUES ('REQ-asm', 'ASSUME-x')"
    )
    # neither source fails the exactly-one CHECK
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO trace_req (id) VALUES ('REQ-none')")
    # both sources fail the exactly-one CHECK
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_req (id, source_msg_id, source_assume_id)"
            " VALUES ('REQ-both', 'MSG-x', 'ASSUME-x')"
        )
    # both REQ rows persisted; the source-FKs are still enforced
    assert _count(store, "trace_req") == 2
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_req (id, source_assume_id)"
            " VALUES ('REQ-bad', 'ASSUME-404')"
        )


def test_msg_dec_mentions_fk_rejects_unminted_dec(store):
    """007 KTD1: an unminted DEC is unmentionable mechanically (per-kind FK)."""
    store.conn.execute("INSERT INTO trace_msg VALUES ('MSG-d', 'how are deletes?')")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_msg_dec_mentions VALUES ('MSG-d', 'DEC-unminted')"
        )
    # confirmation mints the row; only then is it mentionable
    store.conn.execute(
        "INSERT INTO trace_dec (id, target, digest, category, description,"
        " evidence_ref) VALUES ('DEC-1', 'linkding', 'sha256:abc', 'deletion',"
        " 'soft delete with 30-day purge', 'runtime-ref')"
    )
    store.conn.execute(
        "INSERT INTO trace_msg_dec_mentions VALUES ('MSG-d', 'DEC-1')"
    )
    assert _count(store, "trace_msg_dec_mentions") == 1


def test_trace_dec_append_only_discipline(store):
    """007 KTD1: DEC follows the FEAT identity discipline."""
    store.conn.execute(
        "INSERT INTO trace_dec (id, target, digest, category, description,"
        " evidence_ref) VALUES ('DEC-2', 'linkding', 'sha256:abc', 'auth',"
        " 'session-cookie + role enum', 'runtime-ref')"
    )
    row = store.conn.execute(
        "SELECT * FROM trace_dec WHERE id = 'DEC-2'"
    ).fetchone()
    assert row["status"] == "confirmed"
    assert row["category"] == "auth"
    # ids are append-only; rows are never deleted (deprecate instead)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute("UPDATE trace_dec SET id = 'DEC-3' WHERE id = 'DEC-2'")
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.conn.execute("DELETE FROM trace_dec WHERE id = 'DEC-2'")
    store.conn.execute(
        "UPDATE trace_dec SET status = 'deprecated' WHERE id = 'DEC-2'"
    )
    assert store.conn.execute(
        "SELECT status FROM trace_dec WHERE id = 'DEC-2'"
    ).fetchone()["status"] == "deprecated"
    # id-prefix + status enum backstop raw writes
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_dec (id, evidence_ref) VALUES ('XDEC-1', 'r')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "UPDATE trace_dec SET status = 'removed' WHERE id = 'DEC-2'"
        )
    assert set(DEC_STATUSES) == {"confirmed", "deprecated"}


def test_episode_world_enum_and_default(store):
    """007 KTD6: world is brownfield by default and CHECK-constrained."""
    default = store.create_episode("linkding", "sha256:abc", 0)
    assert store.get_episode(default)["world"] == "brownfield"
    for world in WORLDS:
        store.conn.execute(
            "UPDATE episodes SET world = ? WHERE id = ?", (world, default)
        )
        assert store.get_episode(default)["world"] == world
    assert set(WORLDS) == {
        "brownfield", "greenfield_backtranslated", "greenfield_pure"
    }
    # world is a separate axis from mode — both coexist on one episode
    bench = store.create_episode("kanboard", "sha256:def", 0, mode="benchmark")
    store.conn.execute(
        "UPDATE episodes SET world = 'greenfield_backtranslated' WHERE id = ?",
        (bench,),
    )
    row = store.get_episode(bench)
    assert row["mode"] == "benchmark"
    assert row["world"] == "greenfield_backtranslated"
    with pytest.raises(sqlite3.IntegrityError):  # world enum backstops raw writes
        store.conn.execute(
            "UPDATE episodes SET world = 'hybrid' WHERE id = ?", (default,)
        )


def test_batch_validation_class_enum_and_default(store):
    """007 KTD5: validation_class tags the substrate-routing channel."""
    batch = store.ensure_batch("greenfield-batch")
    row = store.conn.execute(
        "SELECT validation_class FROM batches WHERE id = ?", (batch,)
    ).fetchone()
    assert row["validation_class"] == "general"  # backfill default
    for vc in VALIDATION_CLASSES:
        store.conn.execute(
            "UPDATE batches SET validation_class = ? WHERE id = ?", (vc, batch)
        )
        assert store.conn.execute(
            "SELECT validation_class FROM batches WHERE id = ?", (batch,)
        ).fetchone()["validation_class"] == vc
    assert set(VALIDATION_CLASSES) == {"code", "elicitation", "general"}
    assert set(INSIGHT_PROVENANCES) == {
        "manual", "reflector", "researched", "seeded"
    }
    with pytest.raises(sqlite3.IntegrityError):  # enum backstops raw writes
        store.conn.execute(
            "UPDATE batches SET validation_class = 'mixed' WHERE id = ?", (batch,)
        )


def test_greenfield_tables_roundtrip(store):
    """007 KTD2/3/4: assume ledger, proposals + adjudication, founder model."""
    episode = store.create_episode("linkding", "sha256:abc", 0)
    run_id = store.create_run("inc", 0, episode_id=episode, increment_index=1)
    store.conn.execute("INSERT INTO trace_msg VALUES ('MSG-c', 'confirmed it')")
    # assumption ledger: typed risk + status, keyed by run
    store.conn.execute(
        "INSERT INTO trace_assume (id, run_id, claim, basis, risk_if_wrong,"
        " cheapest_test, status, confirmed_by_msg) VALUES ('ASSUME-1', ?,"
        " 'users want tags', 'common in this domain', 'high', 'ask the founder',"
        " 'confirmed', 'MSG-c')",
        (run_id,),
    )
    assume = store.conn.execute(
        "SELECT * FROM trace_assume WHERE id = 'ASSUME-1'"
    ).fetchone()
    assert assume["risk_if_wrong"] == "high"
    assert assume["status"] == "confirmed"
    assert assume["confirmed_by_msg"] == "MSG-c"
    assert set(ASSUME_RISKS) == {"low", "med", "high"}
    assert set(ASSUME_STATUSES) == {"open", "confirmed", "invalidated"}
    with pytest.raises(sqlite3.IntegrityError):  # risk enum
        store.conn.execute(
            "INSERT INTO trace_assume (id, run_id, claim, risk_if_wrong)"
            " VALUES ('ASSUME-bad', ?, 'c', 'critical')",
            (run_id,),
        )
    # typed proposal linked to the assumption it resolves
    store.conn.execute(
        "INSERT INTO trace_proposal (id, run_id, topic, options_json,"
        " recommended, linked_assume_id) VALUES ('PROP-1', ?, 'tag model',"
        " '[\"flat\", \"hierarchical\"]', 'flat', 'ASSUME-1')",
        (run_id,),
    )
    prop = store.conn.execute(
        "SELECT * FROM trace_proposal WHERE id = 'PROP-1'"
    ).fetchone()
    assert prop["recommended"] == "flat"
    assert prop["linked_assume_id"] == "ASSUME-1"
    # a proposal with no linked assumption is legal
    store.conn.execute(
        "INSERT INTO trace_proposal (id, run_id, topic) VALUES ('PROP-2', ?, 't')",
        (run_id,),
    )
    # stored adjudication verdict mapping a proposal to a DEC ref
    store.conn.execute(
        "INSERT INTO trace_dec (id, evidence_ref) VALUES ('DEC-adj', 'ref')"
    )
    store.conn.execute(
        "INSERT INTO trace_proposal_adjudication (proposal_or_msg_id, ref_kind,"
        " ref_id, verdict, confidence, checker_meta, created_at)"
        " VALUES ('PROP-1', 'dec', 'DEC-adj', 'match', 0.91, '{}', 'now')"
    )
    adj = store.conn.execute(
        "SELECT * FROM trace_proposal_adjudication WHERE proposal_or_msg_id = 'PROP-1'"
    ).fetchone()
    assert adj["ref_kind"] == "dec"
    assert adj["confidence"] == pytest.approx(0.91)
    with pytest.raises(sqlite3.IntegrityError):  # ref_kind enum
        store.conn.execute(
            "INSERT INTO trace_proposal_adjudication (proposal_or_msg_id,"
            " ref_kind, ref_id, verdict, created_at)"
            " VALUES ('PROP-1', 'epic', 'X', 'v', 'now')"
        )
    # founder model: blur cache (target-side) → knowledge row referencing it
    blur = store.conn.execute(
        "INSERT INTO founder_blur_cache (target, ref_kind, ref_id, entry_digest,"
        " seed, params_hash, prompt_set_version, blur_text, lint_verdict)"
        " VALUES ('linkding', 'feat', 'FEAT-1', 'sha256:e', 7, 'ph1', 'ps1',"
        " 'items get cleaned up eventually', 'pass')"
    ).lastrowid
    store.conn.execute(
        "INSERT INTO founder_models (episode_id, target, seed, params_hash,"
        " goal_statement) VALUES (?, 'linkding', 7, 'ph1', 'save links for later')",
        (episode,),
    )
    store.conn.execute(
        "INSERT INTO founder_knowledge (episode_id, ref_kind, ref_id, state,"
        " blur_id) VALUES (?, 'feat', 'FEAT-1', 'blurred', ?)",
        (episode, blur),
    )
    fk = store.conn.execute(
        "SELECT * FROM founder_knowledge WHERE episode_id = ?", (episode,)
    ).fetchone()
    assert fk["state"] == "blurred"
    assert fk["blur_id"] == blur
    assert set(FOUNDER_KNOWLEDGE_STATES) == {"intact", "blurred", "dropped"}
    with pytest.raises(sqlite3.IntegrityError):  # state enum
        store.conn.execute(
            "INSERT INTO founder_knowledge (episode_id, ref_kind, ref_id, state)"
            " VALUES (?, 'feat', 'FEAT-1', 'vague')",
            (episode,),
        )
    # the blur cache is uniquely keyed (target, ref, digest, seed, params, prompt)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO founder_blur_cache (target, ref_kind, ref_id,"
            " entry_digest, seed, params_hash, prompt_set_version)"
            " VALUES ('linkding', 'feat', 'FEAT-1', 'sha256:e', 7, 'ph1', 'ps1')"
        )
    # founder_models PK is the episode (one model per episode)
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO founder_models (episode_id, target, seed, params_hash)"
            " VALUES (?, 'linkding', 7, 'ph1')",
            (episode,),
        )
    # settlement reports gained the elicitation-metrics block (defaulted)
    store.conn.execute(
        "INSERT INTO settlement_reports (episode_id, report_json, created_at)"
        " VALUES (?, '{}', 'now')",
        (episode,),
    )
    assert store.conn.execute(
        "SELECT elicitation_metrics_json FROM settlement_reports WHERE episode_id = ?",
        (episode,),
    ).fetchone()["elicitation_metrics_json"] == "{}"
