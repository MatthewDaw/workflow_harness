"""supersession.py — U5: Supersession + temporal validity + un-fold (MAT-144).

A verified PR that contradicts a standing insight (via locality) retires it,
reversibly.

Design: when U3 classifies a colliding incumbent as `supersede`:
  1. Decide the winner: authority > evidence (distinct verified PRs) > recency.
  2. Stamp the loser with `invalidAt` + `supersededBy`.
  3. Un-fold if folded: author a NEW skill revision dropping the superseded
     lesson, repoint TRUE — old revision survives as immutable history.
     Sibling lessons in the same skill body MUST NOT be corrupted.
  4. Never delete. Re-corroboration (a new PR re-teaching the retired pattern)
     revives the idea (`invalidAt` cleared, re-fold).

Key invariants
--------------
* Authority safety: an **inferred** idea (authorityKind='merged') can NEVER
  supersede an **authored** idea ('user_directive' or 'authored_import').
  Between two inferred ideas: more distinct verified PRs → then recency.
* Anti-thrash: each idea tracks supersede/revive oscillation within a sliding
  window; if the oscillation count exceeds the ceiling the operation is
  blocked and logged.
* FP calibration gate: before `unfold_mode='enforce'`, the supersede
  false-positive rate must be below `supersede_fp_ceiling` (default 10%)
  over at least 30 spot-checked verdicts.  In 'shadow' mode the un-fold is
  skipped and the decision is only logged.
* Symbol-deletion: a PR that deletes an anchored symbol/file with no
  superseding change must clean up the stale anchor, not leave it to collide
  forever.

Un-fold discipline
------------------
A skill revision body is a collection of lessons (paragraph-separated).  The
un-fold operation MUST:
  (a) Load the current revision body (the one `foldedIntoRev` points to).
  (b) Locate and remove **only** the superseded idea's paragraph from that
      body, leaving sibling paragraphs intact.
  (c) Mint a NEW revision via the single writer (skills_write.write_revision).
  (d) Repoint the TRUE pointer via CAS.
The old revision row is never mutated — it survives as immutable history.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore
    from learning_service.skills_write import SkillStore
    from learning_service.telemetry import TelemetryAccumulator

from learning_service.db.store import (
    OrgGuardError,
    VersionConflictError,
    VerifyEventRecord,
)
from learning_service.schema.generated.py_types import IdeaRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

DEFAULT_SUPERSEDE_FP_CEILING: float = 0.10  # 10% FP rate
DEFAULT_SUPERSEDE_FP_MIN_SAMPLES: int = 30  # minimum spot-checks before enforcing
DEFAULT_ANTI_THRASH_WINDOW_SECS: int = 7 * 24 * 3600  # 7-day window
DEFAULT_ANTI_THRASH_MAX_FLIPS: int = 3  # max supersede→revive flips per window

# Authority tier ordering — lower number = higher authority.
_AUTHORITY_RANK: dict[str, int] = {
    "user_directive": 0,
    "authored_import": 0,
    "merged": 1,
}


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass
class SupersedeRequest:
    """Everything needed to execute one supersession decision."""
    org: str
    # The incumbent idea being challenged.
    incumbent_idea_id: str
    incumbent_skill_base_name: str
    # The challenger — either an existing idea or a just-distilled insight.
    # Challenger may be None if the supersession is initiated by a new PR
    # whose insight hasn't been persisted yet (open path: just stamp incumbent).
    challenger_idea_id: str | None
    challenger_skill_base_name: str | None
    # The PR that produced the challenger insight.
    challenger_pr_number: int
    challenger_owner_repo: str
    # Challenger authority (the tier of the incoming insight).
    challenger_authority_kind: str  # user_directive | authored_import | merged
    # The skill store for un-fold (skill revision writes).
    # Required when unfold_mode == 'enforce'.
    # The un-fold body that should be REMOVED from the skill body.
    idea_body_to_remove: str | None = None


@dataclass
class SupersedeResult:
    """Outcome of a supersession decision."""
    action: str        # "superseded" | "blocked_authority" | "shadow" | "blocked_thrash" | "blocked_fp_gate"
    incumbent_idea_id: str
    # Set when the incumbent was actually retired.
    invalid_at: int | None = None
    unfold_rev: int | None = None  # New revision minted by un-fold (if any).
    winner_idea_id: str | None = None


@dataclass
class ReviveRequest:
    """Everything needed to revive a superseded idea."""
    org: str
    idea_id: str
    skill_base_name: str
    # The NEW verified PR that re-teaches the retired pattern.
    new_pr_number: int
    new_pr_owner_repo: str


@dataclass
class ReviveResult:
    """Outcome of a revive operation."""
    action: str  # "revived" | "already_active" | "blocked_thrash" | "not_found"
    idea_id: str
    revived_at: int | None = None


@dataclass
class SymbolDeletionRequest:
    """A PR that deletes a file/symbol needs stale anchor cleanup."""
    org: str
    owner_repo: str
    # (file, symbol) pairs that were deleted.
    deleted_pairs: list[tuple[str, str]]


# ---------------------------------------------------------------------------
# FP calibration gate
# ---------------------------------------------------------------------------


@dataclass
class FpCalibrationGate:
    """Tracks the supersede false-positive rate for the calibration gate.

    Usage: call record_verdict() for each spot-checked supersession.
    is_open() returns True when the gate allows enforce (FP rate < ceiling
    over >= min_samples spot-checked verdicts).
    """
    fp_ceiling: float = DEFAULT_SUPERSEDE_FP_CEILING
    min_samples: int = DEFAULT_SUPERSEDE_FP_MIN_SAMPLES

    # {verdict_id: bool} — True = confirmed true positive, False = confirmed FP.
    _spot_checks: dict[str, bool] = field(default_factory=dict)

    def record_verdict(self, verdict_id: str, is_false_positive: bool) -> None:
        """Record a human-spot-checked verdict."""
        self._spot_checks[verdict_id] = not is_false_positive  # True = TP

    @property
    def sample_count(self) -> int:
        return len(self._spot_checks)

    @property
    def fp_rate(self) -> float:
        if not self._spot_checks:
            return 0.0
        fps = sum(1 for v in self._spot_checks.values() if not v)
        return fps / len(self._spot_checks)

    def is_open(self) -> bool:
        """Return True when enforce is allowed (FP rate < ceiling, >= min samples)."""
        if self.sample_count < self.min_samples:
            return False
        return self.fp_rate < self.fp_ceiling


# ---------------------------------------------------------------------------
# Anti-thrash tracker
# ---------------------------------------------------------------------------


@dataclass
class AntiThrashTracker:
    """Tracks supersede/revive oscillation per idea within a sliding window.

    Each flip (supersede or revive event) is appended with its timestamp.
    is_thrashing() returns True if the flip count within the window exceeds
    the max_flips ceiling.
    """
    thrash_window_secs: int = DEFAULT_ANTI_THRASH_WINDOW_SECS
    max_flips: int = DEFAULT_ANTI_THRASH_MAX_FLIPS

    # {idea_id: [epoch_ms of each flip]}
    _flips: dict[str, list[int]] = field(default_factory=dict)

    def record_flip(self, idea_id: str, now_ms: int) -> None:
        """Record a supersede or revive flip for an idea."""
        self._flips.setdefault(idea_id, []).append(now_ms)

    def is_thrashing(self, idea_id: str, now_ms: int) -> bool:
        """Return True if the idea has flipped too many times in the window."""
        flips = self._flips.get(idea_id, [])
        window_start = now_ms - self.thrash_window_secs * 1000
        recent = [t for t in flips if t >= window_start]
        return len(recent) >= self.max_flips

    def flip_count_in_window(self, idea_id: str, now_ms: int) -> int:
        """Return the number of flips in the current window (for telemetry)."""
        flips = self._flips.get(idea_id, [])
        window_start = now_ms - self.thrash_window_secs * 1000
        return sum(1 for t in flips if t >= window_start)


# ---------------------------------------------------------------------------
# Authority decision
# ---------------------------------------------------------------------------


def _authority_rank(kind: str | None) -> int:
    """Return the authority rank (lower = higher authority)."""
    return _AUTHORITY_RANK.get(kind or "merged", 1)


def decide_winner(
    incumbent: IdeaRecord,
    challenger_authority_kind: str,
    challenger_pr_number: int,
    incumbent_sources_count: int,
) -> str:
    """Return 'challenger' or 'incumbent'.

    Decision rule: authority > evidence (distinct verified PRs) > recency.

    Invariant: authored (user_directive / authored_import) ALWAYS beats merged.
    Between two merged: more sources → then lower pr_number wins (recency).
    """
    inc_rank = _authority_rank(incumbent.authorityKind)
    chall_rank = _authority_rank(challenger_authority_kind)

    # Authority tier — lower rank = higher authority.
    if chall_rank < inc_rank:
        return "challenger"
    if inc_rank < chall_rank:
        return "incumbent"

    # Same tier (both authored or both inferred): evidence then recency.
    # For inferred: more distinct verified-PR sources → stronger evidence.
    # (We receive incumbent_sources_count as a proxy for evidence.)
    # No source data for the challenger at this point — assume 1 (the new PR).
    challenger_evidence = 1
    if incumbent_sources_count > challenger_evidence:
        return "incumbent"
    if incumbent_sources_count < challenger_evidence:
        return "challenger"

    # Equal evidence: recency — lower pr_number = older = lose to the new one.
    # The new PR number (challenger_pr_number) is by definition more recent.
    # (PR numbers increase monotonically on GitHub.)
    return "challenger"


# ---------------------------------------------------------------------------
# Core: supersede
# ---------------------------------------------------------------------------


def supersede(
    request: SupersedeRequest,
    store: "LearningStore",
    *,
    unfold_mode: str = "shadow",  # "shadow" | "enforce"
    fp_gate: "FpCalibrationGate | None" = None,
    thrash_tracker: "AntiThrashTracker | None" = None,
    skill_store: "SkillStore | None" = None,
    now_ms: "int | None" = None,
    apply_lesson_delta: "Callable[[str, str], str] | None" = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> SupersedeResult:
    """Execute a supersession decision on the incumbent idea.

    Parameters
    ----------
    request:
        All context for the decision.
    store:
        LearningStore (ideas, anchors, verify events).
    unfold_mode:
        'shadow' — log the decision, do NOT actually un-fold (safe default).
        'enforce' — execute the un-fold (requires fp_gate.is_open() first).
    fp_gate:
        FP calibration gate.  Required in 'enforce' mode (blocks enforce if
        the FP rate is too high or the sample count is insufficient).
    thrash_tracker:
        Anti-thrash tracker.  If provided, blocks the operation when the idea
        is oscillating too fast.
    skill_store:
        SkillStore for writing the un-fold revision.  Required in 'enforce'.
    now_ms:
        Clock injection for testing.
    apply_lesson_delta:
        Callable(winning_body, lesson_body) → merged_body, passed to
        write_revision CAS retry.  Defaults to the skills_write default.

    Returns
    -------
    SupersedeResult
    """
    if not request.org or not request.org.strip():
        raise OrgGuardError("supersede")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    # Load the incumbent.
    incumbent = store.get_idea(
        request.org,
        request.incumbent_skill_base_name,
        request.incumbent_idea_id,
    )
    if incumbent is None:
        logger.warning(
            "supersede: incumbent idea %r not found — skipping",
            request.incumbent_idea_id,
        )
        return SupersedeResult(
            action="not_found",
            incumbent_idea_id=request.incumbent_idea_id,
        )

    # --- Authority safety invariant -------------------------------------------
    # An inferred idea can NEVER supersede an authored one.
    inc_rank = _authority_rank(incumbent.authorityKind)
    chall_rank = _authority_rank(request.challenger_authority_kind)
    if inc_rank < chall_rank:
        # Incumbent is higher authority — block.
        logger.info(
            "supersede BLOCKED (authority invariant): incumbent=%r authority=%r "
            "challenger authority=%r",
            request.incumbent_idea_id,
            incumbent.authorityKind,
            request.challenger_authority_kind,
        )
        return SupersedeResult(
            action="blocked_authority",
            incumbent_idea_id=request.incumbent_idea_id,
        )

    # --- Anti-thrash check ----------------------------------------------------
    if thrash_tracker is not None:
        if thrash_tracker.is_thrashing(request.incumbent_idea_id, now_ms):
            logger.warning(
                "supersede BLOCKED (anti-thrash): idea=%r flips=%d in window",
                request.incumbent_idea_id,
                thrash_tracker.flip_count_in_window(request.incumbent_idea_id, now_ms),
            )
            return SupersedeResult(
                action="blocked_thrash",
                incumbent_idea_id=request.incumbent_idea_id,
            )

    # --- Evidence + recency decision ------------------------------------------
    sources = store.list_idea_sources(request.org, request.incumbent_idea_id)
    winner = decide_winner(
        incumbent=incumbent,
        challenger_authority_kind=request.challenger_authority_kind,
        challenger_pr_number=request.challenger_pr_number,
        incumbent_sources_count=len(sources),
    )

    if winner == "incumbent":
        logger.info(
            "supersede BLOCKED (incumbent wins): idea=%r sources=%d vs challenger pr=%d",
            request.incumbent_idea_id,
            len(sources),
            request.challenger_pr_number,
        )
        return SupersedeResult(
            action="blocked_authority",
            incumbent_idea_id=request.incumbent_idea_id,
        )

    # --- Shadow-mode gate ------------------------------------------------------
    if unfold_mode == "shadow":
        logger.info(
            "supersede SHADOW: would retire incumbent=%r (challenger pr=%d); "
            "unfold_mode=shadow — no writes",
            request.incumbent_idea_id,
            request.challenger_pr_number,
        )
        return SupersedeResult(
            action="shadow",
            incumbent_idea_id=request.incumbent_idea_id,
        )

    # --- FP calibration gate (enforce mode) ------------------------------------
    if unfold_mode == "enforce":
        if fp_gate is not None and not fp_gate.is_open():
            logger.warning(
                "supersede BLOCKED (fp_gate): samples=%d fp_rate=%.3f ceiling=%.3f",
                fp_gate.sample_count,
                fp_gate.fp_rate,
                fp_gate.fp_ceiling,
            )
            return SupersedeResult(
                action="blocked_fp_gate",
                incumbent_idea_id=request.incumbent_idea_id,
            )

    # --- Execute the supersession ---------------------------------------------
    pr_ref_str = f"{request.challenger_owner_repo}#{request.challenger_pr_number}"

    # Stamp invalidAt + supersededBy on the incumbent.
    updated_incumbent = IdeaRecord(
        ideaId=incumbent.ideaId,
        skillBaseName=incumbent.skillBaseName,
        org=incumbent.org,
        body=incumbent.body,
        status=incumbent.status,  # keep "folded" or "open"
        corroborationVersion=incumbent.corroborationVersion + 1,
        foldedIntoRev=incumbent.foldedIntoRev,
        invalidAt=now_ms,
        supersededBy=request.challenger_idea_id,
        supersedes=incumbent.supersedes,
        authored=incumbent.authored,
        authorityKind=incumbent.authorityKind,
        refines=incumbent.refines,
        revivedAt=incumbent.revivedAt,
        legacyRecurrenceFold=incumbent.legacyRecurrenceFold,
        sourceRef=incumbent.sourceRef,
        scopeTag=incumbent.scopeTag,
        authorId=incumbent.authorId,
        verificationRung=incumbent.verificationRung,
    )

    try:
        store.put_idea_conditional(updated_incumbent, incumbent.corroborationVersion)
    except VersionConflictError:
        logger.warning(
            "supersede OCC conflict for idea=%r — racing write; skipping this supersession",
            incumbent.ideaId,
        )
        return SupersedeResult(
            action="shadow",
            incumbent_idea_id=request.incumbent_idea_id,
        )

    # Append verify event (audit log).
    events = store.list_verify_events(request.org, incumbent.ideaId)
    next_seq = (max(e.seq for e in events) + 1) if events else 0
    store.append_verify_event(VerifyEventRecord(
        org=request.org,
        idea_id=incumbent.ideaId,
        seq=next_seq,
        verdict="supersede",
        pr_ref=pr_ref_str,
        authority=request.challenger_authority_kind,
        recorded_at=now_ms,
    ))

    # Record the flip for anti-thrash.
    if thrash_tracker is not None:
        thrash_tracker.record_flip(request.incumbent_idea_id, now_ms)

    logger.info(
        "supersede: retired incumbent=%r invalidAt=%d supersededBy=%r",
        incumbent.ideaId,
        now_ms,
        request.challenger_idea_id,
    )

    # --- Un-fold if the idea was folded -------------------------------------
    unfold_rev: int | None = None
    if incumbent.status == "folded" and incumbent.foldedIntoRev is not None:
        unfold_rev = _execute_unfold(
            incumbent=incumbent,
            request=request,
            store=store,
            skill_store=skill_store,
            now_ms=now_ms,
            apply_lesson_delta=apply_lesson_delta,
        )

    # --- Retire anchors (locality join cleanup) -----------------------------
    from learning_service.anchors import retire_anchors_on_unfold
    retire_anchors_on_unfold(
        store=store,
        org=request.org,
        owner_repo=request.challenger_owner_repo,
        idea_id=incumbent.ideaId,
    )

    # --- Telemetry: record supersede event + un-fold ----------------------------
    if telemetry is not None:
        telemetry.record_supersede_event(unfold_executed=unfold_rev is not None)

    return SupersedeResult(
        action="superseded",
        incumbent_idea_id=request.incumbent_idea_id,
        invalid_at=now_ms,
        unfold_rev=unfold_rev,
        winner_idea_id=request.challenger_idea_id,
    )


# ---------------------------------------------------------------------------
# Un-fold execution
# ---------------------------------------------------------------------------


def _execute_unfold(
    incumbent: IdeaRecord,
    request: SupersedeRequest,
    store: "LearningStore",
    skill_store: "SkillStore | None",
    now_ms: int,
    apply_lesson_delta: "Callable[[str, str], str] | None",
) -> int | None:
    """Author a new skill revision that drops the superseded lesson.

    Returns the new revision number minted, or None if the un-fold could not
    proceed (e.g. skill_store not provided, revision body not found).
    """
    if skill_store is None:
        logger.warning(
            "unfold: skill_store not provided — cannot un-fold idea=%r",
            incumbent.ideaId,
        )
        return None

    # Import the single writer here to avoid a circular import at module level.
    from learning_service.skills_write import RevisionRequest, write_revision

    # Read the current revision body so we can drop only the superseded lesson.
    # The base variant id is "" (empty string) — the skill base name is the
    # DynamoDB key prefix, not the variantId stored in the revision row.
    current_body = skill_store.get_revision_body(
        request.org,
        "",  # base variant id (the default/org variant)
        incumbent.foldedIntoRev,
    )
    if current_body is None:
        logger.warning(
            "unfold: revision body not found for %r rev=%d — cannot un-fold",
            incumbent.ideaId,
            incumbent.foldedIntoRev,
        )
        return None

    # Remove the idea's lesson from the skill body without corrupting siblings.
    lesson_to_remove = request.idea_body_to_remove or incumbent.body
    new_body = _remove_lesson_from_body(current_body, lesson_to_remove)

    rev_request = RevisionRequest(
        org=request.org,
        base_name=incumbent.skillBaseName,
        variant_id="",  # base variant
        body=new_body,
        description=f"un-fold: retired idea {incumbent.ideaId!r} (superseded by PR #{request.challenger_pr_number})",
        idea_id=incumbent.ideaId,
    )

    try:
        result = write_revision(
            rev_request,
            skill_store,
            apply_lesson_delta=apply_lesson_delta,
            now_ms=lambda: now_ms,
        )
        logger.info(
            "unfold: authored new revision=%d for skill=%r (idea=%r retired)",
            result.rev,
            incumbent.skillBaseName,
            incumbent.ideaId,
        )
        return result.rev
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unfold: write_revision failed for idea=%r: %s",
            incumbent.ideaId,
            exc,
        )
        return None


def _remove_lesson_from_body(skill_body: str, lesson_body: str) -> str:
    """Remove the superseded lesson from the skill body.

    Strategy:
    1. Split the skill body into paragraphs (separated by blank lines).
    2. Remove any paragraph that is a substring of (or identical to) the lesson.
    3. If no exact match, remove the paragraph with the highest word-Jaccard
       similarity to the lesson (≥ 0.5 threshold) — the lesson may have been
       re-synthesized during corroboration.
    4. Re-join with blank lines, preserving all other paragraphs intact.

    Sibling corruption guard: paragraphs NOT matching are NEVER touched.
    """
    if not skill_body:
        return skill_body
    if not lesson_body or not lesson_body.strip():
        return skill_body

    lesson_stripped = lesson_body.strip()
    paragraphs = [p.strip() for p in skill_body.split("\n\n") if p.strip()]

    if not paragraphs:
        return skill_body

    # Step 1: exact substring match.
    survivors: list[str] = []
    removed = False
    for para in paragraphs:
        if not removed and (para == lesson_stripped or lesson_stripped in para or para in lesson_stripped):
            removed = True
            continue  # drop this paragraph
        survivors.append(para)

    if removed:
        return "\n\n".join(survivors)

    # Step 2: highest Jaccard similarity (≥ 0.5).
    lesson_words = set(lesson_stripped.lower().split())
    best_idx: int | None = None
    best_sim: float = 0.0
    for i, para in enumerate(paragraphs):
        para_words = set(para.lower().split())
        if not para_words or not lesson_words:
            continue
        inter = len(para_words & lesson_words)
        union = len(para_words | lesson_words)
        sim = inter / union if union else 0.0
        if sim > best_sim:
            best_sim = sim
            best_idx = i

    if best_idx is not None and best_sim >= 0.35:
        survivors = [p for i, p in enumerate(paragraphs) if i != best_idx]
        return "\n\n".join(survivors)

    # No match found — return unchanged (conservative; do not corrupt the body).
    logger.warning(
        "_remove_lesson_from_body: could not locate lesson in body — returning unchanged. "
        "lesson=%r",
        lesson_stripped[:80],
    )
    return skill_body


# ---------------------------------------------------------------------------
# Core: revive (re-corroboration un-retires a superseded idea)
# ---------------------------------------------------------------------------


def revive(
    request: ReviveRequest,
    store: "LearningStore",
    *,
    thrash_tracker: "AntiThrashTracker | None" = None,
    now_ms: "int | None" = None,
    telemetry: "TelemetryAccumulator | None" = None,
) -> ReviveResult:
    """Revive a superseded idea when a new verified PR re-teaches the pattern.

    Invariant: revive happens only on a NEW verified PR (not re-reading the
    old one).  The thrash_tracker blocks oscillation within the window.

    After reviving, the idea is open again (invalidAt cleared, revivedAt set).
    Re-folding requires the normal corroboration path — the caller should
    proceed with corroborate() after a successful revive.
    """
    if not request.org or not request.org.strip():
        raise OrgGuardError("revive")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    idea = store.get_idea(request.org, request.skill_base_name, request.idea_id)
    if idea is None:
        return ReviveResult(action="not_found", idea_id=request.idea_id)

    if idea.invalidAt is None:
        # Already active — no-op.
        return ReviveResult(action="already_active", idea_id=request.idea_id)

    # Anti-thrash.
    if thrash_tracker is not None:
        if thrash_tracker.is_thrashing(request.idea_id, now_ms):
            logger.warning(
                "revive BLOCKED (anti-thrash): idea=%r", request.idea_id
            )
            return ReviveResult(action="blocked_thrash", idea_id=request.idea_id)

    # Clear invalidAt; set revivedAt.
    revived = IdeaRecord(
        ideaId=idea.ideaId,
        skillBaseName=idea.skillBaseName,
        org=idea.org,
        body=idea.body,
        status="open",  # reset to open — re-fold via normal corroboration
        corroborationVersion=idea.corroborationVersion + 1,
        foldedIntoRev=None,   # cleared — no longer pointing at the old revision
        invalidAt=None,       # cleared
        supersededBy=None,    # cleared
        supersedes=idea.supersedes,
        authored=idea.authored,
        authorityKind=idea.authorityKind,
        refines=idea.refines,
        revivedAt=now_ms,
        legacyRecurrenceFold=idea.legacyRecurrenceFold,
        sourceRef=idea.sourceRef,
        scopeTag=idea.scopeTag,
        authorId=idea.authorId,
        verificationRung=idea.verificationRung,
    )

    try:
        store.put_idea_conditional(revived, idea.corroborationVersion)
    except VersionConflictError:
        logger.warning("revive OCC conflict for idea=%r — skipping", idea.ideaId)
        return ReviveResult(action="blocked_thrash", idea_id=request.idea_id)

    # Audit event.
    events = store.list_verify_events(request.org, idea.ideaId)
    next_seq = (max(e.seq for e in events) + 1) if events else 0
    pr_ref_str = f"{request.new_pr_owner_repo}#{request.new_pr_number}"
    store.append_verify_event(VerifyEventRecord(
        org=request.org,
        idea_id=idea.ideaId,
        seq=next_seq,
        verdict="revive",
        pr_ref=pr_ref_str,
        authority="merged",
        recorded_at=now_ms,
    ))

    if thrash_tracker is not None:
        thrash_tracker.record_flip(request.idea_id, now_ms)

    # --- Telemetry: record revive event ----------------------------------------
    if telemetry is not None:
        telemetry.record_revive()

    logger.info("revive: idea=%r revivedAt=%d", idea.ideaId, now_ms)
    return ReviveResult(action="revived", idea_id=request.idea_id, revived_at=now_ms)


# ---------------------------------------------------------------------------
# Symbol-deletion anchor cleanup
# ---------------------------------------------------------------------------


def clean_deleted_symbol_anchors(
    request: SymbolDeletionRequest,
    store: "LearningStore",
) -> list[str]:
    """Mark anchors for deleted (file, symbol) pairs as inactive.

    Returns the list of idea IDs whose anchors were cleaned up.

    A PR that deletes an anchored symbol/file with no superseding change must
    clean up the stale anchor (remove from the current index, keep in history),
    not leave it to collide forever.
    """
    if not request.org or not request.org.strip():
        raise OrgGuardError("clean_deleted_symbol_anchors")

    from learning_service.schema.generated.py_types import AnchorRecord

    cleaned_ideas: list[str] = []
    seen_ideas: set[str] = set()

    for file, symbol in request.deleted_pairs:
        idea_ids = store.get_ideas_by_anchor(request.org, request.owner_repo, file, symbol)
        for idea_id in idea_ids:
            if idea_id in seen_ideas:
                continue
            seen_ideas.add(idea_id)

            # Mark the specific anchor(s) for this idea as inactive.
            anchors = store.get_anchors_for_idea(request.org, request.owner_repo, idea_id)
            for anchor in anchors:
                if anchor.file == file and anchor.symbol == symbol:
                    retired = AnchorRecord(
                        ideaId=anchor.ideaId,
                        ownerRepo=anchor.ownerRepo,
                        file=anchor.file,
                        symbol=anchor.symbol,
                        org=anchor.org,
                        active=False,
                    )
                    store.put_anchor(retired)
                    logger.info(
                        "clean_deleted_symbol_anchors: retired anchor idea=%r file=%r symbol=%r",
                        idea_id,
                        file,
                        symbol,
                    )

            cleaned_ideas.append(idea_id)

    return cleaned_ideas
