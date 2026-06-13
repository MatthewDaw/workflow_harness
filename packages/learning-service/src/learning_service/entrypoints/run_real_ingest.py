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

    # authenticated (>60 req/hr) via GITHUB_TOKEN env var:
    GITHUB_TOKEN=<token> .venv/Scripts/python.exe -m learning_service.entrypoints.run_real_ingest \
        --org puzzle --repo puzzle/okr --max-prs 20

    # or pass the env-var name explicitly:
    .venv/Scripts/python.exe -m learning_service.entrypoints.run_real_ingest \
        --org puzzle --repo puzzle/okr --max-prs 20 --token-env MY_GH_TOKEN
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
    max_pages: int = 5,
    since_pr: int | None = None,
    mode: str = "enforce",
    default_branch: str = "main",
    token: str | None = None,
    token_env: str | None = None,
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
        Cap on the number of most-recent NON-NOISE merged PRs to process.
        The reader skips curriculum-noise PRs (renovate/dependabot) while
        paginating so this budget is spent on real human PRs only.
    max_pages:
        Hard ceiling on the number of GitHub list-pages fetched (default 5).
        Prevents runaway pagination on heavily noise-dominated repos.
    since_pr:
        When supplied, only PRs with number > since_pr are processed (resume).
    mode:
        ``"enforce"`` (default) — writes ideas/anchors to the store.
        ``"shadow"`` — log-only dry-run, no writes.
    default_branch:
        The branch PRs must target to be included (default ``"main"``).
        Set to ``"preview"`` for makeplane/plane, etc.
    token:
        Optional GitHub PAT or fine-grained token.  When provided, every
        GitHub API request carries ``Authorization: Bearer <token>``, lifting
        the rate limit from 60 to 5 000 req/hr.  The value is NEVER logged.
    token_env:
        Name of an environment variable that holds a GitHub token (alternative
        to passing ``token`` directly).  Falls back to ``GITHUB_TOKEN`` when
        neither ``token`` nor ``token_env`` resolves to a value.
    store:
        Optional pre-built LearningStore.  When None an InMemoryLearningStore
        is constructed (no AWS needed).
    reader:
        Optional pre-built reader (for testing; must expose
        ``list_merged_pull_requests``, ``enrich_pr_diff``, and
        ``fetch_pr_file_contents``).  When None a real ``PublicGitHubReader``
        is constructed (token wiring applies).

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
    import os

    from learning_service.db.store import InMemoryLearningStore
    from learning_service.entrypoints.ingest import IngestConfig, run_ingest

    if mode not in ("shadow", "enforce"):
        raise ValueError(f"mode must be 'shadow' or 'enforce', got: {mode!r}")

    # --- Resolve GitHub auth token (NEVER log the value) ----------------------
    resolved_token: str | None = token
    if resolved_token is None and token_env:
        resolved_token = os.environ.get(token_env) or None
    if resolved_token is None:
        resolved_token = os.environ.get("GITHUB_TOKEN") or None
    # Log only whether a token is present, not its value.
    logger.info(
        "run_real_ingest: GitHub token %s",
        "present (authenticated)" if resolved_token else "absent (unauthenticated, 60 req/hr)",
    )

    # --- Fetch merged PRs (with diff enrichment) from GitHub ------------------
    if reader is None:
        from learning_service.github import PublicGitHubReader
        reader = PublicGitHubReader(token=resolved_token)

    logger.info(
        "run_real_ingest: listing merged PRs for %s "
        "(since_pr=%s, max_prs=%s, max_pages=%s, default_branch=%s, skip_noise=True)",
        repo,
        since_pr,
        max_prs,
        max_pages,
        default_branch,
    )

    # Pass skip_noise=True and max_pages so the max_prs budget is spent on
    # real human PRs (not renovate/dependabot noise), and the hard page cap
    # prevents runaway pagination on heavily noise-dominated repos.
    prs = reader.list_merged_pull_requests(
        owner_repo=repo,
        default_branch=default_branch,
        since_pr=since_pr,
        max_prs=max_prs,
        max_pages=max_pages,
        skip_noise=True,
    )
    prs_fetched = len(prs)
    logger.info(
        "run_real_ingest: fetched %d non-noise merged PRs "
        "(noise filtered during pagination, max_prs cap applied by reader)",
        prs_fetched,
    )

    # Enrich each PR with its unified diff (one extra API call per PR).
    for pr in prs:
        if not pr.diff:
            pr.diff = reader.enrich_pr_diff(repo, pr.number)

    # --- Fetch file contents for symbol resolution (anchor extraction) --------
    # The pipeline's anchor extraction (fold/SYMBOL RESOLUTION) needs the source
    # blobs for the changed files.  Build a merged dict keyed by path.
    # tree-sitter (anchor symbol resolution) requires bytes, not str — the
    # pipeline/merge_handler contract is dict[str, bytes].  Encode at the producer.
    file_contents: dict[str, bytes] = {}
    for pr in prs:
        if pr.diff:
            try:
                pr_contents = reader.fetch_pr_file_contents(repo, pr)
                file_contents.update(
                    {
                        path: (src.encode("utf-8") if isinstance(src, str) else src)
                        for path, src in pr_contents.items()
                    }
                )
                logger.debug(
                    "run_real_ingest: fetched %d file blobs for PR #%d",
                    len(pr_contents),
                    pr.number,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "run_real_ingest: failed to fetch file contents for PR #%d: %s",
                    pr.number,
                    exc,
                )
    logger.info(
        "run_real_ingest: file_contents has %d unique paths across all PRs",
        len(file_contents),
    )

    prs_submitted = len(prs)
    logger.info("run_real_ingest: submitting %d PRs to run_ingest (mode=%s)", prs_submitted, mode)

    # --- Build the store if not provided ------------------------------------
    learning_store = store if store is not None else InMemoryLearningStore()

    # --- Build IngestConfig with pr_log + store as dynamic attributes -------
    nli_mode = os.environ.get("AF_NLI_MODE", os.environ.get("LS_NLI_MODE", "replay"))
    judge_mode = os.environ.get("AF_JUDGE_MODE", os.environ.get("LS_JUDGE_MODE", "replay"))
    config = IngestConfig(
        org=org,
        repo=repo,
        since_pr=since_pr,
        mode=mode,
        nli_mode=nli_mode,
        judge_mode=judge_mode,
        default_branch=default_branch,
    )
    # The ingest entrypoint reads these via getattr() — set as dynamic attrs.
    config.pr_log = prs                          # type: ignore[attr-defined]
    config.store = learning_store                # type: ignore[attr-defined]
    config.file_contents = file_contents         # type: ignore[attr-defined]

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
        help="Max number of non-noise merged PRs to process (default: 8). "
             "Curriculum-noise PRs (renovate/dependabot) are skipped during pagination "
             "so this budget is spent on real human PRs only.",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=5,
        help="Hard ceiling on GitHub list-pages fetched (default: 5). "
             "Prevents runaway pagination on noise-dominated repos.",
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
    parser.add_argument(
        "--token-env",
        default=None,
        metavar="ENV_VAR",
        help=(
            "Name of the environment variable holding a GitHub PAT "
            "(e.g. GITHUB_TOKEN).  When set, requests carry "
            "Authorization: Bearer <token>, lifting the rate limit from "
            "60 to 5 000 req/hr.  The token value is never logged."
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
        max_pages=args.max_pages,
        since_pr=args.since_pr,
        mode=args.mode,
        default_branch=args.default_branch,
        token_env=args.token_env,
    )
    import json
    print(json.dumps(stats, indent=2))
    sys.exit(stats["run_ingest_rc"])


if __name__ == "__main__":
    main()
