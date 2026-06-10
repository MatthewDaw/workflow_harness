"""plan-002 U3: the ``run_session`` seam and the scripted-agent fake.

Fully offline — zero quota, no ``claude`` on PATH. Live spawning is faked with
pytest-subprocess (the judge-test pattern); the scripted fake itself needs no
subprocess fakery beyond real ``git`` for diff application.

Scenario map (plan-002 U3 Test scenarios, 1:1):

- fake applies a scripted diff + returns scripted output through the seam:
  ``test_scripted_fake_applies_diff_and_returns_scripted_output``
- timeout kills and finalizes span as ``timeout``:
  ``test_timeout_kills_and_finalizes_span_timeout``
- malformed structured output triggers retry-then-fail:
  ``test_malformed_output_retries_then_fails`` (+ recovery/scripted variants)
- quota-classified envelope raises the distinct quota exception
  (fixture-driven): ``test_quota_envelope_fixture_raises_distinct_exception``
- span lifecycle running→finalized:
  ``test_span_lifecycle_running_then_finalized``
- per-role profile flags assembled correctly (pytest-subprocess argv assert):
  ``test_planner/worker/verifier_profile_argv*``

Plus the Approach's canary: every committed fixture diff in
``specs/fixtures/`` must apply cleanly to the template source (offline,
always) and survive the real gate (primed template only — skips on a fresh
clone before the one documented online ``npm ci``, like U2's gate test).

The live smoke procedure is documented in ``pipeline/sessions.py``.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_families import judge
from agent_families.pipeline import sessions
from agent_families.pipeline.sessions import (
    MODE_ENV,
    QUOTA_SUBTYPES,
    SCRIPT_ENV,
    RoleProfile,
    SessionError,
    SessionQuotaExhausted,
    SessionSchemaViolation,
    SessionScriptError,
    SessionTimeout,
    SessionUnavailable,
    build_session_argv,
    classify_envelope,
    planner_profile,
    reset_session_script,
    run_session,
    verifier_profile,
    worker_profile,
)
from agent_families.pipeline.workspace import (
    DEFAULT_LOCK_PATH,
    DEFAULT_TEMPLATE_DIR,
    instantiate_workspace,
    is_template_primed,
)
from agent_families.store import Store

AGENT_FAMILIES_DIR = Path(__file__).parents[1]
FIXTURE_DIFF_DIR = AGENT_FAMILIES_DIR / "specs" / "fixtures"
QUOTA_FIXTURE = Path(__file__).parent / "fixtures" / "sessions" / "quota-envelope.json"

GATE_TIMEOUT_S = 600

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "failed"]},
        "summary": {"type": "string"},
    },
    "required": ["status", "summary"],
    "additionalProperties": False,
}
GOOD_OUTPUT = {"status": "done", "summary": "implemented the ticket"}


# --- helpers -------------------------------------------------------------------


def result_envelope(output: dict | None = GOOD_OUTPUT, **overrides) -> dict:
    envelope = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 1500,
        "num_turns": 2,
        "result": "done",
        "total_cost_usd": 0.05,
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }
    if output is not None:
        envelope["structured_output"] = output
    envelope.update(overrides)
    return envelope


def assistant_event(input_tokens: int = 10, output_tokens: int = 20) -> dict:
    return {
        "type": "assistant",
        "message": {
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [],
        },
    }


def stream_stdout(*events: dict) -> str:
    return "".join(json.dumps(e) + "\n" for e in events)


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def make_repo(root: Path) -> Path:
    """A minimal git workspace the scripted fake can apply diffs to."""
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text(
        "export const answer = 42\n", encoding="utf-8", newline="\n"
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("add", "-A"),
        ("commit", "-m", "init"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    return root


NEW_FILE_DIFF = """\
diff --git a/src/feature.ts b/src/feature.ts
new file mode 100644
--- /dev/null
+++ b/src/feature.ts
@@ -0,0 +1 @@
+export const feature = true
"""


def write_script(path: Path, steps: list[dict]) -> Path:
    path.write_text(
        json.dumps({"steps": steps}, indent=2), encoding="utf-8", newline="\n"
    )
    return path


def scripted_step(
    output: dict | None = GOOD_OUTPUT, **step_fields
) -> dict:
    return {"envelope": result_envelope(output), **step_fields}


def planner() -> RoleProfile:
    return planner_profile(model="opus", max_turns=3, timeout_s=60.0)


def worker(**kw) -> RoleProfile:
    defaults = dict(
        model="sonnet",
        max_turns=20,
        timeout_s=120.0,
        test_commands=("npm test",),
    )
    defaults.update(kw)
    return worker_profile(**defaults)


@pytest.fixture
def offline_invoke(monkeypatch):
    """Live invocations hit pytest-subprocess as plain `claude` (judge pattern)."""
    monkeypatch.setattr(judge, "resolve_claude_argv", lambda: ["claude"])
    monkeypatch.setattr(judge, "_preflight_ok", True)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(MODE_ENV, raising=False)
    monkeypatch.delenv(SCRIPT_ENV, raising=False)


def run_live(fp, tmp_path, profile, *events, max_retries=0, **kw):
    if events:
        fp.register(["claude", fp.any()], stdout=stream_stdout(*events))
    return run_session(
        "prompt rides stdin",
        SCHEMA,
        profile,
        transcript_path=tmp_path / "transcript.jsonl",
        max_retries=max_retries,
        mode="live",
        **kw,
    )


# --- per-role profile flags (R5/R6: pytest-subprocess assertion on argv) ---------


def test_planner_profile_argv_has_no_tool_surface(
    fp, offline_invoke, tmp_path, clean_env
):
    result = run_live(fp, tmp_path, planner(), assistant_event(), result_envelope())
    assert result.output == GOOD_OUTPUT
    call = [str(arg) for arg in fp.calls[0]]
    assert call[call.index("--tools") + 1] == ""
    assert "--allowedTools" not in call
    assert call[call.index("--model") + 1] == "opus"
    assert call[call.index("--max-turns") + 1] == "3"
    assert call[call.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in call
    assert "--no-session-persistence" in call
    assert json.loads(call[call.index("--json-schema") + 1]) == SCHEMA
    assert "prompt rides stdin" not in call  # content rides stdin, not argv


def test_worker_profile_argv_scopes_writes_and_own_tests(
    fp, offline_invoke, tmp_path, clean_env
):
    profile = worker(test_commands=("npm test", "npm run test:unit"))
    run_live(fp, tmp_path, profile, result_envelope())
    call = [str(arg) for arg in fp.calls[0]]
    assert "--tools" not in call
    allowed = call[call.index("--allowedTools") + 1 :]
    for tool in ("Read", "Glob", "Grep", "Write", "Edit"):
        assert tool in allowed
    assert "Bash(npm test:*)" in allowed
    assert "Bash(npm run test:unit:*)" in allowed
    assert "Bash" not in allowed  # never an unscoped shell (§8)


def test_verifier_profile_argv_has_no_write_tools(
    fp, offline_invoke, tmp_path, clean_env
):
    profile = verifier_profile(
        model="sonnet",
        max_turns=15,
        timeout_s=300.0,
        check_commands=("npm run build", "npx playwright test"),
    )
    run_live(fp, tmp_path, profile, result_envelope())
    call = [str(arg) for arg in fp.calls[0]]
    allowed = call[call.index("--allowedTools") + 1 :]
    assert "Write" not in allowed
    assert "Edit" not in allowed
    assert "Read" in allowed
    assert "Bash(npm run build:*)" in allowed
    assert "Bash(npx playwright test:*)" in allowed
    assert "Bash" not in allowed


def test_profile_factories_reject_empty_command_lists():
    with pytest.raises(SessionError, match="test command"):
        worker(test_commands=())
    with pytest.raises(SessionError, match="check command"):
        verifier_profile(
            model="sonnet", max_turns=5, timeout_s=60.0, check_commands=("",)
        )
    with pytest.raises(SessionError, match="max_turns"):
        RoleProfile(role="worker", model="sonnet", max_turns=0, timeout_s=1.0)


def test_store_db_path_never_reaches_agent_argv(
    fp, offline_invoke, tmp_path, clean_env
):
    """R6: agents never receive the store DB path — the orchestrator is the
    sole store writer; the session argv carries no store reference."""
    store = make_store(tmp_path)
    run_live(fp, tmp_path, planner(), result_envelope(), store=store)
    call = [str(arg) for arg in fp.calls[0]]
    assert not any("library.db" in arg for arg in call)
    assert not any(str(tmp_path) in arg for arg in call)


def test_live_transcript_captures_the_stream(fp, offline_invoke, tmp_path, clean_env):
    run_live(fp, tmp_path, planner(), assistant_event(), result_envelope())
    lines = [
        json.loads(line)
        for line in (tmp_path / "transcript.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert any(e.get("type") == "assistant" for e in lines)
    assert lines[-1]["type"] == "result"


# --- scripted fake (R7) -----------------------------------------------------------


def test_scripted_fake_applies_diff_and_returns_scripted_output(tmp_path, clean_env):
    repo = make_repo(tmp_path / "ws")
    diff = tmp_path / "diffs" / "step1.diff"
    diff.parent.mkdir()
    diff.write_text(NEW_FILE_DIFF, encoding="utf-8", newline="\n")
    script = write_script(
        tmp_path / "script.json",
        [scripted_step(apply_diff="diffs/step1.diff", role="worker")],
    )
    result = run_session(
        "implement the ticket",
        SCHEMA,
        worker(),
        transcript_path=tmp_path / "t.jsonl",
        max_retries=0,
        workspace_root=repo,
        mode="scripted",
        script_path=script,
    )
    # the scripted diff landed in the workspace...
    assert (repo / "src" / "feature.ts").read_text(encoding="utf-8") == (
        "export const feature = true\n"
    )
    # ...and the scripted output came back through the same interface.
    assert result.output == GOOD_OUTPUT
    assert result.attempts == 1
    assert result.cost_usd == 0.05
    transcript = (tmp_path / "t.jsonl").read_text(encoding="utf-8")
    assert json.loads(transcript.splitlines()[-1])["type"] == "result"


def test_scripted_fake_selected_via_env_vars(tmp_path, clean_env, monkeypatch):
    script = write_script(tmp_path / "script.json", [scripted_step()])
    monkeypatch.setenv(MODE_ENV, "scripted")
    monkeypatch.setenv(SCRIPT_ENV, str(script))
    result = run_session(
        "p", SCHEMA, planner(), transcript_path=tmp_path / "t.jsonl", max_retries=0
    )
    assert result.output == GOOD_OUTPUT


def test_scripted_steps_consumed_in_order_across_calls(tmp_path, clean_env):
    first = {"status": "done", "summary": "step one"}
    second = {"status": "done", "summary": "step two"}
    script = write_script(
        tmp_path / "script.json", [scripted_step(first), scripted_step(second)]
    )
    common = dict(
        transcript_path=tmp_path / "t.jsonl",
        max_retries=0,
        mode="scripted",
        script_path=script,
    )
    assert run_session("a", SCHEMA, planner(), **common).output == first
    assert run_session("b", SCHEMA, planner(), **common).output == second
    with pytest.raises(SessionScriptError, match="exhausted"):
        run_session("c", SCHEMA, planner(), **common)
    # the cursor sidecar is durable but resettable
    reset_session_script(script)
    assert run_session("d", SCHEMA, planner(), **common).output == first


def test_scripted_role_mismatch_is_actionable(tmp_path, clean_env):
    script = write_script(tmp_path / "script.json", [scripted_step(role="planner")])
    with pytest.raises(SessionScriptError, match="planner.*worker"):
        run_session(
            "p",
            SCHEMA,
            worker(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=0,
            mode="scripted",
            script_path=script,
        )


def test_scripted_diff_without_workspace_is_actionable(tmp_path, clean_env):
    script = write_script(
        tmp_path / "script.json", [scripted_step(apply_diff="x.diff")]
    )
    with pytest.raises(SessionScriptError, match="workspace_root"):
        run_session(
            "p",
            SCHEMA,
            worker(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=0,
            mode="scripted",
            script_path=script,
        )


def test_invalid_session_mode_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(MODE_ENV, "nonsense")
    with pytest.raises(SessionError, match=MODE_ENV):
        run_session(
            "p", SCHEMA, planner(), transcript_path=tmp_path / "t.jsonl", max_retries=0
        )


# --- span lifecycle (R16) ------------------------------------------------------------


def test_span_lifecycle_running_then_finalized(tmp_path, clean_env, monkeypatch):
    store = make_store(tmp_path)
    status_at_finalize: list[str] = []
    real_finalize = store.finalize_span

    def spy(span_id, status, **kw):
        row = store.conn.execute(
            "SELECT status FROM trace_span WHERE id = ?", (span_id,)
        ).fetchone()
        status_at_finalize.append(row["status"])
        return real_finalize(span_id, status, **kw)

    monkeypatch.setattr(store, "finalize_span", spy)
    script = write_script(tmp_path / "script.json", [scripted_step()])
    result = run_session(
        "p",
        SCHEMA,
        worker(),
        transcript_path=tmp_path / "t.jsonl",
        max_retries=0,
        mode="scripted",
        script_path=script,
        store=store,
        family="builders",
        agent="worker-1",
        ralph_iteration=3,
        prompt_set_version="ps-test",
    )
    # registered `running` at spawn, finalized exactly once on exit
    assert status_at_finalize == ["running"]
    row = store.conn.execute("SELECT * FROM trace_span").fetchone()
    assert row["id"] == result.span_id
    assert row["status"] == "completed"
    assert row["cost_partial"] == 0
    assert row["cost_usd"] == 0.05
    assert row["num_turns"] == 2
    assert row["input_tokens"] == 100
    assert row["output_tokens"] == 50
    assert row["family"] == "builders"
    assert row["agent"] == "worker-1"
    assert row["ralph_iteration"] == 3
    assert row["model_version"] == "sonnet"
    assert row["prompt_set_version"] == "ps-test"


def test_span_agent_defaults_to_role(tmp_path, clean_env):
    store = make_store(tmp_path)
    script = write_script(tmp_path / "script.json", [scripted_step()])
    run_session(
        "p",
        SCHEMA,
        planner(),
        transcript_path=tmp_path / "t.jsonl",
        max_retries=0,
        mode="scripted",
        script_path=script,
        store=store,
    )
    row = store.conn.execute("SELECT agent, model_version FROM trace_span").fetchone()
    assert row["agent"] == "planner"
    assert row["model_version"] == "opus"


# --- timeout (R5/R16) -------------------------------------------------------------------


def test_timeout_kills_and_finalizes_span_timeout(
    fp, offline_invoke, tmp_path, clean_env
):
    store = make_store(tmp_path)
    # a session that streams one assistant message then hangs past the wall clock
    fp.register(
        ["claude", fp.any()],
        stdout=stream_stdout(assistant_event(7, 13)),
        wait=2.0,
    )
    profile = worker(timeout_s=0.05)
    with pytest.raises(SessionTimeout, match="killed"):
        run_session(
            "p",
            SCHEMA,
            profile,
            transcript_path=tmp_path / "t.jsonl",
            max_retries=2,
            mode="live",
            store=store,
        )
    # the kill consumed exactly one spawn — a timeout is not retried here
    assert len(fp.calls) == 1
    row = store.conn.execute("SELECT * FROM trace_span").fetchone()
    assert row["status"] == "timeout"
    # cost/turn fields accumulated best-effort from per-message stream usage,
    # flagged cost_partial (R16): no final envelope ever arrived.
    assert row["cost_partial"] == 1
    assert row["input_tokens"] == 7
    assert row["output_tokens"] == 13
    assert row["num_turns"] == 1
    assert row["cost_usd"] is None
    # the partial transcript persisted
    assert (tmp_path / "t.jsonl").exists()


# --- quota classification (R4) --------------------------------------------------------


def test_quota_envelope_fixture_raises_distinct_exception(tmp_path, clean_env):
    """Fixture-driven: the committed quota envelope (probe landing spot) flows
    through the scripted fake and raises the DISTINCT quota exception."""
    fixture = json.loads(QUOTA_FIXTURE.read_text(encoding="utf-8"))
    store = make_store(tmp_path)
    script = write_script(
        tmp_path / "script.json", [{"envelope": fixture["envelope"]}]
    )
    with pytest.raises(SessionQuotaExhausted, match="quota/rate-limit"):
        run_session(
            "p",
            SCHEMA,
            worker(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=3,
            mode="scripted",
            script_path=script,
            store=store,
        )
    # quota consumed no retries and the span finalized as error
    row = store.conn.execute("SELECT status FROM trace_span").fetchone()
    assert row["status"] == "error"


@pytest.mark.parametrize("subtype", sorted(QUOTA_SUBTYPES))
def test_quota_subtypes_classify_as_quota(subtype):
    envelope = result_envelope(is_error=True, subtype=subtype, result="")
    assert classify_envelope(envelope) == "quota"


def test_plain_error_envelope_is_not_quota(tmp_path, clean_env):
    envelope = result_envelope(
        None, is_error=True, subtype="error_during_execution",
        result="OAuth token expired",
    )
    script = write_script(tmp_path / "script.json", [{"envelope": envelope}])
    with pytest.raises(SessionUnavailable, match="error_during_execution") as exc:
        run_session(
            "p",
            SCHEMA,
            planner(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=0,
            mode="scripted",
            script_path=script,
        )
    assert not isinstance(exc.value, SessionQuotaExhausted)


def test_nonzero_exit_with_quota_stderr_raises_quota(
    fp, offline_invoke, tmp_path, clean_env
):
    fp.register(
        ["claude", fp.any()],
        returncode=1,
        stderr="Claude usage limit reached. Your limit will reset at 3pm.\n",
    )
    with pytest.raises(SessionQuotaExhausted):
        run_live(fp, tmp_path, planner())


def test_nonzero_exit_without_quota_marker_is_unavailable(
    fp, offline_invoke, tmp_path, clean_env
):
    fp.register(["claude", fp.any()], returncode=1, stderr="segfault\n")
    with pytest.raises(SessionUnavailable, match="exited 1"):
        run_live(fp, tmp_path, planner())


def test_classify_ok_envelope():
    assert classify_envelope(result_envelope()) == "ok"


# --- structured-output retry-then-fail (R6) ---------------------------------------------


def test_malformed_output_retries_then_fails(fp, offline_invoke, tmp_path, clean_env):
    store = make_store(tmp_path)
    bad = {"status": "not_an_allowed_status", "summary": "x"}
    fp.register(
        ["claude", fp.any()],
        stdout=stream_stdout(result_envelope(bad)),
        occurrences=3,
    )
    with pytest.raises(SessionSchemaViolation, match="not_an_allowed_status"):
        run_live(fp, tmp_path, planner(), max_retries=2, store=store)
    # initial call + exactly max_retries fresh invocations, one span each
    assert len(fp.calls) == 3
    rows = store.conn.execute(
        "SELECT status FROM trace_span ORDER BY id"
    ).fetchall()
    assert [r["status"] for r in rows] == ["completed"] * 3


def test_malformed_output_recovers_after_feedback(
    fp, offline_invoke, tmp_path, clean_env
):
    fp.register(
        ["claude", fp.any()],
        stdout=stream_stdout(result_envelope({"status": "done"})),  # missing summary
    )
    fp.register(["claude", fp.any()], stdout=stream_stdout(result_envelope()))
    result = run_live(fp, tmp_path, planner(), max_retries=3)
    assert result.attempts == 2
    assert result.output == GOOD_OUTPUT


def test_missing_structured_output_is_a_violation(tmp_path, clean_env):
    script = write_script(
        tmp_path / "script.json", [{"envelope": result_envelope(None)}]
    )
    with pytest.raises(SessionSchemaViolation, match="structured_output"):
        run_session(
            "p",
            SCHEMA,
            planner(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=0,
            mode="scripted",
            script_path=script,
        )


def test_scripted_retry_consumes_next_step(tmp_path, clean_env):
    """The fake mirrors the seam exactly: a violating scripted step rides the
    same feedback-retry path and the retry consumes the NEXT script step."""
    bad = {"status": "done"}  # missing required summary
    script = write_script(
        tmp_path / "script.json", [scripted_step(bad), scripted_step()]
    )
    result = run_session(
        "p",
        SCHEMA,
        planner(),
        transcript_path=tmp_path / "t.jsonl",
        max_retries=1,
        mode="scripted",
        script_path=script,
    )
    assert result.attempts == 2
    assert result.output == GOOD_OUTPUT


def test_extra_validate_rides_the_same_retry_path(tmp_path, clean_env):
    script = write_script(
        tmp_path / "script.json", [scripted_step(), scripted_step()]
    )
    with pytest.raises(SessionSchemaViolation, match="caller subset says no"):
        run_session(
            "p",
            SCHEMA,
            planner(),
            transcript_path=tmp_path / "t.jsonl",
            max_retries=1,
            mode="scripted",
            script_path=script,
            extra_validate=lambda output: "caller subset says no",
        )


# --- argv builder sanity --------------------------------------------------------------


def test_build_session_argv_serializes_schema_canonically(monkeypatch):
    monkeypatch.setattr(judge, "resolve_claude_argv", lambda: ["claude"])
    argv = build_session_argv(planner(), json.dumps(SCHEMA, sort_keys=True))
    assert argv[0] == "claude"
    assert argv.count("--json-schema") == 1


# --- canary: committed fixture diffs vs the real template (Approach) --------------------

FIXTURE_DIFFS = sorted(FIXTURE_DIFF_DIR.glob("*.diff"))


def test_committed_fixture_diffs_exist():
    assert FIXTURE_DIFFS, (
        "plan-002 U3 commits scripted-session fixture diffs under"
        f" {FIXTURE_DIFF_DIR}"
    )


def _template_source_repo(dest: Path) -> Path:
    """Copy the real template's SOURCE (no installed/generated trees) and git-init
    it — enough for `git apply` checks without npm or priming."""
    shutil.copytree(
        DEFAULT_TEMPLATE_DIR,
        dest,
        ignore=shutil.ignore_patterns(
            "node_modules", "dist", ".vite", "data", "test-results",
            "playwright-report", "coverage",
        ),
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("add", "-A"),
        ("commit", "-m", "template source"),
    ):
        subprocess.run(
            ["git", "-C", str(dest), *args], check=True, capture_output=True
        )
    return dest


@pytest.mark.parametrize(
    "diff_path", FIXTURE_DIFFS, ids=[d.name for d in FIXTURE_DIFFS]
)
def test_committed_fixture_diffs_apply_cleanly_to_template_source(
    tmp_path, diff_path
):
    repo = _template_source_repo(tmp_path / "template-src")
    proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--check", str(diff_path)],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.returncode == 0, (
        f"fixture diff {diff_path.name} no longer applies to the template"
        f" source:\n{proc.stderr}"
    )


primed = pytest.mark.skipif(
    not is_template_primed(DEFAULT_TEMPLATE_DIR) or shutil.which("npm") is None,
    reason=(
        "pinned template not primed (run `npm ci` in agent-families/template"
        " once, online) or npm unavailable"
    ),
)


@primed
@pytest.mark.parametrize(
    "diff_path", FIXTURE_DIFFS, ids=[d.name for d in FIXTURE_DIFFS]
)
def test_committed_fixture_diffs_survive_real_gate(tmp_path, diff_path):
    """Approach canary: a template bump that breaks a fixture fails loudly as a
    FIXTURE problem, not a phantom pipeline bug."""
    ws = instantiate_workspace(
        DEFAULT_TEMPLATE_DIR, tmp_path / "ws", lock_path=DEFAULT_LOCK_PATH
    )
    sessions._apply_diff(ws.root, diff_path)
    npm = shutil.which("npm")
    for script in ("typecheck", "lint", "test"):
        proc = subprocess.run(
            [npm, "run", script],
            cwd=ws.root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=GATE_TIMEOUT_S,
        )
        assert proc.returncode == 0, (
            f"fixture {diff_path.name} broke gate command `npm run {script}`:\n"
            f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
