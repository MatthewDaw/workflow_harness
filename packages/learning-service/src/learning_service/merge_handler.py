"""merge_handler.py — U1: PR-merge ingestion driver (MAT-137).

One handler that turns a merged-to-main PR into distilled, anchored candidate
insight(s).  Driven by the user-triggered replay (v1) or, later, a webhook
(deferred continuous mode — same code path, different trigger).

The handler:
1. Checks the org-scoped idempotency cursor (``PROCESSED#<repo>#<prNumber>``).
2. Handles non-default-base PRs (ignored — not verification).
3. Handles closed-unmerged PRs (negative signal — recorded, not distilled).
4. Applies the curriculum filter (skip low-signal noise; log every drop).
5. Enriches with the U7 branch→session link (graceful degradation to PR-only).
6. Distills from three sources (PR diff/title/desc/reviews + session context).
7. Extracts code anchors from the diff (passed to caller for U2 write).
8. Marks the PR as processed (idempotency cursor).

Returns a ``MergeHandlerResult`` which the caller (replay loop or webhook)
uses to decide whether to corroborate/create an idea (U4) and write anchors
(U2).

Shadow vs. enforce mode: in shadow mode the handler computes decisions and
logs them but does NOT write to the store or mark the PR processed.  Enforce
mode writes the cursor and the result is passed to the corroboration step.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from learning_service.github import (
    PullRequest,
    DistillationResult,
    distill_pr,
    is_curriculum_noise,
    RUNG_WEIGHTS,
    scrub_secrets,
    generalize_repo_specifics,
)

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore

from learning_service.db.store import ProcessedPrRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class MergeHandlerResult:
    """The output of processing one PR through the merge handler.

    ``action`` indicates what happened:
      - ``"distilled"``        — positive: candidate insight produced.
      - ``"negative"``         — closed-unmerged PR (rejected, not positive).
      - ``"skipped_idempotent"`` — already processed (cursor hit).
      - ``"skipped_non_default_base"`` — PR targets a non-default branch.
      - ``"skipped_noise"``    — curriculum filter dropped it.
    """

    action: str
    pr_number: int
    owner_repo: str
    reason: str = ""
    distillation: DistillationResult | None = None
    rung: str = "bare"           # effective rung (for weight computation)
    rung_weight: float = 0.4     # rung × 1.0 (credibility applied by U4)
    is_bugfix: bool = False
    kind: str = "normal"         # "normal" | "bugfix-failure-mode"
    anchors_diff: str = ""       # raw diff forwarded to U2 anchor extraction


# ---------------------------------------------------------------------------
# Core handler
# ---------------------------------------------------------------------------


def handle_merged_pr(
    pr: PullRequest,
    org: str,
    store: "LearningStore",
    *,
    default_branch: str = "main",
    mode: str = "shadow",
) -> MergeHandlerResult:
    """Process one PR through the ingest handler.

    Parameters
    ----------
    pr:
        The ``PullRequest`` from the GitHub reader.
    org:
        Org slug (DynamoDB partition key).
    store:
        The ``LearningStore`` (``InMemoryLearningStore`` in tests;
        ``DynamoLearningStore`` in production).
    default_branch:
        The repo's default branch name (e.g. ``"main"`` or ``"master"``).
        PRs targeting other branches are ignored.
    mode:
        ``"shadow"`` — compute + log decisions only (no writes).
        ``"enforce"`` — write the idempotency cursor on success.
    """
    owner_repo = pr.owner_repo

    # 1. Non-default-base guard: only PRs targeting the default branch count.
    if pr.base_branch != default_branch:
        logger.info(
            "merge_handler skip pr=%d repo=%s reason=non_default_base base=%s",
            pr.number, owner_repo, pr.base_branch,
        )
        return MergeHandlerResult(
            action="skipped_non_default_base",
            pr_number=pr.number,
            owner_repo=owner_repo,
            reason=f"base branch {pr.base_branch!r} != default {default_branch!r}",
        )

    # 2. Idempotency cursor: skip if already processed.
    if store.is_pr_processed(org, owner_repo, pr.number):
        logger.info(
            "merge_handler skip pr=%d repo=%s reason=idempotent",
            pr.number, owner_repo,
        )
        return MergeHandlerResult(
            action="skipped_idempotent",
            pr_number=pr.number,
            owner_repo=owner_repo,
            reason="already processed (PROCESSED# cursor)",
        )

    # 3. Closed-unmerged: a negative signal.  Record and return; do not distill.
    if not pr.merged:
        logger.info(
            "merge_handler negative pr=%d repo=%s reason=closed_unmerged",
            pr.number, owner_repo,
        )
        if mode == "enforce":
            store.mark_pr_processed(
                ProcessedPrRecord(
                    org=org,
                    owner_repo=owner_repo,
                    pr_number=pr.number,
                    processed_at=int(time.time() * 1000),
                )
            )
        return MergeHandlerResult(
            action="negative",
            pr_number=pr.number,
            owner_repo=owner_repo,
            reason="closed without merge (negative signal)",
        )

    # 4. Curriculum filter: drop low-signal noise.
    is_noise, noise_reason = is_curriculum_noise(pr)
    if is_noise:
        logger.info(
            "merge_handler skip pr=%d repo=%s reason=%s",
            pr.number, owner_repo, noise_reason,
        )
        if mode == "enforce":
            store.mark_pr_processed(
                ProcessedPrRecord(
                    org=org,
                    owner_repo=owner_repo,
                    pr_number=pr.number,
                    processed_at=int(time.time() * 1000),
                )
            )
        return MergeHandlerResult(
            action="skipped_noise",
            pr_number=pr.number,
            owner_repo=owner_repo,
            reason=noise_reason,
        )

    # 5. Enrich with U7 session context (graceful degradation to None).
    session_ctx: str | None = None
    link = store.get_branch_session_link(
        org, owner_repo, _branch_for_pr(pr)
    )
    if link is not None and link.distilledContext:
        session_ctx = link.distilledContext
        logger.info(
            "merge_handler enriched pr=%d repo=%s session=%s",
            pr.number, owner_repo, link.sessionId,
        )
    else:
        logger.info(
            "merge_handler no_session_link pr=%d repo=%s degrading_to_pr_only",
            pr.number, owner_repo,
        )

    # 6. Distill: produce the candidate insight.
    result = distill_pr(pr, session_context=session_ctx)

    rung_weight = RUNG_WEIGHTS.get(result.rung, 0.4)
    logger.info(
        "merge_handler distilled pr=%d repo=%s rung=%s weight=%.1f kind=%s",
        pr.number, owner_repo, result.rung, rung_weight, result.kind,
    )

    # 7. Mark as processed (enforce mode only).
    if mode == "enforce":
        store.mark_pr_processed(
            ProcessedPrRecord(
                org=org,
                owner_repo=owner_repo,
                pr_number=pr.number,
                processed_at=int(time.time() * 1000),
            )
        )

    return MergeHandlerResult(
        action="distilled",
        pr_number=pr.number,
        owner_repo=owner_repo,
        distillation=result,
        rung=result.rung,
        rung_weight=rung_weight,
        is_bugfix=result.is_bugfix,
        kind=result.kind,
        anchors_diff=pr.diff,
    )


# ---------------------------------------------------------------------------
# Replay driver (v1 user-triggered)
# ---------------------------------------------------------------------------


def replay_merge_log(
    pull_requests: list[PullRequest],
    org: str,
    store: "LearningStore",
    *,
    default_branch: str = "main",
    mode: str = "shadow",
) -> list[MergeHandlerResult]:
    """Fold forward over an ordered list of PRs (the replay driver).

    PRs must be in ascending PR-number (merge-commit) order — the deterministic
    replay order that makes re-running the same log produce the same library.

    Idempotency: already-processed PRs are silently skipped (the cursor).
    """
    results: list[MergeHandlerResult] = []
    for pr in pull_requests:
        r = handle_merged_pr(
            pr,
            org,
            store,
            default_branch=default_branch,
            mode=mode,
        )
        results.append(r)
        logger.debug(
            "replay pr=%d action=%s",
            pr.number, r.action,
        )
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _branch_for_pr(pr: PullRequest) -> str:
    """Return a plausible branch name for the U7 link lookup.

    In v1 the branch name is not always available in the PR payload from the
    replay fixture; we use a stable synthetic branch name as a fallback.
    The U7 link lookup degrades gracefully to None when the branch is absent.
    """
    # The real GitHub API provides head.ref; our PullRequest type doesn't
    # carry it yet (kept minimal).  Return a stable synthetic name so the
    # store lookup can be seeded in tests without adding a field to PullRequest.
    return f"pr-{pr.number}"
