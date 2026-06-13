"""pipeline.py — U9: Full spine orchestration (MAT-147).

Wires the post-distillation pipeline into one callable:

    distilled insight
        -> U2: extract anchors from diff hunks
        -> U2: locality join → incumbent idea IDs
        -> U3: NLI-classify each incumbent (supersede / corroborate / refine / neutral)
        -> U5: supersede incumbents classified as SUPERSEDE
        -> U4: corroborate / create the idea (semantic join → NLI → corroborate or create)
        -> fold at verified_K: write skill revision + register anchors + capture IDEAGOLD

This module is the **orchestration layer** — it calls the real modules (anchors,
classifier, corroboration, supersession, skills_write) and returns a
``PipelineResult`` that records every decision made during the run.

Design notes
------------
* All write modes are injectable: ``mode``, ``unfold_mode``, and
  ``supersede_mode`` determine whether decisions are executed or shadow-logged.
* Every module call is guarded by the org-guard (each module enforces its own;
  we re-check at pipeline entry for defence-in-depth).
* The semantic join (vector search) is replaced by a simple in-memory scan over
  current ideas in the same skill family when no vector store is provided — this
  makes the pipeline fully offline-testable without S3 Vectors.
* NLI fixtures (replay mode) make every classification call deterministic and
  offline — no model load required.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore
    from learning_service.skills_write import SkillStore
    from learning_service.telemetry import TelemetryAccumulator

from learning_service.anchors import (
    Anchor,
    DiffHunk,
    extract_anchors_from_diff,
    get_incumbent_ideas,
    parse_unified_diff,
    write_anchors_on_fold,
)
from learning_service.classifier import NliClassifier, Verdict
from learning_service.corroboration import (
    AuthorCredibilityStore,
    CorroborationRequest,
    create_idea_from_pr,
    corroborate,
    DEFAULT_VERIFIED_K,
)
from learning_service.db.store import OrgGuardError
from learning_service.merge_handler import MergeHandlerResult
from learning_service.necessity import triviality_dedup_filter
from learning_service.schema.generated.py_types import AnchorRecord, GoldenCaseRecord, IdeaRecord
from learning_service.skills_write import (
    GoldenCasePayload,
    RevisionRequest,
    write_revision,
)
from learning_service.supersession import (
    AntiThrashTracker,
    FpCalibrationGate,
    ReviveRequest,
    SupersedeRequest,
    revive,
    supersede,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SupersedeDecision:
    """Record of one supersession decision made during pipeline execution."""
    incumbent_idea_id: str
    verdict: str   # "superseded" | "blocked_authority" | "shadow" | etc.
    unfold_rev: int | None = None


@dataclass
class PipelineResult:
    """The full outcome of processing one PR through the verified learning pipeline."""

    pr_number: int
    owner_repo: str
    org: str

    # Anchor extraction
    anchors_extracted: int = 0
    anchor_resolutions: list[str] = field(default_factory=list)  # "symbol" | "file_fallback"

    # Locality join + supersession
    incumbents_found: list[str] = field(default_factory=list)
    supersede_decisions: list[SupersedeDecision] = field(default_factory=list)

    # Semantic join + corroboration/creation
    idea_action: str = ""       # "created" | "candidate" | "folded" | "already_counted" | "revived"
    idea_id: str = ""
    corroboration_weight: float = 0.0
    folded: bool = False
    new_rev: int | None = None  # revision number minted on fold

    # Golden case
    golden_case_id: str | None = None

    # Necessity gate (fold-time triviality filter)
    triviality_passed: bool = True
    triviality_reason: str = ""


# ---------------------------------------------------------------------------
# Core: run the pipeline for one distilled PR result
# ---------------------------------------------------------------------------


def run_pipeline(
    result: MergeHandlerResult,
    org: str,
    store: "LearningStore",
    classifier: NliClassifier,
    credibility_store: AuthorCredibilityStore,
    skill_store: "SkillStore | None" = None,
    *,
    skill_base_name: str = "default",
    verified_k: float = DEFAULT_VERIFIED_K,
    mode: str = "shadow",           # overall: shadow | enforce
    unfold_mode: str = "shadow",    # U5 un-fold: shadow | enforce
    supersede_mode: str = "shadow", # U5 supersede write: shadow | enforce
    fp_gate: "FpCalibrationGate | None" = None,
    thrash_tracker: "AntiThrashTracker | None" = None,
    now_ms: "int | None" = None,
    telemetry: "TelemetryAccumulator | None" = None,
    file_contents: "dict[str, bytes] | None" = None,
) -> PipelineResult:
    """Run the full verified-learning pipeline for one distilled PR result.

    Steps
    -----
    1. Extract anchors from the diff (U2).
    2. Locality join → incumbent idea IDs (U2).
    3. NLI-classify each incumbent (U3):
       - SUPERSEDE → retire incumbent via U5.
       - CORROBORATE → the incumbent is the semantic-join candidate.
       - REVIVE → if the incumbent is retired (invalidAt set), revive it.
    4. Semantic join: if no supersede/corroborate incumbent found, scan current
       ideas in the same skill family for the closest match (simplified cosine
       approximation via word-Jaccard for offline determinism).
    5. Corroborate the found idea or create a new one (U4).
    6. If folded: fold-time triviality/dedup check (U6 Phase 1).
    7. If folded and triviality passes: write skill revision + register anchors
       + capture IDEAGOLD golden case (U2/skills_write).

    Parameters
    ----------
    result:
        The ``MergeHandlerResult`` with ``action="distilled"`` from
        ``handle_merged_pr``.  Results with other actions are no-ops.
    org:
        Org slug (DynamoDB partition key).
    store:
        LearningStore for idea/anchor/source reads and writes.
    classifier:
        ``NliClassifier`` (replay mode for offline tests; passthrough for prod).
    credibility_store:
        Author credibility weights (user-curated).
    skill_store:
        SkillStore for fold/un-fold revision writes.  If None, fold writes are
        skipped (shadow-equivalent for skills only).
    skill_base_name:
        The skill family this PR's insight belongs to.  In v1 this is derived
        from the PR labels / file paths; the caller supplies it.
    verified_k:
        Fold threshold (accumulated rung × credibility weight).
    mode:
        Overall mode: ``"shadow"`` (log only) or ``"enforce"`` (write ideas).
    unfold_mode / supersede_mode:
        Per-subsystem enforce flags passed to ``supersession.supersede``.
    fp_gate:
        FP calibration gate for supersession (see U5).
    thrash_tracker:
        Anti-thrash tracker for supersede/revive oscillation (see U5).
    now_ms:
        Clock injection (epoch-ms) for testing.
    telemetry:
        Telemetry accumulator for metrics.
    file_contents:
        ``{repo-relative path: bytes}`` for tree-sitter anchor extraction.
        If None, all anchors fall back to file-level.

    Returns
    -------
    PipelineResult
        A record of every decision made during this pipeline run.
    """
    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pipeline_result = PipelineResult(
        pr_number=result.pr_number,
        owner_repo=result.owner_repo,
        org=org,
    )

    # Only process distilled PRs.
    if result.action != "distilled" or result.distillation is None:
        logger.debug(
            "pipeline skip pr=%d action=%s (not distilled)",
            result.pr_number,
            result.action,
        )
        pipeline_result.idea_action = f"skipped_{result.action}"
        return pipeline_result

    distillation = result.distillation
    diff_text = result.anchors_diff or distillation.raw_anchors_from_diff or ""

    # ------------------------------------------------------------------
    # Step 1: Extract anchors from the diff (U2)
    # ------------------------------------------------------------------
    hunks: list[DiffHunk] = []
    if diff_text:
        hunks = parse_unified_diff(diff_text, file_contents or {})
    anchors = extract_anchors_from_diff(hunks)

    pipeline_result.anchors_extracted = len(anchors)
    pipeline_result.anchor_resolutions = [a.resolution for a in anchors]
    logger.info(
        "pipeline anchor_extract pr=%d anchors=%d",
        result.pr_number,
        len(anchors),
    )

    # ------------------------------------------------------------------
    # Step 2: Locality join — find incumbent ideas (U2)
    # ------------------------------------------------------------------
    incumbents: list[str] = []
    if anchors:
        incumbents = get_incumbent_ideas(store, org, result.owner_repo, anchors)
    pipeline_result.incumbents_found = incumbents

    logger.info(
        "pipeline locality_join pr=%d incumbents=%d",
        result.pr_number,
        len(incumbents),
    )

    # ------------------------------------------------------------------
    # Step 3: Classify each incumbent and execute supersession (U3/U5)
    # ------------------------------------------------------------------
    corroborate_candidate_id: str | None = None

    for incumbent_id in incumbents:
        # Load the incumbent idea to get its body.
        incumbent_idea = store.get_idea(org, skill_base_name, incumbent_id)
        if incumbent_idea is None:
            logger.warning(
                "pipeline: incumbent %r not found in skill=%r — skipping",
                incumbent_id,
                skill_base_name,
            )
            continue

        # If the incumbent is already retired and we are the PR that teaches
        # the same pattern again → this is a revive path.
        if incumbent_idea.invalidAt is not None:
            # Check if this new PR is teaching the same lesson (entailment).
            try:
                clf_result = classifier.classify(
                    incumbent_idea.body,
                    distillation.body,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "pipeline: NLI classify failed for incumbent=%r: %s — skip",
                    incumbent_id,
                    exc,
                )
                continue

            if clf_result.verdict == Verdict.CORROBORATE:
                # New PR re-teaches a retired pattern → revive it.
                revive_req = ReviveRequest(
                    org=org,
                    idea_id=incumbent_id,
                    skill_base_name=skill_base_name,
                    new_pr_number=result.pr_number,
                    new_pr_owner_repo=result.owner_repo,
                )
                if mode == "enforce":
                    rv = revive(revive_req, store, thrash_tracker=thrash_tracker, now_ms=now_ms, telemetry=telemetry)
                    pipeline_result.idea_action = rv.action
                    pipeline_result.idea_id = incumbent_id
                    logger.info(
                        "pipeline revive pr=%d idea=%r action=%s",
                        result.pr_number,
                        incumbent_id,
                        rv.action,
                    )
                else:
                    pipeline_result.idea_action = "shadow_revive"
                    pipeline_result.idea_id = incumbent_id
                    logger.info(
                        "pipeline shadow_revive pr=%d idea=%r",
                        result.pr_number,
                        incumbent_id,
                    )
                corroborate_candidate_id = incumbent_id
            continue  # retired ideas don't get superseded again

        # Classify: new insight vs. standing incumbent.
        try:
            clf_result = classifier.classify(
                incumbent_idea.body,
                distillation.body,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "pipeline: NLI classify failed for incumbent=%r: %s — skip",
                incumbent_id,
                exc,
            )
            continue

        logger.info(
            "pipeline classify pr=%d incumbent=%r verdict=%s confidence=%.3f",
            result.pr_number,
            incumbent_id,
            clf_result.verdict.value,
            clf_result.nli_confidence,
        )

        if clf_result.verdict == Verdict.SUPERSEDE:
            # Execute supersession (U5).
            sources = store.list_idea_sources(org, incumbent_id)
            sup_req = SupersedeRequest(
                org=org,
                incumbent_idea_id=incumbent_id,
                incumbent_skill_base_name=skill_base_name,
                challenger_idea_id=None,   # new PR, not yet persisted
                challenger_skill_base_name=skill_base_name,
                challenger_pr_number=result.pr_number,
                challenger_owner_repo=result.owner_repo,
                challenger_authority_kind="merged",
                idea_body_to_remove=incumbent_idea.body,
            )
            sup_result = supersede(
                sup_req,
                store,
                unfold_mode=unfold_mode,
                fp_gate=fp_gate,
                thrash_tracker=thrash_tracker,
                skill_store=skill_store,
                now_ms=now_ms,
                telemetry=telemetry,
            )
            pipeline_result.supersede_decisions.append(
                SupersedeDecision(
                    incumbent_idea_id=incumbent_id,
                    verdict=sup_result.action,
                    unfold_rev=sup_result.unfold_rev,
                )
            )
            logger.info(
                "pipeline supersede pr=%d incumbent=%r action=%s",
                result.pr_number,
                incumbent_id,
                sup_result.action,
            )

        elif clf_result.verdict == Verdict.CORROBORATE:
            # The incumbent is a corroboration candidate.
            # Take the first CORROBORATE incumbent as the semantic-join candidate.
            if corroborate_candidate_id is None:
                corroborate_candidate_id = incumbent_id
                logger.info(
                    "pipeline corroborate_candidate pr=%d idea=%r",
                    result.pr_number,
                    incumbent_id,
                )

        elif clf_result.verdict == Verdict.REFINE:
            logger.info(
                "pipeline refine pr=%d incumbent=%r — keeping both",
                result.pr_number,
                incumbent_id,
            )
        else:
            # NEUTRAL — no action
            logger.debug(
                "pipeline neutral pr=%d incumbent=%r — no-op",
                result.pr_number,
                incumbent_id,
            )

    # ------------------------------------------------------------------
    # Step 4: Semantic join fallback — scan current ideas
    # ------------------------------------------------------------------
    # If no corroborate candidate was found via locality, scan the current
    # ideas in this skill family for semantic similarity (word-Jaccard proxy
    # for cosine — fully offline, no vector store required).
    if corroborate_candidate_id is None:
        corroborate_candidate_id = _semantic_join_fallback(
            store=store,
            org=org,
            skill_base_name=skill_base_name,
            challenger_body=distillation.body,
            classifier=classifier,
            pr_number=result.pr_number,
            jaccard_floor=0.15,  # permissive offline proxy — NLI confirms
        )

    # ------------------------------------------------------------------
    # Step 5: Corroborate the found idea or create a new one (U4)
    # ------------------------------------------------------------------
    idea_id: str
    if corroborate_candidate_id is not None:
        idea_id = corroborate_candidate_id
        req = CorroborationRequest(
            org=org,
            idea_id=idea_id,
            skill_base_name=skill_base_name,
            pr_number=result.pr_number,
            owner_repo=result.owner_repo,
            rung=result.rung,
            author_id=distillation.author_login,
            challenger_body=distillation.body,
            authority_kind="merged",
        )
        if mode == "enforce":
            corr_result = corroborate(
                req,
                store,
                credibility_store,
                verified_k=verified_k,
                now_ms=now_ms,
                telemetry=telemetry,
            )
            pipeline_result.idea_action = corr_result.action
            pipeline_result.idea_id = corr_result.idea_id
            pipeline_result.corroboration_weight = corr_result.corroboration_weight
            pipeline_result.folded = corr_result.folded
            logger.info(
                "pipeline corroborate pr=%d idea=%r action=%s weight=%.3f folded=%s",
                result.pr_number,
                idea_id,
                corr_result.action,
                corr_result.corroboration_weight,
                corr_result.folded,
            )
        else:
            pipeline_result.idea_action = "shadow_corroborate"
            pipeline_result.idea_id = idea_id
            logger.info(
                "pipeline shadow_corroborate pr=%d idea=%r",
                result.pr_number,
                idea_id,
            )
    else:
        # No incumbent — create a new idea.
        idea_id = _generate_idea_id(result.pr_number, result.owner_repo)
        if mode == "enforce":
            create_result = create_idea_from_pr(
                org=org,
                idea_id=idea_id,
                skill_base_name=skill_base_name,
                pr_number=result.pr_number,
                owner_repo=result.owner_repo,
                rung=result.rung,
                author_id=distillation.author_login,
                body=distillation.body,
                store=store,
                credibility_store=credibility_store,
                authority_kind="merged",
                verified_k=verified_k,
                now_ms=now_ms,
                telemetry=telemetry,
            )
            pipeline_result.idea_action = create_result.action
            pipeline_result.idea_id = create_result.idea_id
            pipeline_result.corroboration_weight = create_result.corroboration_weight
            pipeline_result.folded = create_result.folded
            logger.info(
                "pipeline create pr=%d idea=%r action=%s weight=%.3f folded=%s",
                result.pr_number,
                idea_id,
                create_result.action,
                create_result.corroboration_weight,
                create_result.folded,
            )
        else:
            pipeline_result.idea_action = "shadow_create"
            pipeline_result.idea_id = idea_id
            logger.info(
                "pipeline shadow_create pr=%d idea=%r",
                result.pr_number,
                idea_id,
            )

    # ------------------------------------------------------------------
    # Step 6 & 7: If folded — necessity gate (Phase 1) + revision write
    # ------------------------------------------------------------------
    if pipeline_result.folded and mode == "enforce":
        idea_record = store.get_idea(org, skill_base_name, pipeline_result.idea_id)
        if idea_record is None:
            logger.error(
                "pipeline: idea %r not found after fold — cannot write revision",
                pipeline_result.idea_id,
            )
            return pipeline_result

        # Phase 1 triviality/dedup filter (U6).
        # Convert Anchor objects to AnchorRecord objects for the filter.
        anchor_records = [
            AnchorRecord(
                ideaId=idea_record.ideaId,
                ownerRepo=result.owner_repo,
                file=a.file,
                symbol=a.symbol,
                org=org,
                active=True,
            )
            for a in anchors
        ]
        live_ideas = store.list_current_ideas(org, skill_base_name)
        triviality = triviality_dedup_filter(
            idea=idea_record,
            anchors=anchor_records,
            live_ideas=[i for i in live_ideas if i.ideaId != idea_record.ideaId],
        )
        pipeline_result.triviality_passed = triviality.passed
        pipeline_result.triviality_reason = triviality.reason

        if not triviality.passed:
            logger.info(
                "pipeline triviality_blocked pr=%d idea=%r reason=%r",
                result.pr_number,
                pipeline_result.idea_id,
                triviality.reason,
            )
            return pipeline_result

        # Write skill revision + capture IDEAGOLD golden case (fold path).
        if skill_store is not None:
            _execute_fold(
                idea=idea_record,
                org=org,
                skill_base_name=skill_base_name,
                store=store,
                skill_store=skill_store,
                anchors=anchors,
                owner_repo=result.owner_repo,
                pipeline_result=pipeline_result,
                now_ms=now_ms,
                telemetry=telemetry,
            )
        else:
            logger.info(
                "pipeline fold: skill_store not provided — anchors/revision write skipped "
                "(skill_store=None is acceptable for idea-only tests)",
            )

    return pipeline_result


# ---------------------------------------------------------------------------
# Fold execution helper
# ---------------------------------------------------------------------------


def _execute_fold(
    idea: IdeaRecord,
    org: str,
    skill_base_name: str,
    store: "LearningStore",
    skill_store: "SkillStore",
    anchors: list,
    owner_repo: str,
    pipeline_result: PipelineResult,
    now_ms: int,
    telemetry: "TelemetryAccumulator | None",
) -> None:
    """Write skill revision + register anchors + capture IDEAGOLD golden case."""

    # Build the golden case payload (before→after snapshot).
    # "before" = current skill body (may be empty if no prior revision),
    # "after"  = the idea body being folded in.
    current_rev_body: str | None = None
    pointer = skill_store.get_true_pointer(org, skill_base_name)
    if pointer is not None:
        current_rev_body = skill_store.get_revision_body(org, "", pointer.rev)

    before_body = current_rev_body or ""
    after_body = (before_body + "\n\n" + idea.body).strip() if before_body else idea.body

    golden_case_id = idea.ideaId  # keyed by idea (one golden case per idea)

    golden_case = GoldenCasePayload(
        case_id=golden_case_id,
        before=before_body,
        after=after_body,
        idea_body=idea.body,
    )

    rev_request = RevisionRequest(
        org=org,
        base_name=skill_base_name,
        variant_id="",   # base variant
        body=after_body,
        description=f"fold: idea {idea.ideaId!r} verified by PR #{pipeline_result.pr_number}",
        golden_case=golden_case,
        idea_id=idea.ideaId,
    )

    try:
        wr = write_revision(rev_request, skill_store, now_ms=lambda: now_ms)
        pipeline_result.new_rev = wr.rev
        pipeline_result.golden_case_id = golden_case_id
        logger.info(
            "pipeline fold_revision pr=%d idea=%r rev=%d golden_case=%r",
            pipeline_result.pr_number,
            idea.ideaId,
            wr.rev,
            golden_case_id,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "pipeline fold: write_revision failed for idea=%r: %s",
            idea.ideaId,
            exc,
        )
        return

    # Register anchors in the anchor index (U2).
    if anchors:
        write_anchors_on_fold(
            store=store,
            org=org,
            owner_repo=owner_repo,
            idea_id=idea.ideaId,
            anchors=anchors,
            telemetry=telemetry,
        )
        logger.info(
            "pipeline anchors_registered pr=%d idea=%r count=%d",
            pipeline_result.pr_number,
            idea.ideaId,
            len(anchors),
        )

    # Also store the golden case in the learning store for the necessity gate.
    golden_record = GoldenCaseRecord(
        org=org,
        skillBaseName=skill_base_name,
        caseId=golden_case_id,
        before=before_body,
        after=after_body,
        ideaBody=idea.body,
    )
    store.put_golden_case(golden_record)


# ---------------------------------------------------------------------------
# Semantic join fallback (offline word-Jaccard proxy)
# ---------------------------------------------------------------------------


def _semantic_join_fallback(
    store: "LearningStore",
    org: str,
    skill_base_name: str,
    challenger_body: str,
    classifier: NliClassifier,
    pr_number: int,
    jaccard_floor: float = 0.30,
) -> str | None:
    """Find the most semantically similar current idea via word-Jaccard.

    Returns the idea_id of the best corroborate candidate, or None if no
    match exceeds the floor.

    This replaces the cosine / S3 Vectors semantic join for offline tests.
    It uses word-Jaccard as a cheap proxy, then NLI-classifies the top
    candidate.  A Jaccard floor of 0.30 is deliberately conservative — only
    clearly overlapping lessons pass.
    """
    current_ideas = store.list_current_ideas(org, skill_base_name)
    if not current_ideas:
        return None

    challenger_words = set(challenger_body.lower().split())
    if not challenger_words:
        return None

    best_idea_id: str | None = None
    best_score: float = 0.0

    for idea in current_ideas:
        idea_words = set(idea.body.lower().split())
        if not idea_words:
            continue
        inter = len(challenger_words & idea_words)
        union = len(challenger_words | idea_words)
        score = inter / union if union else 0.0
        if score > best_score:
            best_score = score
            best_idea_id = idea.ideaId

    if best_idea_id is None or best_score < jaccard_floor:
        logger.info(
            "pipeline semantic_join pr=%d no_candidate (best_score=%.3f floor=%.2f)",
            pr_number,
            best_score,
            jaccard_floor,
        )
        return None

    # NLI-classify the top candidate.
    best_idea = store.get_idea(org, skill_base_name, best_idea_id)
    if best_idea is None:
        return None

    try:
        clf = classifier.classify(best_idea.body, challenger_body)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "pipeline semantic_join: NLI failed for idea=%r: %s", best_idea_id, exc
        )
        return None

    if clf.verdict == Verdict.CORROBORATE:
        logger.info(
            "pipeline semantic_join pr=%d candidate=%r score=%.3f verdict=corroborate",
            pr_number,
            best_idea_id,
            best_score,
        )
        return best_idea_id

    logger.info(
        "pipeline semantic_join pr=%d candidate=%r score=%.3f verdict=%s (not corroborate)",
        pr_number,
        best_idea_id,
        best_score,
        clf.verdict.value,
    )
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_idea_id(pr_number: int, owner_repo: str) -> str:
    """Generate a stable, deterministic idea ID from the PR number + repo."""
    # Use a UUID5 (SHA-1 namespace) so the same PR always maps to the same ID.
    namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # URL namespace
    name = f"{owner_repo}#pr{pr_number}"
    return str(uuid.uuid5(namespace, name))
