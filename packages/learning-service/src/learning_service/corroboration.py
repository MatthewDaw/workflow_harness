"""corroboration.py — U4: Verified corroboration (MAT-143).

corroborationWeight = Σ rungWeight(source) × authorCredibility(authorId) +
                       recurrence_bonus(0.2/repeat)

Fold when corroborationWeight >= verified_K (default 2).
Rung: {test: 1.0, normal: 0.6, bare: 0.4}, never 0.
Author credibility: user-curated, default 0.5.

Key behaviours implemented here
--------------------------------
* **Idempotency on prRef.number** — re-processing a PR must not double-count.
* **Fold threshold** — fold when corroborationWeight >= verified_K (not raw count).
* **Author credibility** — each hit's weight is × author credibility (user-curated,
  default 0.5, never auto-demoted).
* **Recurrence bonus** — 0.2 per repeat hit (same prRef.number seen again after
  an earlier write) to reward recurrence of the same lesson across reruns.
* **MERGE-REWRITE (body re-synthesis) only on corroborate** — when a new PR's
  insight overlaps heavily with the incumbent body, the two are re-synthesized
  into one paragraph.  Never on a contradiction verdict.
* **Cross-org guard** on every write path.
* **Conditional / OCC write** (put_idea_conditional) throughout.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore

from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
    ProcessedPrRecord,
    VersionConflictError,
)
from learning_service.schema.generated.py_types import (
    IdeaRecord,
    IdeaSourceRecord,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

RUNG_WEIGHTS: dict[str, float] = {
    "test": 1.0,
    "normal": 0.6,
    "bare": 0.4,
}

DEFAULT_VERIFIED_K: float = 2.0
DEFAULT_AUTHOR_CREDIBILITY: float = 0.5
# Higher default when a distinct non-author reviewer is present.
REVIEWER_CREDIBILITY: float = 0.7
# Bonus weight added per repeat hit of the same prRef.number.
RECURRENCE_BONUS: float = 0.2
# Maximum retries on an OCC conflict.
MAX_OCC_RETRIES: int = 5


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass
class CorroborationRequest:
    """Everything needed to record one corroboration vote on an idea."""

    org: str
    idea_id: str
    skill_base_name: str
    # The PR reference that produced this vote.
    pr_number: int
    owner_repo: str
    # Verification rung inferred from the diff/PR: test | normal | bare.
    rung: str
    # PR author login (for author-credibility lookup).
    author_id: str
    # New insight body for potential re-synthesis (MERGE-REWRITE).
    challenger_body: str
    # Authority kind: always "merged" for the inferred lane.
    authority_kind: str = "merged"
    # Optional: whether this PR had a distinct non-author reviewer present.
    has_reviewer: bool = False


@dataclass
class CorroborationResult:
    """Outcome of one corroboration vote."""

    action: str            # "candidate" | "folded" | "already_counted" | "created"
    idea_id: str
    corroboration_weight: float
    folded: bool
    body: str              # final idea body (re-synthesized if MERGE-REWRITE)
    new_source_written: bool
    version: int           # corroborationVersion after this write


# ---------------------------------------------------------------------------
# Author-credibility store (user-curated, in-memory for now)
# ---------------------------------------------------------------------------


class AuthorCredibilityStore:
    """Holds user-curated author credibility weights.

    Default: 0.5 for unknown/single-author.
    Boosts may be applied retroactively.
    The system NEVER auto-demotes already-folded ideas.
    """

    def __init__(
        self,
        overrides: dict[str, float] | None = None,
        *,
        default: float = DEFAULT_AUTHOR_CREDIBILITY,
        reviewer_default: float = REVIEWER_CREDIBILITY,
    ) -> None:
        self._overrides: dict[str, float] = dict(overrides or {})
        self._default = default
        self._reviewer_default = reviewer_default

    def get(self, author_id: str, *, has_reviewer: bool = False) -> float:
        """Return the credibility weight for *author_id*.

        Falls back to ``reviewer_default`` if *has_reviewer* is True (the PR
        had a distinct non-author reviewer), otherwise ``default``.
        """
        if author_id in self._overrides:
            return self._overrides[author_id]
        return self._reviewer_default if has_reviewer else self._default

    def set(self, author_id: str, weight: float) -> None:
        """Set a user-curated weight for *author_id* (retroactive boost allowed)."""
        if not (0.0 < weight <= 2.0):
            raise ValueError(
                f"Author credibility weight must be in (0, 2]; got {weight!r}"
            )
        self._overrides[author_id] = weight

    def delete(self, author_id: str) -> None:
        """Remove a credibility override (falls back to default)."""
        self._overrides.pop(author_id, None)


# ---------------------------------------------------------------------------
# Weight helpers
# ---------------------------------------------------------------------------


def rung_weight(rung: str) -> float:
    """Return the weight for a rung.  Never 0 — a merged PR always means something."""
    w = RUNG_WEIGHTS.get(rung)
    if w is None:
        logger.warning("unknown rung %r — defaulting to bare (0.4)", rung)
        return 0.4
    return w


def corroboration_weight(
    sources: list[IdeaSourceRecord],
    credibility_store: AuthorCredibilityStore,
) -> float:
    """Compute corroborationWeight from the idea's source list.

    Formula:
        Σ over distinct prRef.number of (rungWeight(s) × authorCredibility(s.authorId))
        + recurrence_bonus (0.2 per repeat hit)

    ``sources`` should be the current list of IdeaSourceRecord for the idea.
    Sources without a prRef are authored sources (no PR weight).
    """
    # Group by distinct prRef.number
    seen_pr: dict[int, list[IdeaSourceRecord]] = {}
    for src in sources:
        pr_num = _extract_pr_number(src.prRef)
        if pr_num is None:
            continue
        seen_pr.setdefault(pr_num, []).append(src)

    total = 0.0
    for pr_num, hits in seen_pr.items():
        # Take the highest-rung hit for the base weight (one PR may be processed
        # multiple times in replay overlaps — idempotency guard, but the bonus
        # accumulates for repeats).
        best = hits[0]
        for h in hits[1:]:
            if rung_weight(h.verificationRung or "bare") > rung_weight(best.verificationRung or "bare"):
                best = h
        base_w = rung_weight(best.verificationRung or "bare")
        cred = credibility_store.get(
            best.authorId or "",
            has_reviewer=False,  # has_reviewer is request-time info, not stored
        )
        total += base_w * cred
        # Recurrence bonus: each additional hit beyond the first adds 0.2.
        repeats = len(hits) - 1
        total += repeats * RECURRENCE_BONUS

    return total


def _extract_pr_number(pr_ref: str | None) -> int | None:
    """Extract the PR number from a prRef string like 'owner/repo#<number>'."""
    if pr_ref is None:
        return None
    parts = pr_ref.rsplit("#", 1)
    if len(parts) != 2:
        return None
    try:
        return int(parts[1])
    except (ValueError, TypeError):
        return None


def distinct_pr_count(sources: list[IdeaSourceRecord]) -> int:
    """Return the count of distinct prRef.number values (telemetry)."""
    seen = set()
    for src in sources:
        pr_num = _extract_pr_number(src.prRef)
        if pr_num is not None:
            seen.add(pr_num)
    return len(seen)


# ---------------------------------------------------------------------------
# Body re-synthesis (MERGE-REWRITE on corroborate)
# ---------------------------------------------------------------------------


def _paragraphs_overlap(body_a: str, body_b: str, threshold: float = 0.6) -> bool:
    """Rough overlap check using Jaccard on word sets.

    The MERGE-REWRITE requirement says two ideas that say largely the same
    thing must consolidate into one paragraph.  We use a simple word-Jaccard
    as a proxy for semantic overlap in the absence of an embedding call.
    Threshold 0.6 is deliberately conservative — only clear duplicates.
    """
    words_a = set(body_a.lower().split())
    words_b = set(body_b.lower().split())
    if not words_a or not words_b:
        return False
    intersection = words_a & words_b
    union = words_a | words_b
    return len(intersection) / len(union) >= threshold


def resynthesize_body(incumbent: str, challenger: str) -> str:
    """Re-synthesize two overlapping paragraphs into one (MERGE-REWRITE).

    Produces a single merged paragraph that captures the essence of both.
    In production this would be an LLM call; here we do a clean structural
    merge that avoids near-duplicate accumulation.

    Rules:
    - If the challenger is already contained in the incumbent, keep the incumbent.
    - If the two are highly overlapping (same core claim, slightly different wording),
      keep the longer / more specific one — do NOT concatenate both.
    - Otherwise, take the incumbent and append any unique clauses from the challenger.
    """
    inc_stripped = incumbent.strip()
    chal_stripped = challenger.strip()

    if not chal_stripped or chal_stripped in inc_stripped:
        return inc_stripped

    if not inc_stripped:
        return chal_stripped

    # For near-duplicate paragraphs (high Jaccard overlap), keep only the longer
    # one (more specific).  Concatenating would produce exactly the near-duplicate
    # accumulation the plan prohibits.
    if _paragraphs_overlap(inc_stripped, chal_stripped, threshold=0.5):
        # Keep whichever is longer / more specific; prefer challenger as it
        # is the more recent insight.
        return chal_stripped if len(chal_stripped) >= len(inc_stripped) else inc_stripped

    # Low overlap — safe to combine: incumbent + unique clauses from challenger.
    def _clauses(text: str) -> list[str]:
        # Split on common clause separators and strip.
        import re as _re
        parts = _re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
        return [p.strip() for p in parts if p.strip()]

    inc_clauses = _clauses(inc_stripped)
    chal_clauses = _clauses(chal_stripped)
    inc_lower = {c.lower() for c in inc_clauses}

    merged = list(inc_clauses)
    for clause in chal_clauses:
        if clause.lower() not in inc_lower:
            merged.append(clause)
            inc_lower.add(clause.lower())

    result = " ".join(merged)
    if not result.endswith((".", "!", "?")):
        result += "."
    return result


# ---------------------------------------------------------------------------
# Core corroboration function
# ---------------------------------------------------------------------------


def corroborate(
    request: CorroborationRequest,
    store: "LearningStore",
    credibility_store: AuthorCredibilityStore,
    *,
    verified_k: float = DEFAULT_VERIFIED_K,
    now_ms: int | None = None,
    max_retries: int = MAX_OCC_RETRIES,
) -> CorroborationResult:
    """Record one corroboration vote on an existing idea.

    This is the core of U4.  The caller is responsible for:
    - Verifying the NLI verdict is CORROBORATE (not SUPERSEDE/REFINE/NEUTRAL).
    - Supplying the correct idea_id (found by the semantic join).

    Steps
    -----
    1. Cross-org guard.
    2. Load current sources; check idempotency (same prRef.number already present?).
    3. Write the new source record.
    4. Re-compute corroborationWeight.
    5. If overlapping paragraphs → MERGE-REWRITE.
    6. Conditional write (OCC) on corroborationVersion.
    7. If weight >= verified_K → fold (status = "folded").

    Returns CorroborationResult.
    """
    if not request.org or not request.org.strip():
        raise OrgGuardError("corroborate")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pr_ref_str = f"{request.owner_repo}#{request.pr_number}"
    source_id = f"pr#{request.pr_number}"  # stable per PR

    # Track whether we wrote a new source in this corroborate() call.
    # This is set on the first attempt and must survive OCC retries
    # (the source write is idempotent — put_idea_source upserts by sourceId).
    wrote_new_source: bool | None = None  # None = not yet determined

    for attempt in range(max_retries):
        # Load the idea.
        idea = store.get_idea(request.org, request.skill_base_name, request.idea_id)
        if idea is None:
            raise ValueError(
                f"corroborate: idea {request.idea_id!r} not found in "
                f"org={request.org!r} skill={request.skill_base_name!r}"
            )

        # Load existing sources.
        sources = store.list_idea_sources(request.org, request.idea_id)

        # Idempotency: skip if this PR was already counted BEFORE this call.
        # On OCC retries, the source was written in attempt 0 — treat as new.
        existing_pr_nums = {_extract_pr_number(s.prRef) for s in sources}

        if wrote_new_source is None:
            # First attempt: check if the PR was already in the store.
            already_counted = request.pr_number in existing_pr_nums
            if not already_counted:
                # Write the new source record.
                new_source = IdeaSourceRecord(
                    ideaId=request.idea_id,
                    sourceId=source_id,
                    org=request.org,
                    prRef=pr_ref_str,
                    authorityKind=request.authority_kind,
                    verificationRung=request.rung,
                    authorId=request.author_id,
                )
                store.put_idea_source(new_source)
                wrote_new_source = True
            else:
                wrote_new_source = False
        else:
            # OCC retry: we already wrote the source in a prior attempt.
            # The idempotency check would now see the PR as "present" — but we
            # know we wrote it this call, so it is NOT an already-counted case.
            already_counted = not wrote_new_source

        # Re-fetch sources to include the new one if written.
        sources = store.list_idea_sources(request.org, request.idea_id)

        # Compute weight over all distinct PRs.
        weight = corroboration_weight(sources, credibility_store)

        # MERGE-REWRITE: re-synthesize body if paragraphs overlap significantly.
        new_body = idea.body
        if wrote_new_source and _paragraphs_overlap(idea.body, request.challenger_body):
            new_body = resynthesize_body(idea.body, request.challenger_body)
            logger.info(
                "corroborate MERGE-REWRITE idea=%s weight=%.3f",
                request.idea_id,
                weight,
            )

        # Determine if we should fold.
        should_fold = weight >= verified_k and idea.status != "folded"
        new_status = "folded" if should_fold else idea.status

        # Build updated idea record.
        updated_idea = IdeaRecord(
            ideaId=idea.ideaId,
            skillBaseName=idea.skillBaseName,
            org=idea.org,
            body=new_body,
            status=new_status,
            corroborationVersion=idea.corroborationVersion + 1,
            foldedIntoRev=idea.foldedIntoRev,
            invalidAt=idea.invalidAt,
            supersededBy=idea.supersededBy,
            supersedes=idea.supersedes,
            authored=idea.authored,
            authorityKind=idea.authorityKind,
            refines=idea.refines,
            revivedAt=idea.revivedAt,
            legacyRecurrenceFold=idea.legacyRecurrenceFold,
            sourceRef=idea.sourceRef,
            scopeTag=idea.scopeTag,
            authorId=idea.authorId,
            verificationRung=idea.verificationRung,
        )

        try:
            store.put_idea_conditional(updated_idea, idea.corroborationVersion)
        except VersionConflictError:
            if attempt < max_retries - 1:
                logger.info(
                    "corroborate OCC conflict on idea=%s (attempt %d) — retrying",
                    request.idea_id,
                    attempt + 1,
                )
                continue
            raise

        action = "already_counted" if already_counted else (
            "folded" if should_fold else "candidate"
        )

        logger.info(
            "corroborate: idea=%s action=%s weight=%.3f version=%d folded=%s",
            request.idea_id,
            action,
            weight,
            updated_idea.corroborationVersion,
            should_fold,
        )

        return CorroborationResult(
            action=action,
            idea_id=request.idea_id,
            corroboration_weight=weight,
            folded=new_status == "folded",
            body=new_body,
            new_source_written=wrote_new_source or False,
            version=updated_idea.corroborationVersion,
        )

    raise VersionConflictError(
        idea_id=request.idea_id,
        expected=0,
        current=-1,
    )


# ---------------------------------------------------------------------------
# create_idea — helper to create a new idea from the first PR hit
# ---------------------------------------------------------------------------


def create_idea_from_pr(
    org: str,
    idea_id: str,
    skill_base_name: str,
    pr_number: int,
    owner_repo: str,
    rung: str,
    author_id: str,
    body: str,
    store: "LearningStore",
    credibility_store: AuthorCredibilityStore,
    *,
    authority_kind: str = "merged",
    has_reviewer: bool = False,
    now_ms: int | None = None,
    verified_k: float = DEFAULT_VERIFIED_K,
) -> CorroborationResult:
    """Create a brand-new idea record from the first PR hit and record its source.

    Returns a CorroborationResult with action="created" (or "folded" if the
    first hit already crosses verified_K — e.g. a test-rung PR from a
    high-credibility author).
    """
    if not org or not org.strip():
        raise OrgGuardError("create_idea_from_pr")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    pr_ref_str = f"{owner_repo}#{pr_number}"
    source_id = f"pr#{pr_number}"

    cred = credibility_store.get(author_id, has_reviewer=has_reviewer)
    w = rung_weight(rung) * cred

    should_fold = w >= verified_k
    status = "folded" if should_fold else "open"

    # Create the idea record (first-write: expected_version = -1).
    idea = IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill_base_name,
        org=org,
        body=body,
        status=status,
        corroborationVersion=0,
        authorityKind=authority_kind,
    )
    store.put_idea_conditional(idea, expected_version=-1)

    # Write the source record.
    src = IdeaSourceRecord(
        ideaId=idea_id,
        sourceId=source_id,
        org=org,
        prRef=pr_ref_str,
        authorityKind=authority_kind,
        verificationRung=rung,
        authorId=author_id,
    )
    store.put_idea_source(src)

    logger.info(
        "create_idea_from_pr: org=%s idea=%s rung=%s weight=%.3f folded=%s",
        org,
        idea_id,
        rung,
        w,
        should_fold,
    )

    return CorroborationResult(
        action="folded" if should_fold else "created",
        idea_id=idea_id,
        corroboration_weight=w,
        folded=should_fold,
        body=body,
        new_source_written=True,
        version=0,
    )


# ---------------------------------------------------------------------------
# Weight recompute helper (retroactive credibility boost)
# ---------------------------------------------------------------------------


def recompute_weight(
    org: str,
    idea_id: str,
    skill_base_name: str,
    store: "LearningStore",
    credibility_store: AuthorCredibilityStore,
) -> float:
    """Re-compute corroborationWeight for an idea using the current credibility store.

    Used when a credibility boost is applied retroactively.  Does NOT write to
    the store (read-only) — returns the new weight for the caller to decide
    whether to fold/unfold.

    Invariant: a boost may RAISE the weight (fold faster), but the system
    never auto-demotes already-folded ideas.
    """
    sources = store.list_idea_sources(org, idea_id)
    return corroboration_weight(sources, credibility_store)
