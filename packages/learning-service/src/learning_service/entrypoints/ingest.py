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
    LS_MODE                shadow    Overall mode: shadow logs decisions without writing.
    LS_VERIFIED_K          2         Fold threshold (accumulated rung×credibility weight).
    LS_NLI_MODE            replay    NLI mode forwarded to agent_families.nli.classify.
    LS_JUDGE_MODE          replay    Judge mode forwarded to agent_families.judge.run_judge.
    LS_VERIFIED_LEARNING_MODE  shadow  verified_learning_mode gate (shadow|enforce).
    LS_SUPERSEDE_MODE      shadow    supersede_mode gate (shadow|enforce).
    LS_UNFOLD_MODE         shadow    unfold_mode gate (shadow|enforce).

Mode-flip gates (Gap 3)
-----------------------
The ``enforce`` modes for ``verified_learning_mode``, ``supersede_mode``, and
``unfold_mode`` are **not** trusted from the environment blindly.  At startup,
``gate_status(telemetry.snapshot(), config)`` is consulted:

- ``LS_VERIFIED_LEARNING_MODE=enforce`` is accepted only when the gate says
  the anchor-resolution signal is calibrated.
- ``LS_SUPERSEDE_MODE=enforce`` / ``LS_UNFOLD_MODE=enforce`` are accepted only
  when the supersede FP rate is below the ceiling over enough spot-checks.

If the gate is not open, the mode falls back to ``shadow`` and a warning is
logged.  The override is deliberate — an operator who has pre-loaded spot-check
data into the accumulator (or who is running with a known-clean corpus) can
still flip to enforce by ensuring the gate is open at call time.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field

from learning_service.telemetry import (
    GateConfig,
    TelemetryAccumulator,
    gate_status,
    to_json_dict,
)

logger = logging.getLogger(__name__)


@dataclass
class IngestConfig:
    """Runtime configuration for one ingest run."""

    org: str
    repo: str
    since_pr: int | None = None
    mode: str = "shadow"  # shadow | enforce (overall ingest mode)
    # Per-subsystem mode flags — each may be independently enforced or shadowed.
    # These are gated by gate_status() before any enforce write is allowed.
    verified_learning_mode: str = "shadow"   # gate: anchor-resolution calibrated
    supersede_mode: str = "shadow"           # gate: FP rate < ceiling
    unfold_mode: str = "shadow"              # gate: supersede gate must pass first
    verified_k: float = 2.0
    nli_mode: str = "replay"
    judge_mode: str = "replay"
    # Extension point: boto3 DynamoDB resource injected at runtime; None → dry-run.
    dynamo: object = field(default=None, repr=False)
    # Pre-seeded telemetry accumulator (for testing or warm-start with spot-check data).
    telemetry: TelemetryAccumulator = field(default_factory=TelemetryAccumulator, repr=False)
    # Gate config thresholds.
    gate_config: GateConfig = field(default_factory=GateConfig, repr=False)


def _resolve_modes_via_telemetry(config: IngestConfig) -> dict[str, str]:
    """Consult gate_status() to validate mode-flip flags (Gap 3).

    Returns a dict of resolved modes:
      {
        "verified_learning_mode": "shadow" | "enforce",
        "supersede_mode":         "shadow" | "enforce",
        "unfold_mode":            "shadow" | "enforce",
      }

    An operator-requested ``enforce`` is honoured only when the corresponding
    telemetry gate is open.  If the gate is not open, the mode silently falls
    back to ``shadow`` and a warning is logged — this prevents bulk-enforce on
    an uncalibrated corpus.
    """
    snap = config.telemetry.snapshot()
    gs = gate_status(snap, config=config.gate_config)

    resolved: dict[str, str] = {}

    # verified_learning_mode gate: anchor-resolution signal calibrated.
    vl_requested = config.verified_learning_mode
    if vl_requested == "enforce" and not gs.verified_learning_gate_open:
        logger.warning(
            "ingest: verified_learning_mode=enforce requested but gate blocked (%s) "
            "— falling back to shadow",
            gs.verified_learning_reason,
        )
        resolved["verified_learning_mode"] = "shadow"
    else:
        resolved["verified_learning_mode"] = vl_requested

    # supersede_mode gate: FP rate < ceiling over enough spot-checks.
    sup_requested = config.supersede_mode
    if sup_requested == "enforce" and not gs.supersede_gate_open:
        logger.warning(
            "ingest: supersede_mode=enforce requested but gate blocked (%s) "
            "— falling back to shadow",
            gs.supersede_reason,
        )
        resolved["supersede_mode"] = "shadow"
    else:
        resolved["supersede_mode"] = sup_requested

    # unfold_mode gate: depends on the supersede gate.
    unfold_requested = config.unfold_mode
    if unfold_requested == "enforce" and not gs.unfold_gate_open:
        logger.warning(
            "ingest: unfold_mode=enforce requested but gate blocked (%s) "
            "— falling back to shadow",
            gs.unfold_reason,
        )
        resolved["unfold_mode"] = "shadow"
    else:
        resolved["unfold_mode"] = unfold_requested

    logger.info(
        "ingest gate_status: vl_gate=%s sup_gate=%s unfold_gate=%s | "
        "resolved: vl=%s sup=%s unfold=%s",
        gs.verified_learning_gate_open,
        gs.supersede_gate_open,
        gs.unfold_gate_open,
        resolved["verified_learning_mode"],
        resolved["supersede_mode"],
        resolved["unfold_mode"],
    )
    return resolved


def _emit_telemetry(config: IngestConfig, resolved_modes: dict[str, str]) -> None:
    """Emit accumulated telemetry as a structured log line (Gap 2).

    In production this is the CloudWatch structured-log surface: the Lambda
    log group is exported to CloudWatch Logs Insights / a MetricFilter, so
    every ``TELEMETRY`` line is queryable and can drive a CloudWatch Dashboard.

    The ``to_json_dict(snapshot)`` payload is the canonical R4 metric dict.
    """
    snap = config.telemetry.snapshot()
    payload = to_json_dict(snap)
    payload["org"] = config.org
    payload["repo"] = config.repo
    payload["resolved_modes"] = resolved_modes
    # Emit as a single structured-log line (parseable by CloudWatch Logs Insights).
    logger.info("TELEMETRY %s", json.dumps(payload))


def run_ingest(config: IngestConfig) -> int:
    """Execute the ingest job.  Returns exit code (0 = success, non-zero = error).

    Validates config, consults the telemetry gate to resolve enforce/shadow modes
    (Gap 3), runs the merge handler loop, and emits calibration metrics (Gap 2)
    at the end of the run.

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

    # --- Gate check: resolve per-subsystem enforce modes via telemetry (Gap 3) --
    resolved_modes = _resolve_modes_via_telemetry(config)

    logger.info(
        "ingest start: org=%s repo=%s since_pr=%s mode=%s verified_k=%s "
        "vl_mode=%s sup_mode=%s unfold_mode=%s",
        config.org,
        config.repo,
        config.since_pr,
        config.mode,
        config.verified_k,
        resolved_modes["verified_learning_mode"],
        resolved_modes["supersede_mode"],
        resolved_modes["unfold_mode"],
    )

    # --- stub: PR iteration + merge handler ------------------------------------
    # The full implementation is U1 (PR ingestion driver).  The scaffolded loop:
    #   for pr in list_merged_pull_requests(config.repo, since=config.since_pr):
    #       if is_pr_processed(config.org, config.repo, pr.number):
    #           continue  # idempotency cursor
    #       candidates = merge_handler(pr, config, telemetry=config.telemetry)
    #       for candidate in candidates:
    #           corroborate_or_create(candidate, config, telemetry=config.telemetry)
    #       mark_pr_processed(config.org, config.repo, pr.number)
    logger.info("ingest stub: no PRs processed (U1 handler not yet wired)")

    # --- Emit calibration metrics (Gap 2) --------------------------------------
    _emit_telemetry(config, resolved_modes)

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
    parser.add_argument(
        "--verified-learning-mode",
        choices=("shadow", "enforce"),
        default=os.environ.get("LS_VERIFIED_LEARNING_MODE", "shadow"),
        help="verified_learning_mode gate (telemetry-gated; default: shadow)",
    )
    parser.add_argument(
        "--supersede-mode",
        choices=("shadow", "enforce"),
        default=os.environ.get("LS_SUPERSEDE_MODE", "shadow"),
        help="supersede_mode gate (telemetry-gated; default: shadow)",
    )
    parser.add_argument(
        "--unfold-mode",
        choices=("shadow", "enforce"),
        default=os.environ.get("LS_UNFOLD_MODE", "shadow"),
        help="unfold_mode gate (telemetry-gated; default: shadow)",
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
        verified_learning_mode=args.verified_learning_mode,
        supersede_mode=args.supersede_mode,
        unfold_mode=args.unfold_mode,
        nli_mode=os.environ.get("LS_NLI_MODE", "replay"),
        judge_mode=os.environ.get("LS_JUDGE_MODE", "replay"),
    )
    sys.exit(run_ingest(config))


def lambda_handler(event: dict, context: object) -> dict:
    """AWS Lambda handler for the container Lambda ingest entrypoint.

    This is the CMD target set in the LearningStack CDK construct:
      ``learning_service.entrypoints.ingest.lambda_handler``

    The Lambda event may supply ``org``, ``repo``, ``since_pr``, and ``mode``
    fields.  Missing fields fall back to environment variable defaults so the
    ingest job can also be invoked directly from the CLI (``learning-ingest``).

    Example event::

        {
            "org": "acme",
            "repo": "acme/backend",
            "since_pr": 100,
            "mode": "shadow"
        }
    """
    import os

    org = event.get("org", "")
    repo = event.get("repo", "")
    since_pr = event.get("since_pr", None)
    mode = event.get("mode", os.environ.get("LS_MODE", "shadow"))

    if not org or not repo:
        return {
            "statusCode": 400,
            "body": "org and repo are required in the Lambda event payload",
        }

    config = IngestConfig(
        org=org,
        repo=repo,
        since_pr=since_pr,
        mode=mode,
        verified_learning_mode=event.get(
            "verified_learning_mode",
            os.environ.get("LS_VERIFIED_LEARNING_MODE", "shadow"),
        ),
        supersede_mode=event.get(
            "supersede_mode",
            os.environ.get("LS_SUPERSEDE_MODE", "shadow"),
        ),
        unfold_mode=event.get(
            "unfold_mode",
            os.environ.get("LS_UNFOLD_MODE", "shadow"),
        ),
        nli_mode=os.environ.get("LS_NLI_MODE", "replay"),
        judge_mode=os.environ.get("LS_JUDGE_MODE", "replay"),
    )

    exit_code = run_ingest(config)
    if exit_code == 0:
        return {"statusCode": 200, "body": f"ingest ok: org={org} repo={repo}"}
    return {
        "statusCode": 500,
        "body": f"ingest failed with exit code {exit_code}: org={org} repo={repo}",
    }


if __name__ == "__main__":
    main()
