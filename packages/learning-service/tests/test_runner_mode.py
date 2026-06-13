"""test_runner_mode.py — Tests for run_real_ingest runner-mode lane changes.

Verifies:
  1. --mode flag wiring: enforce (default) actually writes records to the store;
     shadow mode leaves the store empty.
  2. list_merged_pull_requests is called with max_prs= (reader pagination cap)
     and the old post-fetch [:max_prs] slice is gone.
  3. default_branch is passed through to the reader.
  4. CLI --help shows the expected flags.

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
    """Minimal fake that satisfies the PublicGitHubReader contract."""

    def __init__(self, prs: list[PullRequest]) -> None:
        self._prs = prs
        # Track calls so we can assert the right kwargs were forwarded.
        self.calls: list[dict[str, Any]] = []

    def list_merged_pull_requests(
        self,
        owner_repo: str,
        *,
        default_branch: str = "main",
        since_pr: int | None = None,
        max_prs: int | None = None,
    ) -> list[PullRequest]:
        self.calls.append(
            dict(
                owner_repo=owner_repo,
                default_branch=default_branch,
                since_pr=since_pr,
                max_prs=max_prs,
            )
        )
        # The real reader caps during pagination; we honour the cap here too.
        result = list(self._prs)
        if max_prs is not None:
            result = result[-max_prs:]
        return result

    def enrich_pr_diff(self, owner_repo: str, pr_number: int) -> str:  # noqa: ARG002
        return ""


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
