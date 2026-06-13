"""Tests for IngestConfig.default_branch threading through run_ingest.

Verifies that run_ingest passes the configured default_branch to replay_merge_log
rather than hardcoding 'main', so repos like makeplane/plane (base='preview')
are processed correctly.
"""
from __future__ import annotations

import pytest

from learning_service.db.store import InMemoryLearningStore
from learning_service.entrypoints.ingest import IngestConfig, run_ingest
from learning_service.github import PullRequest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr(
    number: int = 1,
    title: str = "Add feature",
    body: str = "Implements a useful feature.",
    base_branch: str = "main",
    merged: bool = True,
    merged_at: str | None = "2026-06-12T10:00:00Z",
    author_login: str = "alice",
    diff: str = "--- a/src/f.ts\n+++ b/src/f.ts\n@@ -1 +1 @@\n+// added\n",
    owner_repo: str = "org/repo",
) -> PullRequest:
    return PullRequest(
        number=number,
        title=title,
        body=body,
        base_branch=base_branch,
        merged=merged,
        merged_at=merged_at,
        author_login=author_login,
        diff=diff,
        review_comments=[],
        linked_issues=[],
        labels=[],
        owner_repo=owner_repo,
    )


def _config(default_branch: str = "main", pr_log=None, store=None, mode: str = "enforce") -> IngestConfig:
    cfg = IngestConfig(
        org="test-org",
        repo="org/repo",
        mode=mode,
        default_branch=default_branch,
    )
    cfg.pr_log = pr_log or []  # type: ignore[attr-defined]
    cfg.store = store or InMemoryLearningStore()  # type: ignore[attr-defined]
    return cfg


# ---------------------------------------------------------------------------
# Test: IngestConfig.default_branch field exists and defaults to 'main'
# ---------------------------------------------------------------------------


def test_ingest_config_default_branch_field_defaults_to_main():
    """IngestConfig.default_branch exists and defaults to 'main'."""
    cfg = IngestConfig(org="x", repo="x/y")
    assert cfg.default_branch == "main"


def test_ingest_config_default_branch_can_be_set():
    """IngestConfig.default_branch can be set to any value."""
    cfg = IngestConfig(org="x", repo="x/y", default_branch="preview")
    assert cfg.default_branch == "preview"


# ---------------------------------------------------------------------------
# Test: PR targeting 'main' is processed when default_branch='main'
# ---------------------------------------------------------------------------


def test_pr_targeting_main_processed_when_default_branch_main():
    """A PR targeting 'main' is distilled when default_branch='main' (enforce mode writes cursor)."""
    store = InMemoryLearningStore()
    pr = _pr(base_branch="main")
    cfg = _config(default_branch="main", pr_log=[pr], store=store, mode="enforce")
    rc = run_ingest(cfg)
    assert rc == 0
    # enforce mode writes the processed cursor on distill
    processed = getattr(store, "_processed_prs", {})
    assert len(processed) >= 1, f"Expected PR to be processed (cursor written), got: {processed}"


# ---------------------------------------------------------------------------
# Test: PR targeting 'preview' is dropped when default_branch='main'
# ---------------------------------------------------------------------------


def test_pr_targeting_preview_dropped_when_default_branch_main():
    """A PR targeting 'preview' is skipped (non-default base) when default_branch='main'."""
    store = InMemoryLearningStore()
    pr = _pr(base_branch="preview")
    cfg = _config(default_branch="main", pr_log=[pr], store=store, mode="enforce")
    rc = run_ingest(cfg)
    assert rc == 0
    # No PR processed — skipped due to non-default base
    processed = getattr(store, "_processed_prs", {})
    assert len(processed) == 0, f"Expected 0 processed PRs (non-default base), got: {processed}"


# ---------------------------------------------------------------------------
# Test: PR targeting 'preview' is processed when default_branch='preview'
# ---------------------------------------------------------------------------


def test_pr_targeting_preview_processed_when_default_branch_preview():
    """A PR targeting 'preview' is distilled when default_branch='preview' (enforce writes cursor)."""
    store = InMemoryLearningStore()
    pr = _pr(base_branch="preview")
    cfg = _config(default_branch="preview", pr_log=[pr], store=store, mode="enforce")
    rc = run_ingest(cfg)
    assert rc == 0
    # enforce mode writes the processed cursor when distilled
    processed = getattr(store, "_processed_prs", {})
    assert len(processed) >= 1, (
        "Expected the PR to be processed (cursor written) when default_branch='preview', "
        f"but processed_prs is empty. store._ideas={getattr(store, '_ideas', {})}"
    )


# ---------------------------------------------------------------------------
# Test: PR targeting 'main' is dropped when default_branch='preview'
# ---------------------------------------------------------------------------


def test_pr_targeting_main_dropped_when_default_branch_preview():
    """A PR targeting 'main' is skipped when default_branch='preview'."""
    store = InMemoryLearningStore()
    pr = _pr(base_branch="main")
    cfg = _config(default_branch="preview", pr_log=[pr], store=store, mode="enforce")
    rc = run_ingest(cfg)
    assert rc == 0
    # No PR processed — 'main' != 'preview' so it's skipped_non_default_base
    processed = getattr(store, "_processed_prs", {})
    assert len(processed) == 0, f"Expected 0 processed PRs (non-default base), got: {processed}"


# ---------------------------------------------------------------------------
# Test: mixed PRs — only those targeting the configured default_branch survive
# ---------------------------------------------------------------------------


def test_mixed_prs_only_matching_base_processed():
    """With default_branch='preview', only PRs targeting 'preview' are processed (cursor written)."""
    store = InMemoryLearningStore()
    prs = [
        _pr(number=1, base_branch="preview", title="Feature A"),
        _pr(number=2, base_branch="main", title="Feature B"),
        _pr(number=3, base_branch="preview", title="Feature C"),
        _pr(number=4, base_branch="develop", title="Feature D"),
    ]
    cfg = _config(default_branch="preview", pr_log=prs, store=store, mode="enforce")
    rc = run_ingest(cfg)
    assert rc == 0
    # PRs #1 and #3 target 'preview' → enforce mode writes the cursor for them
    processed = getattr(store, "_processed_prs", {})
    assert len(processed) == 2, (
        f"Expected 2 processed PRs (targeting 'preview'), got {len(processed)}: {processed}"
    )
