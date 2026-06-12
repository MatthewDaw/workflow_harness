"""User-triggered ingest job entrypoint (v1 primary trigger).

This is the ``learning-ingest`` CLI command.  The user points at a repo and
hits run; the job lists merged-to-main PRs in commit order and folds each
forward through the merge handler — a controlled, deterministic replay of the
merge log.  Idempotent and resumable via the org-scoped ``PROCESSED#<repo>#<prNumber>``
cursor.

Continuous mode (pull_request webhook) is a deferred follow-on — see
:mod:`learning_service.entrypoints.webhook`.  The merge *handler* is the same
code path; only the trigger differs.

Command-line usage::

    learning-ingest --org <org> --repo <owner/repo> [--since-pr <N>] [--mode shadow|enforce]

Environment variables (all optional; defaults shown):
    LS_MODE            shadow          Overall mode: shadow logs decisions without writing.
    LS_VERIFIED_K      2               Fold threshold (accumulated rung×credibility weight).
    LS_NLI_MODE        replay          NLI mode forwarded to agent_families.nli.classify.
    LS_JUDGE_MODE      replay          Judge mode forwarded to agent_families.judge.run_judge.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class IngestConfig:
    """Runtime configuration for one ingest run."""

    org: str
    repo: str
    since_pr: int | None = None
    mode: str = "shadow"  # shadow | enforce
    verified_k: float = 2.0
    nli_mode: str = "replay"
    judge_mode: str = "replay"
    # Extension point: boto3 DynamoDB resource injected at runtime; None → dry-run.
    dynamo: object = field(default=None, repr=False)


def run_ingest(config: IngestConfig) -> int:
    """Execute the ingest job.  Returns exit code (0 = success, non-zero = error).

    Stub implementation: the handler loop is wired in U1 (PR ingestion driver).
    This entrypoint validates config, logs the run parameters, and hands off to
    the merge handler for each PR in the merge log.

    The NLI and judge seams (imported from :mod:`learning_service.nli` and
    :mod:`learning_service.judge`) are available in-process — their import is
    tested by :func:`tests.test_skeleton.test_nli_classify_importable` and
    :func:`tests.test_skeleton.test_judge_run_judge_importable`.
    """
    # --- validate config -------------------------------------------------------
    if not config.org or not config.repo:
        logger.error("org and repo are required")
        return 1
    if config.mode not in ("shadow", "enforce"):
        logger.error("mode must be 'shadow' or 'enforce', got: %r", config.mode)
        return 1

    logger.info(
        "ingest start: org=%s repo=%s since_pr=%s mode=%s verified_k=%s",
        config.org,
        config.repo,
        config.since_pr,
        config.mode,
        config.verified_k,
    )

    # --- stub: PR iteration + merge handler ------------------------------------
    # The full implementation is U1 (PR ingestion driver).  The scaffolded loop:
    #   for pr in list_merged_pull_requests(config.repo, since=config.since_pr):
    #       if is_pr_processed(config.org, config.repo, pr.number):
    #           continue  # idempotency cursor
    #       candidates = merge_handler(pr, config)
    #       for candidate in candidates:
    #           corroborate_or_create(candidate, config)
    #       mark_pr_processed(config.org, config.repo, pr.number)
    logger.info("ingest stub: no PRs processed (U1 handler not yet wired)")

    logger.info("ingest done: org=%s repo=%s", config.org, config.repo)
    return 0


def main(argv: list[str] | None = None) -> None:
    """CLI entry point registered as ``learning-ingest`` in pyproject.toml."""
    import os

    parser = argparse.ArgumentParser(
        prog="learning-ingest",
        description="Replay merged-to-main PRs through the Verified Learning loop.",
    )
    parser.add_argument("--org", required=True, help="Organisation slug (DynamoDB partition key)")
    parser.add_argument("--repo", required=True, help="owner/repo (e.g. acme/backend)")
    parser.add_argument("--since-pr", type=int, default=None, help="Resume from this PR number")
    parser.add_argument(
        "--mode",
        choices=("shadow", "enforce"),
        default=os.environ.get("LS_MODE", "shadow"),
        help="shadow=log only; enforce=write (default: shadow)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    config = IngestConfig(
        org=args.org,
        repo=args.repo,
        since_pr=args.since_pr,
        mode=args.mode,
        nli_mode=os.environ.get("LS_NLI_MODE", "replay"),
        judge_mode=os.environ.get("LS_JUDGE_MODE", "replay"),
    )
    sys.exit(run_ingest(config))


if __name__ == "__main__":
    main()
