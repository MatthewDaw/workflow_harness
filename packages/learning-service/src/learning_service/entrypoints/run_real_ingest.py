"""run_real_ingest.py — Glue: real GitHub fetch -> run_ingest pipeline.

Wires a PublicGitHubReader to the existing run_ingest entrypoint so a user
can point at any public repo and get the full Verified Learning pipeline to
run against its most-recent merged PRs.

Usage (module)::

    from learning_service.entrypoints.run_real_ingest import run_real_ingest
    stats = run_real_ingest("puzzle", "puzzle/okr", max_prs=5)
    # enforce mode (writes to store):
    stats = run_real_ingest("puzzle", "puzzle/okr", max_prs=5, mode="enforce")

Usage (CLI)::

    .venv/Scripts/python.exe -m learning_service.entrypoints.run_real_ingest \
        --org puzzle --repo puzzle/okr --max-prs 5

    # shadow (log-only, no writes):
    .venv/Scripts/python.exe -m learning_service.entrypoints.run_real_ingest \
        --org puzzle --repo puzzle/okr --max-prs 5 --mode shadow

    # repos where the default branch is not 'main' (e.g. makeplane/plane uses 'preview'):
    .venv/Scripts/python.exe -m learning_service.entrypoints.run_real_ingest \
        --org makeplane --repo makeplane/plane --default-branch preview --max-prs 5
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

logger = logging.getLogger(__name__)


def run_real_ingest(
    org: str,
    repo: str,
    *,
    max_prs: int = 8,
    since_pr: int | None = None,
    mode: str = "enforce",
    default_branch: str = "main",
    store: Any = None,
    reader: Any = None,
) -> dict:
    """Fetch real merged PRs from GitHub and run the Verified Learning ingest pipeline.

    Parameters
    ----------
    org:
        Organisation slug used as the DynamoDB partition key and telemetry label.
    repo:
        ``owner/repo`` string, e.g. ``"puzzle/okr"`` or ``"makeplane/plane"``.
    max_prs:
        Cap on the number of most-recent merged PRs to process.  The reader
        now stops paginating once this many merged PRs are gathered (no
        post-fetch slice needed).  Keeps GitHub API consumption within the
        unauthenticated 60 req/hr quota.
    since_pr:
        When supplied, only PRs with number > since_pr are processed (resume).
    mode:
        ``"enforce"`` (default) — writes ideas/anchors to the store.
        ``"shadow"`` — log-only dry-run, no writes.
    default_branch:
        The branch PRs must target to be included (default ``"main"``).
        Set to ``"preview"`` for makeplane/plane, etc.
    store:
        Optional pre-built LearningStore.  When None an InMemoryLearningStore
        is constructed (no AWS needed).
    reader:
        Optional pre-built reader (for testing; must expose
        ``list_merged_pull_requests`` and ``enrich_pr_diff``).  When None a
        real ``PublicGitHubReader`` is constructed.

    Returns
    -------
    dict with keys:
        prs_fetched       — number of PRs returned by the GitHub reader
        prs_submitted     — number of PRs actually passed to run_ingest
        run_ingest_rc     — integer return code from run_ingest (0 = success)
        ideas             — number of idea records in the store after the run
        processed_prs     — number of processed-PR cursors written to the store
        anchors           — number of anchor records in the store after the run
    """
    from learning_service.db.store import InMemoryLearningStore
    from learning_service.entrypoints.ingest import IngestConfig, run_ingest

    if mode not in ("shadow", "enforce"):
        raise ValueError(f"mode must be 'shadow' or 'enforce', got: {mode!r}")

    # --- Fetch merged PRs (with diff enrichment) from GitHub ------------------
    if reader is None:
        from learning_service.github import PublicGitHubReader
        reader = PublicGitHubReader()

    logger.info(
        "run_real_ingest: listing merged PRs for %s (since_pr=%s, max_prs=%s, default_branch=%s)",
        repo,
        since_pr,
        max_prs,
        default_branch,
    )

    # The reader now accepts max_prs and stops paginating early — no post-fetch
    # slice is needed.  This keeps GitHub API calls within the unauthenticated
    # 60 req/hr budget even for large repos (e.g. makeplane/plane with ~8 k PRs).
    prs = reader.list_merged_pull_requests(
        owner_repo=repo,
        default_branch=default_branch,
        since_pr=since_pr,
        max_prs=max_prs,
    )
    prs_fetched = len(prs)
    logger.info("run_real_ingest: fetched %d merged PRs (max_prs cap applied by reader)", prs_fetched)

    # Enrich each PR with its unified diff (one extra API call per PR).
    for pr in prs:
        if not pr.diff:
            pr.diff = reader.enrich_pr_diff(repo, pr.number)

    prs_submitted = len(prs)
    logger.info("run_real_ingest: submitting %d PRs to run_ingest (mode=%s)", prs_submitted, mode)

    # --- Build the store if not provided ------------------------------------
    learning_store = store if store is not None else InMemoryLearningStore()

    # --- Build IngestConfig with pr_log + store as dynamic attributes -------
    import os
    nli_mode = os.environ.get("AF_NLI_MODE", os.environ.get("LS_NLI_MODE", "replay"))
    judge_mode = os.environ.get("AF_JUDGE_MODE", os.environ.get("LS_JUDGE_MODE", "replay"))
    config = IngestConfig(
        org=org,
        repo=repo,
        since_pr=since_pr,
        mode=mode,
        nli_mode=nli_mode,
        judge_mode=judge_mode,
    )
    # The ingest entrypoint reads these via getattr() — set as dynamic attrs.
    config.pr_log = prs                 # type: ignore[attr-defined]
    config.store = learning_store       # type: ignore[attr-defined]

    # --- Run the pipeline ----------------------------------------------------
    rc = run_ingest(config)

    # --- Collect stats from the store ----------------------------------------
    ideas_count = len(getattr(learning_store, "_ideas", {}))
    processed_prs_count = len(getattr(learning_store, "_processed_prs", {}))
    anchors_count = len(getattr(learning_store, "_anchors", []))

    stats = {
        "prs_fetched": prs_fetched,
        "prs_submitted": prs_submitted,
        "run_ingest_rc": rc,
        "ideas": ideas_count,
        "processed_prs": processed_prs_count,
        "anchors": anchors_count,
    }
    logger.info("run_real_ingest: done — %s", stats)
    return stats


def main(argv: list[str] | None = None) -> None:
    """CLI entry point: python -m learning_service.entrypoints.run_real_ingest ..."""
    parser = argparse.ArgumentParser(
        prog="run_real_ingest",
        description="Fetch real GitHub PRs and run the Verified Learning ingest pipeline.",
    )
    parser.add_argument("--org", required=True, help="Organisation slug (telemetry / store partition)")
    parser.add_argument("--repo", required=True, help="owner/repo, e.g. puzzle/okr")
    parser.add_argument(
        "--max-prs",
        type=int,
        default=8,
        help="Max number of most-recent merged PRs to process (default: 8)",
    )
    parser.add_argument(
        "--since-pr",
        type=int,
        default=None,
        help="Only process PRs with number > N (resume cursor)",
    )
    parser.add_argument(
        "--mode",
        choices=("shadow", "enforce"),
        default="enforce",
        help="shadow=log only (no writes); enforce=write ideas/anchors to store (default: enforce)",
    )
    parser.add_argument(
        "--default-branch",
        default="main",
        help=(
            "Default branch PRs must target (default: main). "
            "Set to 'preview' for makeplane/plane, etc."
        ),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    stats = run_real_ingest(
        args.org,
        args.repo,
        max_prs=args.max_prs,
        since_pr=args.since_pr,
        mode=args.mode,
        default_branch=args.default_branch,
    )
    import json
    print(json.dumps(stats, indent=2))
    sys.exit(stats["run_ingest_rc"])


if __name__ == "__main__":
    main()
