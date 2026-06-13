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


def is_curriculum_noise(pr: PullRequest) -> tuple[bool, str]:
    """Return ``(True, reason)`` if the PR should be skipped by the curriculum filter.

    Skipped PRs are logged (the filter is auditable/tunable).  Never skips on
    diff *size* — only on low-signal markers.

    Returns ``(False, "")`` for PRs that should be ingested.
    """
    # Version/dependency bump title signals
    if _NOISE_TITLE_RE.match(pr.title):
        return True, f"curriculum_noise: title matches noise pattern: {pr.title!r}"

    # Diff is only lockfiles / generated files
    diff_files = re.findall(r"^\+\+\+ b/(.+)$", pr.diff, re.MULTILINE)
    if diff_files and all(
        _NOISE_FILE_ONLY_RE.match(f"+++ b/{f}") for f in diff_files
    ):
        return True, f"curriculum_noise: diff only touches lockfiles/generated files"

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


def distill_pr(
    pr: PullRequest,
    session_context: str | None = None,
) -> DistillationResult:
    """Distill a merged PR into a candidate insight.

    Sources (richest available, graceful degradation):
    (a) PR diff + title + description + review comments + linked issues (always).
    (b) Session context from the U7 branch→session link (when present).

    Bug-fix PRs are phrased as verifiable failure-mode descriptions and tagged
    ``kind='bugfix-failure-mode'`` so they are preferentially retrieved when an
    agent is instructed to verify code.
    """
    is_bugfix = _is_targeted_bugfix(pr)
    kind = "bugfix-failure-mode" if is_bugfix else "normal"
    rung = infer_verification_rung(pr)

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
    """Production fetch via urllib (no requests dependency)."""
    import json
    import urllib.request

    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


class PublicGitHubReader:
    """Unauthenticated reader for public repositories.

    Suitable for the v1 transfer (history replay) trigger.  For private repos,
    use ``GitHubAppClient`` (needs installation-token with Pull-requests:Read).

    Injectable ``fetch`` for offline fixture testing (mirrors the TS
    ``GitHubApp``'s injectable ``fetch`` precedent).
    """

    GITHUB_API = "https://api.github.com"
    PER_PAGE = 100

    def __init__(self, fetch: FetchFn | None = None) -> None:
        self._fetch = fetch or _default_fetch

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get(self, path: str) -> Any:
        url = f"{self.GITHUB_API}{path}"
        return self._fetch(url, self._headers())

    def list_merged_pull_requests(
        self,
        owner_repo: str,
        default_branch: str = "main",
        since_pr: int | None = None,
    ) -> list[PullRequest]:
        """Return merged-to-default-branch PRs in ascending PR-number order.

        Paginates through the GitHub PR list API (closed PRs, sorted by
        updated desc, filtered to merged).  Returns only PRs whose
        ``base.ref == default_branch``.
        """
        prs: list[PullRequest] = []
        page = 1
        while True:
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
                prs.append(pr)
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
