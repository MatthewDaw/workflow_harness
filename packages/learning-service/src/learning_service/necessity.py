"""necessity.py — U6: Necessity gate over golden cases (MAT-145).

Two-phase design per the plan:

Phase 1 — Fold-time triviality/dedup filter (runs in-process at fold):
  - Non-empty body check.
  - Has anchors check (inferred ideas should have code anchors; authored are
    exempt so this check is skipped for authored_import / user_directive).
  - Near-duplicate check: if the new idea body is too similar (word-Jaccard ≥
    NEAR_DUPLICATE_THRESHOLD) to any live idea in the same skill family, it is
    a dedup candidate — skip the fold.

  Invocation: call ``triviality_dedup_filter(idea, anchors, live_ideas)``
  before writing the folded status.  Returns a ``TrivialityResult`` with
  ``passed`` bool and a ``reason`` string for logging/auditing.

Phase 2 — Scheduled necessity ablation (daily EventBridge scan):
  - For each folded, non-authored idea that has a golden case:
      a. Run the golden judge with vs. without the idea body (ablation test).
      b. If the judge says the case outcome is unaffected by the idea →
         demote (stamp ``invalidAt`` on the idea).
      c. If no golden case is found → ``no_signal`` (never demotes).
      d. If the idea is mis-retrieved (the golden case is unrelated to the
         idea) → ``scope_miss`` (tightens scope, never demotes as unnecessary).
  - Authored ideas (authorityKind in {user_directive, authored_import}) are
    ALWAYS exempt — the human asserted them; mis-retrieval still tightens scope
    but does not demote.
  - Graduated judge enforcement: starts advisory, promotes to soft-block only
    after empirical FP rate < 15% over ≥ 50 human-reviewed verdicts, and
    hard-block only for catastrophic-severity / 2-judge ensemble.

Key invariants (tested by the acceptance suite):
  1. test_unrelated_golden_case_is_no_signal       — absent/unrelated case → no_signal
  2. test_necessity_is_with_vs_without             — ablation = with vs. without
  3. test_rare_but_necessary_kept                  — score difference keeps the idea
  4. test_misretrieval_is_scope_signal_not_unnecessary — scope_miss, not unnecessary
  5. test_triviality_dedup_filter_at_fold          — trivial/dedup blocked at fold
  6. test_scheduled_scan_demotes_folded_useless    — scan demotes within one cycle
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore
    from learning_service.telemetry import TelemetryAccumulator

from learning_service.db.store import VersionConflictError
from learning_service.schema.generated.py_types import (
    AnchorRecord,
    GoldenCaseRecord,
    IdeaRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / thresholds
# ---------------------------------------------------------------------------

# Word-Jaccard threshold above which two idea bodies are treated as near-duplicates.
NEAR_DUPLICATE_THRESHOLD: float = 0.70

# Graduated enforcement stages.
ENFORCEMENT_ADVISORY = "advisory"
ENFORCEMENT_SOFT_BLOCK = "soft_block"
ENFORCEMENT_HARD_BLOCK = "hard_block"

# FP calibration thresholds for stage promotion.
SOFT_BLOCK_FP_CEILING: float = 0.15   # FP rate < 15 %
SOFT_BLOCK_MIN_SAMPLES: int = 50       # over ≥ 50 human-reviewed verdicts

# Score difference (with_idea − without_idea) below which an idea is "useless".
NECESSITY_SCORE_DIFF_THRESHOLD: float = 0.05

# Authority kinds that are exempt from necessity ablation.
_AUTHORED_AUTHORITY_KINDS: frozenset[str] = frozenset(
    {"user_directive", "authored_import"}
)

# Maximum OCC retries on a demote write.
MAX_OCC_RETRIES: int = 3


# ---------------------------------------------------------------------------
# Public result types
# ---------------------------------------------------------------------------


@dataclass
class TrivialityResult:
    """Result of the fold-time triviality/dedup filter."""
    passed: bool          # True → idea may be folded; False → skip/block
    reason: str           # Human-readable reason (for logging/auditing)
    near_duplicate_of: str | None = None  # IdeaId of the near-duplicate, if any


@dataclass
class NecessityVerdict:
    """Result of the scheduled necessity ablation for one idea."""
    idea_id: str
    outcome: str          # "necessary" | "unnecessary" | "no_signal" | "scope_miss" | "exempt"
    score_with: float | None = None     # judge score (0..1) with idea in context
    score_without: float | None = None  # judge score without idea
    score_diff: float | None = None     # score_with − score_without
    demoted: bool = False               # True if invalidAt was stamped
    reason: str = ""


@dataclass
class ScanResult:
    """Summary of one scheduled necessity scan."""
    ideas_scanned: int = 0
    ideas_demoted: int = 0
    no_signal: int = 0
    scope_miss: int = 0
    exempt: int = 0
    verdicts: list[NecessityVerdict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# FP calibration gate for graduated judge enforcement
# ---------------------------------------------------------------------------


@dataclass
class NecessityFpGate:
    """Tracks LLM-judge false-positive rate for graduated enforcement.

    Stage progression:
      advisory (default)
        → soft_block once FP rate < SOFT_BLOCK_FP_CEILING over ≥ SOFT_BLOCK_MIN_SAMPLES
        → hard_block only for catastrophic-severity or 2-judge ensemble agreement

    The gate exposes the current stage and can be queried to determine whether
    a demote should be executed or merely logged (advisory).
    """
    fp_ceiling: float = SOFT_BLOCK_FP_CEILING
    min_samples: int = SOFT_BLOCK_MIN_SAMPLES
    _spot_checks: dict[str, bool] = field(default_factory=dict)  # verdict_id → True=TP

    def record_verdict(self, verdict_id: str, is_false_positive: bool) -> None:
        """Record a human-spot-checked verdict."""
        self._spot_checks[verdict_id] = not is_false_positive  # True = TP

    @property
    def sample_count(self) -> int:
        return len(self._spot_checks)

    @property
    def fp_rate(self) -> float:
        if not self._spot_checks:
            return 1.0  # conservative: no data → assume 100% FP (don't enforce)
        fps = sum(1 for v in self._spot_checks.values() if not v)
        return fps / len(self._spot_checks)

    @property
    def stage(self) -> str:
        if self.sample_count >= self.min_samples and self.fp_rate < self.fp_ceiling:
            return ENFORCEMENT_SOFT_BLOCK
        return ENFORCEMENT_ADVISORY

    def may_demote(self) -> bool:
        """Return True if the current stage allows an actual demote write."""
        return self.stage in (ENFORCEMENT_SOFT_BLOCK, ENFORCEMENT_HARD_BLOCK)


# ---------------------------------------------------------------------------
# Phase 1: Fold-time triviality/dedup filter
# ---------------------------------------------------------------------------


def _word_jaccard(a: str, b: str) -> float:
    """Word-level Jaccard similarity between two strings.

    Punctuation is stripped from each token so "attacks." and "attacks"
    compare equal — consistent with the word-overlap intent.
    """
    import re as _re
    _tokenize = lambda text: set(
        _re.sub(r"[^\w]", "", tok.lower())
        for tok in text.split()
        if _re.sub(r"[^\w]", "", tok)  # skip tokens that are purely punctuation
    )
    words_a = _tokenize(a)
    words_b = _tokenize(b)
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / len(words_a | words_b)


def triviality_dedup_filter(
    idea: IdeaRecord,
    anchors: list[AnchorRecord],
    live_ideas: list[IdeaRecord],
    *,
    near_duplicate_threshold: float = NEAR_DUPLICATE_THRESHOLD,
) -> TrivialityResult:
    """Run the fold-time triviality/dedup filter on a candidate idea.

    Called immediately before marking an idea as ``status="folded"`` to block
    trivial or near-duplicate ideas from entering the skill library.

    Checks (in order):
    1. Non-empty body — the idea must have a non-blank body.
    2. Has anchors — an inferred idea (authority_kind == 'merged') should have
       at least one code anchor; authored ideas are exempt from this check.
    3. Not a near-duplicate — the idea body must not be too similar (word-Jaccard
       ≥ near_duplicate_threshold) to any currently-live idea in the same skill
       family.

    Parameters
    ----------
    idea:
        The candidate idea about to be folded.
    anchors:
        The AnchorRecord list associated with the idea (may be empty if the
        anchor index hasn't been written yet; the check uses this list directly).
    live_ideas:
        Currently-live (non-retired) ideas in the same skill family, excluding
        the candidate itself.
    near_duplicate_threshold:
        Jaccard threshold above which the candidate is a near-duplicate.

    Returns
    -------
    TrivialityResult
        ``passed=True`` when the idea may be folded; ``passed=False`` when it
        should be blocked (reason explains why).
    """
    # 1. Non-empty body.
    if not idea.body or not idea.body.strip():
        return TrivialityResult(passed=False, reason="empty_body")

    # 2. Has anchors (inferred ideas only; authored are exempt).
    is_authored = idea.authorityKind in _AUTHORED_AUTHORITY_KINDS or idea.authored
    if not is_authored and not anchors:
        return TrivialityResult(passed=False, reason="no_anchors")

    # 3. Near-duplicate dedup.
    for live in live_ideas:
        if live.ideaId == idea.ideaId:
            continue  # skip self
        sim = _word_jaccard(idea.body, live.body)
        if sim >= near_duplicate_threshold:
            return TrivialityResult(
                passed=False,
                reason="near_duplicate",
                near_duplicate_of=live.ideaId,
            )

    return TrivialityResult(passed=True, reason="ok")


# ---------------------------------------------------------------------------
# Golden-judge ablation helpers
# ---------------------------------------------------------------------------


def _judge_satisfaction(
    lesson: str,
    candidate_body: str,
    run_judge_fn: Callable[..., Any] | None,
    model: str,
) -> float:
    """Ask the golden judge whether candidate_body still satisfies lesson.

    Returns a float score in [0.0, 1.0]:
      1.0 = clearly satisfied
      0.0 = clearly not satisfied / judge unavailable

    Uses the same golden_judge.judge_golden implementation so there is ONE
    judge, not two.
    """
    from learning_service.entrypoints.golden_judge import judge_golden

    verdict = judge_golden(lesson, candidate_body, run_judge_fn=run_judge_fn, model=model)
    # satisfied is bool; convert to 1.0/0.0 for score arithmetic.
    return 1.0 if verdict.get("satisfied") else 0.0


def _is_misretrieval(
    golden_case: GoldenCaseRecord,
    idea: IdeaRecord,
) -> bool:
    """Heuristic: is the golden case unrelated to this idea?

    A mis-retrieval is detected when the word overlap between the golden
    case's ideaBody and the current idea body is very low — meaning the
    case was captured for a different idea and got attached to this one.

    Threshold: Jaccard < 0.20 → scope miss.  The threshold is deliberately
    generous (0.20 rather than 0.10) so that genuinely unrelated topics
    (e.g. Python naming vs. Java DI) are caught, while ideas that share a
    few common words ("use", "for") are not mis-classified.
    """
    jaccard = _word_jaccard(golden_case.ideaBody, idea.body)
    return jaccard < 0.20


# ---------------------------------------------------------------------------
# Phase 2: Scheduled necessity ablation (single-idea)
# ---------------------------------------------------------------------------


def assess_necessity(
    idea: IdeaRecord,
    store: "LearningStore",
    *,
    run_judge_fn: Callable[..., Any] | None = None,
    model: str = "claude-haiku-4-5",
    fp_gate: NecessityFpGate | None = None,
    score_diff_threshold: float = NECESSITY_SCORE_DIFF_THRESHOLD,
    now_ms: int | None = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> NecessityVerdict:
    """Assess the necessity of one folded idea against its golden case.

    Algorithm
    ---------
    1. If the idea is authored → exempt (return immediately).
    2. Fetch the golden case (keyed by caseId == idea.ideaId).
    3. If no golden case → no_signal (never demotes).
    4. If the golden case is a mis-retrieval (low ideaBody↔idea overlap) →
       scope_miss (scope signal, not unnecessary).
    5. Run the golden judge:
       a. With the idea body prepended to the candidate (context included).
       b. Without the idea body (context excluded).
    6. score_diff = score_with − score_without.
       If score_diff > score_diff_threshold → necessary (keep).
       Else → unnecessary.
    7. Demote (stamp invalidAt) only if the FP gate allows it (advisory → log only).

    Parameters
    ----------
    idea:           The folded idea to assess.
    store:          LearningStore (for golden case and idea reads/writes).
    run_judge_fn:   Injectable judge callable (offline tests use a mock).
    model:          LLM model for the judge.
    fp_gate:        Graduated enforcement gate; None → always advisory.
    score_diff_threshold:
                    Minimum score difference to count as "necessary."
    now_ms:         Clock injection (tests).

    Returns
    -------
    NecessityVerdict
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # 1. Authored exempt.
    if idea.authorityKind in _AUTHORED_AUTHORITY_KINDS or idea.authored:
        return NecessityVerdict(
            idea_id=idea.ideaId,
            outcome="exempt",
            reason="authored_directive_exempt",
        )

    # 2. Fetch golden case.
    golden = store.get_golden_case(idea.org, idea.skillBaseName, idea.ideaId)
    if golden is None:
        return NecessityVerdict(
            idea_id=idea.ideaId,
            outcome="no_signal",
            reason="no_golden_case_found",
        )

    # 3. Mis-retrieval check.
    if _is_misretrieval(golden, idea):
        return NecessityVerdict(
            idea_id=idea.ideaId,
            outcome="scope_miss",
            reason="golden_case_unrelated_to_idea",
        )

    # 4. With-vs-without ablation.
    # The "with" context: prepend the idea body as a retrieved insight.
    candidate_with = f"[Relevant insight: {idea.body}]\n\n{golden.after}"
    candidate_without = golden.after

    lesson = golden.ideaBody  # the lesson the golden case was captured for

    score_with = _judge_satisfaction(lesson, candidate_with, run_judge_fn, model)
    score_without = _judge_satisfaction(lesson, candidate_without, run_judge_fn, model)
    score_diff = score_with - score_without

    if score_diff > score_diff_threshold:
        # The idea meaningfully improves the outcome → necessary.
        return NecessityVerdict(
            idea_id=idea.ideaId,
            outcome="necessary",
            score_with=score_with,
            score_without=score_without,
            score_diff=score_diff,
            demoted=False,
            reason="score_diff_above_threshold",
        )

    # 5. Unnecessary — demote if gate allows.
    outcome = "unnecessary"
    demoted = False

    gate_allows = fp_gate.may_demote() if fp_gate is not None else False

    if gate_allows:
        # Stamp invalidAt (OCC retry loop).
        demoted = _demote_idea(idea, store, now_ms)
        reason = "demoted_by_necessity_scan" if demoted else "demote_failed_occ"
    else:
        reason = "advisory_only_gate_not_open"
        logger.info(
            "necessity ADVISORY: idea=%r would be demoted (score_diff=%.3f) "
            "but fp_gate is in advisory mode",
            idea.ideaId,
            score_diff,
        )

    # --- Telemetry: record necessity demotion ------------------------------------
    if telemetry is not None and demoted:
        telemetry.record_necessity_demotion()

    return NecessityVerdict(
        idea_id=idea.ideaId,
        outcome=outcome,
        score_with=score_with,
        score_without=score_without,
        score_diff=score_diff,
        demoted=demoted,
        reason=reason,
    )


def _demote_idea(idea: IdeaRecord, store: "LearningStore", now_ms: int) -> bool:
    """Stamp invalidAt on an idea (OCC, up to MAX_OCC_RETRIES).

    Returns True if the demote was written successfully.
    """
    for attempt in range(MAX_OCC_RETRIES):
        # Re-read on retry to get fresh version.
        current = store.get_idea(idea.org, idea.skillBaseName, idea.ideaId)
        if current is None:
            logger.warning("necessity demote: idea=%r no longer exists", idea.ideaId)
            return False
        if current.invalidAt is not None:
            # Already retired by another path (e.g. supersession) — treat as success.
            return True

        demoted_idea = IdeaRecord(
            ideaId=current.ideaId,
            skillBaseName=current.skillBaseName,
            org=current.org,
            body=current.body,
            status=current.status,
            corroborationVersion=current.corroborationVersion + 1,
            foldedIntoRev=current.foldedIntoRev,
            invalidAt=now_ms,                      # ← stamp temporal retirement
            supersededBy=current.supersededBy,
            supersedes=current.supersedes,
            authored=current.authored,
            authorityKind=current.authorityKind,
            refines=current.refines,
            revivedAt=current.revivedAt,
            legacyRecurrenceFold=current.legacyRecurrenceFold,
            sourceRef=current.sourceRef,
            scopeTag=current.scopeTag,
            authorId=current.authorId,
            verificationRung=current.verificationRung,
        )

        try:
            store.put_idea_conditional(demoted_idea, current.corroborationVersion)
            logger.info(
                "necessity scan: demoted idea=%r (necessity_scan invalidAt=%d)",
                idea.ideaId,
                now_ms,
            )
            return True
        except VersionConflictError:
            if attempt < MAX_OCC_RETRIES - 1:
                logger.info(
                    "necessity demote OCC conflict (attempt %d) — retrying", attempt + 1
                )
                continue
            logger.warning(
                "necessity demote exhausted retries for idea=%r", idea.ideaId
            )
            return False

    return False


# ---------------------------------------------------------------------------
# Phase 2: Scheduled scan runner (EventBridge rate(1 day) trigger)
# ---------------------------------------------------------------------------


def run_necessity_scan(
    org: str,
    skill_base_name: str,
    store: "LearningStore",
    *,
    run_judge_fn: Callable[..., Any] | None = None,
    model: str = "claude-haiku-4-5",
    fp_gate: NecessityFpGate | None = None,
    necessity_sample_rate: float = 1.0,
    necessity_min_firings: int = 0,
    score_diff_threshold: float = NECESSITY_SCORE_DIFF_THRESHOLD,
    now_ms: int | None = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> ScanResult:
    """Run the daily necessity scan over folded ideas for one skill family.

    Designed to be called by the EventBridge ``rate(1 day)`` trigger targeting
    the Python necessity handler.  The caller may pass ``necessity_sample_rate``
    (0..1) to sub-sample the folded population (cost control) and
    ``necessity_min_firings`` to skip ideas that haven't had enough golden-case
    firings yet.

    Parameters
    ----------
    org:                Owning org slug.
    skill_base_name:    Skill family to scan.
    store:              LearningStore.
    run_judge_fn:       Injectable judge callable (offline tests).
    model:              LLM model.
    fp_gate:            Graduated enforcement gate.
    necessity_sample_rate:
                        Fraction of folded ideas to assess (1.0 = all).
    necessity_min_firings:
                        Minimum number of golden-case firings required before
                        assessing (0 = assess all).  Placeholder for v1 — the
                        firing count is not yet tracked; pass 0 to skip.
    score_diff_threshold:
                        Passed through to assess_necessity.
    now_ms:             Clock injection.

    Returns
    -------
    ScanResult
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    result = ScanResult()

    # Fetch all current (non-retired) ideas for this skill family.
    all_ideas = store.list_current_ideas(org, skill_base_name)
    folded_ideas = [i for i in all_ideas if i.status == "folded"]

    import random
    for idea in folded_ideas:
        # Sub-sample for cost control.
        if necessity_sample_rate < 1.0 and random.random() >= necessity_sample_rate:
            continue

        result.ideas_scanned += 1

        verdict = assess_necessity(
            idea,
            store,
            run_judge_fn=run_judge_fn,
            model=model,
            fp_gate=fp_gate,
            score_diff_threshold=score_diff_threshold,
            now_ms=now_ms,
            telemetry=telemetry,
        )

        result.verdicts.append(verdict)

        if verdict.outcome == "unnecessary" and verdict.demoted:
            result.ideas_demoted += 1
        elif verdict.outcome == "no_signal":
            result.no_signal += 1
        elif verdict.outcome == "scope_miss":
            result.scope_miss += 1
        elif verdict.outcome == "exempt":
            result.exempt += 1

        logger.info(
            "necessity scan: skill=%r idea=%r outcome=%s demoted=%s score_diff=%s",
            skill_base_name,
            idea.ideaId,
            verdict.outcome,
            verdict.demoted,
            f"{verdict.score_diff:.3f}" if verdict.score_diff is not None else "n/a",
        )

    return result


# ---------------------------------------------------------------------------
# EventBridge Lambda handler (scheduled scan entrypoint)
# ---------------------------------------------------------------------------


def scheduled_scan_handler(event: dict, context: object) -> dict:
    """Lambda handler for the daily necessity scan (EventBridge trigger).

    Expected event shapes:

    Targeted scan (explicit org + skill)::

        {
            "org": "<org>",
            "skill_base_name": "<name>",
            "necessity_sample_rate": 1.0,  # optional
            "necessity_min_firings": 0,    # optional
        }

    Global scan (EventBridge scheduled invocation — no org/skill supplied)::

        {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {
                "necessity_sample_rate": 1.0,
                "necessity_min_firings": 0,
            }
        }

    When ``org`` or ``skill_base_name`` are absent (or empty), the handler
    performs a **global scan**: it enumerates every (org, skill_base_name) pair
    that has at least one current folded non-authored idea, then runs
    ``run_necessity_scan`` for each.  This is the correct behaviour for the
    EventBridge ``rate(1 day)`` rule, which carries no org/skill payload.

    The store and judge are wired from environment variables at cold start.
    This handler is a thin wrapper — the real logic is in run_necessity_scan().

    For offline tests, inject ``_test_store`` and ``_test_judge_fn`` into the
    event dict (they are popped before processing).
    """
    import json

    test_store = event.pop("_test_store", None)
    test_judge_fn = event.pop("_test_judge_fn", None)
    test_telemetry = event.pop("_test_telemetry", None)

    # Support both top-level keys (targeted) and EventBridge detail envelope.
    detail = event.get("detail", {}) or {}
    org = event.get("org", "")
    skill_base_name = event.get("skill_base_name", "")
    necessity_sample_rate = float(
        event.get("necessity_sample_rate", detail.get("necessity_sample_rate", 1.0))
    )
    necessity_min_firings = int(
        event.get("necessity_min_firings", detail.get("necessity_min_firings", 0))
    )

    if test_store is None:
        # Production: wire boto3 store from env.
        import os
        from learning_service.db.store import DynamoLearningStore
        table = os.environ.get("HARNESS_TABLE", "harness")
        store = DynamoLearningStore(table_name=table)
    else:
        store = test_store

    fp_gate = NecessityFpGate()

    # R4: one accumulator per invocation — records demotions for telemetry emission.
    from learning_service.telemetry import TelemetryAccumulator, to_json_dict
    telemetry: TelemetryAccumulator = test_telemetry or TelemetryAccumulator()

    if org and skill_base_name:
        # Targeted scan: single (org, skill) pair.
        scan_result = run_necessity_scan(
            org,
            skill_base_name,
            store,
            run_judge_fn=test_judge_fn,
            necessity_sample_rate=necessity_sample_rate,
            necessity_min_firings=necessity_min_firings,
            fp_gate=fp_gate,
            telemetry=telemetry,
        )
        aggregated = scan_result
    else:
        # Global scan: enumerate all (org, skill_base_name) pairs with folded
        # non-authored ideas, then run a scan for each.
        targets = store.list_skills_with_folded_non_authored_ideas()
        logger.info(
            "necessity global scan: found %d (org, skill) targets to scan",
            len(targets),
        )
        aggregated = ScanResult()
        for target_org, target_skill in targets:
            partial = run_necessity_scan(
                target_org,
                target_skill,
                store,
                run_judge_fn=test_judge_fn,
                necessity_sample_rate=necessity_sample_rate,
                necessity_min_firings=necessity_min_firings,
                fp_gate=fp_gate,
                telemetry=telemetry,
            )
            aggregated.ideas_scanned += partial.ideas_scanned
            aggregated.ideas_demoted += partial.ideas_demoted
            aggregated.no_signal += partial.no_signal
            aggregated.scope_miss += partial.scope_miss
            aggregated.exempt += partial.exempt
            aggregated.verdicts.extend(partial.verdicts)

    # R4: emit accumulated telemetry as a structured log line.
    snap = telemetry.snapshot()
    telem_payload = to_json_dict(snap)
    telem_payload["org"] = org or "global"
    telem_payload["skill_base_name"] = skill_base_name or "global"
    telem_payload["ideas_demoted"] = aggregated.ideas_demoted
    logger.info("TELEMETRY %s", json.dumps(telem_payload))

    summary = {
        "ideas_scanned": aggregated.ideas_scanned,
        "ideas_demoted": aggregated.ideas_demoted,
        "no_signal": aggregated.no_signal,
        "scope_miss": aggregated.scope_miss,
        "exempt": aggregated.exempt,
    }
    return {"statusCode": 200, "body": json.dumps(summary)}
