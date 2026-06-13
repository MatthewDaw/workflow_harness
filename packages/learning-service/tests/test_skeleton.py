"""MAT-148 (F1) — Python learning-service skeleton + container-Lambda scaffold.

Acceptance checklist (from Linear MAT-148):
  [x] Service builds + runs offline; container image scaffold present
  [x] nli.classify + judge.run_judge importable and callable in-process
  [x] No SQLite/sqlite-vec dependency in the live path
  [x] Ingest-job entrypoint stub + (deferred) webhook stub

Tests here are OFFLINE (no model load, no network, no quota) — all NLI calls
go through the record/replay seam in AF_NLI_MODE=replay.

The ``slow`` marker is reserved for tests that load the real cross-encoder model;
those are opt-in via ``pytest --run-slow``.
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Acceptance item 1: nli.classify importable and callable in-process
# ---------------------------------------------------------------------------


def test_nli_classify_importable():
    """learning_service.nli must re-export agent_families.nli.classify."""
    import learning_service.nli as ls_nli
    import agent_families.nli as af_nli

    # The re-exported symbol must be the same function object.
    assert ls_nli.classify is af_nli.classify, (
        "learning_service.nli.classify must be agent_families.nli.classify — "
        "not a copy, the same object"
    )


def test_nli_result_type_importable():
    """NliResult dataclass must be importable from learning_service.nli."""
    from learning_service.nli import NliResult
    assert NliResult is not None


def test_nli_classify_callable_replay(tmp_path):
    """classify() returns an NliResult in replay mode given a pre-seeded fixture.

    We seed a minimal fixture file for a known (premise, hypothesis) pair, then
    call classify() in replay mode and assert the result matches the fixture.
    """
    import json
    from learning_service.nli import classify, request_hash, NliResult

    model = "cross-encoder/nli-deberta-v3-base"
    premise = "Always use snake_case for Python identifiers."
    hypothesis = "Python identifiers should use camelCase."

    # Build the fixture envelope.
    h = request_hash(model, premise, hypothesis)
    envelope = {
        "label": "contradiction",
        "confidence": 0.9123,
        "logits": [2.5, -1.2, 0.1],
    }
    fixture_data = {
        "request_hash": h,
        "request": {"model": model, "premise": premise, "hypothesis": hypothesis},
        "envelope": envelope,
    }
    fixture_file = tmp_path / f"{h}.json"
    fixture_file.write_text(
        json.dumps(fixture_data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    result = classify(
        premise,
        hypothesis,
        model=model,
        mode="replay",
        fixtures_dir=tmp_path,
    )

    assert isinstance(result, NliResult)
    assert result.label == "contradiction"
    assert abs(result.confidence - 0.9123) < 1e-6
    assert result.request_hash == h


def test_nli_replay_byte_identical_offline(tmp_path):
    """Two replay calls for the same (premise, hypothesis) return identical results."""
    import json
    from learning_service.nli import classify, request_hash

    model = "cross-encoder/nli-deberta-v3-base"
    premise = "Prefer composition over inheritance."
    hypothesis = "Inheritance should be preferred over composition."

    h = request_hash(model, premise, hypothesis)
    envelope = {"label": "contradiction", "confidence": 0.88, "logits": [2.0, -0.5, 0.2]}
    fixture_data = {
        "request_hash": h,
        "request": {"model": model, "premise": premise, "hypothesis": hypothesis},
        "envelope": envelope,
    }
    (tmp_path / f"{h}.json").write_text(
        json.dumps(fixture_data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    r1 = classify(premise, hypothesis, model=model, mode="replay", fixtures_dir=tmp_path)
    r2 = classify(premise, hypothesis, model=model, mode="replay", fixtures_dir=tmp_path)

    assert r1.label == r2.label
    assert r1.confidence == r2.confidence
    assert r1.request_hash == r2.request_hash


# ---------------------------------------------------------------------------
# Acceptance item 2: judge.run_judge importable and callable in-process
# ---------------------------------------------------------------------------


def test_judge_run_judge_importable():
    """learning_service.judge must re-export agent_families.judge.run_judge."""
    import learning_service.judge as ls_judge
    import agent_families.judge as af_judge

    assert ls_judge.run_judge is af_judge.run_judge, (
        "learning_service.judge.run_judge must be agent_families.judge.run_judge"
    )


def test_judge_result_type_importable():
    """JudgeResult dataclass must be importable from learning_service.judge."""
    from learning_service.judge import JudgeResult
    assert JudgeResult is not None


def test_judge_resolve_edge_schema_importable():
    """RESOLVE_EDGE_SCHEMA must be importable from learning_service.judge."""
    from learning_service.judge import RESOLVE_EDGE_SCHEMA
    assert isinstance(RESOLVE_EDGE_SCHEMA, dict)
    assert "properties" in RESOLVE_EDGE_SCHEMA


def test_judge_run_judge_callable_replay(tmp_path):
    """run_judge() returns a JudgeResult in replay mode given a pre-seeded fixture."""
    import json
    from learning_service.judge import run_judge, request_hash, JudgeResult, RESOLVE_EDGE_SCHEMA

    model = "claude-sonnet-4-5"
    prompt = "Are these insights consistent? A: use snake_case. B: use camelCase."
    h = request_hash(prompt, RESOLVE_EDGE_SCHEMA, model)

    envelope = {
        "structured_output": {"outcome": "contradicts", "confidence": 0.95, "rationale": "opposite naming conventions"},
        "is_error": False,
        "total_cost_usd": 0.001,
        "duration_ms": 1200,
    }
    fixture_data = {
        "request_hash": h,
        "request": {"model": model, "prompt": prompt, "schema": RESOLVE_EDGE_SCHEMA},
        "envelope": envelope,
    }
    (tmp_path / f"{h}.json").write_text(
        json.dumps(fixture_data, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )

    result = run_judge(
        prompt,
        RESOLVE_EDGE_SCHEMA,
        model,
        max_retries=1,
        mode="replay",
        fixtures_dir=tmp_path,
    )

    assert isinstance(result, JudgeResult)
    assert result.output["outcome"] == "contradicts"
    assert result.request_hash == h


# ---------------------------------------------------------------------------
# Acceptance item 3: No SQLite/sqlite-vec dependency in the live path
# ---------------------------------------------------------------------------


def test_no_sqlite_vec_in_learning_service():
    """learning_service must not import sqlite-vec or agent_families.store/vecindex."""
    # Collect all modules currently imported under learning_service.
    forbidden_fragments = ("sqlite_vec", "sqlite-vec", "agent_families.store", "agent_families.vecindex")
    imported_modules = list(sys.modules.keys())

    # Import the full learning_service package to trigger any transitive imports.
    import learning_service
    import learning_service.nli
    import learning_service.judge
    import learning_service.entrypoints.ingest
    import learning_service.entrypoints.webhook

    after = set(sys.modules.keys())
    new_imports = after - set(imported_modules)

    bad = [m for m in new_imports if any(f in m for f in forbidden_fragments)]
    assert not bad, (
        f"learning_service imports forbidden SQLite-coupled modules: {bad}\n"
        "The live loop must not depend on store.py / vecindex.py / sqlite-vec."
    )


def test_agent_families_store_not_imported():
    """agent_families.store must not be in sys.modules after importing learning_service."""
    # Ensure a fresh import context check.
    for key in list(sys.modules.keys()):
        if "agent_families.store" in key or "agent_families.vecindex" in key:
            # These might already be loaded from a prior test; skip this check if so.
            pytest.skip("agent_families.store already imported by a prior test — isolation not possible in this run")

    import learning_service.nli
    import learning_service.judge

    assert "agent_families.store" not in sys.modules, (
        "agent_families.store must not be imported transitively via learning_service"
    )
    assert "agent_families.vecindex" not in sys.modules, (
        "agent_families.vecindex must not be imported transitively via learning_service"
    )


def test_sqlite_vec_not_imported_after_nli_import():
    """sqlite_vec must not appear in sys.modules after importing nli bridge."""
    import learning_service.nli  # noqa: F401

    bad = [k for k in sys.modules if "sqlite_vec" in k or "sqlite-vec" in k]
    assert not bad, f"sqlite_vec appeared in sys.modules after nli import: {bad}"


# ---------------------------------------------------------------------------
# Acceptance item 4a: Ingest-job entrypoint stub is runnable
# ---------------------------------------------------------------------------


def test_ingest_entrypoint_importable():
    """learning_service.entrypoints.ingest must be importable."""
    from learning_service.entrypoints import ingest
    assert hasattr(ingest, "main")
    assert hasattr(ingest, "run_ingest")
    assert hasattr(ingest, "IngestConfig")


def test_ingest_run_ingest_returns_zero_on_valid_config():
    """run_ingest with a valid config must return 0 (success) in the stub."""
    from learning_service.entrypoints.ingest import run_ingest, IngestConfig

    cfg = IngestConfig(org="acme", repo="acme/backend", mode="shadow")
    rc = run_ingest(cfg)
    assert rc == 0, f"run_ingest returned {rc}, expected 0"


def test_ingest_run_ingest_returns_nonzero_on_invalid_mode():
    """run_ingest with an invalid mode must return a non-zero exit code."""
    from learning_service.entrypoints.ingest import run_ingest, IngestConfig

    cfg = IngestConfig(org="acme", repo="acme/backend", mode="invalid_mode")
    rc = run_ingest(cfg)
    assert rc != 0, "run_ingest should reject an invalid mode"


def test_ingest_run_ingest_returns_nonzero_on_empty_org():
    """run_ingest with an empty org must return a non-zero exit code."""
    from learning_service.entrypoints.ingest import run_ingest, IngestConfig

    cfg = IngestConfig(org="", repo="acme/backend", mode="shadow")
    rc = run_ingest(cfg)
    assert rc != 0, "run_ingest should reject an empty org"


def test_ingest_main_parses_args(capsys):
    """main() must parse --org / --repo / --mode without error."""
    from learning_service.entrypoints.ingest import main
    import pytest

    with pytest.raises(SystemExit) as exc_info:
        main(["--org", "acme", "--repo", "acme/backend", "--mode", "shadow"])
    assert exc_info.value.code == 0


def test_ingest_config_defaults():
    """IngestConfig defaults must match the plan config values."""
    from learning_service.entrypoints.ingest import IngestConfig

    cfg = IngestConfig(org="acme", repo="acme/backend")
    assert cfg.mode == "shadow"
    assert cfg.verified_k == 2.0
    assert cfg.nli_mode == "replay"
    assert cfg.judge_mode == "replay"


# ---------------------------------------------------------------------------
# Acceptance item 4b: Deferred webhook stub is importable + has handler
# ---------------------------------------------------------------------------


def test_webhook_stub_importable():
    """learning_service.entrypoints.webhook must be importable and expose handler."""
    from learning_service.entrypoints import webhook
    assert hasattr(webhook, "handler"), "webhook module must expose a 'handler' callable"
    assert callable(webhook.handler)


def test_webhook_handler_returns_200():
    """The webhook stub handler must return a 200-status dict (Lambda contract)."""
    from learning_service.entrypoints.webhook import handler

    response = handler({"source": "test"}, object())
    assert isinstance(response, dict)
    assert response.get("statusCode") == 200


# ---------------------------------------------------------------------------
# Container image scaffold sanity check
# ---------------------------------------------------------------------------


def test_dockerfile_exists():
    """Dockerfile must exist in the learning-service package root."""
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    assert dockerfile.exists(), (
        f"Dockerfile not found at {dockerfile}; the container image scaffold is required"
    )


def test_dockerfile_does_not_install_sqlite_vec():
    """Dockerfile must not have a pip install line for sqlite-vec.

    Comments explaining what is NOT installed are fine; what matters is that
    no ``pip install`` or requirements line names sqlite-vec as a package to add.
    """
    dockerfile = Path(__file__).parent.parent / "Dockerfile"
    lines = dockerfile.read_text(encoding="utf-8").splitlines()
    install_lines = [
        ln for ln in lines
        if not ln.strip().startswith("#") and ("pip install" in ln.lower() or "requirements" in ln.lower())
    ]
    bad = [ln for ln in install_lines if "sqlite-vec" in ln.lower() or "sqlite_vec" in ln.lower()]
    assert not bad, (
        "Dockerfile must not pip-install sqlite-vec — that dependency belongs only in "
        f"the agent-families offline sandbox. Offending lines: {bad}"
    )


def test_pyproject_does_not_list_sqlite_vec():
    """pyproject.toml must not list sqlite-vec as an installable dependency.

    Comments explaining the exclusion are fine; what matters is that the
    [project] dependencies array does not contain sqlite-vec.
    """
    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    deps = data.get("project", {}).get("dependencies", [])
    bad = [d for d in deps if "sqlite-vec" in d.lower() or "sqlite_vec" in d.lower()]
    assert not bad, (
        f"pyproject.toml [project].dependencies must not list sqlite-vec: {bad}"
    )
    # Also check dev deps.
    dev_deps = data.get("dependency-groups", {}).get("dev", [])
    bad_dev = [d for d in dev_deps if isinstance(d, str) and ("sqlite-vec" in d.lower() or "sqlite_vec" in d.lower())]
    assert not bad_dev, (
        f"pyproject.toml [dependency-groups].dev must not list sqlite-vec: {bad_dev}"
    )


# ---------------------------------------------------------------------------
# Slow (real model) smoke — opt in with pytest --run-slow
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_nli_classify_real_model_contradiction():
    """Real cross-encoder classifies a clear contradiction correctly (slow — model load)."""
    from learning_service.nli import classify

    result = classify(
        "Always use snake_case for Python function names.",
        "Python function names should use camelCase.",
        mode="passthrough",
    )
    assert result.label == "contradiction", (
        f"Expected 'contradiction' for opposing naming conventions, got {result.label!r}"
    )
    assert result.confidence > 0.5
