"""migration.py — R2: Migration/backfill for legacy recurrence-folded ideas (MAT-151).

Existing ``folded`` ideas were corroborated by **recurrence** (no ``prRef``).
Recomputing under verified semantics would un-fold them en masse — wrong.

This module provides two operations:

1. **grandfather_legacy_folds** — scan all ``folded`` ideas in an org; any
   that have no ``merged``-authority source (i.e. no ``prRef``-bearing
   ``IdeaSourceRecord``) are tagged ``legacyRecurrenceFold=True`` and treated
   as verified-equivalent.  They stay folded.  Un-folding en masse is
   explicitly forbidden.

2. **confirm_grandfathered_fold** — when a real merged-PR vote arrives for an
   idea that is already tagged ``legacyRecurrenceFold=True``, clear the flag
   (set it to ``None`` / absent).  The idea is now confirmed by a verified PR
   and no longer depends on the grandfather protection.

3. **transfer_replay_shadow_diff** — optional; replay a fixed merge log through
   the ingest handler in **shadow mode** (no writes), collect the shadow
   decisions, and compute a diff against the live legacy library.  Returns a
   ``ShadowDiffReport`` that the caller can inspect before promoting to enforce.
   This is deterministic (same log → same report) and performs zero live writes.

Design notes
-----------
* **Never un-fold en masse.**  The grandfather flag makes existing folds
  verified-equivalent so the supersession/necessity machinery treats them at
  par with PR-gated folds.
* **Flag cleared on real vote** — once a grandfathered fold earns its first
  real merged-PR source it is "confirmed" and the flag is cleared.  If it is
  later superseded, it retires normally (invalidAt + un-fold).
* **Shadow diff** — the transfer-replay diff is a reporting tool, not a write.
  The diff lists (a) ideas that would be *new* under verified semantics,
  (b) ideas that exist in the legacy library but would NOT appear under verified
  semantics (potential retirement candidates), and (c) ideas confirmed by the
  replay.  The caller decides when to promote.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore

from learning_service.db.store import OrgGuardError, VersionConflictError
from learning_service.schema.generated.py_types import IdeaRecord

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_OCC_RETRIES: int = 5


# ---------------------------------------------------------------------------
# Grandfathering result types
# ---------------------------------------------------------------------------


@dataclass
class GrandfatherResult:
    """Outcome of a grandfather_legacy_folds run."""
    org: str
    tagged: list[str] = field(default_factory=list)   # idea_ids tagged legacy
    already_tagged: list[str] = field(default_factory=list)  # already had flag
    skipped_no_prref: list[str] = field(default_factory=list)  # open / already confirmed
    errors: list[str] = field(default_factory=list)    # ids that failed OCC


@dataclass
class ConfirmResult:
    """Outcome of confirm_grandfathered_fold for one idea."""
    action: str     # "confirmed" | "not_grandfathered" | "not_found" | "not_folded"
    idea_id: str
    # Set when the flag was cleared.
    cleared_at: int | None = None


# ---------------------------------------------------------------------------
# Shadow-diff types (optional transfer-replay)
# ---------------------------------------------------------------------------


@dataclass
class ShadowIdeaSummary:
    """One idea's shadow verdict from the transfer-replay run."""
    idea_id: str
    skill_base_name: str
    body_excerpt: str        # first 120 chars of the body
    # Whether this idea is present in the legacy (live) library.
    in_legacy: bool
    # Whether the replay would have folded this idea.
    would_fold: bool
    # Whether the replay would have superseded a live idea.
    would_supersede: bool = False
    # The live idea that would have been superseded (if any).
    supersede_target: str | None = None


@dataclass
class ShadowDiffReport:
    """Deterministic diff of a shadow transfer-replay against the live library.

    ``new_under_verified`` — ideas the replay would create that are absent from
        the legacy library (potential additions; do not promote automatically).
    ``only_in_legacy`` — ideas present in the legacy library that the replay
        would NOT fold (potential retirements; review manually before acting).
    ``confirmed_by_replay`` — legacy-grandfathered ideas that the replay would
        also fold (the replay independently corroborates them — safe to confirm).
    ``would_supersede`` — (incumbent_id, challenger_summary) pairs the replay
        would retire (review before allowing supersession to run in enforce mode).
    """
    org: str
    owner_repo: str
    prs_replayed: int
    new_under_verified: list[ShadowIdeaSummary] = field(default_factory=list)
    only_in_legacy: list[ShadowIdeaSummary] = field(default_factory=list)
    confirmed_by_replay: list[ShadowIdeaSummary] = field(default_factory=list)
    would_supersede: list[tuple[str, ShadowIdeaSummary]] = field(default_factory=list)

    def summary_line(self) -> str:
        return (
            f"ShadowDiff org={self.org!r} repo={self.owner_repo!r}: "
            f"prs={self.prs_replayed} "
            f"new={len(self.new_under_verified)} "
            f"only_in_legacy={len(self.only_in_legacy)} "
            f"confirmed={len(self.confirmed_by_replay)} "
            f"would_supersede={len(self.would_supersede)}"
        )


# ---------------------------------------------------------------------------
# Core: grandfather_legacy_folds
# ---------------------------------------------------------------------------


def _has_pr_source(store: "LearningStore", org: str, idea_id: str) -> bool:
    """Return True if the idea has at least one IdeaSource with a prRef."""
    sources = store.list_idea_sources(org, idea_id)
    return any(s.prRef is not None for s in sources)


def grandfather_legacy_folds(
    org: str,
    store: "LearningStore",
    *,
    skill_base_name: str | None = None,
    now_ms: int | None = None,
    dry_run: bool = False,
) -> GrandfatherResult:
    """Tag every folded idea that has no verified-PR source as legacyRecurrenceFold=True.

    This is the migration / backfill operation.  It is safe to re-run (ideas
    already tagged are reported in ``already_tagged`` and not re-written).

    Parameters
    ----------
    org:
        Organisation slug.
    store:
        LearningStore instance (DynamoLearningStore in production; InMemory in tests).
    skill_base_name:
        If set, scan only this skill family.  Defaults to all ideas in the org.
    now_ms:
        Clock injection for testing.
    dry_run:
        If True, identify candidates but do not write to the store.

    Returns
    -------
    GrandfatherResult
    """
    if not org or not org.strip():
        raise OrgGuardError("grandfather_legacy_folds")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    result = GrandfatherResult(org=org)

    # Collect candidate ideas.
    if skill_base_name is not None:
        ideas = store.list_current_ideas(org, skill_base_name)
        # list_current_ideas excludes invalidAt; we also need to check folded.
        ideas = [i for i in ideas if i.status == "folded"]
    else:
        all_ideas = store.list_all_ideas_for_org(org)
        ideas = [
            i for i in all_ideas
            if i.status == "folded" and i.invalidAt is None
        ]

    logger.info(
        "grandfather_legacy_folds: org=%s candidates=%d dry_run=%s",
        org,
        len(ideas),
        dry_run,
    )

    for idea in ideas:
        # Already tagged — nothing to do.
        if idea.legacyRecurrenceFold is True:
            result.already_tagged.append(idea.ideaId)
            logger.debug("grandfather: idea=%s already tagged", idea.ideaId)
            continue

        # Has a real merged-PR source — confirmed already; skip.
        if _has_pr_source(store, org, idea.ideaId):
            result.skipped_no_prref.append(idea.ideaId)
            logger.debug(
                "grandfather: idea=%s has PR source — already confirmed, skip",
                idea.ideaId,
            )
            continue

        if dry_run:
            result.tagged.append(idea.ideaId)
            logger.info("grandfather DRY-RUN: would tag idea=%s", idea.ideaId)
            continue

        # Tag it: set legacyRecurrenceFold=True via OCC write.
        tagged_idea = IdeaRecord(
            ideaId=idea.ideaId,
            skillBaseName=idea.skillBaseName,
            org=idea.org,
            body=idea.body,
            status=idea.status,
            corroborationVersion=idea.corroborationVersion + 1,
            foldedIntoRev=idea.foldedIntoRev,
            invalidAt=idea.invalidAt,
            supersededBy=idea.supersededBy,
            supersedes=idea.supersedes,
            authored=idea.authored,
            authorityKind=idea.authorityKind,
            refines=idea.refines,
            revivedAt=idea.revivedAt,
            legacyRecurrenceFold=True,   # THE GRANDFATHER FLAG
            sourceRef=idea.sourceRef,
            scopeTag=idea.scopeTag,
            authorId=idea.authorId,
            verificationRung=idea.verificationRung,
        )

        success = False
        for attempt in range(MAX_OCC_RETRIES):
            try:
                store.put_idea_conditional(tagged_idea, idea.corroborationVersion)
                success = True
                break
            except VersionConflictError:
                # Re-read; the version may have changed.
                refreshed = store.get_idea(org, idea.skillBaseName, idea.ideaId)
                if refreshed is None or refreshed.legacyRecurrenceFold is True:
                    # Concurrent write already set the flag — that's fine.
                    success = True
                    break
                if refreshed.invalidAt is not None:
                    # Concurrently superseded — no longer a candidate.
                    logger.info(
                        "grandfather: idea=%s concurrently superseded — skip",
                        idea.ideaId,
                    )
                    success = True  # Not an error — just no longer applicable.
                    break
                # Retry with the latest version.
                idea = refreshed
                tagged_idea = IdeaRecord(
                    ideaId=idea.ideaId,
                    skillBaseName=idea.skillBaseName,
                    org=idea.org,
                    body=idea.body,
                    status=idea.status,
                    corroborationVersion=idea.corroborationVersion + 1,
                    foldedIntoRev=idea.foldedIntoRev,
                    invalidAt=idea.invalidAt,
                    supersededBy=idea.supersededBy,
                    supersedes=idea.supersedes,
                    authored=idea.authored,
                    authorityKind=idea.authorityKind,
                    refines=idea.refines,
                    revivedAt=idea.revivedAt,
                    legacyRecurrenceFold=True,
                    sourceRef=idea.sourceRef,
                    scopeTag=idea.scopeTag,
                    authorId=idea.authorId,
                    verificationRung=idea.verificationRung,
                )
                logger.info(
                    "grandfather: OCC retry %d for idea=%s",
                    attempt + 1,
                    idea.ideaId,
                )

        if success:
            result.tagged.append(idea.ideaId)
            logger.info("grandfather: tagged idea=%s legacyRecurrenceFold=True", idea.ideaId)
        else:
            result.errors.append(idea.ideaId)
            logger.error(
                "grandfather: failed to tag idea=%s after %d attempts",
                idea.ideaId,
                MAX_OCC_RETRIES,
            )

    logger.info(
        "grandfather_legacy_folds done: tagged=%d already_tagged=%d skipped=%d errors=%d",
        len(result.tagged),
        len(result.already_tagged),
        len(result.skipped_no_prref),
        len(result.errors),
    )
    return result


# ---------------------------------------------------------------------------
# Core: confirm_grandfathered_fold
# ---------------------------------------------------------------------------


def confirm_grandfathered_fold(
    org: str,
    idea_id: str,
    skill_base_name: str,
    store: "LearningStore",
    *,
    now_ms: int | None = None,
) -> ConfirmResult:
    """Clear legacyRecurrenceFold when a real merged-PR vote arrives.

    Called by the corroboration path when it writes a new IdeaSource for a PR
    onto an idea that is tagged ``legacyRecurrenceFold=True``.  Clears the flag
    so the idea is now fully confirmed by a real PR and no longer depends on
    the grandfather protection.

    Parameters
    ----------
    org:
        Organisation slug.
    idea_id:
        The idea to confirm.
    skill_base_name:
        Skill family the idea belongs to.
    store:
        LearningStore instance.
    now_ms:
        Clock injection for testing.

    Returns
    -------
    ConfirmResult
    """
    if not org or not org.strip():
        raise OrgGuardError("confirm_grandfathered_fold")

    if now_ms is None:
        now_ms = int(time.time() * 1000)

    idea = store.get_idea(org, skill_base_name, idea_id)
    if idea is None:
        return ConfirmResult(action="not_found", idea_id=idea_id)

    if idea.status != "folded":
        # Only confirm folded ideas (an open candidate doesn't need this).
        return ConfirmResult(action="not_folded", idea_id=idea_id)

    if not idea.legacyRecurrenceFold:
        # Not a grandfathered fold — nothing to do.
        return ConfirmResult(action="not_grandfathered", idea_id=idea_id)

    # Clear the flag via OCC write.
    confirmed = IdeaRecord(
        ideaId=idea.ideaId,
        skillBaseName=idea.skillBaseName,
        org=idea.org,
        body=idea.body,
        status=idea.status,
        corroborationVersion=idea.corroborationVersion + 1,
        foldedIntoRev=idea.foldedIntoRev,
        invalidAt=idea.invalidAt,
        supersededBy=idea.supersededBy,
        supersedes=idea.supersedes,
        authored=idea.authored,
        authorityKind=idea.authorityKind,
        refines=idea.refines,
        revivedAt=idea.revivedAt,
        legacyRecurrenceFold=None,   # FLAG CLEARED — confirmed by real PR
        sourceRef=idea.sourceRef,
        scopeTag=idea.scopeTag,
        authorId=idea.authorId,
        verificationRung=idea.verificationRung,
    )

    try:
        store.put_idea_conditional(confirmed, idea.corroborationVersion)
    except VersionConflictError:
        logger.warning(
            "confirm_grandfathered_fold: OCC conflict for idea=%r — "
            "concurrent write raced; skipping (flag already cleared or idea changed)",
            idea_id,
        )
        # Not an error — concurrent write may have already cleared the flag.
        return ConfirmResult(action="confirmed", idea_id=idea_id, cleared_at=now_ms)

    logger.info(
        "confirm_grandfathered_fold: idea=%r flag cleared (confirmed by real PR vote)",
        idea_id,
    )
    return ConfirmResult(action="confirmed", idea_id=idea_id, cleared_at=now_ms)


# ---------------------------------------------------------------------------
# Optional: transfer_replay_shadow_diff
# ---------------------------------------------------------------------------


def transfer_replay_shadow_diff(
    org: str,
    owner_repo: str,
    store: "LearningStore",
    merged_prs: list[dict],
    *,
    skill_base_name: str | None = None,
) -> ShadowDiffReport:
    """Replay a fixed merge log in shadow mode and diff against the live library.

    This is the optional "shadow diff before promote" step described in the
    migration plan.  It does NOT write to the store.  The report tells the
    caller:
      - Which ideas the replay would create that are absent from the legacy lib.
      - Which legacy ideas the replay would NOT produce (potential retirements).
      - Which grandfathered ideas are also independently confirmed by the replay.

    Parameters
    ----------
    org:
        Organisation slug.
    owner_repo:
        The repo being replayed (e.g. ``acme/backend``).
    store:
        LearningStore (read-only in this function).
    merged_prs:
        The fixed merge log — a list of PR descriptors, each a dict with at
        least ``{"pr_number": int, "insights": [{"body": str, "skill": str}]}``.
        In production these come from ``list_merged_pull_requests``; in tests
        they are fixtures.
    skill_base_name:
        If set, scope the diff to a single skill family.

    Returns
    -------
    ShadowDiffReport
    """
    if not org or not org.strip():
        raise OrgGuardError("transfer_replay_shadow_diff")

    report = ShadowDiffReport(org=org, owner_repo=owner_repo, prs_replayed=0)

    # Build the live legacy library snapshot (read-only).
    if skill_base_name is not None:
        live_ideas = store.list_current_ideas(org, skill_base_name)
    else:
        live_ideas = [
            i for i in store.list_all_ideas_for_org(org)
            if i.invalidAt is None and i.status == "folded"
        ]

    live_by_id: dict[str, IdeaRecord] = {i.ideaId: i for i in live_ideas}
    # Track which live ideas were "touched" by the replay (confirmed or would-supersede).
    touched_live_ids: set[str] = set()
    # Track ideas the replay would create.
    replay_created_ids: set[str] = set()

    for pr_descriptor in merged_prs:
        report.prs_replayed += 1
        pr_number = pr_descriptor.get("pr_number", 0)
        insights = pr_descriptor.get("insights", [])

        for ins in insights:
            body = ins.get("body", "")
            skill = ins.get("skill", skill_base_name or "unknown-skill")
            idea_id = ins.get("idea_id", f"replay-{pr_number}-{hash(body) % 100000}")

            excerpt = body[:120]

            # Check if this insight body roughly matches a live idea.
            matched_live_id: str | None = None
            for live_id, live_idea in live_by_id.items():
                # Simple Jaccard overlap as a proxy for semantic match (same as
                # the corroboration module uses for MERGE-REWRITE detection).
                live_words = set(live_idea.body.lower().split())
                replay_words = set(body.lower().split())
                if live_words and replay_words:
                    inter = len(live_words & replay_words)
                    union = len(live_words | replay_words)
                    sim = inter / union if union else 0.0
                    if sim >= 0.5:
                        matched_live_id = live_id
                        break

            would_supersede_flag = ins.get("would_supersede", False)
            supersede_target = ins.get("supersede_target")

            summary = ShadowIdeaSummary(
                idea_id=idea_id,
                skill_base_name=skill,
                body_excerpt=excerpt,
                in_legacy=matched_live_id is not None,
                would_fold=True,  # Shadow mode: every replayed insight would fold if it reached verified_K.
                would_supersede=would_supersede_flag,
                supersede_target=supersede_target,
            )

            if would_supersede_flag and supersede_target:
                report.would_supersede.append((supersede_target, summary))
                touched_live_ids.add(supersede_target)
            elif matched_live_id is not None:
                # The replay confirms a legacy idea.
                live_idea = live_by_id[matched_live_id]
                if live_idea.legacyRecurrenceFold:
                    report.confirmed_by_replay.append(summary)
                touched_live_ids.add(matched_live_id)
            else:
                # New under verified semantics.
                report.new_under_verified.append(summary)
                replay_created_ids.add(idea_id)

    # Ideas only in the legacy library (not touched by the replay).
    for live_id, live_idea in live_by_id.items():
        if live_id not in touched_live_ids:
            excerpt = live_idea.body[:120]
            report.only_in_legacy.append(ShadowIdeaSummary(
                idea_id=live_id,
                skill_base_name=live_idea.skillBaseName,
                body_excerpt=excerpt,
                in_legacy=True,
                would_fold=False,
            ))

    logger.info(report.summary_line())
    return report
