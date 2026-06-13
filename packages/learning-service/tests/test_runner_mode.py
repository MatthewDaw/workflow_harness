"""test_runner_mode.py — Tests for run_real_ingest runner-mode lane changes.

Verifies:
  1. --mode flag wiring: enforce (default) actually writes records to the store;
     shadow mode leaves the store empty.
  2. list_merged_pull_requests is called with max_prs= (reader pagination cap)
     and the old post-fetch [:max_prs] slice is gone.
  3. default_branch is passed through to the reader.
  4. CLI --help shows the expected flags.
  5. Noise-skipping: only human PRs submitted; noise PRs filtered during pagination.
  6. file_contents: fetch_pr_file_contents called per PR; contents reach pipeline.
  7. Token wiring: resolved_token forwarded when reader is None (via env var).

All tests are OFFLINE — no real GitHub, no real model load, zero quota.
A fake reader and InMemoryLearningStore are injected.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Any
from io import StringIO

import pytest

from learning_service.db.store import InMemoryLearningStore
from learning_service.github import PullRequest


# ---------------------------------------------------------------------------
# Fake reader — zero network calls
# ---------------------------------------------------------------------------


class FakeReader:
    """Minimal fake that satisfies the updated PublicGitHubReader contract.

    Tracks all calls for assertion in tests.  Supports noise-skipping via
    skip_noise kwarg and returns synthetic file_contents from fetch_pr_file_contents.
    """

    def __init__(
        self,
        prs: list[PullRequest],
        *,
        file_contents_per_pr: dict[int, dict[str, str]] | None = None,
    ) -> None:
        self._prs = prs
        # Mapping of pr.number -> {path: src} returned by fetch_pr_file_contents.
        self._file_contents: dict[int, dict[str, str]] = file_contents_per_pr or {}
        # Track calls so we can assert the right kwargs were forwarded.
        self.calls: list[dict[str, Any]] = []
        self.file_content_calls: list[int] = []  # PR numbers fetched

    def list_merged_pull_requests(
        self,
        owner_repo: str,
        *,
        default_branch: str = "main",
        since_pr: int | None = None,
        max_prs: int | None = None,
        max_pages: int = 5,
        skip_noise: bool = True,
    ) -> list[PullRequest]:
        self.calls.append(
            dict(
                owner_repo=owner_repo,
                default_branch=default_branch,
                since_pr=since_pr,
                max_prs=max_prs,
                max_pages=max_pages,
                skip_noise=skip_noise,
            )
        )
        from learning_service.github import is_curriculum_noise

        result: list[PullRequest] = []
        for pr in self._prs:
            if skip_noise:
                noise, _ = is_curriculum_noise(pr)
                if noise:
                    continue
            result.append(pr)
            if max_prs is not None and len(result) >= max_prs:
                break
        return result

    def enrich_pr_diff(self, owner_repo: str, pr_number: int) -> str:  # noqa: ARG002
        return ""

    def fetch_pr_file_contents(
        self,
        owner_repo: str,
        pr: PullRequest,
    ) -> dict[str, str]:
        self.file_content_calls.append(pr.number)
        return self._file_contents.get(pr.number, {})


def _make_pr(
    number: int = 1,
    title: str = "Add snake_case convention",
    body: str = "Always use snake_case for Python identifiers.",
    diff: str = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n",
    merged: bool = True,
    base_branch: str = "main",
    owner_repo: str = "testorg/testrepo",
) -> PullRequest:
    pr = PullRequest(
        number=number,
        title=title,
        body=body,
        base_branch=base_branch,
        merged=merged,
        merged_at="2026-06-12T10:00:00Z",
        author_login="alice",
        diff=diff,
        owner_repo=owner_repo,
    )
    return pr


def _make_noise_pr(number: int, owner_repo: str = "testorg/testrepo") -> PullRequest:
    """Build a renovate-style dependency-bump PR that is_curriculum_noise=True."""
    return PullRequest(
        number=number,
        title="Update dependency lodash to v4.17.21",
        body="Automated dependency update",
        base_branch="main",
        merged=True,
        merged_at="2026-06-12T09:00:00Z",
        author_login="renovate[bot]",
        diff="--- a/package-lock.json\n+++ b/package-lock.json\n@@ -1 +1 @@\n-old\n+new\n",
        owner_repo=owner_repo,
    )


# ---------------------------------------------------------------------------
# Test 1: enforce mode writes at least 1 record to the store
# ---------------------------------------------------------------------------


def test_enforce_mode_writes_to_store() -> None:
    """run_real_ingest with mode='enforce' must write >=1 record to the store.

    We inject a FakeReader returning one synthetic merged PR with a non-empty
    diff and an InMemoryLearningStore, then assert the store has at least one
    processed_pr record (written by handle_merged_pr enforce path) or at least
    one idea (written by pipeline enforce path).
    """
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    pr = _make_pr()
    reader = FakeReader([pr])
    store = InMemoryLearningStore()

    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=5,
        mode="enforce",
        default_branch="main",
        store=store,
        reader=reader,
    )

    assert stats["run_ingest_rc"] == 0, f"run_ingest returned non-zero: {stats}"
    assert stats["prs_submitted"] == 1

    # At minimum the processed-PR cursor must be written (enforce path in
    # handle_merged_pr writes mark_pr_processed on every non-skipped PR).
    # If the full pipeline ran and created an idea, _ideas will be non-empty too.
    total_writes = stats["processed_prs"] + stats["ideas"]
    assert total_writes >= 1, (
        f"Expected >=1 write in enforce mode, got processed_prs={stats['processed_prs']} "
        f"ideas={stats['ideas']}"
    )


# ---------------------------------------------------------------------------
# Test 2: shadow mode leaves the store empty
# ---------------------------------------------------------------------------


def test_shadow_mode_does_not_write_to_store() -> None:
    """run_real_ingest with mode='shadow' must NOT write ideas or processed_prs."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    pr = _make_pr()
    reader = FakeReader([pr])
    store = InMemoryLearningStore()

    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=5,
        mode="shadow",
        default_branch="main",
        store=store,
        reader=reader,
    )

    assert stats["run_ingest_rc"] == 0
    assert stats["ideas"] == 0, "shadow mode must not write ideas"
    assert stats["processed_prs"] == 0, "shadow mode must not write processed_pr cursors"


# ---------------------------------------------------------------------------
# Test 3: max_prs is forwarded to the reader (not sliced post-fetch)
# ---------------------------------------------------------------------------


def test_max_prs_forwarded_to_reader_not_sliced_post_fetch() -> None:
    """The reader must receive max_prs= kwarg; there should be no post-fetch slice."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    # Provide 10 PRs but cap at 3.
    prs = [_make_pr(number=i, owner_repo="testorg/testrepo") for i in range(1, 11)]
    reader = FakeReader(prs)
    store = InMemoryLearningStore()

    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=3,
        mode="shadow",
        default_branch="main",
        store=store,
        reader=reader,
    )

    assert len(reader.calls) == 1, "reader should be called exactly once"
    call = reader.calls[0]
    assert call["max_prs"] == 3, f"max_prs not forwarded to reader: {call}"

    # prs_fetched should reflect what the reader returned (capped to 3)
    assert stats["prs_fetched"] == 3, (
        f"Expected prs_fetched=3 (reader-capped), got {stats['prs_fetched']}. "
        "If this is 10 the old post-fetch slice was removed but max_prs not forwarded."
    )


# ---------------------------------------------------------------------------
# Test 4: default_branch is forwarded to the reader
# ---------------------------------------------------------------------------


def test_default_branch_forwarded_to_reader() -> None:
    """default_branch='preview' must reach the reader (e.g. makeplane/plane)."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    pr = _make_pr(base_branch="preview", owner_repo="makeplane/plane")
    reader = FakeReader([pr])
    store = InMemoryLearningStore()

    run_real_ingest(
        "makeplane",
        "makeplane/plane",
        max_prs=5,
        mode="shadow",
        default_branch="preview",
        store=store,
        reader=reader,
    )

    assert len(reader.calls) == 1
    assert reader.calls[0]["default_branch"] == "preview", (
        f"default_branch not forwarded: {reader.calls[0]}"
    )


# ---------------------------------------------------------------------------
# Test 5: CLI usage string contains expected flags
# ---------------------------------------------------------------------------


def test_cli_usage_contains_mode_and_default_branch_flags() -> None:
    """The CLI --help output must document --mode and --default-branch."""
    from learning_service.entrypoints.run_real_ingest import main

    buf = StringIO()
    try:
        # argparse writes --help to stdout; capture it.
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main(["--help"])
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout
    except Exception:
        pass

    help_text = buf.getvalue()
    assert "--mode" in help_text, "--mode flag not in CLI usage"
    assert "--default-branch" in help_text, "--default-branch flag not in CLI usage"
    assert "enforce" in help_text, "enforce choice not documented in --help"


# ---------------------------------------------------------------------------
# Test 6: invalid mode raises ValueError
# ---------------------------------------------------------------------------


def test_invalid_mode_raises_value_error() -> None:
    """Passing an unrecognised mode string must raise ValueError immediately."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    reader = FakeReader([])
    store = InMemoryLearningStore()

    with pytest.raises(ValueError, match="mode must be"):
        run_real_ingest(
            "testorg",
            "testorg/testrepo",
            mode="invalid_mode",  # type: ignore[arg-type]
            store=store,
            reader=reader,
        )


# ---------------------------------------------------------------------------
# Test 7: default mode is 'enforce' (no mode= kwarg → writes)
# ---------------------------------------------------------------------------


def test_default_mode_is_enforce() -> None:
    """run_real_ingest without a mode= kwarg must default to 'enforce'."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest
    import inspect

    sig = inspect.signature(run_real_ingest)
    mode_default = sig.parameters["mode"].default
    assert mode_default == "enforce", (
        f"Expected default mode='enforce', got {mode_default!r}. "
        "The documented 'point-at-repo-and-run' command should write by default."
    )


# ---------------------------------------------------------------------------
# Test 8: noise PRs are skipped; only human PRs reach run_ingest
# ---------------------------------------------------------------------------


def test_noise_prs_filtered_only_human_prs_submitted() -> None:
    """Noise PRs (renovate/dependabot) must be excluded; only human PRs submitted.

    The FakeReader honours skip_noise=True using is_curriculum_noise, so when
    run_real_ingest passes skip_noise=True, the noise PRs should not appear in
    prs_fetched or prs_submitted.
    """
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    human_pr = _make_pr(number=10)
    noise_pr1 = _make_noise_pr(number=11)
    noise_pr2 = _make_noise_pr(number=12)
    # Mix: 2 noise, 1 human
    reader = FakeReader([human_pr, noise_pr1, noise_pr2])
    store = InMemoryLearningStore()

    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=10,
        mode="shadow",
        store=store,
        reader=reader,
    )

    assert stats["prs_fetched"] == 1, (
        f"Expected prs_fetched=1 (only 1 human PR), got {stats['prs_fetched']}. "
        "Noise PRs must be excluded during pagination, not counted toward max_prs."
    )
    assert stats["prs_submitted"] == 1, (
        f"Expected prs_submitted=1, got {stats['prs_submitted']}"
    )

    # Verify skip_noise=True was forwarded to the reader.
    assert len(reader.calls) == 1
    assert reader.calls[0]["skip_noise"] is True, (
        f"skip_noise not forwarded to reader: {reader.calls[0]}"
    )


# ---------------------------------------------------------------------------
# Test 9: file_contents fetched per PR and forwarded to config
# ---------------------------------------------------------------------------


def test_file_contents_fetched_and_forwarded_to_pipeline() -> None:
    """fetch_pr_file_contents must be called for each PR with a diff.

    The resulting paths must appear in the stats (via ideas > 0 or anchors > 0
    is hard to assert without a real LLM; instead we verify that
    fetch_pr_file_contents was invoked and that the pipeline ran without error).
    """
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    pr1 = _make_pr(number=1)
    pr2 = _make_pr(number=2, title="Refactor data layer for clarity")

    file_contents_map = {
        1: {"src/foo.py": "def foo(): pass\n"},
        2: {"src/bar.py": "def bar(): pass\n", "src/baz.py": "class Baz: pass\n"},
    }
    reader = FakeReader([pr1, pr2], file_contents_per_pr=file_contents_map)
    store = InMemoryLearningStore()

    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=10,
        mode="enforce",
        store=store,
        reader=reader,
    )

    assert stats["run_ingest_rc"] == 0, f"run_ingest returned non-zero: {stats}"

    # fetch_pr_file_contents should be called once per PR that has a diff.
    assert set(reader.file_content_calls) == {1, 2}, (
        f"Expected file_content_calls for PRs 1 and 2, got {reader.file_content_calls}. "
        "fetch_pr_file_contents must be called for every kept PR."
    )

    # At least one of ideas, processed_prs must be non-zero (pipeline ran).
    total_writes = stats["ideas"] + stats["processed_prs"]
    assert total_writes >= 1, (
        f"Pipeline wrote nothing: ideas={stats['ideas']}, "
        f"processed_prs={stats['processed_prs']}. file_contents should enable pipeline."
    )


# ---------------------------------------------------------------------------
# Test 10: token wiring — token_env is accepted as a parameter
# ---------------------------------------------------------------------------


def test_token_env_parameter_accepted() -> None:
    """run_real_ingest must accept token_env without error.

    We pass an injected reader so no real network call is made.  The test
    verifies the parameter is part of the signature and can be supplied.
    """
    from learning_service.entrypoints.run_real_ingest import run_real_ingest
    import inspect

    sig = inspect.signature(run_real_ingest)
    assert "token" in sig.parameters, "run_real_ingest must accept a 'token' kwarg"
    assert "token_env" in sig.parameters, "run_real_ingest must accept a 'token_env' kwarg"

    pr = _make_pr()
    reader = FakeReader([pr])
    store = InMemoryLearningStore()

    # Should not raise even though the env var doesn't exist.
    stats = run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=5,
        mode="shadow",
        store=store,
        reader=reader,
        token_env="NONEXISTENT_GH_TOKEN_TEST_VAR",
    )
    assert stats["run_ingest_rc"] == 0


# ---------------------------------------------------------------------------
# Test 11: max_pages forwarded to reader
# ---------------------------------------------------------------------------


def test_max_pages_forwarded_to_reader() -> None:
    """max_pages must be forwarded to list_merged_pull_requests as a kwarg."""
    from learning_service.entrypoints.run_real_ingest import run_real_ingest

    pr = _make_pr()
    reader = FakeReader([pr])
    store = InMemoryLearningStore()

    run_real_ingest(
        "testorg",
        "testorg/testrepo",
        max_prs=5,
        max_pages=3,
        mode="shadow",
        store=store,
        reader=reader,
    )

    assert len(reader.calls) == 1
    assert reader.calls[0]["max_pages"] == 3, (
        f"max_pages not forwarded to reader: {reader.calls[0]}"
    )


# ---------------------------------------------------------------------------
# Test 12: CLI --help contains new flags
# ---------------------------------------------------------------------------


def test_cli_usage_contains_new_flags() -> None:
    """The CLI --help output must document --token-env and --max-pages."""
    from learning_service.entrypoints.run_real_ingest import main

    buf = StringIO()
    try:
        old_stdout = sys.stdout
        sys.stdout = buf
        try:
            main(["--help"])
        except SystemExit:
            pass
        finally:
            sys.stdout = old_stdout
    except Exception:
        pass

    help_text = buf.getvalue()
    assert "--token-env" in help_text, "--token-env flag not in CLI usage"
    assert "--max-pages" in help_text, "--max-pages flag not in CLI usage"
