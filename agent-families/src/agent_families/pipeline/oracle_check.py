"""Grader-side verified-oracle answer checker (plan-003 U5, R12).

The verified-oracle premise: every Q&A answer the explorer sends across the
wall is grounded in a fresh observation and validated by a grader-side checker
BEFORE the pipeline sees it. The checker contract (R12):

- questions carry mandatory FEAT mentions — the retrieval mechanism: the
  mentioned FEATs' registry evidence is what the checker validates against
  (:func:`retrieve_registry_evidence`);
- checker input = (question, answer text, the explorer's fresh-observation
  a11y snapshot, the registry evidence for the mentioned FEATs);
- single-shot judge with **default-fail framing** (uncertainty fails), riding
  Phase 0's ``run_judge`` record/replay seam — the offline suite replays
  fixtures with zero quota;
- output = ``{verdict, contradiction: {claim, evidence_ref, observed}}`` —
  the contradiction object is exactly what the explorer's fresh-context
  retry prompt embeds (:func:`agent_families.pipeline.explorer.ask_question`
  owns the retry/refund/review-queue arms).

Fixture-key discipline (Phase 0 R23): the checker prompt carries NO volatile
data — registry evidence is embedded as (FEAT id + captured a11y snapshots),
never as evidence file paths or timestamps, so record/replay keys are stable
across machines and runs.

Tunables (judge model, schema-violation retries) are caller-supplied per the
established seam precedent — routing them from ``thresholds.toml`` is the
episode orchestrator's job (003 U6); nothing is hardcoded here.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent_families.judge import run_judge
from agent_families.store import QA_CHECKER_VERDICTS

if TYPE_CHECKING:
    from agent_families.store import Store

logger = logging.getLogger(__name__)

# The checker's structured-output contract (R12): verdict from the store's
# closed enum; a fail MUST carry the contradiction object the retry embeds,
# a pass MUST carry null (enforced by :func:`validate_checker_output` on the
# judge's schema-retry path).
CHECKER_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(QA_CHECKER_VERDICTS)},
        "contradiction": {
            "type": ["object", "null"],
            "properties": {
                "claim": {"type": "string"},
                "evidence_ref": {"type": "string"},
                "observed": {"type": "string"},
            },
            "required": ["claim", "evidence_ref", "observed"],
            "additionalProperties": False,
        },
    },
    "required": ["verdict", "contradiction"],
    "additionalProperties": False,
}


class OracleCheckError(Exception):
    """Checker misuse or a broken R12 invariant, with an actionable message."""


@dataclass(frozen=True)
class CheckerVerdict:
    """One grader-side checker outcome."""

    verdict: str  # "pass" | "fail"
    contradiction: dict | None
    request_hash: str
    attempts: int

    @property
    def passed(self) -> bool:
        return self.verdict == "pass"


# --- mentioned-FEAT retrieval (the R12 retrieval mechanism) ---------------------


def _load_evidence_a11y(evidence_ref: str) -> list:
    """The captured a11y snapshots inside a registry evidence file (U3's
    ``confirm_candidate`` format). Unreadable/malformed evidence yields an
    empty list — the default-fail checker then has nothing to confirm
    against, which fails the answer rather than crashing the round-trip."""
    try:
        payload = json.loads(Path(evidence_ref).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return []
    captured = payload.get("captured") if isinstance(payload, dict) else None
    if not isinstance(captured, list):
        return []
    return [c.get("a11y") for c in captured if isinstance(c, dict) and c.get("a11y")]


def retrieve_registry_evidence(store: Store, mentions) -> list[dict]:
    """The registry evidence for a question's mentioned FEATs (R12).

    Mentions are mandatory — an empty mention set means the question never
    carried its retrieval keys and cannot be checked. Every mentioned FEAT
    must have a registry row (the mentions FK guarantees this for persisted
    questions; this guards direct callers). Returns
    ``[{"feat_id", "evidence_ref", "a11y"}]`` — the prompt embeds only
    ``feat_id`` + ``a11y`` (R23: no volatile paths in judge prompts).
    """
    unique = tuple(dict.fromkeys(mentions))
    if not unique:
        raise OracleCheckError(
            "questions carry mandatory FEAT mentions (R12) — the checker"
            " received none, so there is no registry evidence to validate"
            " against"
        )
    rows: list[dict] = []
    for fid in unique:
        row = store.conn.execute(
            "SELECT id, evidence_ref FROM trace_feat WHERE id = ?", (fid,)
        ).fetchone()
        if row is None:
            raise OracleCheckError(
                f"{fid} has no registry row — features become mentionable"
                " only once runtime-confirmed and minted (R10), so a"
                " question with this mention should never have reached the"
                " checker"
            )
        rows.append(
            {
                "feat_id": fid,
                "evidence_ref": row["evidence_ref"],
                "a11y": _load_evidence_a11y(row["evidence_ref"]),
            }
        )
    return rows


# --- the checker prompt (deterministic; R23 fixture-key discipline) -------------


def build_checker_prompt(
    question: str, answer: str, observation_a11y, evidence: list[dict]
) -> str:
    """The single-shot, default-fail checker brief (R12).

    Embeds the question, the answer text, the explorer's fresh-observation
    a11y snapshot, and per-FEAT registry a11y evidence — and nothing volatile
    (no paths, no timestamps), so the request hash is replay-stable.
    """
    evidence_block = "\n".join(
        f"### {row['feat_id']}\n"
        + json.dumps(row["a11y"], sort_keys=True, ensure_ascii=False)
        for row in evidence
    )
    observation_block = json.dumps(
        observation_a11y, sort_keys=True, ensure_ascii=False
    )
    return (
        "You are the grader-side oracle checker. An explorer answered a"
        " pipeline question about the target app; decide whether the answer"
        " is consistent with the explorer's fresh UI observation and the"
        " registry evidence for the FEATs the question mentions.\n\n"
        f"## Question\n{question}\n\n"
        f"## Answer under check\n{answer}\n\n"
        f"## Fresh observation (a11y snapshot)\n{observation_block}\n\n"
        f"## Registry evidence for the mentioned FEATs\n{evidence_block}\n\n"
        "Rules (default-fail framing):\n"
        "- Your verdict MUST be 'fail' unless every claim in the answer is"
        " positively supported by the fresh observation or the registry"
        " evidence. If you are uncertain, default to 'fail'.\n"
        "- On 'fail', set contradiction to {claim, evidence_ref, observed}:"
        " the answer's offending claim, which evidence contradicts it (a"
        " FEAT id or 'fresh-observation'), and what was actually observed."
        " This object is embedded verbatim in the explorer's retry prompt.\n"
        "- On 'pass', set contradiction to null.\n\n"
        "Return structured output only: {\"verdict\", \"contradiction\"}"
        " matching the provided schema."
    )


def validate_checker_output(output: dict) -> str | None:
    """``run_judge`` extra-validate hook: the verdict/contradiction pairing."""
    if output["verdict"] == "fail" and not isinstance(
        output.get("contradiction"), dict
    ):
        return (
            "a fail verdict must carry a contradiction object"
            " {claim, evidence_ref, observed} — it is what the retry prompt"
            " embeds (R12)"
        )
    if output["verdict"] == "pass" and output.get("contradiction") is not None:
        return "a pass verdict must carry contradiction: null"
    return None


# --- the checker call (single-shot judge over the record/replay seam) -----------


def check_answer(
    question: str,
    answer: str,
    observation_a11y,
    evidence: list[dict],
    *,
    model: str,
    max_retries: int,
    mode: str | None = None,
    fixtures_dir: str | Path | None = None,
) -> CheckerVerdict:
    """One grader-side answer check (R12): single-shot judge, default-fail.

    ``max_retries`` covers structured-output contract violations only (the
    judge seam's schema-retry path) — checker REJECTIONS are not retried
    here; the explorer-side fresh-context re-answer loop owns those
    (``ask_question``), because a rejection needs a new answer, not a new
    judgment.
    """
    prompt = build_checker_prompt(question, answer, observation_a11y, evidence)
    result = run_judge(
        prompt,
        CHECKER_SCHEMA,
        model,
        max_retries=max_retries,
        extra_validate=validate_checker_output,
        mode=mode,
        fixtures_dir=fixtures_dir,
    )
    verdict = CheckerVerdict(
        verdict=result.output["verdict"],
        contradiction=result.output["contradiction"],
        request_hash=result.request_hash,
        attempts=result.attempts,
    )
    logger.info(
        "oracle check %s (hash=%s, attempts=%d)",
        verdict.verdict,
        verdict.request_hash,
        verdict.attempts,
    )
    return verdict
