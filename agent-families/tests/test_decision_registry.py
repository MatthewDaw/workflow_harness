"""plan-007 U4: the decision registry (DEC) — extraction alongside FEAT, the
runtime-confirmed mint path, and the mentioned-only DEC-coverage lint (R1, KTD1).

Fully offline, like its FEAT sibling (test_registry.py): the browse channel is a
scripted fake, decisions parse from plain dicts, and the lint joins persisted
rows. Zero quota, no docker, no network.

## Conformance

Required acceptance tests (plan-007 U4) — invariant -> enforcing test:

- "a decision present in source but NOT runtime-confirmed is not minted (source
  proposes, runtime confirms - entry by entry)"
  -> ``test_source_only_dec_not_minted`` (the planted source-only decision
  enumerates fine but the browse channel reports ``element_absent`` on the
  running app; asserts no trace_dec row, and the DEC-mention FK still rejects
  the id)
- "a DEC mentioned in the increment's MSGs but unresolved (no
  question/proposal/ASSUME) -> typed failure; an UNMENTIONED unresolved DEC ->
  ignored (v1 semantics, no failure)"
  -> ``test_dec_coverage_mentioned_only`` (one typed LintFinding for the
  mentioned-unresolved DEC; zero findings for the unmentioned one)
- "the DEC-coverage lint is a no-op in brownfield world"
  -> ``test_dec_coverage_brownfield_noop`` (a mentioned-unresolved DEC that
  WOULD fail in a greenfield world yields zero findings in brownfield)

Unit test scenarios (plan-007 U4) -> tests:

- linkding-shaped fixture yields DEC rows with categories + evidence refs
  -> ``test_decisions_minted_with_categories_and_evidence``
- out-of-taxonomy category flagged (proposed, not rejected)
  -> ``test_out_of_taxonomy_category_flagged_not_rejected``
- compound decision split per the granularity rubric (rubric ships in the prompt)
  -> ``test_granularity_rubric_and_decisions_in_prompt``
- DEC ids append-only; a vanished decision deprecates, never deletes
  -> ``test_dec_ids_append_only``
- a minted DEC is mentionable; an unminted one is not (the FK enforces it)
  -> ``test_minted_dec_is_mentionable`` (and the FK arm of
  ``test_source_only_dec_not_minted``)
- the DEC array is optional in the contract; duplicates rejected
  -> ``test_decisions_from_output_optional_and_rejects_duplicates``
- a mentioned DEC resolved by a question / an ASSUME / a proposal -> no failure
  -> ``test_dec_coverage_resolved_by_question_assume_proposal``
- the same pass yields both arrays; run_pre_research stays FEAT-only
  -> ``test_run_pre_research_full_returns_both_arrays``
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agent_families.grading.registry import (
    PRE_RESEARCH_SCHEMA,
    DecisionCandidate,
    KNOWN_DEC_CATEGORIES,
    LINT_DEC_COVERAGE,
    RegistryError,
    build_pre_research_prompt,
    confirm_decision,
    dec_id,
    decision_rows,
    decisions_from_output,
    deprecate_dec,
    grader_profile,
    lint_dec_coverage,
    mint_dec,
    out_of_taxonomy_categories,
    refresh_decisions,
    run_pre_research,
    run_pre_research_full,
)
from agent_families.pipeline.planning import SEVERITY_ERROR
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "cd" * 32


# --- helpers -------------------------------------------------------------------


class FakeBrowse:
    """The orchestrator-mediated browse channel, scripted (R15 pattern):
    answers ``ok`` with an a11y snapshot, except for selectors naming a
    planted-absent key (the running app does not have the element)."""

    def __init__(self, absent: tuple[str, ...] = ()) -> None:
        self.absent = absent
        self.calls: list[dict] = []

    def __call__(self, step: dict) -> dict:
        self.calls.append(step)
        if any(key in step.get("selector", "") for key in self.absent):
            return {"status": "element_absent", "a11y": ""}
        return {
            "status": "ok",
            "a11y": f"snapshot:{step['action']}:{step['selector']}",
            "screenshot_ref": "shots/step.png",
        }


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def dec(key: str, category: str = "auth-gated", **kw) -> DecisionCandidate:
    defaults = dict(
        key=key,
        category=category,
        description=f"the app resolved {key.replace('-', ' ')}",
        route="bookmarks/models.py",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/settings"}},
            {"action": "snapshot", "selector": f"[data-dec={key}]", "args": {}},
        ),
    )
    return DecisionCandidate(**{**defaults, **kw})


def refresh(store, tmp_path, decisions, absent: tuple[str, ...] = ()):
    browse = FakeBrowse(absent)
    result = refresh_decisions(
        store,
        decisions,
        browse,
        target=TARGET,
        digest=DIGEST,
        evidence_dir=tmp_path / "evidence",
    )
    return result, browse


def add_msg(store: Store, msg_id: str) -> None:
    store.conn.execute(
        "INSERT OR IGNORE INTO trace_msg (id, content) VALUES (?, '')", (msg_id,)
    )


def mention_dec(store: Store, did: str, msg_id: str) -> None:
    add_msg(store, msg_id)
    store.conn.execute(
        "INSERT INTO trace_msg_dec_mentions (msg_id, dec_id) VALUES (?, ?)",
        (msg_id, did),
    )


def make_episode(store: Store) -> int:
    cur = store.conn.execute(
        "INSERT INTO episodes (target, digest, snapshot_id, created_at)"
        " VALUES (?, ?, 0, '2026-06-10T00:00:00+00:00')",
        (TARGET, DIGEST),
    )
    return int(cur.lastrowid)


# --- REQUIRED: source proposes, runtime confirms (KTD1) ------------------------


def test_source_only_dec_not_minted(tmp_path):
    """A decision present in source but not confirmable on the running app is
    NOT minted — source proposes, runtime confirms, entry by entry."""
    store = make_store(tmp_path)
    dead = dec("export-format-v0", route="legacy/export.py")
    result, browse = refresh(
        store,
        tmp_path,
        [dec("auth-session-cookie"), dead],
        absent=("export-format-v0",),
    )

    assert result.unconfirmed == ("export-format-v0",)
    assert "DEC-export-format-v0" not in result.minted
    # no registry row: it does not exist
    assert (
        store.conn.execute(
            "SELECT * FROM trace_dec WHERE id = 'DEC-export-format-v0'"
        ).fetchone()
        is None
    )
    # and it is not mentionable: the mention FK rejects the unminted id
    add_msg(store, "MSG-1")
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute(
            "INSERT INTO trace_msg_dec_mentions (msg_id, dec_id)"
            " VALUES ('MSG-1', 'DEC-export-format-v0')"
        )
    # the dead decision WAS probed on the running app before rejection
    assert any("export-format-v0" in c.get("selector", "") for c in browse.calls)


# --- REQUIRED: DEC-coverage mentioned-only (KTD1) ------------------------------


def test_dec_coverage_mentioned_only(tmp_path):
    """A mentioned-but-unresolved DEC fails the lint; an UNMENTIONED DEC is
    ignored (v1 semantics)."""
    store = make_store(tmp_path)
    refresh(store, tmp_path, [dec("auth-session-cookie"), dec("delete-soft")])

    # only DEC-auth-session-cookie is mentioned in the increment's MSGs; nothing
    # surfaces it (no question / proposal / ASSUME).
    mention_dec(store, "DEC-auth-session-cookie", "MSG-q1")

    findings = lint_dec_coverage(
        store, world="greenfield_backtranslated", msg_ids=["MSG-q1"]
    )
    assert len(findings) == 1
    (finding,) = findings
    assert finding.lint == LINT_DEC_COVERAGE
    assert finding.severity == SEVERITY_ERROR
    assert "DEC-auth-session-cookie" in finding.location
    # the unmentioned DEC-delete-soft produced no finding (ignored, v1)
    assert all("DEC-delete-soft" not in f.observed for f in findings)


# --- REQUIRED: brownfield no-op (KTD1) -----------------------------------------


def test_dec_coverage_brownfield_noop(tmp_path):
    """The DEC-coverage lint is a no-op in brownfield — the same mentioned,
    unresolved DEC that fails in greenfield yields nothing here."""
    store = make_store(tmp_path)
    refresh(store, tmp_path, [dec("auth-session-cookie")])
    mention_dec(store, "DEC-auth-session-cookie", "MSG-q1")

    # would fail in a greenfield world...
    assert lint_dec_coverage(
        store, world="greenfield_backtranslated", msg_ids=["MSG-q1"]
    )
    # ...but is silent in brownfield
    assert lint_dec_coverage(store, world="brownfield", msg_ids=["MSG-q1"]) == []


# --- resolved paths: question / ASSUME / proposal ------------------------------


def test_dec_coverage_resolved_by_question_assume_proposal(tmp_path):
    """A mentioned DEC surfaced by a question, an ASSUME, or a proposal clears
    the lint — the KTD1 join over qa_log x trace_assume x trace_proposal."""
    store = make_store(tmp_path)
    refresh(
        store,
        tmp_path,
        [dec("by-question"), dec("by-assume"), dec("by-proposal")],
    )

    # by-question: the mention MSG is a qa_log question slot
    mention_dec(store, "DEC-by-question", "MSG-q")
    episode_id = make_episode(store)
    store.conn.execute(
        "INSERT INTO qa_log (episode_id, question, question_msg_id, created_at)"
        " VALUES (?, 'what auth?', 'MSG-q', '2026-06-10T00:00:00+00:00')",
        (episode_id,),
    )

    # by-assume: the mention MSG confirmed an ASSUME
    mention_dec(store, "DEC-by-assume", "MSG-a")
    store.conn.execute(
        "INSERT INTO trace_assume (id, claim, confirmed_by_msg)"
        " VALUES ('ASSUME-1', 'deletes are soft', 'MSG-a')"
    )

    # by-proposal: a proposal resolving an ASSUME the mention MSG confirmed
    mention_dec(store, "DEC-by-proposal", "MSG-p")
    store.conn.execute(
        "INSERT INTO trace_assume (id, claim, confirmed_by_msg)"
        " VALUES ('ASSUME-2', 'tenancy is single', 'MSG-p')"
    )
    store.conn.execute(
        "INSERT INTO trace_proposal (id, topic, linked_assume_id)"
        " VALUES ('PROP-1', 'tenancy', 'ASSUME-2')"
    )

    findings = lint_dec_coverage(
        store,
        world="greenfield_backtranslated",
        msg_ids=["MSG-q", "MSG-a", "MSG-p"],
    )
    assert findings == [], "every mentioned DEC was surfaced"


def test_lint_rejects_unknown_world(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RegistryError, match="unknown world"):
        lint_dec_coverage(store, world="sideways", msg_ids=["MSG-1"])


def test_lint_empty_scope_is_silent(tmp_path):
    store = make_store(tmp_path)
    assert lint_dec_coverage(
        store, world="greenfield_backtranslated", msg_ids=[]
    ) == []


def test_lint_ignores_deprecated_dec(tmp_path):
    """A mention of a deprecated DEC does not force coverage (v1: confirmed
    DECs only)."""
    store = make_store(tmp_path)
    refresh(store, tmp_path, [dec("auth-session-cookie")])
    mention_dec(store, "DEC-auth-session-cookie", "MSG-q1")
    deprecate_dec(store, "DEC-auth-session-cookie")
    assert lint_dec_coverage(
        store, world="greenfield_backtranslated", msg_ids=["MSG-q1"]
    ) == []


# --- extraction: rows carry categories + evidence ------------------------------


def test_decisions_minted_with_categories_and_evidence(tmp_path):
    store = make_store(tmp_path)
    result, browse = refresh(
        store,
        tmp_path,
        [dec("auth-session-cookie", category="auth-gated"),
         dec("delete-soft", category="core-crud")],
    )
    assert set(result.minted) == {"DEC-auth-session-cookie", "DEC-delete-soft"}

    rows = decision_rows(store, TARGET)
    assert len(rows) == 2
    by_id = {r["id"]: r for r in rows}
    assert by_id["DEC-auth-session-cookie"]["category"] == "auth-gated"
    assert by_id["DEC-delete-soft"]["category"] == "core-crud"
    for row in rows:
        assert row["status"] == "confirmed"
        assert row["digest"] == DIGEST
        assert row["target"] == TARGET
        assert row["description"]
        assert row["evidence_ref"] and Path(row["evidence_ref"]).exists()
        evidence = json.loads(Path(row["evidence_ref"]).read_text(encoding="utf-8"))
        assert evidence["digest"] == DIGEST
        assert evidence["dec_key"] == row["id"].removeprefix("DEC-")
        assert len(evidence["captured"]) == 2  # one capture per confirm step


def test_mint_dec_refuses_empty_evidence_and_duplicate_ids(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RegistryError, match="evidence"):
        mint_dec(store, dec("a-choice"), "  ", target=TARGET, digest=DIGEST)
    mint_dec(store, dec("a-choice"), "ev/a.json", target=TARGET, digest=DIGEST)
    with pytest.raises(RegistryError, match="never reused"):
        mint_dec(store, dec("a-choice"), "ev/a.json", target=TARGET, digest=DIGEST)


def test_minted_dec_is_mentionable(tmp_path):
    store = make_store(tmp_path)
    mint_dec(store, dec("auth-session-cookie"), "ev/a.json", target=TARGET,
             digest=DIGEST)
    mention_dec(store, "DEC-auth-session-cookie", "MSG-1")  # FK accepts it
    row = store.conn.execute(
        "SELECT dec_id FROM trace_msg_dec_mentions WHERE msg_id = 'MSG-1'"
    ).fetchone()
    assert row["dec_id"] == "DEC-auth-session-cookie"


# --- out-of-taxonomy flagging (grow-by-exception) ------------------------------


def test_out_of_taxonomy_category_flagged_not_rejected(tmp_path):
    store = make_store(tmp_path)
    novel = dec("multi-tenancy", category="tenancy-model")  # not in taxonomy
    known = dec("auth-session-cookie", category="auth-gated")
    assert "auth-gated" in KNOWN_DEC_CATEGORIES
    assert "tenancy-model" not in KNOWN_DEC_CATEGORIES
    assert not novel.in_taxonomy and known.in_taxonomy

    result, _ = refresh(store, tmp_path, [novel, known])
    # flagged for taxonomy growth...
    assert result.out_of_taxonomy == ("tenancy-model",)
    # ...but NOT rejected: both decisions still minted
    assert set(result.minted) == {"DEC-multi-tenancy", "DEC-auth-session-cookie"}

    assert out_of_taxonomy_categories([novel, known, novel]) == ("tenancy-model",)


# --- granularity rubric + decisions ride the prompt ----------------------------


def test_granularity_rubric_and_decisions_in_prompt(tmp_path):
    prompt = build_pre_research_prompt(
        target=TARGET, source_root=tmp_path, ui_observations="nav shows Settings"
    )
    assert "decisions" in prompt
    assert "DEC-<key>" in prompt
    assert "GRANULARITY RUBRIC" in prompt
    assert "independently-reversible" in prompt
    assert "30-day purge" in prompt  # the compound-split example
    # known taxonomy categories are offered to the enumerator
    assert "auth-gated" in prompt
    # the schema still only requires candidates (decisions optional)
    assert PRE_RESEARCH_SCHEMA["required"] == ["candidates"]
    assert "decisions" in PRE_RESEARCH_SCHEMA["properties"]


# --- DEC ids append-only -------------------------------------------------------


def test_dec_ids_append_only(tmp_path):
    """A refresh never renumbers or reuses a DEC id; a vanished decision flips
    to `deprecated`, it is not deleted (the FEAT identity discipline)."""
    store = make_store(tmp_path)
    r1, _ = refresh(store, tmp_path, [dec("auth-session-cookie"), dec("delete-soft")])
    assert set(r1.minted) == {"DEC-auth-session-cookie", "DEC-delete-soft"}

    # refresh #2: auth-session-cookie vanished, tenancy-single is new
    r2, _ = refresh(store, tmp_path, [dec("delete-soft"), dec("tenancy-single")])
    assert r2.minted == ("DEC-tenancy-single",)
    assert r2.reconfirmed == ("DEC-delete-soft",)
    assert r2.deprecated == ("DEC-auth-session-cookie",)

    rows = {r["id"]: r["status"] for r in decision_rows(store, TARGET)}
    assert rows == {
        "DEC-auth-session-cookie": "deprecated",  # flipped, never deleted
        "DEC-delete-soft": "confirmed",  # same id, never renumbered
        "DEC-tenancy-single": "confirmed",
    }

    # refresh #3: the vanished decision reappears — SAME id, no new number
    r3, _ = refresh(
        store, tmp_path,
        [dec("auth-session-cookie"), dec("delete-soft"), dec("tenancy-single")],
    )
    assert r3.minted == ()
    assert set(r3.reconfirmed) == set(rows)
    assert {r["id"] for r in decision_rows(store, TARGET)} == set(rows)

    # the store's triggers enforce the discipline below the API too
    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        store.conn.execute(
            "UPDATE trace_dec SET id = 'DEC-renumbered'"
            " WHERE id = 'DEC-delete-soft'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="never deleted"):
        store.conn.execute(
            "DELETE FROM trace_dec WHERE id = 'DEC-auth-session-cookie'"
        )


def test_deprecate_dec_errors_on_unknown(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RegistryError, match="does not exist"):
        deprecate_dec(store, "DEC-never-was")


def test_dec_id_is_pure_derivation():
    assert dec_id("soft-delete") == "DEC-soft-delete"
    with pytest.raises(RegistryError, match="invalid"):
        dec_id("Not A Key")


# --- candidate validation ------------------------------------------------------


def test_decision_candidate_validation_rejects_malformed():
    with pytest.raises(RegistryError, match="kebab-case"):
        dec("Bad Key")
    with pytest.raises(RegistryError, match="category"):
        dec("a-key", category="  ")
    with pytest.raises(RegistryError, match="description"):
        dec("a-key", description="")
    with pytest.raises(RegistryError, match="confirm_steps"):
        dec("a-key", confirm_steps=())
    with pytest.raises(RegistryError, match="browse request"):
        dec("a-key", confirm_steps=({"action": "goto"},))


def test_decisions_from_output_optional_and_rejects_duplicates():
    # a FEAT-only enumeration is legal: missing/empty decisions -> []
    assert decisions_from_output({"candidates": []}) == []
    assert decisions_from_output({"candidates": [], "decisions": []}) == []

    payload = {
        "key": "auth-session-cookie",
        "category": "auth-gated",
        "description": "session cookie + role enum",
        "route": "accounts/models.py",
        "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
    }
    parsed = decisions_from_output({"candidates": [], "decisions": [payload]})
    assert [d.key for d in parsed] == ["auth-session-cookie"]
    assert parsed[0].category == "auth-gated"
    with pytest.raises(RegistryError, match="duplicate"):
        decisions_from_output(
            {"candidates": [], "decisions": [payload, dict(payload)]}
        )


def test_confirm_decision_unconfirmed_returns_none(tmp_path):
    browse = FakeBrowse(absent=("ghost",))
    ref = confirm_decision(
        dec("ghost"), browse, evidence_dir=tmp_path / "ev", digest=DIGEST
    )
    assert ref is None


# --- the same pass yields both arrays; run_pre_research stays FEAT-only ---------


def grader_step(output: dict) -> dict:
    return {
        "role": "grader",
        "envelope": {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 900,
            "num_turns": 3,
            "result": "enumerated",
            "total_cost_usd": 0.04,
            "usage": {"input_tokens": 500, "output_tokens": 200},
            "structured_output": output,
        },
    }


def _candidate_payload(key: str) -> dict:
    return {
        "key": key,
        "area": "bookmarks",
        "behavior": f"user can {key}",
        "route": "bookmarks/urls.py",
        "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
        "scenario_steps": [f"Exercise {key}"],
        "expected_outcome": "visible",
        "tier": "must",
    }


def _decision_payload(key: str, category: str = "auth-gated") -> dict:
    return {
        "key": key,
        "category": category,
        "description": f"the app resolved {key}",
        "route": "models.py",
        "confirm_steps": [{"action": "goto", "selector": "", "args": {}}],
    }


def _write_script(tmp_path, output: dict, name: str = "grader-script") -> Path:
    script = tmp_path / f"{name}.json"
    script.write_text(
        json.dumps({"steps": [grader_step(output)]}, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return script


def test_run_pre_research_full_returns_both_arrays(tmp_path):
    output = {
        "candidates": [_candidate_payload("bookmark-create")],
        "decisions": [
            _decision_payload("auth-session-cookie"),
            _decision_payload("delete-soft", category="core-crud"),
        ],
    }
    script = _write_script(tmp_path, output)
    result = run_pre_research_full(
        grader_profile(model="sonnet", max_turns=20, timeout_s=300.0),
        target=TARGET,
        source_root=tmp_path,
        transcript_path=tmp_path / "transcripts" / "pre.jsonl",
        max_retries=0,
        ui_observations="nav shows Settings",
        mode="scripted",
        script_path=script,
    )
    assert [c.key for c in result.candidates] == ["bookmark-create"]
    assert [d.key for d in result.decisions] == [
        "auth-session-cookie",
        "delete-soft",
    ]
    assert all(isinstance(d, DecisionCandidate) for d in result.decisions)

    # the FEAT-only view stays a plain candidate list (back-compat)
    script2 = _write_script(tmp_path, output, name="grader-script-2")
    candidates = run_pre_research(
        grader_profile(model="sonnet", max_turns=20, timeout_s=300.0),
        target=TARGET,
        source_root=tmp_path,
        transcript_path=tmp_path / "transcripts" / "pre2.jsonl",
        max_retries=0,
        ui_observations="nav shows Settings",
        mode="scripted",
        script_path=script2,
    )
    assert [c.key for c in candidates] == ["bookmark-create"]
