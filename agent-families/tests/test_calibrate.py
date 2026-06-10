"""plan-003 U8: mutation-seeded verifier audits + frozen-set bootstrap (R24/R25).

Fully offline: the verifier under audit is a scripted fake (the orchestrator
seam's VerifierResult shape), witness execution is injectable (the seeded
end-to-end audit scripts it; the real-subprocess path is exercised with git,
which every test machine has), and staging applies the committed hand-authored
diffs with real ``git apply`` against a copy of the template stack — zero
quota, zero docker, no ``claude`` on PATH.

## Conformance

Test-scenario / invariant -> test mapping (plan-003 U8):

- witness failing on the mutant blocks the audit (mutant disqualified, not
  verifier-flagged): ``test_witness_failure_blocks_audit_mutant_disqualified``
  (+ ``test_witness_timeout_disqualifies``,
  ``test_disqualified_audit_never_advances_watermark_or_cadence``)
- verifier false-pass flags and marks suspect range correctly:
  ``test_verifier_false_pass_flags_and_marks_suspect_range``
- clean audit clears the window:
  ``test_clean_audit_clears_window``
- frozen-set persistence round-trips verdict pairs with their judge inputs:
  ``test_frozen_set_round_trips_pairs_with_judge_inputs``
  (+ ``test_frozen_set_merge_accumulates``,
  ``test_frozen_set_conflicting_repersist_rejected``,
  ``test_frozen_set_rejects_deterministic_and_unknown_scen``)
- Verification — one seeded mutant audit runs end-to-end against the fake
  verifier in CI: ``test_seeded_mutant_audit_end_to_end_caught`` and
  ``test_seeded_mutant_audit_end_to_end_false_pass`` (committed fixture,
  real ``git apply`` staging against a template copy, fake verifier)
- every mutant ships with a witness; fixtures stay appliable to the template
  stack (R24): ``test_committed_mutant_fixtures_load_and_apply``
- R14 repro discipline on witness envelopes:
  ``test_witness_envelope_validation``
- config cadence (R24 "on a config cadence"; thresholds routed by the
  run-assembly wiring): ``test_audit_due_cadence``,
  ``test_calibrate_config_validation``
- instrument health surfaces in the settlement report (R23 seam), without
  timestamps: ``test_instrument_health_feeds_settlement_report``
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agent_families.grading.calibrate import (
    AUDIT_OUTCOMES,
    CalibrateConfig,
    CalibrateError,
    FROZEN_SET_SIZE_FLOOR,
    FROZEN_SET_VERSION,
    MutantFixture,
    WitnessEnvelope,
    audit_due,
    audit_log,
    frozen_set_ready,
    instrument_health,
    last_clean_watermark,
    load_frozen_set,
    load_mutant,
    load_mutants,
    persist_frozen_set,
    run_mutation_audit,
    run_witness,
    stage_mutant,
    suspect_chk_ids,
)
from agent_families.grading.settle import (
    ScenarioVerdict,
    assemble_report,
    write_scen_rows,
)
from agent_families.pipeline.orchestrator import VerifierResult
from agent_families.store import Store

AF_ROOT = Path(__file__).resolve().parents[1]
MUTANTS_DIR = AF_ROOT / "fixtures" / "mutants"
TEMPLATE_DIR = AF_ROOT / "template"


# --- scaffolding ---------------------------------------------------------------


def make_store(base: Path) -> Store:
    base.mkdir(parents=True, exist_ok=True)
    store = Store(base / "library.db")
    store.migrate()
    return store


def seed_chks(store: Store, n: int, prefix: str = "CHK") -> list[str]:
    """n verifier verdicts (trace_chk rows), with the FK chain they need."""
    with store.transaction():
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_msg (id, content)"
            " VALUES ('MSG-cal', 'seed')"
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_req (id, source_msg_id)"
            " VALUES ('REQ-cal', 'MSG-cal')"
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_tkt (id) VALUES ('TKT-cal')"
        )
        store.conn.execute(
            "INSERT OR IGNORE INTO trace_ac (id, ticket_id, req_id)"
            " VALUES ('AC-cal', 'TKT-cal', 'REQ-cal')"
        )
        start = store.conn.execute(
            "SELECT COUNT(*) AS n FROM trace_chk"
        ).fetchone()["n"]
        ids = []
        for i in range(start, start + n):
            cid = f"{prefix}-{i:04d}"
            store.conn.execute(
                "INSERT INTO trace_chk (id, ac_id, result, repro_command,"
                " evidence) VALUES (?, 'AC-cal', 'pass', '{}', 'seed')",
                (cid,),
            )
            ids.append(cid)
    return ids


def good_witness(**overrides) -> WitnessEnvelope:
    base = dict(
        command="npm test -- server/index.test.ts",
        cwd=".",
        timeout=600,
        expected_exit=1,
    )
    base.update(overrides)
    return WitnessEnvelope(**base)


def make_mutant(mutant_id: str = "bad-diff", **witness_overrides) -> MutantFixture:
    return MutantFixture(
        mutant_id=mutant_id,
        description="hand-authored bad diff for audit tests",
        diff_text="diff --git a/x b/x\n",
        witness=good_witness(**witness_overrides),
    )


def scripted_witness(exit_code):
    """RunWitness fake returning a fixed observed exit (None = timeout)."""

    def _run(witness, workspace_root):
        return exit_code

    return _run


class SentinelVerifier:
    """Fails the test if the audit scores the verifier when it must not."""

    def __call__(self, workspace_root: Path) -> VerifierResult:
        raise AssertionError(
            "verifier must never run on a disqualified mutant (R24)"
        )


class FakeVerifier:
    """Scripted verifier seam: fixed verdict, records what it was shown."""

    def __init__(self, passed: bool):
        self.passed = passed
        self.seen: list[Path] = []

    def __call__(self, workspace_root: Path) -> VerifierResult:
        self.seen.append(Path(workspace_root))
        return VerifierResult(passed=self.passed, detail="scripted")


def template_copy(tmp_path: Path) -> Path:
    """A disposable workspace holding the template file the mutants touch."""
    ws = tmp_path / "ws"
    (ws / "server").mkdir(parents=True)
    shutil.copy(
        TEMPLATE_DIR / "server" / "index.ts", ws / "server" / "index.ts"
    )
    return ws


# --- witness envelope / fixture validation (R14 discipline, R24) -----------------


def test_witness_envelope_validation():
    good_witness()  # the base envelope is valid
    with pytest.raises(CalibrateError, match="non-empty"):
        good_witness(command="   ")
    with pytest.raises(CalibrateError, match="absolute paths"):
        good_witness(command='vitest run "C:\\abs\\index.test.ts"')
    with pytest.raises(CalibrateError, match="workspace-relative"):
        good_witness(cwd="C:/abs")
    with pytest.raises(CalibrateError, match="inside the workspace"):
        good_witness(cwd="../escape")
    with pytest.raises(CalibrateError, match="timeout"):
        good_witness(timeout=0)
    with pytest.raises(CalibrateError, match="expected_exit"):
        good_witness(expected_exit=-1)


def test_mutant_fixture_validation():
    with pytest.raises(CalibrateError, match="lowercase slug"):
        make_mutant(mutant_id="Bad Diff")
    with pytest.raises(CalibrateError, match="description"):
        MutantFixture(
            mutant_id="x", description=" ", diff_text="diff --git a/x b/x\n",
            witness=good_witness(),
        )
    with pytest.raises(CalibrateError, match="git unified diff"):
        MutantFixture(
            mutant_id="x", description="d", diff_text="not a diff",
            witness=good_witness(),
        )


def test_mutant_loading_from_directories(tmp_path: Path):
    root = tmp_path / "mutants"
    d = root / "my-mutant"
    d.mkdir(parents=True)
    (d / "mutant.diff").write_text(
        "diff --git a/x b/x\n", encoding="utf-8", newline="\n"
    )
    (d / "mutant.json").write_text(
        json.dumps(
            {
                "description": "breaks x",
                "witness": {
                    "command": "npm test", "cwd": ".", "timeout": 60,
                    "expected_exit": 1,
                },
            }
        ),
        encoding="utf-8",
        newline="\n",
    )
    mutant = load_mutant(d)
    assert mutant.mutant_id == "my-mutant"  # the directory name is the id
    assert mutant.witness.expected_exit == 1
    assert load_mutants(root) == (mutant,)

    (d / "mutant.diff").unlink()
    with pytest.raises(CalibrateError, match="missing mutant.diff"):
        load_mutant(d)
    with pytest.raises(CalibrateError, match="does not exist"):
        load_mutants(tmp_path / "nope")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(CalibrateError, match="no mutant fixtures"):
        load_mutants(empty)


def test_mutant_json_rejects_unknown_keys_and_missing_witness(tmp_path: Path):
    d = tmp_path / "m1"
    d.mkdir()
    (d / "mutant.diff").write_text(
        "diff --git a/x b/x\n", encoding="utf-8", newline="\n"
    )
    (d / "mutant.json").write_text(
        '{"description": "d", "extra": 1}', encoding="utf-8", newline="\n"
    )
    with pytest.raises(CalibrateError, match="unknown key 'extra'"):
        load_mutant(d)
    (d / "mutant.json").write_text(
        '{"description": "d"}', encoding="utf-8", newline="\n"
    )
    with pytest.raises(CalibrateError, match="ships with a witness"):
        load_mutant(d)


# --- committed fixtures stay loadable and appliable (the template canary) ---------


def test_committed_mutant_fixtures_load_and_apply(tmp_path: Path):
    mutants = load_mutants(MUTANTS_DIR)
    ids = [m.mutant_id for m in mutants]
    assert "validation-dropped" in ids
    assert "created-status-wrong" in ids
    for mutant in mutants:
        # Every committed mutant ships a witness envelope (R24).
        assert mutant.witness.expected_exit >= 0
        ws = tmp_path / mutant.mutant_id
        (ws / "server").mkdir(parents=True)
        shutil.copy(
            TEMPLATE_DIR / "server" / "index.ts", ws / "server" / "index.ts"
        )
        before = (ws / "server" / "index.ts").read_text(encoding="utf-8")
        stage_mutant(mutant, ws)
        after = (ws / "server" / "index.ts").read_text(encoding="utf-8")
        assert after != before, f"mutant {mutant.mutant_id} changed nothing"
        # no staging droppings left behind
        assert not list(ws.glob(".af-mutant-*.diff"))


def test_stage_mutant_drift_is_a_hard_error(tmp_path: Path):
    ws = template_copy(tmp_path)
    drifted = MutantFixture(
        mutant_id="drifted",
        description="context no longer matches the template",
        diff_text=(
            "diff --git a/server/index.ts b/server/index.ts\n"
            "--- a/server/index.ts\n"
            "+++ b/server/index.ts\n"
            "@@ -1,1 +1,1 @@\n"
            "-this line never existed\n"
            "+mutated\n"
        ),
        witness=good_witness(),
    )
    with pytest.raises(CalibrateError, match="drifted from the template"):
        stage_mutant(drifted, ws)


# --- the witness runner (real subprocess path) -------------------------------------


def test_run_witness_executes_real_subprocess(tmp_path: Path):
    ok = WitnessEnvelope(
        command="git --version", cwd=".", timeout=60, expected_exit=0
    )
    assert run_witness(ok, tmp_path) == 0
    bad = WitnessEnvelope(
        command="git frobnicate-no-such-subcommand", cwd=".", timeout=60,
        expected_exit=1,
    )
    assert run_witness(bad, tmp_path) != 0


# --- the audit (R24) -----------------------------------------------------------------


def test_witness_failure_blocks_audit_mutant_disqualified(tmp_path: Path):
    """A witness that does not reproduce the bad behavior disqualifies the
    mutant and blocks the audit: the verifier is never scored, never flagged."""
    store = make_store(tmp_path)
    seed_chks(store, 2)
    ws = tmp_path / "ws"
    ws.mkdir()
    mutant = make_mutant()  # expected_exit=1
    result = run_mutation_audit(
        store,
        mutant,
        ws,
        SentinelVerifier(),  # raises if the audit scores the verifier
        verifier_id="verifier-A",
        witness_runner=scripted_witness(0),  # bad behavior did NOT reproduce
        stage=False,
    )
    assert result.outcome == "disqualified"
    assert not result.flagged
    assert result.suspect_chk_ids == ()
    assert result.witness_exit == 0
    assert suspect_chk_ids(store, "verifier-A") == ()
    records = audit_log(store, "verifier-A")
    assert [r["outcome"] for r in records] == ["disqualified"]
    assert "equivalent-mutant guard" in records[0]["detail"]


def test_witness_timeout_disqualifies(tmp_path: Path):
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    result = run_mutation_audit(
        store,
        make_mutant(),
        ws,
        SentinelVerifier(),
        verifier_id="verifier-A",
        witness_runner=scripted_witness(None),  # hung witness proves nothing
        stage=False,
    )
    assert result.outcome == "disqualified"


def test_verifier_false_pass_flags_and_marks_suspect_range(tmp_path: Path):
    """False-pass marks exactly the verdicts since the last clean audit."""
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    pre_clean = seed_chks(store, 2)  # verdicts before the clean audit

    catching = FakeVerifier(passed=False)
    clean = run_mutation_audit(
        store, make_mutant("m-one"), ws, catching,
        verifier_id="verifier-A", witness_runner=scripted_witness(1),
        stage=False,
    )
    assert clean.outcome == "caught"
    assert not clean.flagged
    assert catching.seen == [ws]

    in_window = seed_chks(store, 3)  # verdicts after the clean audit
    passing = FakeVerifier(passed=True)
    flagged = run_mutation_audit(
        store, make_mutant("m-two"), ws, passing,
        verifier_id="verifier-A", witness_runner=scripted_witness(1),
        stage=False,
    )
    assert flagged.outcome == "false_pass"
    assert flagged.flagged
    # exactly the post-clean verdicts are suspect — never the pre-clean ones
    assert flagged.suspect_chk_ids == tuple(in_window)
    assert set(suspect_chk_ids(store, "verifier-A")) == set(in_window)
    assert not set(pre_clean) & set(suspect_chk_ids(store, "verifier-A"))
    outcomes = [r["outcome"] for r in audit_log(store, "verifier-A")]
    assert outcomes == ["caught", "false_pass"]


def test_clean_audit_clears_window(tmp_path: Path):
    """A clean audit advances the watermark: later false-passes mark only
    verdicts produced after it; earlier suspect marks stay recorded."""
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    def audit(passed: bool, mutant_id: str):
        return run_mutation_audit(
            store, make_mutant(mutant_id), ws, FakeVerifier(passed=passed),
            verifier_id="verifier-A", witness_runner=scripted_witness(1),
            stage=False,
        )

    first = seed_chks(store, 2)
    flagged = audit(True, "m-one")  # no clean audit yet: full window suspect
    assert flagged.suspect_chk_ids == tuple(first)

    audit(False, "m-two")  # clean audit clears the window
    after_clean = audit(True, "m-three")  # no new verdicts since the clean
    assert after_clean.suspect_chk_ids == ()

    fresh = seed_chks(store, 1)
    again = audit(True, "m-four")
    assert again.suspect_chk_ids == tuple(fresh)  # only post-clean verdicts
    # marks accumulate (recorded and surfaced only — enforcement is Phase 3)
    assert set(suspect_chk_ids(store, "verifier-A")) == set(first) | set(fresh)


def test_disqualified_audit_never_advances_watermark_or_cadence(tmp_path: Path):
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    seed_chks(store, 3)
    config = CalibrateConfig(
        audit_cadence=3, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR
    )
    assert audit_due(store, "verifier-A", config)
    run_mutation_audit(
        store, make_mutant(), ws, SentinelVerifier(),
        verifier_id="verifier-A", witness_runner=scripted_witness(0),
        stage=False,
    )
    # the verifier was never scored: the audit is still due, watermark still 0
    assert audit_due(store, "verifier-A", config)
    assert last_clean_watermark(store, "verifier-A") == 0


def test_audit_due_cadence(tmp_path: Path):
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    config = CalibrateConfig(
        audit_cadence=3, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR
    )
    assert not audit_due(store, "verifier-A", config)  # zero verdicts yet
    seed_chks(store, 2)
    assert not audit_due(store, "verifier-A", config)
    seed_chks(store, 1)
    assert audit_due(store, "verifier-A", config)  # 3 verdicts, never audited
    run_mutation_audit(
        store, make_mutant(), ws, FakeVerifier(passed=False),
        verifier_id="verifier-A", witness_runner=scripted_witness(1),
        stage=False,
    )
    assert not audit_due(store, "verifier-A", config)  # clock reset
    seed_chks(store, 3)
    assert audit_due(store, "verifier-A", config)


def test_run_mutation_audit_validates_inputs(tmp_path: Path):
    store = make_store(tmp_path)
    with pytest.raises(CalibrateError, match="workspace does not exist"):
        run_mutation_audit(
            store, make_mutant(), tmp_path / "nope", FakeVerifier(True),
            verifier_id="v", stage=False,
        )
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(CalibrateError, match="verifier_id"):
        run_mutation_audit(
            store, make_mutant(), ws, FakeVerifier(True),
            verifier_id="  ", stage=False,
        )


def test_calibrate_config_validation():
    CalibrateConfig(audit_cadence=1, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR)
    with pytest.raises(CalibrateError, match="audit_cadence"):
        CalibrateConfig(
            audit_cadence=0, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR
        )
    with pytest.raises(CalibrateError, match="frozen_set_min_size"):
        CalibrateConfig(
            audit_cadence=1, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR - 1
        )


# --- the unit verification: seeded mutant audit end-to-end in CI -------------------
# The committed fixture stages via real `git apply`; the witness runner is
# scripted (the real `npm test` witness is the documented live procedure —
# the offline suite runs with no primed node_modules); the verifier is fake.


def test_seeded_mutant_audit_end_to_end_caught(tmp_path: Path):
    store = make_store(tmp_path)
    seed_chks(store, 1)
    mutant = load_mutant(MUTANTS_DIR / "validation-dropped")
    ws = template_copy(tmp_path)
    verifier = FakeVerifier(passed=False)  # a verifier that does its job
    result = run_mutation_audit(
        store, mutant, ws, verifier,
        verifier_id="ticket-verifier",
        witness_runner=scripted_witness(mutant.witness.expected_exit),
    )
    # the bad diff really landed in the workspace the verifier was shown
    mutated = (ws / "server" / "index.ts").read_text(encoding="utf-8")
    assert "title must be a non-empty string" not in mutated
    assert verifier.seen == [ws]
    assert result.outcome == "caught"
    assert last_clean_watermark(store, "ticket-verifier") >= 1
    health = instrument_health(store)
    assert health["mutation_audits"][0]["mutant_id"] == "validation-dropped"
    assert health["flagged_verifiers"] == []


def test_seeded_mutant_audit_end_to_end_false_pass(tmp_path: Path):
    store = make_store(tmp_path)
    verdicts = seed_chks(store, 2)
    mutant = load_mutant(MUTANTS_DIR / "created-status-wrong")
    ws = template_copy(tmp_path)
    result = run_mutation_audit(
        store, mutant, ws, FakeVerifier(passed=True),
        verifier_id="ticket-verifier",
        witness_runner=scripted_witness(mutant.witness.expected_exit),
    )
    assert result.outcome == "false_pass"
    assert result.flagged
    assert result.suspect_chk_ids == tuple(verdicts)
    health = instrument_health(store)
    assert health["flagged_verifiers"] == ["ticket-verifier"]
    assert health["suspect_chk_ids"]["ticket-verifier"] == sorted(verdicts)


def test_instrument_health_feeds_settlement_report(tmp_path: Path):
    """instrument_health drops into assemble_report's seam (R23) and carries
    no timestamps — settlement reports render deterministically (U7)."""
    store = make_store(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    run_mutation_audit(
        store, make_mutant(), ws, FakeVerifier(passed=True),
        verifier_id="verifier-A", witness_runner=scripted_witness(1),
        stage=False,
    )
    health = instrument_health(store)
    report = assemble_report(
        episode_id=1,
        snapshot_id=0,
        target="linkding",
        verdicts=[],
        scen_ids={},
        instrument_health=health,
    )
    assert report["instrument_health"] == health
    flat = json.dumps(health)
    assert "created_at" not in flat and '"at"' not in flat
    for record in health["mutation_audits"]:
        assert record["outcome"] in AUDIT_OUTCOMES


# --- frozen replay set: bootstrap persistence (R25) ---------------------------------


def seed_scens(store: Store) -> dict[str, str]:
    """An episode with one judged-single, one judged-panel, and one
    deterministic SCEN row (the U7 write path)."""
    episode_id = store.create_episode("linkding", "sha256:cafe", 0)
    with store.transaction():
        for feat in ("FEAT-li-001", "FEAT-li-002", "FEAT-li-003"):
            store.conn.execute(
                "INSERT INTO trace_feat (id, evidence_ref, target, digest)"
                " VALUES (?, 'evidence/seed.json', 'linkding',"
                " 'sha256:cafe')",
                (feat,),
            )
    verdicts = [
        ScenarioVerdict(
            scenario_id="scen-add-bookmark",
            feat_id="FEAT-li-001",
            tier="must",
            verdict="pass",
            judge_mode="single",
            judge_metadata={"confidence": "high", "reasoning": "equivalent"},
            judge_input={"diff": {"clone_only": [], "target_only": ["x"]}},
            failure=None,
            evidence_stale=False,
        ),
        ScenarioVerdict(
            scenario_id="scen-search",
            feat_id="FEAT-li-002",
            tier="should",
            verdict="fail",
            judge_mode="panel",
            judge_metadata={"tally": {"fail": 2, "pass": 1}},
            judge_input={"diff": {"clone_only": ["y"], "target_only": []}},
            failure="judged_different",
            evidence_stale=False,
        ),
        ScenarioVerdict(
            scenario_id="scen-health",
            feat_id="FEAT-li-003",
            tier="free",
            verdict="pass",
            judge_mode="deterministic",
            judge_metadata={"reason": "identical trees"},
            judge_input={},
            failure=None,
            evidence_stale=False,
        ),
    ]
    scen_ids = write_scen_rows(store, episode_id, 0, verdicts)
    return scen_ids


def test_frozen_set_round_trips_pairs_with_judge_inputs(tmp_path: Path):
    store = make_store(tmp_path)
    scen_ids = seed_scens(store)
    judged = [scen_ids["scen-add-bookmark"], scen_ids["scen-search"]]
    path = tmp_path / "frozen" / "linkding.json"

    doc = persist_frozen_set(store, judged, path, verified_by="mattdaw")
    loaded = load_frozen_set(path)
    assert loaded == doc
    assert loaded["version"] == FROZEN_SET_VERSION
    by_id = {p["scen_id"]: p for p in loaded["pairs"]}
    assert set(by_id) == set(judged)
    # the judge-input payloads round-trip intact (R22 — replay re-judging
    # needs the original inputs)
    add = by_id[scen_ids["scen-add-bookmark"]]
    assert add["judge_input"] == {
        "diff": {"clone_only": [], "target_only": ["x"]}
    }
    assert add["verdict"] == "pass" and add["judge_mode"] == "single"
    search = by_id[scen_ids["scen-search"]]
    assert search["judge_input"] == {
        "diff": {"clone_only": ["y"], "target_only": []}
    }
    assert search["verdict"] == "fail" and search["judge_mode"] == "panel"
    assert all(p["verified_by"] == "mattdaw" for p in loaded["pairs"])
    # byte-stable on disk: \n newlines, identical bytes on re-persist
    first_bytes = path.read_bytes()
    assert b"\r" not in first_bytes
    persist_frozen_set(store, judged, path, verified_by="mattdaw")
    assert path.read_bytes() == first_bytes


def test_frozen_set_merge_accumulates(tmp_path: Path):
    store = make_store(tmp_path)
    scen_ids = seed_scens(store)
    path = tmp_path / "frozen.json"
    persist_frozen_set(
        store, [scen_ids["scen-add-bookmark"]], path, verified_by="mattdaw"
    )
    doc = persist_frozen_set(
        store, [scen_ids["scen-search"]], path, verified_by="mattdaw"
    )
    assert {p["scen_id"] for p in doc["pairs"]} == {
        scen_ids["scen-add-bookmark"],
        scen_ids["scen-search"],
    }


def test_frozen_set_conflicting_repersist_rejected(tmp_path: Path):
    store = make_store(tmp_path)
    scen_ids = seed_scens(store)
    path = tmp_path / "frozen.json"
    sid = scen_ids["scen-add-bookmark"]
    persist_frozen_set(store, [sid], path, verified_by="mattdaw")
    store.conn.execute(
        "UPDATE trace_scen SET result = 'fail' WHERE id = ?", (sid,)
    )
    with pytest.raises(CalibrateError, match="append-only"):
        persist_frozen_set(store, [sid], path, verified_by="mattdaw")


def test_frozen_set_rejects_deterministic_and_unknown_scen(tmp_path: Path):
    store = make_store(tmp_path)
    scen_ids = seed_scens(store)
    path = tmp_path / "frozen.json"
    with pytest.raises(CalibrateError, match="deterministically"):
        persist_frozen_set(
            store, [scen_ids["scen-health"]], path, verified_by="mattdaw"
        )
    with pytest.raises(CalibrateError, match="does not exist"):
        persist_frozen_set(
            store, ["SCEN-e1-nope"], path, verified_by="mattdaw"
        )
    with pytest.raises(CalibrateError, match="verified_by"):
        persist_frozen_set(
            store, [scen_ids["scen-search"]], path, verified_by=" "
        )
    with pytest.raises(CalibrateError, match="at least one"):
        persist_frozen_set(store, [], path, verified_by="mattdaw")
    assert not path.exists()  # nothing persisted on any rejection


def test_load_frozen_set_validates_shape(tmp_path: Path):
    path = tmp_path / "frozen.json"
    with pytest.raises(CalibrateError, match="not found"):
        load_frozen_set(path)
    path.write_text('{"version": 99, "pairs": []}', encoding="utf-8")
    with pytest.raises(CalibrateError, match="version"):
        load_frozen_set(path)
    path.write_text(
        json.dumps({"version": FROZEN_SET_VERSION, "pairs": [{"scen_id": "x"}]}),
        encoding="utf-8",
    )
    with pytest.raises(CalibrateError, match="missing"):
        load_frozen_set(path)


def test_frozen_set_ready_is_the_phase2_exit_criterion():
    config = CalibrateConfig(
        audit_cadence=1, frozen_set_min_size=FROZEN_SET_SIZE_FLOOR
    )
    pair = {"scen_id": "SCEN-e1-x"}
    assert not frozen_set_ready({"pairs": [pair] * 19}, config)
    assert frozen_set_ready(
        {"pairs": [pair] * FROZEN_SET_SIZE_FLOOR}, config
    )
