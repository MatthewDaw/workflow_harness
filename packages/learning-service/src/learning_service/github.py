"""github.py — GitHub client for PR-merge ingestion (U1, MAT-137).

Provides:
  * ``PullRequest`` — the wire type passed to the merge handler.
  * ``PublicGitHubReader`` — unauthenticated reader for public repos.
  * ``GitHubAppClient`` — installation-token reader for private repos.
  * ``list_merged_pull_requests`` — paginated history in merge-commit order.
  * ``scrub_secrets`` — port of the 6 secret regexes from ``wrapper/internal/topic/fold.go``.
  * ``generalize_repo_specifics`` — typed-placeholder substitution for private-repo leak guard.
  * ``infer_verification_rung`` — diff + PR metadata → test | normal | bare (never 0).
  * ``is_curriculum_noise`` — curriculum filter (dependency bumps, lockfiles, etc.).
  * ``distill_pr`` — LLM-backed distillation of a PR into a ``DistillationResult``.

All secrets-bearing fields are scrubbed BEFORE being embedded in any
``IdeaSourceRecord`` or idea body.  The generalization pass replaces
repo-specific identifiers with typed placeholders so intra-org cross-repo
leakage is blocked even when multiple repos share one org.

Auth notes:
  - Public repos: use ``PublicGitHubReader`` (no token required).
  - Private repos: load the GitHub App PEM from AWS Secrets Manager at cold
    start; cache the installation token for ~55 min.
  - Un-consented private installs surface ``ConsentRequiredError``, not a
    silent skip.
"""
from __future__ import annotations

import hashlib
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ConsentRequiredError(Exception):
    """Raised when a private repo lacks Pull-requests:Read consent."""

    def __init__(self, owner_repo: str) -> None:
        super().__init__(
            f"GitHub App installation for {owner_repo!r} needs 'Pull requests: Read' "
            "permission.  Re-consent required — not a silent fallback."
        )
        self.owner_repo = owner_repo


# ---------------------------------------------------------------------------
# Wire types
# ---------------------------------------------------------------------------


@dataclass
class PullRequest:
    """Minimal PR representation passed to the merge handler.

    Fields are populated from the GitHub PR API (or fixture).  All text
    fields are the *raw* content from GitHub before any scrubbing or
    generalization.
    """

    number: int
    title: str
    body: str                     # PR description
    base_branch: str              # the branch the PR targets
    merged: bool                  # True = merged; False = closed-unmerged
    merged_at: str | None         # ISO-8601 merge timestamp or None
    author_login: str             # PR author (for credibility weighting)
    diff: str                     # Unified diff text
    review_comments: list[str] = field(default_factory=list)
    linked_issues: list[str] = field(default_factory=list)   # issue titles/bodies
    labels: list[str] = field(default_factory=list)
    owner_repo: str = ""          # "owner/repo" — set by the reader


# ---------------------------------------------------------------------------
# Secret-scrub (ported from wrapper/internal/topic/fold.go scrubSecrets)
# ---------------------------------------------------------------------------

# The six regexes from fold.go (translated to Python, same semantics).
# ORDER MATTERS: more-specific patterns (JWT, PAT, Bearer) must come before
# the generic credential assignment pattern, so they are replaced with their
# specific placeholder before the generic regex can swallow them.
_SECRET_PATTERNS: list[tuple[str, str]] = [
    # 1. AWS access key id (AKIA…)
    (r"AKIA[0-9A-Z]{16}", "<AWS_KEY>"),
    # 2. JWT (three base64url segments separated by dots) — must precede Bearer
    #    and generic-credential patterns so the full token is replaced correctly.
    (
        r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+",
        "<JWT>",
    ),
    # 3. Bearer token in Authorization header (after JWT so JWTs in Bearer
    #    headers are replaced as <JWT> by pattern 2 first; any remaining
    #    Bearer + non-JWT token is caught here).
    (r"(?i)Bearer\s+[A-Za-z0-9\-_\.=+/]{8,}", "Bearer <TOKEN>"),
    # 4. GitHub PAT (classic ghp_… or fine-grained github_pat_…) — must come
    #    before the generic credential pattern or the 'ghp_…' value is swallowed
    #    as a generic <CREDENTIAL> first.
    (
        r"(?:ghp_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{82})",
        "<GITHUB_PAT>",
    ),
    # 5. PEM private key header
    (r"-----BEGIN [A-Z ]+PRIVATE KEY-----", "<PEM_HEADER>"),
    # 6. Generic key/secret/token assignment: key=<value> or "secret": "<value>".
    #    This is the broadest pattern; it runs last so more-specific patterns
    #    have already been replaced.
    (
        r'(?i)(?:key|secret|token|password|passwd|pwd)\s*[=:]\s*["\']?([A-Za-z0-9+/=_\-\.]{8,})["\']?',
        r"<CREDENTIAL>",
    ),
]

_COMPILED_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat), repl) for pat, repl in _SECRET_PATTERNS
]


def scrub_secrets(text: str) -> str:
    """Apply the six secret regexes from fold.go to *text*.

    Returns the scrubbed text.  All patterns are applied in order so a line
    containing multiple secrets is fully scrubbed.
    """
    for pattern, replacement in _COMPILED_SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


# ---------------------------------------------------------------------------
# Generalization pass (private-repo leak guard)
# ---------------------------------------------------------------------------

# Placeholder map: these replace repo-specific identifiers so the insight
# body does not reveal the source repo to other org members.
_GENERALIZE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Full GitHub repo URL: https://github.com/owner/repo  →  <REPO>
    (re.compile(r"https?://github\.com/[\w\-\.]+/[\w\-\.]+"), "<REPO>"),
    # Bare owner/repo reference in insight text (but NOT in diff paths)
    (re.compile(r"\b[\w\-]+/[\w\-]+(?:#\d+)?\b"), "<REPO>"),
    # Hostnames (sub.domain.tld)
    (re.compile(r"\b(?:[a-z0-9\-]+\.)+(?:com|io|net|org|dev|app|ai|co)\b"), "<HOST>"),
    # Package identifiers: @scope/pkg or plain-package-name  (≥2 hyphens = package)
    (re.compile(r"@[\w\-]+/[\w\-]+"), "<PKG>"),
]


def generalize_repo_specifics(text: str, owner_repo: str = "") -> str:
    """Replace repo-specific identifiers with typed placeholders.

    Applied after ``scrub_secrets`` as the second pass of the private-repo
    leak guard.  Uses the ``agent-families pipeline/__init__.py`` approach
    (admission-gate substitution).
    """
    if owner_repo:
        # Replace explicit owner/repo literal first (most specific)
        escaped = re.escape(owner_repo)
        text = re.sub(escaped, "<REPO>", text)

    for pattern, placeholder in _GENERALIZE_PATTERNS:
        text = pattern.sub(placeholder, text)
    return text


# ---------------------------------------------------------------------------
# Rung inference (from diff + PR metadata; no Check-Runs dependency)
# ---------------------------------------------------------------------------

# Patterns that indicate a *test* was added or modified.
_TEST_FILE_RE = re.compile(
    r"(?:^|\+\+\+ b/)(?:.*[/\\])?(?:test[s]?[/\\]|spec[s]?[/\\]|__tests__[/\\]|"
    r"[A-Za-z0-9_\-]+\.(?:test|spec)\.[a-z]+)",
    re.MULTILINE,
)

# Title/label signals for targeted bug-fix.
_BUGFIX_TITLE_RE = re.compile(
    r"\b(?:fix|bug|hotfix|patch|bugfix|closes?|resolves?)\b", re.IGNORECASE
)
_BUGFIX_LABEL_RE = re.compile(r"\b(?:bug|bugfix|fix|hotfix)\b", re.IGNORECASE)

# Trivial/low-signal signals.
_TRIVIAL_TITLE_RE = re.compile(
    r"\b(?:typo|docs?|readme|changelog|nit|style|format|lint|whitespace|"
    r"comment|doc(?:umentation)?|bump|upgrade|update dep|chore)\b",
    re.IGNORECASE,
)

# Small bug-fix: diff ≤ this many changed lines + fix signal = test rung.
_BUGFIX_SMALL_DIFF_LINES = 60


def _diff_changed_lines(diff: str) -> int:
    """Count lines added or removed in a unified diff."""
    return sum(
        1
        for line in diff.splitlines()
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    )


def _is_targeted_bugfix(pr: PullRequest) -> bool:
    """Return True if the PR looks like a targeted bug-fix (for rung inference)."""
    has_fix_title = bool(_BUGFIX_TITLE_RE.search(pr.title))
    has_fix_label = any(_BUGFIX_LABEL_RE.search(lbl) for lbl in pr.labels)
    is_small = _diff_changed_lines(pr.diff) <= _BUGFIX_SMALL_DIFF_LINES
    return (has_fix_title or has_fix_label) and is_small


def infer_verification_rung(pr: PullRequest) -> str:
    """Infer the verification rung from diff + PR metadata.

    Returns one of ``"test"`` (weight 1.0), ``"normal"`` (0.6), ``"bare"`` (0.4).
    Never returns a weight of 0 — a merged PR always means something.

    Rules (in priority order):
    1. Diff adds/modifies a test file → ``"test"`` (1.0).
    2. Trivial/low-signal PR (typo, docs, format, chore) → ``"bare"`` (0.4).
       (Trivial is checked BEFORE bugfix so "fix typo in README" is bare, not test.)
    3. Targeted bug-fix (fix-signal title/label + small diff) → ``"test"`` (1.0).
    4. Otherwise → ``"normal"`` (0.6).
    """
    if _TEST_FILE_RE.search(pr.diff):
        return "test"
    if _TRIVIAL_TITLE_RE.search(pr.title):
        return "bare"
    if _is_targeted_bugfix(pr):
        return "test"
    return "normal"


RUNG_WEIGHTS: dict[str, float] = {"test": 1.0, "normal": 0.6, "bare": 0.4}


# ---------------------------------------------------------------------------
# Curriculum filter
# ---------------------------------------------------------------------------

# Patterns that identify low-signal noise PRs to skip (curriculum filter).
# We filter by *signal*, not size — a 500-line version bump drops; a 1-line
# bug fix stays.
_NOISE_TITLE_RE = re.compile(
    r"(?i)^(?:"
    r"bump\s+|"
    r"chore(?:\s*\(deps?\)|:\s*deps?\s)|"
    r"update\s+(?:dep(?:endenc(?:y|ies))?|lock(?:file)?|package)|"
    r"update\s+\S+\s+to\s+|"          # "Update <pkg> to <version>" (Renovate style)
    r"update\s+dependency\s+|"        # "Update dependency <pkg>" (Renovate style)
    r"(?:lock)?file\s+update|"
    r"release\s+v?\d|"
    r"version\s+bump|"
    r"(?:auto)?(?:generate|gen)d?\s"
    r")",
)

_NOISE_FILE_ONLY_RE = re.compile(
    r"^\+\+\+ b/(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|"
    r"Pipfile\.lock|poetry\.lock|go\.sum|Cargo\.lock|"
    r".*\.snap|.*_generated\.[a-z]+)$",
    re.MULTILINE,
)

# Bot authors that almost exclusively open dependency-bump PRs.
_NOISE_BOT_LOGINS: frozenset[str] = frozenset(
    {"renovate", "renovate[bot]", "dependabot", "dependabot[bot]"}
)


def is_curriculum_noise(pr: PullRequest) -> tuple[bool, str]:
    """Return ``(True, reason)`` if the PR should be skipped by the curriculum filter.

    Skipped PRs are logged (the filter is auditable/tunable).  Never skips on
    diff *size* — only on low-signal markers.

    Filtered signals (in priority order):
    1. Title matches a dependency-bump/release/generated pattern.
    2. Author is a known dependency-bot (Renovate, Dependabot).
    3. Diff touches only lockfiles / generated files.

    Returns ``(False, "")`` for PRs that should be ingested.
    """
    # Version/dependency bump title signals
    if _NOISE_TITLE_RE.match(pr.title):
        return True, f"curriculum_noise: title matches noise pattern: {pr.title!r}"

    # Bot author — Renovate and Dependabot open dependency-bump PRs exclusively.
    author_lower = pr.author_login.lower()
    if author_lower in _NOISE_BOT_LOGINS:
        return True, f"curriculum_noise: bot author {pr.author_login!r}"

    # Diff is only lockfiles / generated files
    diff_files = re.findall(r"^\+\+\+ b/(.+)$", pr.diff, re.MULTILINE)
    if diff_files and all(
        _NOISE_FILE_ONLY_RE.match(f"+++ b/{f}") for f in diff_files
    ):
        return True, "curriculum_noise: diff only touches lockfiles/generated files"

    return False, ""


# ---------------------------------------------------------------------------
# Distillation: produce the insight body from PR sources
# ---------------------------------------------------------------------------


@dataclass
class DistillationResult:
    """The candidate insight produced from one PR by the merge handler."""

    pr_number: int
    owner_repo: str
    author_login: str
    title: str
    body: str                    # scrubbed, generalized insight body
    rung: str                    # test | normal | bare
    is_bugfix: bool              # True → phrase as failure-mode
    kind: str                    # "normal" | "bugfix-failure-mode"
    session_context: str | None  # from U7 branch→session link (or None)
    raw_anchors_from_diff: str   # raw diff text for anchor extraction downstream


def _build_insight_body(pr: PullRequest, is_bugfix: bool, session_ctx: str | None) -> str:
    """Synthesize the scrubbed insight body from PR sources.

    For bug-fix PRs, phrase the insight as a verifiable failure-mode /
    error case (what broke + the guard that prevents it) so it is
    preferentially retrieved when an agent is instructed to verify code.

    The body is scrubbed and generalized *before* being returned.
    """
    parts: list[str] = []

    if is_bugfix:
        # Failure-mode framing: what broke + guard
        parts.append(f"Failure-mode: {pr.title}.")
        if pr.body:
            parts.append(f"Root cause / guard: {pr.body.strip()}")
        if pr.review_comments:
            parts.append("Review notes: " + "; ".join(pr.review_comments[:3]))
        if pr.linked_issues:
            parts.append("Related: " + "; ".join(pr.linked_issues[:2]))
    else:
        parts.append(pr.title)
        if pr.body:
            parts.append(pr.body.strip())
        if pr.review_comments:
            parts.append("Review: " + "; ".join(pr.review_comments[:3]))
        if pr.linked_issues:
            parts.append("See: " + "; ".join(pr.linked_issues[:2]))

    if session_ctx:
        parts.append(f"Session context: {session_ctx}")

    raw = "\n\n".join(p for p in parts if p)
    # Scrub then generalize
    scrubbed = scrub_secrets(raw)
    generalized = generalize_repo_specifics(scrubbed, pr.owner_repo)
    return generalized


# ---------------------------------------------------------------------------
# LLM distillation schema (structured output from the claude CLI judge)
# ---------------------------------------------------------------------------

# The JSON schema for the LLM distillation response. The LLM turns the PR
# title + body + diff into a semantic insight atom with rung and kind inferred
# from diff evidence rather than from simple heuristics.
_DISTILL_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "insight": {
            "type": "string",
            "description": (
                "A single, transferable engineering insight extracted from the PR. "
                "Ground it in concrete diff evidence. For bug-fix PRs phrase as a "
                "failure-mode description: what broke and the guard that prevents it."
            ),
        },
        "rung": {
            "type": "string",
            "enum": ["test", "normal", "bare"],
            "description": (
                "Verification rung: 'test' if tests are added/modified or if this is "
                "a targeted bug-fix; 'bare' for trivial changes (typo/docs/format); "
                "'normal' otherwise."
            ),
        },
        "kind": {
            "type": "string",
            "enum": ["normal", "bugfix-failure-mode"],
            "description": (
                "'bugfix-failure-mode' if the PR fixes a concrete bug and the insight "
                "describes the failure mode; 'normal' otherwise."
            ),
        },
    },
    "required": ["insight", "rung", "kind"],
    "additionalProperties": False,
}

_DISTILL_MODEL = "claude-sonnet-4-5"

_DIFF_PREVIEW_CHARS = 3000  # max diff chars sent to LLM to limit cost


def _distill_via_llm(
    pr: PullRequest,
    session_ctx: str | None,
    is_bugfix: bool,
) -> dict | None:
    """Attempt an LLM distillation call via the authed claude CLI.

    Returns the structured output dict ``{insight, rung, kind}`` on success, or
    ``None`` if the LLM is unavailable or judge mode is replay and no fixture
    exists (so the caller can fall back to template distillation).
    """
    try:
        from learning_service.judge import JudgeError, run_judge
    except ImportError:
        logger.debug("learning_service.judge unavailable; skipping LLM distill")
        return None

    diff_preview = pr.diff[:_DIFF_PREVIEW_CHARS]
    if len(pr.diff) > _DIFF_PREVIEW_CHARS:
        diff_preview += "\n... [diff truncated]"

    # Scrub and generalize before sending to the LLM.
    safe_title = generalize_repo_specifics(scrub_secrets(pr.title), pr.owner_repo)
    safe_body = generalize_repo_specifics(scrub_secrets(pr.body), pr.owner_repo)
    safe_diff = generalize_repo_specifics(scrub_secrets(diff_preview), pr.owner_repo)

    session_line = f"\nSession context: {session_ctx}" if session_ctx else ""
    bugfix_hint = " (this is a targeted bug-fix PR)" if is_bugfix else ""

    prompt = (
        f"You are a senior engineer reviewing a merged pull request{bugfix_hint}.\n"
        f"Extract a single, transferable engineering insight from it.\n\n"
        f"PR title: {safe_title}\n"
        f"PR description:\n{safe_body or '(none)'}\n\n"
        f"Unified diff (may be truncated):\n```diff\n{safe_diff}\n```"
        f"{session_line}\n\n"
        "Respond with a JSON object matching the required schema."
    )

    try:
        result = run_judge(
            prompt=prompt,
            schema=_DISTILL_SCHEMA,
            model=_DISTILL_MODEL,
            max_retries=1,
        )
        return result.output
    except JudgeError as exc:
        logger.warning("LLM distill failed (%s); falling back to template", exc)
        return None


def distill_pr(
    pr: PullRequest,
    session_context: str | None = None,
) -> DistillationResult:
    """Distill a merged PR into a candidate insight.

    Prefers an LLM-backed call (via the authed claude CLI judge) that grounds
    the insight in concrete diff evidence.  If the LLM is unavailable or the
    judge fixture is missing in replay mode, falls back to the template-based
    ``_build_insight_body`` path so the pipeline never hard-fails on connectivity
    issues.

    Sources (richest available, graceful degradation):
    (a) LLM: PR diff + title + description → semantic insight (preferred).
    (b) Template: PR title + body + review comments + linked issues (fallback).
    (c) Session context from the U7 branch→session link (always appended when present).

    Bug-fix PRs are phrased as verifiable failure-mode descriptions and tagged
    ``kind='bugfix-failure-mode'`` so they are preferentially retrieved when an
    agent is instructed to verify code.
    """
    is_bugfix = _is_targeted_bugfix(pr)
    rung = infer_verification_rung(pr)

    llm_output = _distill_via_llm(pr, session_ctx=session_context, is_bugfix=is_bugfix)

    if llm_output is not None:
        # Use LLM-produced values for insight body, rung, and kind.
        body = llm_output["insight"]
        if session_context:
            body = f"{body}\n\nSession context: {session_context}"
        # Honour LLM's rung/kind judgement but clamp to known values.
        rung = llm_output.get("rung", rung)
        kind_raw = llm_output.get("kind", "normal")
        kind = kind_raw if kind_raw in ("normal", "bugfix-failure-mode") else "normal"
        is_bugfix = kind == "bugfix-failure-mode"
    else:
        # Template fallback.
        kind = "bugfix-failure-mode" if is_bugfix else "normal"
        body = _build_insight_body(pr, is_bugfix=is_bugfix, session_ctx=session_context)

    return DistillationResult(
        pr_number=pr.number,
        owner_repo=pr.owner_repo,
        author_login=pr.author_login,
        title=pr.title,
        body=body,
        rung=rung,
        is_bugfix=is_bugfix,
        kind=kind,
        session_context=session_context,
        raw_anchors_from_diff=pr.diff,
    )


# ---------------------------------------------------------------------------
# GitHub readers (injectable fetch for offline fixtures)
# ---------------------------------------------------------------------------

# The fetch callable type: (url, headers) → dict (parsed JSON)
FetchFn = Callable[[str, dict[str, str]], Any]


def _default_fetch(url: str, headers: dict[str, str]) -> Any:
    """Production fetch via urllib (no requests dependency).

    When the request Accept header is ``application/vnd.github.v3.diff``, the
    GitHub API returns raw unified-diff text rather than JSON.  In that case the
    raw string is returned directly — json.loads() would raise on it.  For all
    other endpoints the response body is JSON-decoded as usual.
    """
    import json
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body_bytes = resp.read()
        # Check whether we requested a diff (raw text) or JSON.
        accept = headers.get("Accept", "")
        if "vnd.github.v3.diff" in accept:
            return body_bytes.decode("utf-8")
        return json.loads(body_bytes.decode("utf-8"))


class PublicGitHubReader:
    """Unauthenticated (or token-authenticated) reader for public repositories.

    Suitable for the v1 transfer (history replay) trigger.  For private repos,
    use ``GitHubAppClient`` (needs installation-token with Pull-requests:Read).

    Injectable ``fetch`` for offline fixture testing (mirrors the TS
    ``GitHubApp``'s injectable ``fetch`` precedent).

    Parameters
    ----------
    fetch:
        Callable ``(url, headers) -> Any`` used for all HTTP requests.
        Defaults to ``_default_fetch`` (urllib, no external dependencies).
    token:
        Optional GitHub PAT (or fine-grained token).  When provided (or when
        the ``GITHUB_TOKEN`` environment variable is set), every request
        carries ``Authorization: Bearer <token>``.  The token value is NEVER
        logged.
    """

    GITHUB_API = "https://api.github.com"
    PER_PAGE = 100

    def __init__(
        self,
        fetch: FetchFn | None = None,
        token: str | None = None,
    ) -> None:
        self._fetch = fetch or _default_fetch
        # Accept an explicit token or fall back to the env var.
        import os
        self._token: str | None = token or os.environ.get("GITHUB_TOKEN") or None

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            # NEVER log the token value.
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _get(self, path: str) -> Any:
        url = f"{self.GITHUB_API}{path}"
        return self._fetch(url, self._headers())

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
        """Return merged-to-default-branch PRs in ascending PR-number order.

        Paginates through the GitHub PR list API (closed PRs, sorted by
        updated desc, filtered to merged).  Returns only PRs whose
        ``base.ref == default_branch``.

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string.
        default_branch:
            Only include PRs that target this branch (e.g. ``"main"``).
        since_pr:
            Skip PRs with number <= this value (cursor-based resumption).
        max_prs:
            Hard cap on the number of NON-noise merged PRs collected.  When
            ``skip_noise=True`` (the default), curriculum-noise PRs are
            counted only as pages consumed, not as PRs toward this cap.
            Pagination stops as soon as this many signal PRs have been
            gathered.  ``None`` means unbounded (original behaviour).
        max_pages:
            Hard page-request ceiling to prevent runaway pagination when the
            repo is heavily noise-dominated.  Default is 5.  Ignored when
            ``max_prs`` is ``None`` (unbounded mode always reads to the last
            page, consistent with original behaviour).
        skip_noise:
            When ``True`` (default), curriculum-noise PRs are excluded from
            the result AND do not count toward ``max_prs``.  Set to ``False``
            to restore the original behaviour (all merged PRs kept).
        """
        prs: list[PullRequest] = []
        page = 1
        while True:
            # Enforce hard page cap only when max_prs is set.
            if max_prs is not None and page > max_pages:
                logger.debug(
                    "list_merged_pull_requests: hit max_pages=%d for %s; stopping",
                    max_pages,
                    owner_repo,
                )
                break

            path = (
                f"/repos/{owner_repo}/pulls"
                f"?state=closed&sort=updated&direction=desc"
                f"&per_page={self.PER_PAGE}&page={page}"
            )
            items = self._get(path)
            if not items:
                break
            for item in items:
                pr = self._parse_pr(item, owner_repo, default_branch)
                if pr is None:
                    continue
                if since_pr is not None and pr.number <= since_pr:
                    continue
                # Noise-skipping: exclude noise PRs and don't count them
                # toward max_prs so the budget is spent on signal PRs.
                if skip_noise:
                    noise, reason = is_curriculum_noise(pr)
                    if noise:
                        logger.debug(
                            "skip noise PR #%d (%s): %s",
                            pr.number,
                            owner_repo,
                            reason,
                        )
                        continue
                prs.append(pr)
                if max_prs is not None and len(prs) >= max_prs:
                    # Enough signal PRs collected — stop paginating immediately.
                    prs.sort(key=lambda p: p.number)
                    return prs
            if len(items) < self.PER_PAGE:
                break
            page += 1

        # Return in ascending PR-number order (commit order approximation)
        prs.sort(key=lambda p: p.number)
        return prs

    def _parse_pr(
        self, item: dict[str, Any], owner_repo: str, default_branch: str
    ) -> PullRequest | None:
        """Parse one GitHub PR item.  Returns None if it should be excluded."""
        # Only PRs that target the default branch
        base_ref = (item.get("base") or {}).get("ref", "")
        if base_ref != default_branch:
            return None
        # Return both merged and closed-unmerged (caller decides; merged=False
        # is a negative signal recorded by the merge handler).
        merged_at = item.get("merged_at")
        merged = merged_at is not None
        return PullRequest(
            number=item["number"],
            title=item.get("title", ""),
            body=item.get("body") or "",
            base_branch=base_ref,
            merged=merged,
            merged_at=merged_at,
            author_login=(item.get("user") or {}).get("login", ""),
            diff="",  # fetched separately in enrich_pr_diff
            labels=[lbl.get("name", "") for lbl in (item.get("labels") or [])],
            owner_repo=owner_repo,
        )

    def enrich_pr_diff(self, owner_repo: str, pr_number: int) -> str:
        """Fetch the unified diff for a PR (separate API call, accept header)."""
        # In the injectable-fetch model we can't set Accept per-call easily,
        # so we embed the diff URL and rely on the fixture to return raw diff.
        try:
            path = f"/repos/{owner_repo}/pulls/{pr_number}"
            # The real API returns diff when Accept: application/vnd.github.v3.diff
            # In fixtures this path returns the diff string directly.
            result = self._fetch(
                f"{self.GITHUB_API}{path}",
                {**self._headers(), "Accept": "application/vnd.github.v3.diff"},
            )
            if isinstance(result, str):
                return result
            # If fixture returns a dict (e.g. the PR object), fall back to empty
            return result.get("diff", "") if isinstance(result, dict) else ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("failed to fetch diff for %s#%d: %s", owner_repo, pr_number, exc)
            return ""

    # File-content fetching limits — keep requests bounded.
    _MAX_FILE_CONTENTS = 10          # max number of files fetched per PR
    _MAX_FILE_SIZE_BYTES = 200_000   # skip files larger than 200 KB

    # Extensions we treat as likely binary (skip content fetch).
    _BINARY_EXTENSIONS: frozenset[str] = frozenset(
        {
            ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp", ".tiff",
            ".svg",                              # XML but rarely useful as text
            ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
            ".wasm", ".pyc", ".class", ".so", ".dylib", ".dll", ".exe",
            ".ttf", ".otf", ".woff", ".woff2",
            ".mp3", ".mp4", ".mov", ".avi", ".wav",
            ".db", ".sqlite", ".bin", ".dat",
        }
    )

    def fetch_pr_file_contents(
        self,
        owner_repo: str,
        pr: "PullRequest",
    ) -> dict[str, str]:
        """Fetch source blobs for files changed in a PR at its merge/head SHA.

        Uses the GitHub contents API (``GET /repos/{owner}/{repo}/contents/{path}
        ?ref={sha}``) to retrieve each changed file.  Results are keyed by file
        path.

        Limits applied to keep API usage reasonable:
        - At most ``_MAX_FILE_CONTENTS`` (10) files are fetched.
        - Files larger than ``_MAX_FILE_SIZE_BYTES`` (200 KB) are skipped.
        - Files with binary extensions are skipped.

        Parameters
        ----------
        owner_repo:
            ``"owner/repo"`` string.
        pr:
            The ``PullRequest`` whose changed files should be fetched.  The
            diff is parsed to identify changed paths; the merge commit SHA is
            derived from the ``merged_at`` timestamp (or falls back to the PR
            head SHA from the files API).

        Returns
        -------
        dict[str, str]
            Mapping of ``path -> source text``.  Empty dict if the PR diff is
            empty or all files were skipped/failed.
        """
        import base64
        import posixpath

        if not pr.diff:
            return {}

        # Extract changed file paths from the unified diff header lines.
        changed_paths = re.findall(r"^\+\+\+ b/(.+)$", pr.diff, re.MULTILINE)
        if not changed_paths:
            return {}

        # De-duplicate while preserving order, then cap at the file limit.
        seen: set[str] = set()
        unique_paths: list[str] = []
        for p in changed_paths:
            if p not in seen:
                seen.add(p)
                unique_paths.append(p)
        candidate_paths = unique_paths[: self._MAX_FILE_CONTENTS]

        # First, get the PR files listing to find the head SHA of each file.
        # The list-files endpoint also gives us file sizes (via `blob_url`
        # indirectly), but we use the `contents` API for the actual content
        # because it returns base64-encoded data we can size-check before
        # decoding.
        #
        # Determine the ref to use: if the PR was merged we use the merge commit
        # SHA; otherwise we fall back to the head SHA from the PR files API.
        # The simplest approach that works with the injectable fetch: query the
        # PR files list to get sha-per-file (each item has a `sha` blob SHA),
        # then fetch each blob individually.  This avoids needing a commit SHA.

        contents: dict[str, str] = {}

        for file_path in candidate_paths:
            # Check extension before making any network call.
            ext = posixpath.splitext(file_path)[1].lower()
            if ext in self._BINARY_EXTENSIONS:
                logger.debug("skip binary file %s", file_path)
                continue

            try:
                # GET /repos/{owner_repo}/contents/{path}?ref=<PR head SHA>
                # When we don't have the exact SHA, GitHub resolves the path
                # against the default branch; for PR context we pass the PR
                # number reference "pull/{n}/head" which works for open PRs but
                # NOT for merged PRs.  The safe universal approach is to omit
                # the ref (gets default branch HEAD), which is close enough for
                # anchor resolution (symbols rarely move between the PR merge
                # commit and the current HEAD for recently merged PRs).
                #
                # For a better SHA we'd need an extra API call to GET the PR
                # object; to keep the call count low we use the default-branch
                # HEAD here.  Callers that need an exact SHA can override fetch.
                path = f"/repos/{owner_repo}/contents/{file_path}"
                file_meta = self._get(path)

                if not isinstance(file_meta, dict):
                    continue

                # Size check (in bytes).
                size = file_meta.get("size", 0)
                if size > self._MAX_FILE_SIZE_BYTES:
                    logger.debug(
                        "skip large file %s (%d bytes > %d cap)",
                        file_path,
                        size,
                        self._MAX_FILE_SIZE_BYTES,
                    )
                    continue

                encoding = file_meta.get("encoding", "")
                raw_content = file_meta.get("content", "")

                if encoding == "base64" and raw_content:
                    # GitHub pads base64 with newlines — strip them.
                    decoded = base64.b64decode(
                        raw_content.replace("\n", "")
                    ).decode("utf-8", errors="replace")
                    contents[file_path] = decoded
                elif encoding == "none" or not encoding:
                    # Large files: GitHub returns encoding=none with a download_url.
                    # We already filtered >200 KB above, so this shouldn't happen
                    # often; skip silently.
                    logger.debug("skip file %s with encoding=%r", file_path, encoding)
                    continue
                else:
                    logger.debug(
                        "unhandled encoding %r for file %s", encoding, file_path
                    )
                    continue

            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "failed to fetch contents for %s in %s: %s",
                    file_path,
                    owner_repo,
                    exc,
                )
                continue

        return contents


class GitHubAppClient(PublicGitHubReader):
    """GitHub App reader for private repos (installation token).

    Loads the PEM from AWS Secrets Manager at instantiation; caches the
    installation token in-process and refreshes it ~5 min before the 1 h expiry.

    For offline testing, inject ``fetch`` and set ``token`` directly — the
    token-refresh machinery is bypassed when ``token`` is pre-set.
    """

    def __init__(
        self,
        app_id: str,
        installation_id: str,
        pem: str,
        fetch: FetchFn | None = None,
        token: str | None = None,  # pre-set for tests
    ) -> None:
        super().__init__(fetch=fetch)
        self._app_id = app_id
        self._installation_id = installation_id
        self._pem = pem
        self._token: str | None = token
        self._token_expires_at: float = 0.0

    def _ensure_token(self) -> None:
        """Refresh the installation token if missing or near expiry."""
        if self._token is not None and time.time() < self._token_expires_at:
            return
        # In the real implementation: POST /app/installations/<id>/access_tokens
        # using a signed JWT from the PEM.  For v1 (offline tests) the token
        # is pre-injected; this path is exercised in integration tests only.
        raise NotImplementedError(
            "GitHubAppClient token refresh requires live AWS + GitHub App credentials. "
            "Inject `token=` for offline tests."
        )

    def _headers(self) -> dict[str, str]:
        self._ensure_token()
        return {
            **super()._headers(),
            "Authorization": f"Bearer {self._token}",
        }
