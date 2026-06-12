"""plan-010 U3: finish runtime demotion + re-scope downstream plans (R6-R11).

Phase C's smallest unit. The audit's headline finding is that the work-execution
runtime is *already* stage-shaped (plan->work->verify routed by fixed callables,
never by a family router); plan 009 already demoted `router.route`,
`run_boundary_ticket`, and the agent-split engine. This unit removes the LAST
runtime family/agent references and reconciles the three downstream plans to the
stage model — a demotion, not a deletion: every demoted column/row survives for
reversibility (DESIGN §4), the runtime simply stops *using* it as a routing role.

Fully offline: store/DB introspection, a SQLite authorizer that denies reads of
the demoted column, the existing toy-spec pipeline (the plan-002 U8 fake), and
plain-text reads of the downstream plan files. Zero quota, no `claude` on PATH.

## Conformance (plan-010 U3 required acceptance tests -> the invariant each enforces)

- ``test_base_prompt_specialty_not_read_at_runtime`` (R7) — with a SQLite
  authorizer DENYING every READ of ``agents.base_prompt_specialty`` (writes still
  allowed) installed on every Store, a full plan->work->verify run COMPLETES; the
  guard is proven live (a direct read raises); and no SELECT in ``src/`` reads the
  column. Nothing assembles a specialty section into a prompt.
- ``test_demoted_columns_and_rows_preserved`` (R7/R8) — the
  ``agents.base_prompt_specialty`` column and the span ``family``/``agent`` columns
  still EXIST post-plan (schema introspection); seeded family/agent rows are not
  dropped. Demotion stops USING them as runtime roles; it never drops them.
- ``test_seeding_produces_stages_not_routing_taxonomy`` (R6) — ``_seed_taxonomy``
  materializes exactly the four pipeline STAGE roles (idempotently), and no
  production code consumes the seeded rows as a router input (``router.route`` has
  zero call sites in ``src/``).
- ``test_enforcement_persona_rotation_kept`` (R8) —
  ``enforcement.PersonaRotationRule`` / ``should_rotate_personas`` (the §9
  explorer prompt-style rotation decoy) is STILL present and functional; the
  demotion must not delete it.
- ``test_downstream_plans_carry_r3_reconciliation`` (R9/R10/R11) — plan 005's
  R12/R13/R14 are marked superseded-by-R3, plan 004's family-scoped-retrieval
  requirements are rewritten to whole-store, and plan 003 carries the
  terminology-only note — each annotation present in the respective plan file.

Test-scenario coverage (plan-010 U3 "Test scenarios", in addition to the above):

- no runtime read of base_prompt_specialty -> ``test_base_prompt_specialty_not_read_at_runtime``
- seeding produces stages not a routing taxonomy -> ``test_seeding_produces_stages_not_routing_taxonomy``
- grep confirms no family/agent runtime routing outside demoted files ->
  ``test_no_family_agent_runtime_routing_outside_demoted_files``
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import test_pipeline_e2e as e2e
from agent_families import cli
from agent_families.pipeline import enforcement
from agent_families.store import Store
from agent_families.vecindex import VecIndex

SRC = Path(__file__).resolve().parent.parent / "src"
PLANS = Path(__file__).resolve().parents[2] / "docs" / "plans"

# The four DESIGN §3 pipeline STAGE roles seeded (as families, for reversibility).
EXPECTED_STAGE_ROLES = {"planner", "worker", "verifier", "context-retriever"}


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "library.db")
    s.migrate()
    VecIndex(s, 4).migrate()
    try:
        yield s
    finally:
        s.close()


def _deny_specialty_read(action, arg1, arg2, dbname, trigger):
    """SQLite authorizer: DENY any READ of ``agents.base_prompt_specialty``.

    ``SQLITE_READ`` fires per-column at statement-compile time for SELECTs only;
    writes fire ``SQLITE_INSERT``/``SQLITE_UPDATE`` (column arg is None), so this
    blocks reads while leaving the column writable — the faithful "patch the read
    to RAISE" the plan asks for.
    """
    if (
        action == sqlite3.SQLITE_READ
        and arg1 == "agents"
        and arg2 == "base_prompt_specialty"
    ):
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


# === R7: base_prompt_specialty is never READ at runtime =========================


def test_base_prompt_specialty_not_read_at_runtime(tmp_path, monkeypatch):
    """With a read-deny authorizer on every Store, a full plan->work->verify run
    completes; the guard is proven live; and no SELECT in src reads the column (R7).

    (The R3 ``assign`` stage — plan-010 U1 — is not yet wired in this worktree, so
    the run exercised is the existing plan->work->verify pipeline, which is the
    whole runtime the demotion invariant must hold across.)
    """
    orig_init = Store.__init__

    def guarded_init(self, *args, **kwargs):
        orig_init(self, *args, **kwargs)
        self.conn.set_authorizer(_deny_specialty_read)

    monkeypatch.setattr(Store, "__init__", guarded_init)

    # A full run completes under the read-guard — no stage reads the column.
    ran_store, result, _run_id = e2e.run_spec(
        tmp_path, "01-trivial.md", e2e.trivial_steps, gate=e2e.PASS_GATE
    )
    assert result.status == "success"

    # The guard is genuinely live on the SAME connection the pipeline used: writing
    # the column still works (INSERT), but any direct READ raises. This proves the
    # passing run above was not a no-op authorizer — a real read would have failed.
    created = cli._seed_taxonomy(ran_store, active_cap=50)  # INSERTs the column
    assert created == len(EXPECTED_STAGE_ROLES)
    # Rows landed (the INSERT of base_prompt_specialty was allowed); count WITHOUT
    # referencing the guarded column.
    rows = ran_store.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
    assert rows == len(EXPECTED_STAGE_ROLES)
    # ... but any direct READ of the column raises — the guard is live, not a no-op.
    with pytest.raises(sqlite3.DatabaseError):
        ran_store.conn.execute("SELECT base_prompt_specialty FROM agents").fetchone()

    # Belt-and-braces: no SELECT anywhere in src reads the column (the only textual
    # occurrences are the CREATE TABLE schema and the create_agent INSERT — writes).
    offenders = []
    for py in SRC.rglob("*.py"):
        for raw in py.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if "base_prompt_specialty" in line and line.upper().startswith("SELECT"):
                offenders.append((py.relative_to(SRC).as_posix(), line))
    assert offenders == [], f"a runtime SELECT reads base_prompt_specialty: {offenders}"


# === R7/R8: demoted columns + rows survive (demote, never drop) =================


def test_demoted_columns_and_rows_preserved(store):
    """The demoted ``agents.base_prompt_specialty`` and span ``family``/``agent``
    columns still exist; seeded family/agent rows are not dropped (R7/R8, §4)."""
    agent_cols = {
        r["name"] for r in store.conn.execute("PRAGMA table_info(agents)").fetchall()
    }
    assert "base_prompt_specialty" in agent_cols  # demoted, not dropped

    span_cols = {
        r["name"]
        for r in store.conn.execute("PRAGMA table_info(trace_span)").fetchall()
    }
    assert {"family", "agent"} <= span_cols  # traceability columns kept (§13)

    # The seeded family/agent rows physically survive (reversibility): seeding
    # creates them and a re-seed leaves them untouched.
    assert cli._seed_taxonomy(store, active_cap=50) == len(EXPECTED_STAGE_ROLES)
    fam_count = store.conn.execute("SELECT COUNT(*) AS n FROM families").fetchone()["n"]
    agent_count = store.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()["n"]
    assert fam_count == len(EXPECTED_STAGE_ROLES)
    assert agent_count == len(EXPECTED_STAGE_ROLES)  # one generic agent per stage
    # A span row can still carry the demoted family/agent labels (write path intact).
    store.conn.execute(
        "INSERT INTO trace_span (id, run_id, family, agent, status)"
        " VALUES ('SPAN-demote-1', NULL, 'worker', 'generalist', 'completed')"
    )
    row = store.conn.execute(
        "SELECT family, agent FROM trace_span WHERE id = 'SPAN-demote-1'"
    ).fetchone()
    assert (row["family"], row["agent"]) == ("worker", "generalist")


# === R6: seeding is stage setup, not a routing taxonomy =========================


def test_seeding_produces_stages_not_routing_taxonomy(store):
    """``_seed_taxonomy`` materializes exactly the four pipeline STAGE roles
    (idempotently) and nothing consumes the seeded rows as a router input (R6)."""
    created = cli._seed_taxonomy(store, active_cap=50)
    assert created == len(EXPECTED_STAGE_ROLES)
    assert cli._seed_taxonomy(store, active_cap=50) == 0  # idempotent

    # The seeded entities are precisely the DESIGN §3 STAGE roles — not an arbitrary
    # family taxonomy a router would choose between.
    seeded = {
        r["name"] for r in store.conn.execute("SELECT name FROM families").fetchall()
    }
    assert seeded == EXPECTED_STAGE_ROLES
    assert {name for name, _ in cli.FAMILY_SEEDS} == EXPECTED_STAGE_ROLES

    # No production code consumes the seeded rows as a router input: ``router.route``
    # — the per-request family/agent selector — has zero call sites in src (the
    # definition in router.py is kept for reversibility but never invoked). We match
    # the CALL token ``router.route(`` so a docstring mention does not trip this.
    offenders = [
        py.relative_to(SRC).as_posix()
        for py in SRC.rglob("*.py")
        if py.name != "router.py" and "router.route(" in py.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"production code routes over the seeded rows: {offenders}"


def test_no_family_agent_runtime_routing_outside_demoted_files():
    """A source scan confirms no family/agent runtime ROUTING survives anywhere in
    src outside the kept-for-reversibility demoted files (R6/R8 + plan-009 R13/R14).

    Matches call/def TOKENS, not prose, so a docstring naming the deleted machinery
    as "removed" does not trip this. ``router.py`` (the demoted ``route`` definition)
    and ``retrieval.py`` (where the boundary-ticket machinery was deleted in plan
    009) are the only files permitted to mention the tokens at all.
    """
    demoted_files = {"router.py", "retrieval.py"}
    routing_tokens = ("router.route(", "run_boundary_ticket(", ".select_persona(")
    offenders = []
    for py in SRC.rglob("*.py"):
        if py.name in demoted_files:
            continue
        text = py.read_text(encoding="utf-8")
        for tok in routing_tokens:
            if tok in text:
                offenders.append((py.relative_to(SRC).as_posix(), tok))
    assert offenders == [], f"family/agent runtime routing survives in src: {offenders}"


# === R8: the enforcement persona-rotation decoy is KEPT =========================


def test_enforcement_persona_rotation_kept():
    """``PersonaRotationRule`` / ``should_rotate_personas`` (the §9 explorer
    prompt-style rotation — a deliberate decoy for a "persona" grep) is still
    present and functional; the demotion must not delete it (R8)."""
    assert hasattr(enforcement, "PersonaRotationRule")
    assert callable(enforcement.should_rotate_personas)

    rule = enforcement.PersonaRotationRule(window=3, min_improvement=0.01)
    # A stalled planner-score series rotates; a healthy upward trend never does.
    assert enforcement.should_rotate_personas([0.5, 0.5, 0.5], rule) is True
    assert enforcement.should_rotate_personas([0.5, 0.7, 0.9], rule) is False


# === R9/R10/R11: downstream plans carry the R3 reconciliation ===================


def _plan_text(name: str) -> str:
    matches = sorted(PLANS.glob(name))
    assert matches, f"downstream plan not found: {name}"
    return matches[0].read_text(encoding="utf-8")


def test_downstream_plans_carry_r3_reconciliation():
    """Plan 005 R12/R13/R14 marked superseded-by-R3, plan 004 family-scoped
    retrieval rewritten to whole-store, plan 003 carries the terminology-only note
    — each annotation present in the respective plan file (R9/R10/R11)."""
    plan005 = _plan_text("2026-06-10-005-*.md")
    assert "SUPERSEDED BY R3" in plan005
    for req in ("R12", "R13", "R14"):
        assert req in plan005, f"plan 005 annotation must name {req}"
    assert "2026-06-12-010" in plan005  # points at the superseding plan

    plan004 = _plan_text("2026-06-10-004-*.md")
    assert "REWRITTEN BY R3" in plan004
    assert "family-scoped retrieval" in plan004
    assert "whole-store" in plan004  # the model it is rewritten to
    assert "2026-06-12-010" in plan004

    plan003 = _plan_text("2026-06-10-003-*.md")
    assert "R3 RECONCILIATION (terminology-only)" in plan003
    assert "stage-role" in plan003
    assert "2026-06-12-010" in plan003
