"""Judge runner tests — fully offline, zero quota, no `claude` on PATH (R23).

Subprocess behavior is faked with pytest-subprocess; replay-mode tests hit JSON
fixtures only and must make zero subprocess calls.

Manual record-mode smoke (documented per U4 verification, NOT in CI): with the
claude CLI installed and logged in on the subscription, from agent-families/ run

    $env:AF_JUDGE_MODE = "record"
    $env:AF_JUDGE_FIXTURES = "tests/fixtures/judge"
    uv run python -c "import sys; sys.path.insert(0, 'tests'); import test_judge as t; \
        from agent_families.judge import run_judge; \
        print(run_judge(t.COMMITTED_PROMPT, t.COMMITTED_SCHEMA, t.COMMITTED_MODEL, max_retries=3))"

then inspect the refreshed fixture under tests/fixtures/judge/ and commit it
deliberately.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from agent_families import judge
from agent_families.judge import (
    FIXTURES_ENV,
    MODE_ENV,
    OUTCOMES,
    JudgeError,
    JudgeFixtureMissing,
    JudgeSchemaViolation,
    JudgeUnavailable,
    request_hash,
    resolve_claude_argv,
    run_judge,
    validate_against_schema,
    write_fixture,
)

COMMITTED_PROMPT = (
    "Place this insight in the library taxonomy.\n\n"
    "Insight: Pin subprocess encoding to utf-8 on Windows so judge output parses"
    " identically across machines."
)
COMMITTED_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "confidence": {"type": "number"},
    },
    "required": ["outcome", "confidence"],
    "additionalProperties": False,
}
COMMITTED_MODEL = "sonnet"

COMMITTED_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "judge"


def good_envelope(output: dict | None = None) -> dict:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1234,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.0042,
        "structured_output": {"outcome": "new_skill", "confidence": 0.9},
    }
    if output is not None:
        envelope["structured_output"] = output
    return envelope


@pytest.fixture
def offline_invoke(monkeypatch):
    """Make passthrough/record invocations hit pytest-subprocess as plain `claude`."""
    monkeypatch.setattr(judge, "resolve_claude_argv", lambda: ["claude"])
    monkeypatch.setattr(judge, "_preflight_ok", True)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.delenv(FIXTURES_ENV, raising=False)


# --- request hashing ----------------------------------------------------------


def test_request_hash_is_deterministic_and_key_order_independent():
    h1 = request_hash("p", {"a": 1, "b": 2}, "sonnet")
    h2 = request_hash("p", {"b": 2, "a": 1}, "sonnet")
    assert h1 == h2
    assert request_hash("p", {"a": 1, "b": 2}, "opus") != h1
    assert request_hash("q", {"a": 1, "b": 2}, "sonnet") != h1


# --- replay mode ---------------------------------------------------------------


def test_replay_returns_parsed_result_with_zero_subprocess_calls(
    fp, tmp_path, clean_env, caplog
):
    write_fixture(
        tmp_path, COMMITTED_PROMPT, COMMITTED_SCHEMA, COMMITTED_MODEL, good_envelope()
    )
    with caplog.at_level(logging.INFO, logger="agent_families.judge"):
        result = run_judge(
            COMMITTED_PROMPT,
            COMMITTED_SCHEMA,
            COMMITTED_MODEL,
            max_retries=3,
            fixtures_dir=tmp_path,
        )
    assert result.output == {"outcome": "new_skill", "confidence": 0.9}
    assert result.attempts == 1
    assert result.cost_usd == 0.0042
    assert result.duration_ms == 1234
    assert list(fp.calls) == []
    assert any("cost_usd" in record.getMessage() for record in caplog.records)


def test_committed_fixture_replays(fp, clean_env):
    result = run_judge(
        COMMITTED_PROMPT,
        COMMITTED_SCHEMA,
        COMMITTED_MODEL,
        max_retries=3,
        fixtures_dir=COMMITTED_FIXTURES_DIR,
    )
    assert result.output["outcome"] == "new_skill"
    assert list(fp.calls) == []


def test_replay_is_the_default_mode(fp, tmp_path, clean_env):
    # No mode argument, no env var: a missing fixture proves we are replaying,
    # not invoking the CLI.
    with pytest.raises(JudgeFixtureMissing):
        run_judge("p", COMMITTED_SCHEMA, "sonnet", max_retries=0, fixtures_dir=tmp_path)
    assert list(fp.calls) == []


def test_missing_fixture_names_the_request_hash(tmp_path, clean_env):
    h = request_hash("unrecorded prompt", COMMITTED_SCHEMA, "sonnet")
    with pytest.raises(JudgeFixtureMissing, match=h):
        run_judge(
            "unrecorded prompt",
            COMMITTED_SCHEMA,
            "sonnet",
            max_retries=0,
            fixtures_dir=tmp_path,
        )


def test_fixtures_dir_resolves_from_env_var(fp, tmp_path, monkeypatch, clean_env):
    write_fixture(tmp_path, "env prompt", COMMITTED_SCHEMA, "sonnet", good_envelope())
    monkeypatch.setenv(FIXTURES_ENV, str(tmp_path))
    result = run_judge("env prompt", COMMITTED_SCHEMA, "sonnet", max_retries=0)
    assert result.output["outcome"] == "new_skill"


def test_invalid_mode_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setenv(MODE_ENV, "nonsense")
    with pytest.raises(JudgeError, match=MODE_ENV):
        run_judge("p", COMMITTED_SCHEMA, "sonnet", max_retries=0, fixtures_dir=tmp_path)


def test_is_error_envelope_surfaces_subtype(tmp_path, clean_env):
    envelope = {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "OAuth token expired",
    }
    write_fixture(tmp_path, "p", COMMITTED_SCHEMA, "sonnet", envelope)
    with pytest.raises(JudgeUnavailable, match="error_during_execution"):
        run_judge("p", COMMITTED_SCHEMA, "sonnet", max_retries=0, fixtures_dir=tmp_path)


# --- passthrough mode: transport failures ---------------------------------------


def test_malformed_envelope_retries_then_raises(fp, offline_invoke, clean_env):
    fp.register(["claude", fp.any()], stdout="this is not json", occurrences=3)
    with pytest.raises(JudgeUnavailable, match="malformed"):
        run_judge(
            "p", COMMITTED_SCHEMA, "sonnet", max_retries=2, mode="passthrough"
        )
    assert len(fp.calls) == 3


def test_non_dict_envelope_counts_as_malformed(fp, offline_invoke, clean_env):
    fp.register(["claude", fp.any()], stdout="[1, 2, 3]", occurrences=2)
    with pytest.raises(JudgeUnavailable, match="malformed"):
        run_judge(
            "p", COMMITTED_SCHEMA, "sonnet", max_retries=1, mode="passthrough"
        )
    assert len(fp.calls) == 2


def test_nonzero_exit_raises_judge_unavailable(fp, offline_invoke, clean_env):
    fp.register(
        ["claude", fp.any()], stderr="quota exhausted for today", returncode=1
    )
    with pytest.raises(JudgeUnavailable, match="quota exhausted"):
        run_judge(
            "p", COMMITTED_SCHEMA, "sonnet", max_retries=2, mode="passthrough"
        )
    assert len(fp.calls) == 1


# --- preflight -------------------------------------------------------------------


def test_preflight_missing_executable_names_install_step(monkeypatch):
    monkeypatch.setattr(judge, "_preflight_ok", False)
    monkeypatch.setattr(judge.shutil, "which", lambda name: None)
    with pytest.raises(
        JudgeUnavailable, match=r"npm install -g @anthropic-ai/claude-code"
    ):
        judge.preflight()


def test_preflight_version_check_caches_per_process(fp, monkeypatch):
    monkeypatch.setattr(judge, "_preflight_ok", False)
    monkeypatch.setattr(judge, "resolve_claude_argv", lambda: ["claude"])
    fp.register(["claude", "--version"], stdout="2.0.0")
    judge.preflight()
    judge.preflight()  # cached: no second subprocess registration needed
    assert len(fp.calls) == 1


def test_preflight_nonzero_version_exit_is_unavailable(fp, monkeypatch):
    monkeypatch.setattr(judge, "_preflight_ok", False)
    monkeypatch.setattr(judge, "resolve_claude_argv", lambda: ["claude"])
    fp.register(["claude", "--version"], stderr="not installed правильно", returncode=1)
    with pytest.raises(JudgeUnavailable, match="--version"):
        judge.preflight()


# --- schema-violation feedback retries -------------------------------------------


def test_schema_violation_retries_exactly_n_times(fp, offline_invoke, clean_env):
    bad = {"outcome": "not_a_real_outcome", "confidence": 0.5}
    fp.register(
        ["claude", fp.any()], stdout=json.dumps(good_envelope(bad)), occurrences=3
    )
    with pytest.raises(JudgeSchemaViolation, match="not_a_real_outcome"):
        run_judge(
            "p", COMMITTED_SCHEMA, "sonnet", max_retries=2, mode="passthrough"
        )
    assert len(fp.calls) == 3  # initial call + exactly max_retries feedback retries


def test_schema_violation_recovers_after_feedback(fp, offline_invoke, clean_env):
    bad = {"outcome": "new_skill"}  # missing required 'confidence'
    fp.register(["claude", fp.any()], stdout=json.dumps(good_envelope(bad)))
    fp.register(["claude", fp.any()], stdout=json.dumps(good_envelope()))
    result = run_judge(
        "p", COMMITTED_SCHEMA, "sonnet", max_retries=3, mode="passthrough"
    )
    assert result.attempts == 2
    assert result.output == {"outcome": "new_skill", "confidence": 0.9}
    assert len(fp.calls) == 2


def test_missing_structured_output_is_a_violation(fp, offline_invoke, clean_env):
    envelope = good_envelope()
    del envelope["structured_output"]
    fp.register(["claude", fp.any()], stdout=json.dumps(envelope), occurrences=2)
    with pytest.raises(JudgeSchemaViolation, match="structured_output"):
        run_judge(
            "p", COMMITTED_SCHEMA, "sonnet", max_retries=1, mode="passthrough"
        )
    assert len(fp.calls) == 2


def test_extra_validate_rides_the_same_retry_path(fp, offline_invoke, clean_env):
    # Schema-valid output rejected by the caller's allowed-outcome subset (R6):
    # same feedback-retry-then-fail path as a schema violation.
    fp.register(
        ["claude", fp.any()], stdout=json.dumps(good_envelope()), occurrences=2
    )
    subset_message = "outcome 'new_skill' is not allowed for this call type"
    with pytest.raises(JudgeSchemaViolation, match="not allowed for this call type"):
        run_judge(
            "p",
            COMMITTED_SCHEMA,
            "sonnet",
            max_retries=1,
            mode="passthrough",
            extra_validate=lambda output: subset_message,
        )
    assert len(fp.calls) == 2


def test_replayed_retry_chain_is_fixture_addressable(fp, tmp_path, clean_env):
    # Each feedback retry has a distinct prompt, hence its own fixture key —
    # record once, replay the whole violation-then-recovery chain offline.
    bad = {"outcome": "lint_reject"}  # missing 'confidence'
    write_fixture(tmp_path, "p", COMMITTED_SCHEMA, "sonnet", good_envelope(bad))
    violation = validate_against_schema(bad, COMMITTED_SCHEMA)
    retry_prompt = judge._feedback_prompt("p", bad, violation)
    write_fixture(
        tmp_path, retry_prompt, COMMITTED_SCHEMA, "sonnet", good_envelope()
    )
    result = run_judge(
        "p", COMMITTED_SCHEMA, "sonnet", max_retries=2, fixtures_dir=tmp_path
    )
    assert result.attempts == 2
    assert result.output["outcome"] == "new_skill"
    assert list(fp.calls) == []


# --- argv construction: the Windows quoting probe ---------------------------------


def test_schema_with_quotes_percent_caret_roundtrips_unmodified(
    fp, offline_invoke, clean_env
):
    weird_schema = {
        "type": "object",
        "properties": {
            "reply": {"type": "string", "enum": ['he said "100% sure"', "x^y & z"]},
        },
        "required": ["reply"],
        "additionalProperties": False,
    }
    fp.register(
        ["claude", fp.any()],
        stdout=json.dumps(good_envelope({"reply": "x^y & z"})),
    )
    result = run_judge(
        "the prompt goes via stdin", weird_schema, "sonnet", max_retries=0,
        mode="passthrough",
    )
    assert result.output == {"reply": "x^y & z"}
    call = [str(arg) for arg in fp.calls[0]]
    schema_arg = call[call.index("--json-schema") + 1]
    assert json.loads(schema_arg) == weird_schema
    assert call[call.index("--tools") + 1] == ""
    assert "--bare" not in call
    assert "the prompt goes via stdin" not in call  # content rides stdin, not argv


def test_bare_flag_is_appended_only_when_enabled(fp, offline_invoke, clean_env):
    fp.register(["claude", fp.any()], stdout=json.dumps(good_envelope()))
    run_judge(
        "p", COMMITTED_SCHEMA, "sonnet", max_retries=0, mode="passthrough", bare=True
    )
    call = [str(arg) for arg in fp.calls[0]]
    assert call[-1] == "--bare"
    assert call[call.index("--model") + 1] == "sonnet"


# --- executable resolution ----------------------------------------------------------


def test_resolve_node_entrypoint_from_npm_shim(tmp_path, monkeypatch):
    npm_dir = tmp_path / "npm"
    shim = npm_dir / "claude.CMD"
    entry = npm_dir / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    node = tmp_path / "nodejs" / "node.exe"
    for f in (shim, entry, node):
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text("stub", encoding="utf-8")
    which = {"claude": str(shim), "node": str(node)}
    monkeypatch.setattr(judge.shutil, "which", lambda name: which.get(name))
    assert resolve_claude_argv() == [str(node), str(entry)]


def test_resolve_falls_back_to_shim_when_node_is_missing(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("stub", encoding="utf-8")
    which = {"claude": str(shim)}
    monkeypatch.setattr(judge.shutil, "which", lambda name: which.get(name))
    assert resolve_claude_argv() == [str(shim)]


def test_resolve_plain_executable_is_used_directly(monkeypatch):
    path = str(Path("/usr/local/bin/claude"))
    monkeypatch.setattr(
        judge.shutil, "which", lambda name: path if name == "claude" else None
    )
    assert resolve_claude_argv() == [path]


# --- record mode ------------------------------------------------------------------


def test_record_mode_writes_fixture_then_replays(
    fp, offline_invoke, tmp_path, clean_env
):
    fp.register(["claude", fp.any()], stdout=json.dumps(good_envelope()))
    recorded = run_judge(
        "record me", COMMITTED_SCHEMA, "sonnet", max_retries=0, mode="record",
        fixtures_dir=tmp_path,
    )
    h = request_hash("record me", COMMITTED_SCHEMA, "sonnet")
    fixture_path = tmp_path / f"{h}.json"
    assert fixture_path.exists()
    raw = fixture_path.read_bytes()
    assert b"\r\n" not in raw  # newline discipline: committed fixtures are LF-only
    replayed = run_judge(
        "record me", COMMITTED_SCHEMA, "sonnet", max_retries=0, mode="replay",
        fixtures_dir=tmp_path,
    )
    assert replayed.output == recorded.output
    assert len(fp.calls) == 1  # replay added no subprocess call


# --- schema validator ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("instance", "schema", "fragment"),
    [
        ({"outcome": "bogus"}, {"properties": {"outcome": {"enum": ["a"]}}}, "enum"),
        ({}, {"type": "object", "required": ["outcome"]}, "required"),
        ("text", {"type": "object"}, "expected type object"),
        ({"confidence": "high"}, {"properties": {"confidence": {"type": "number"}}},
         "expected type number"),
        ({"extra": 1}, {"type": "object", "properties": {},
                        "additionalProperties": False}, "unexpected key"),
        ({"tags": ["ok", 7]}, {"properties": {"tags": {
            "type": "array", "items": {"type": "string"}}}}, "tags[1]"),
        ({"confidence": True}, {"properties": {"confidence": {"type": "number"}}},
         "expected type number"),
    ],
)
def test_validator_reports_violations(instance, schema, fragment):
    violation = validate_against_schema(instance, schema)
    assert violation is not None
    assert fragment in violation


def test_validator_accepts_conforming_output():
    assert (
        validate_against_schema(
            {"outcome": "no_placement", "confidence": 0.25}, COMMITTED_SCHEMA
        )
        is None
    )
