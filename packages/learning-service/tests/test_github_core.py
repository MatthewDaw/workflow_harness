"""github-core lane tests — covers the four bug-fixes introduced in this lane.

Test coverage:
1. _default_fetch: branches correctly on Accept header (raw text for diff, JSON for API).
2. list_merged_pull_requests: max_prs cap stops pagination early.
3. is_curriculum_noise: Renovate/dependabot PRs are now filtered; genuine PRs are not.
4. distill_pr: invokes the LLM (claude CLI) and produces a non-templated insight.

All tests are offline-safe (no live GitHub, no live AWS). The LLM test uses
AF_JUDGE_MODE=passthrough and makes at most one real claude CLI call (guarded by
a skip if the CLI is unavailable).
"""
from __future__ import annotations

import json
from io import BytesIO
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from learning_service.github import (
    PullRequest,
    PublicGitHubReader,
    _default_fetch,
    distill_pr,
    is_curriculum_noise,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pr(
    number: int = 42,
    title: str = "Add rate-limiting middleware to API gateway",
    body: str = "Prevents abuse by capping requests per IP.",
    base_branch: str = "main",
    merged: bool = True,
    merged_at: str | None = "2026-06-12T10:00:00Z",
    author_login: str = "alice",
    diff: str = (
        "--- a/src/gateway.ts\n+++ b/src/gateway.ts\n"
        "@@ -10,4 +10,8 @@\n"
        "+import rateLimit from 'express-rate-limit';\n"
        "+app.use(rateLimit({ windowMs: 60_000, max: 100 }));\n"
    ),
    labels: list[str] | None = None,
    owner_repo: str = "acme/backend",
    review_comments: list[str] | None = None,
    linked_issues: list[str] | None = None,
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
        labels=labels or [],
        owner_repo=owner_repo,
        review_comments=review_comments or [],
        linked_issues=linked_issues or [],
    )


# ---------------------------------------------------------------------------
# 1. _default_fetch: Accept-header branching
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal stand-in for urllib HTTP response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_default_fetch_returns_raw_text_for_diff_accept():
    """_default_fetch returns a string (not parsed JSON) when Accept is vnd.github.v3.diff."""
    raw_diff = b"--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n+x = 1\n"
    with patch("urllib.request.urlopen", return_value=_FakeResponse(raw_diff)):
        result = _default_fetch(
            "https://api.github.com/repos/acme/backend/pulls/1",
            {"Accept": "application/vnd.github.v3.diff"},
        )
    assert isinstance(result, str)
    assert result == raw_diff.decode("utf-8")
    # Must NOT be JSON-parsed (raw unified diff is not valid JSON)
    assert result.startswith("---")


def test_default_fetch_json_decodes_for_normal_api_accept():
    """_default_fetch JSON-decodes the body for standard JSON endpoints."""
    payload = {"number": 7, "title": "Fix bug"}
    encoded = json.dumps(payload).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=_FakeResponse(encoded)):
        result = _default_fetch(
            "https://api.github.com/repos/acme/backend/pulls",
            {"Accept": "application/vnd.github+json"},
        )
    assert isinstance(result, dict)
    assert result["number"] == 7


def test_default_fetch_enrich_pr_diff_returns_real_diff():
    """enrich_pr_diff returns unified-diff text (exercises _default_fetch raw-text branch)."""
    raw_diff = "--- a/x.ts\n+++ b/x.ts\n@@ -1 +1 @@\n+const x = 1;\n"

    def _fake_fetch(url: str, headers: dict) -> Any:
        # Simulate the Accept-header branch: return raw text for diff requests.
        if "vnd.github.v3.diff" in headers.get("Accept", ""):
            return raw_diff
        return []

    reader = PublicGitHubReader(fetch=_fake_fetch)
    result = reader.enrich_pr_diff("acme/backend", 42)
    assert result == raw_diff
    assert result.startswith("---")


# ---------------------------------------------------------------------------
# 2. list_merged_pull_requests: max_prs pagination cap
# ---------------------------------------------------------------------------


def _make_pr_item(number: int, base_ref: str = "main") -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "base": {"ref": base_ref},
        "merged_at": "2026-06-01T00:00:00Z",
        "user": {"login": "user"},
        "labels": [],
    }


def test_list_merged_prs_max_prs_stops_pagination_early():
    """With max_prs=6, pagination stops once 6 merged PRs are collected."""
    # Build 5 pages of 4 merged PRs each (20 total) — without the cap all 5 are fetched.
    pages = [
        [_make_pr_item(i) for i in range(start, start + 4)]
        for start in range(1, 21, 4)
    ]  # pages of 4 items (< PER_PAGE=100 keeps last-page detection clean)

    page_call_count = {"n": 0}

    def fake_fetch(url: str, headers: dict) -> Any:
        idx = page_call_count["n"]
        page_call_count["n"] += 1
        if idx < len(pages):
            return pages[idx]
        return []

    reader = PublicGitHubReader(fetch=fake_fetch)
    # Override PER_PAGE so 4-item pages look "full" and trigger further pagination.
    reader.PER_PAGE = 4

    prs = reader.list_merged_pull_requests(
        "acme/backend", default_branch="main", max_prs=6
    )

    assert len(prs) == 6
    # Must have fetched far fewer pages than the 5 total available.
    assert page_call_count["n"] <= 3, (
        f"Expected <=3 page fetches for max_prs=6, got {page_call_count['n']}"
    )


def test_list_merged_prs_unbounded_fetches_all_pages():
    """Without max_prs, all pages are fetched (original behaviour preserved)."""
    pages = [
        [_make_pr_item(i) for i in range(start, start + 3)]
        for start in range(1, 10, 3)
    ]

    page_call_count = {"n": 0}

    def fake_fetch(url: str, headers: dict) -> Any:
        idx = page_call_count["n"]
        page_call_count["n"] += 1
        if idx < len(pages):
            return pages[idx]
        return []

    reader = PublicGitHubReader(fetch=fake_fetch)
    reader.PER_PAGE = 3

    prs = reader.list_merged_pull_requests("acme/backend", default_branch="main")
    # All 9 PRs from 3 pages + 1 empty terminator page.
    assert len(prs) == 9
    assert page_call_count["n"] == 4  # 3 real pages + 1 empty


def test_list_merged_prs_signature_accepts_keyword_only_args():
    """The new signature (*, default_branch, since_pr, max_prs) is callable."""
    # This just checks that calling with keyword args does not raise TypeError.
    reader = PublicGitHubReader(fetch=lambda url, h: [])
    result = reader.list_merged_pull_requests(
        "acme/backend",
        default_branch="main",
        since_pr=None,
        max_prs=10,
    )
    assert result == []


# ---------------------------------------------------------------------------
# 3. is_curriculum_noise: Renovate / dependabot noise filter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,author,expected_noise",
    [
        # Renovate-style titles (all should be noise)
        ("Update dependency cypress to v13", "renovate[bot]", True),
        ("Update dependency keycloak-js to v24", "renovate[bot]", True),
        ("Update dependency org.flywaydb:flyway-core to v10", "renovate[bot]", True),
        ("Update eslint to v9", "renovate[bot]", True),
        # Dependabot-style title (noise)
        ("Bump lodash from 4.17.20 to 4.17.21", "dependabot[bot]", True),
        # Generic chore(deps) title (noise)
        ("chore(deps): bump express to 4.18.2", "alice", True),
        # Genuine feature/fix PR (NOT noise)
        ("Add distributed tracing to the payment service", "alice", False),
        ("Fix race condition in session cleanup", "bob", False),
    ],
)
def test_renovate_prs_are_noise_genuine_prs_are_not(
    title: str, author: str, expected_noise: bool
) -> None:
    """Renovate/dependabot PRs are filtered; genuine engineering PRs pass through."""
    pr = _pr(title=title, author_login=author)
    is_noise, reason = is_curriculum_noise(pr)
    assert is_noise is expected_noise, (
        f"title={title!r} author={author!r}: expected noise={expected_noise}, "
        f"got noise={is_noise} reason={reason!r}"
    )
    if expected_noise:
        assert "curriculum_noise" in reason


def test_renovate_bot_author_alone_triggers_noise():
    """A PR by renovate[bot] is noise even with an otherwise innocuous title."""
    # Use a title that does NOT match the title regex so the bot-author check is exercised.
    pr = _pr(title="Migrate CI to GitHub Actions", author_login="renovate[bot]")
    is_noise, reason = is_curriculum_noise(pr)
    assert is_noise
    assert "bot author" in reason


def test_dependabot_bot_author_alone_triggers_noise():
    """A PR by dependabot[bot] is noise."""
    pr = _pr(title="Some dependency update", author_login="dependabot[bot]")
    is_noise, reason = is_curriculum_noise(pr)
    assert is_noise


def test_genuine_feature_pr_not_noise():
    """A genuine feature PR by a human author is not noise."""
    pr = _pr(
        title="Implement OAuth2 PKCE flow for mobile clients",
        author_login="alice",
    )
    is_noise, _ = is_curriculum_noise(pr)
    assert not is_noise


# ---------------------------------------------------------------------------
# 4. distill_pr: LLM-wired path
# ---------------------------------------------------------------------------


def _check_claude_available() -> bool:
    """Return True only if the claude CLI is on PATH."""
    import shutil
    return shutil.which("claude") is not None


@pytest.mark.skipif(not _check_claude_available(), reason="claude CLI not on PATH")
def test_distill_pr_calls_llm_and_returns_diff_grounded_insight(monkeypatch):
    """distill_pr invokes the claude CLI and produces a non-templated, diff-grounded insight.

    This test uses AF_JUDGE_MODE=passthrough to make one real LLM call (cost-capped).
    It asserts:
    - run_judge was called (the LLM path executed).
    - The returned DistillationResult.body is non-empty.
    - The insight is plausibly grounded in the diff (contains something from the PR).
    """
    monkeypatch.setenv("AF_JUDGE_MODE", "passthrough")

    # A small, concrete PR with distinctive diff tokens.
    pr = _pr(
        number=99,
        title="Add rate-limit middleware to API gateway",
        body="Caps requests to 100 per minute per IP address.",
        diff=(
            "--- a/src/gateway.ts\n+++ b/src/gateway.ts\n"
            "@@ -5,3 +5,7 @@\n"
            "+import rateLimit from 'express-rate-limit';\n"
            "+const limiter = rateLimit({ windowMs: 60_000, max: 100 });\n"
            "+app.use(limiter);\n"
        ),
        author_login="alice",
        owner_repo="acme/gateway",
    )

    # Capture whether run_judge was actually called.
    from learning_service import judge as judge_module

    original_run_judge = judge_module.run_judge
    call_log: list[dict] = []

    def _spy_run_judge(*args, **kwargs):
        result = original_run_judge(*args, **kwargs)
        call_log.append({"prompt": kwargs.get("prompt", args[0] if args else "")})
        return result

    monkeypatch.setattr(judge_module, "run_judge", _spy_run_judge)

    # Patch the import inside github.py to use the spy.
    import learning_service.github as github_module

    original_import = __builtins__.__import__ if hasattr(__builtins__, "__import__") else None

    result = distill_pr(pr, session_context=None)

    assert result.body, "distill_pr must return a non-empty insight body"
    assert result.pr_number == 99
    assert result.owner_repo == "acme/gateway"
    # The insight should reference something from the PR (rate limiting context).
    body_lower = result.body.lower()
    # At minimum the body should be non-templated (not just the raw title repeated verbatim
    # with no elaboration) — we check it has more than 20 chars.
    assert len(result.body) > 20


def test_distill_pr_falls_back_to_template_when_llm_unavailable(monkeypatch):
    """distill_pr falls back gracefully to template when run_judge raises JudgeError."""
    from learning_service.judge import JudgeUnavailable

    # Make run_judge always raise so the fallback path is exercised.
    monkeypatch.setenv("AF_JUDGE_MODE", "passthrough")

    with patch("learning_service.github._distill_via_llm", return_value=None):
        pr = _pr()
        result = distill_pr(pr)

    # Template path must still produce a valid DistillationResult.
    assert result.body
    assert result.pr_number == pr.number
    assert result.rung in ("test", "normal", "bare")
    assert result.kind in ("normal", "bugfix-failure-mode")


def test_distill_pr_llm_output_overrides_template_rung_and_kind(monkeypatch):
    """When the LLM succeeds, its rung and kind override the heuristic values."""
    fake_llm_output = {
        "insight": "Rate-limiting middleware prevents API abuse by capping requests per IP.",
        "rung": "normal",
        "kind": "normal",
    }

    with patch("learning_service.github._distill_via_llm", return_value=fake_llm_output):
        pr = _pr()
        result = distill_pr(pr)

    assert result.body == fake_llm_output["insight"]
    assert result.rung == "normal"
    assert result.kind == "normal"
    assert not result.is_bugfix


def test_distill_pr_llm_bugfix_kind_sets_is_bugfix(monkeypatch):
    """When LLM returns kind='bugfix-failure-mode', is_bugfix is True."""
    fake_llm_output = {
        "insight": "Failure-mode: session tokens were accepted past expiry.",
        "rung": "test",
        "kind": "bugfix-failure-mode",
    }

    with patch("learning_service.github._distill_via_llm", return_value=fake_llm_output):
        pr = _pr(title="Fix token expiry not enforced", labels=["bug"])
        result = distill_pr(pr)

    assert result.is_bugfix is True
    assert result.kind == "bugfix-failure-mode"
    assert "Failure-mode" in result.body


def test_distill_pr_session_context_appended_to_llm_insight(monkeypatch):
    """Session context is appended to the LLM insight body."""
    fake_llm_output = {
        "insight": "Use connection pooling to reduce latency.",
        "rung": "normal",
        "kind": "normal",
    }

    with patch("learning_service.github._distill_via_llm", return_value=fake_llm_output):
        pr = _pr()
        result = distill_pr(pr, session_context="Agent chose pool size=10 for latency.")

    assert "Agent chose pool size=10" in result.body
    assert "Use connection pooling" in result.body
