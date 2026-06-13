"""telemetry.py — R4: Shadow→enforce calibration metrics (MAT-153).

Emits per-skill/org calibration metrics that the mode-flip enforce gates read.
All metrics are computed from existing store data + in-memory event accumulators.

Metric groups
-------------

1. **Corroboration weight distribution** (per skill/org):
   - ``corroboration_weight_distribution``: histogram of idea corroborationWeights.
   - ``per_author_credibility``: map of authorId → credibility weight (user-curated).
   - ``distinct_pr_distribution``: histogram of distinct-PR counts per idea.

2. **Anchor resolution rate** (per skill/org):
   - ``symbol_anchor_count``: int — anchor records resolved to a symbol (not __file__).
   - ``file_fallback_count``: int — anchor records falling back to __file__.
   - ``symbol_resolution_rate``: float [0, 1] — symbol / (symbol + file).

3. **Supersede / un-fold calibration** (per skill/org):
   - ``supersede_event_count``: int — verified supersessions executed.
   - ``unfold_count``: int — un-fold operations (folded idea retired).
   - ``supersede_fp_rate``: float — confirmed FPs / total spot-checked verdicts.
   - ``supersede_fp_sample_count``: int — how many verdicts have been spot-checked.

4. **Revive + necessity**:
   - ``revive_count``: int — ideas revived after supersession.
   - ``necessity_demotion_count``: int — ideas demoted by the necessity scan.

5. **NLI move distribution** (per classifier call batch):
   - ``nli_move_distribution``: dict mapping verdict → count.
   - ``judge_fallback_count``: int — low-confidence verdicts routed to the judge.

6. **Authored-vs-inferred ratio**:
   - ``authored_count``: int — authored ideas (user_directive + authored_import).
   - ``inferred_count``: int — inferred ideas (merged).
   - ``authored_inferred_ratio``: float — authored / total (0.0 if total == 0).

Enforce gate reads
------------------
The enforce gates for ``verified_learning_mode``, ``supersede_mode``, and
``unfold_mode`` use :func:`gate_status` to decide whether telemetry allows
flipping from *shadow* → *enforce*.  Conditions (per the plan):

- ``verified_learning_mode`` → enforce when symbol_resolution_rate is **measured**
  (>= ``min_anchor_samples`` have been recorded).
- ``supersede_mode`` / ``unfold_mode`` → enforce when
  supersede_fp_sample_count >= ``supersede_fp_min_samples`` AND
  supersede_fp_rate < ``supersede_fp_ceiling``.

Usage
-----

  # During corroboration / ingest:
  acc = TelemetryAccumulator()
  acc.record_corroboration_weight(idea_id, weight=0.6)
  acc.record_anchor_resolution(resolved_as_symbol=True)
  acc.record_nli_verdict("supersede", low_confidence=False)

  # During supersession:
  acc.record_supersede_event(unfold_executed=True)
  acc.record_supersede_fp_check("v#001", is_false_positive=False)

  # After revive / necessity:
  acc.record_revive()
  acc.record_necessity_demotion()

  # Snapshot the metrics:
  snapshot = acc.snapshot()
  gate = gate_status(snapshot, config=GateConfig())

  # Retrieve per-org metrics from the store:
  org_metrics = compute_org_metrics(org="acme", skill_base_name="style", store=store,
                                    credibility_store=cred_store)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from learning_service.db.store import LearningStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class GateConfig:
    """Thresholds that determine when telemetry gates allow enforce mode."""

    # Supersession FP gate (mirrors U5 constants).
    supersede_fp_ceiling: float = 0.10          # FP rate must be < 10 %
    supersede_fp_min_samples: int = 30           # over >= 30 spot-checked verdicts

    # Minimum anchor-resolution samples before the symbol-rate signal is trusted.
    min_anchor_samples: int = 10


# ---------------------------------------------------------------------------
# Metric snapshot (immutable, serialisable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorResolutionMetrics:
    """Counts of symbol-level vs file-fallback anchor resolutions."""
    symbol_count: int = 0
    file_fallback_count: int = 0

    @property
    def total(self) -> int:
        return self.symbol_count + self.file_fallback_count

    @property
    def symbol_resolution_rate(self) -> float:
        """Fraction of anchors resolved to a named symbol (not __file__)."""
        if self.total == 0:
            return 0.0
        return self.symbol_count / self.total


@dataclass(frozen=True)
class CorroborationWeightMetrics:
    """Distribution of corroboration weights and distinct-PR counts."""
    # List of (idea_id, weight) pairs — not frozen by idea; just the values.
    weights: tuple[float, ...] = ()
    # List of distinct-PR counts — one per idea.
    distinct_pr_counts: tuple[int, ...] = ()

    @property
    def mean_weight(self) -> float:
        if not self.weights:
            return 0.0
        return sum(self.weights) / len(self.weights)

    @property
    def max_weight(self) -> float:
        if not self.weights:
            return 0.0
        return max(self.weights)

    @property
    def mean_distinct_prs(self) -> float:
        if not self.distinct_pr_counts:
            return 0.0
        return sum(self.distinct_pr_counts) / len(self.distinct_pr_counts)


@dataclass(frozen=True)
class SupersedeMetrics:
    """Supersession + FP calibration metrics."""
    supersede_event_count: int = 0
    unfold_count: int = 0
    supersede_fp_rate: float = 0.0         # confirmed FPs / total spot-checked
    supersede_fp_sample_count: int = 0     # total spot-checked verdicts


@dataclass(frozen=True)
class NliMoveMetrics:
    """NLI verdict distribution + judge-fallback counts."""
    corroborate: int = 0
    refine: int = 0
    supersede: int = 0
    neutral: int = 0
    judge_fallback_count: int = 0          # low-confidence → routed to judge

    @property
    def total(self) -> int:
        return self.corroborate + self.refine + self.supersede + self.neutral

    def distribution(self) -> dict[str, float]:
        """Return fraction of each verdict over all recorded NLI calls."""
        t = self.total
        if t == 0:
            return {
                "corroborate": 0.0,
                "refine": 0.0,
                "supersede": 0.0,
                "neutral": 0.0,
            }
        return {
            "corroborate": self.corroborate / t,
            "refine": self.refine / t,
            "supersede": self.supersede / t,
            "neutral": self.neutral / t,
        }

    @property
    def judge_fallback_rate(self) -> float:
        if self.total == 0:
            return 0.0
        return self.judge_fallback_count / self.total


@dataclass(frozen=True)
class AuthoredInferredMetrics:
    """Authored vs inferred idea counts."""
    authored_count: int = 0
    inferred_count: int = 0

    @property
    def total(self) -> int:
        return self.authored_count + self.inferred_count

    @property
    def authored_inferred_ratio(self) -> float:
        """Fraction of ideas that are authored.  0.0 when no ideas."""
        if self.total == 0:
            return 0.0
        return self.authored_count / self.total


@dataclass(frozen=True)
class TelemetrySnapshot:
    """Immutable snapshot of all R4 calibration metrics for one accumulator scope."""

    # Per-author credibility map (authorId → weight) — mutable dict, but stored
    # as a tuple of (k, v) pairs so the snapshot is hashable/frozen.
    per_author_credibility: tuple[tuple[str, float], ...] = ()

    corroboration: CorroborationWeightMetrics = field(
        default_factory=CorroborationWeightMetrics
    )
    anchor: AnchorResolutionMetrics = field(
        default_factory=AnchorResolutionMetrics
    )
    supersede: SupersedeMetrics = field(
        default_factory=SupersedeMetrics
    )
    revive_count: int = 0
    necessity_demotion_count: int = 0
    nli: NliMoveMetrics = field(
        default_factory=NliMoveMetrics
    )
    authored_inferred: AuthoredInferredMetrics = field(
        default_factory=AuthoredInferredMetrics
    )

    def author_credibility_map(self) -> dict[str, float]:
        return dict(self.per_author_credibility)


# ---------------------------------------------------------------------------
# Enforce gate decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateStatus:
    """Whether each mode-flip gate allows enforce, and why."""

    # verified_learning_mode gate: symbol resolution rate is measured.
    verified_learning_gate_open: bool = False
    verified_learning_reason: str = ""

    # supersede_mode gate: FP rate < ceiling over >= min_samples.
    supersede_gate_open: bool = False
    supersede_reason: str = ""

    # unfold_mode gate: same as supersede_gate (un-fold requires the supersede
    # gate to pass first — the plan says "un-fold enforce last").
    unfold_gate_open: bool = False
    unfold_reason: str = ""


def gate_status(snapshot: TelemetrySnapshot, *, config: GateConfig | None = None) -> GateStatus:
    """Evaluate whether each mode-flip gate allows enforce.

    Parameters
    ----------
    snapshot:
        The current telemetry snapshot.
    config:
        Gate thresholds; defaults to GateConfig() if not provided.

    Returns
    -------
    GateStatus
        A frozen dataclass with one bool + reason per gate.
    """
    cfg = config or GateConfig()

    # --- verified_learning_mode gate ---
    anchor = snapshot.anchor
    if anchor.total >= cfg.min_anchor_samples:
        vl_open = True
        vl_reason = (
            f"anchor samples={anchor.total} >= {cfg.min_anchor_samples}; "
            f"symbol_resolution_rate={anchor.symbol_resolution_rate:.3f}"
        )
    else:
        vl_open = False
        vl_reason = (
            f"anchor samples={anchor.total} < {cfg.min_anchor_samples} "
            f"(need {cfg.min_anchor_samples - anchor.total} more)"
        )

    # --- supersede_mode gate ---
    sup = snapshot.supersede
    if sup.supersede_fp_sample_count < cfg.supersede_fp_min_samples:
        sup_open = False
        sup_reason = (
            f"fp_sample_count={sup.supersede_fp_sample_count} < "
            f"{cfg.supersede_fp_min_samples} (need "
            f"{cfg.supersede_fp_min_samples - sup.supersede_fp_sample_count} more spot-checks)"
        )
    elif sup.supersede_fp_rate >= cfg.supersede_fp_ceiling:
        sup_open = False
        sup_reason = (
            f"fp_rate={sup.supersede_fp_rate:.3f} >= ceiling={cfg.supersede_fp_ceiling:.3f}; "
            f"FP rate too high — stay in shadow"
        )
    else:
        sup_open = True
        sup_reason = (
            f"fp_rate={sup.supersede_fp_rate:.3f} < ceiling={cfg.supersede_fp_ceiling:.3f} "
            f"over {sup.supersede_fp_sample_count} samples — gate open"
        )

    # --- unfold_mode gate ---
    # Un-fold enforce is contingent on the supersede gate passing.
    if not sup_open:
        unfold_open = False
        unfold_reason = f"unfold_mode blocked: supersede gate not open ({sup_reason})"
    else:
        unfold_open = True
        unfold_reason = f"unfold_mode open: supersede gate passed ({sup_reason})"

    logger.info(
        "gate_status: vl=%s sup=%s unfold=%s",
        vl_open,
        sup_open,
        unfold_open,
    )

    return GateStatus(
        verified_learning_gate_open=vl_open,
        verified_learning_reason=vl_reason,
        supersede_gate_open=sup_open,
        supersede_reason=sup_reason,
        unfold_gate_open=unfold_open,
        unfold_reason=unfold_reason,
    )


# ---------------------------------------------------------------------------
# Mutable accumulator (in-process, one per ingest run or Lambda invocation)
# ---------------------------------------------------------------------------


class TelemetryAccumulator:
    """Collects telemetry events during an ingest run or across calls.

    Thread-safety: not thread-safe.  Use one accumulator per Lambda invocation
    (each invocation is single-threaded).

    All record_*() methods are **idempotent within a call** — they simply
    append counts; no dedup is performed here.  The caller is responsible for
    not double-counting (e.g. by checking idempotency at the PR level before
    calling record_corroboration_weight).
    """

    def __init__(self) -> None:
        # Corroboration weights (idea_id → weight)
        self._weights: list[float] = []
        self._distinct_pr_counts: list[int] = []

        # Per-author credibility (authorId → latest weight recorded)
        self._per_author_credibility: dict[str, float] = {}

        # Anchor resolution
        self._symbol_count: int = 0
        self._file_fallback_count: int = 0

        # Supersession
        self._supersede_events: int = 0
        self._unfold_count: int = 0
        # Spot-check records: {verdict_id: is_tp (True=TP, False=FP)}
        self._fp_spot_checks: dict[str, bool] = {}

        # Revive + necessity
        self._revive_count: int = 0
        self._necessity_demotion_count: int = 0

        # NLI verdicts
        self._nli_corroborate: int = 0
        self._nli_refine: int = 0
        self._nli_supersede: int = 0
        self._nli_neutral: int = 0
        self._judge_fallback_count: int = 0

        # Authored vs inferred
        self._authored_count: int = 0
        self._inferred_count: int = 0

    # ------------------------------------------------------------------
    # Record helpers
    # ------------------------------------------------------------------

    def record_corroboration_weight(
        self, weight: float, *, distinct_pr_count: int = 0
    ) -> None:
        """Record the corroboration weight for one idea (snapshot after a vote)."""
        self._weights.append(weight)
        self._distinct_pr_counts.append(distinct_pr_count)

    def record_author_credibility(self, author_id: str, credibility: float) -> None:
        """Record (or overwrite) the credibility weight for an author."""
        self._per_author_credibility[author_id] = credibility

    def record_anchor_resolution(self, *, resolved_as_symbol: bool) -> None:
        """Record one anchor resolution event.

        Parameters
        ----------
        resolved_as_symbol:
            True when tree-sitter resolved the hunk to a named symbol;
            False when it fell back to the file-level sentinel.
        """
        if resolved_as_symbol:
            self._symbol_count += 1
        else:
            self._file_fallback_count += 1

    def record_supersede_event(self, *, unfold_executed: bool) -> None:
        """Record one supersession event.

        Parameters
        ----------
        unfold_executed:
            True when an un-fold skill-revision was actually authored (enforce
            mode); False in shadow mode (decision logged but no write).
        """
        self._supersede_events += 1
        if unfold_executed:
            self._unfold_count += 1

    def record_supersede_fp_check(self, verdict_id: str, *, is_false_positive: bool) -> None:
        """Record a human-spot-checked supersession verdict.

        Parameters
        ----------
        verdict_id:
            A stable ID for the spot-checked decision (e.g. "idea#<id>_pr#<n>").
        is_false_positive:
            True when a human confirmed the supersession was wrong.
        """
        self._fp_spot_checks[verdict_id] = not is_false_positive  # True = TP

    def record_revive(self) -> None:
        """Record one revive event (a superseded idea was revived)."""
        self._revive_count += 1

    def record_necessity_demotion(self) -> None:
        """Record one necessity demote event."""
        self._necessity_demotion_count += 1

    def record_nli_verdict(self, verdict: str, *, low_confidence: bool = False) -> None:
        """Record one NLI classification call.

        Parameters
        ----------
        verdict:
            One of "corroborate" | "refine" | "supersede" | "neutral".
        low_confidence:
            True when the raw NLI confidence was below the threshold — the
            verdict was downgraded to refine and the judge fallback was invoked.
        """
        v = verdict.lower()
        if v == "corroborate":
            self._nli_corroborate += 1
        elif v == "refine":
            self._nli_refine += 1
        elif v == "supersede":
            self._nli_supersede += 1
        else:
            self._nli_neutral += 1

        if low_confidence:
            self._judge_fallback_count += 1

    def record_idea_authority(self, authority_kind: str) -> None:
        """Record one idea by its authority kind.

        Authored kinds: user_directive, authored_import.
        Inferred kind: merged.
        """
        if authority_kind in ("user_directive", "authored_import"):
            self._authored_count += 1
        else:
            self._inferred_count += 1

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> TelemetrySnapshot:
        """Return an immutable snapshot of the current accumulated metrics."""
        # FP rate computation.
        n_checks = len(self._fp_spot_checks)
        if n_checks == 0:
            fp_rate = 0.0
        else:
            n_fps = sum(1 for is_tp in self._fp_spot_checks.values() if not is_tp)
            fp_rate = n_fps / n_checks

        return TelemetrySnapshot(
            per_author_credibility=tuple(sorted(self._per_author_credibility.items())),
            corroboration=CorroborationWeightMetrics(
                weights=tuple(self._weights),
                distinct_pr_counts=tuple(self._distinct_pr_counts),
            ),
            anchor=AnchorResolutionMetrics(
                symbol_count=self._symbol_count,
                file_fallback_count=self._file_fallback_count,
            ),
            supersede=SupersedeMetrics(
                supersede_event_count=self._supersede_events,
                unfold_count=self._unfold_count,
                supersede_fp_rate=fp_rate,
                supersede_fp_sample_count=n_checks,
            ),
            revive_count=self._revive_count,
            necessity_demotion_count=self._necessity_demotion_count,
            nli=NliMoveMetrics(
                corroborate=self._nli_corroborate,
                refine=self._nli_refine,
                supersede=self._nli_supersede,
                neutral=self._nli_neutral,
                judge_fallback_count=self._judge_fallback_count,
            ),
            authored_inferred=AuthoredInferredMetrics(
                authored_count=self._authored_count,
                inferred_count=self._inferred_count,
            ),
        )

    def reset(self) -> None:
        """Clear all counters (start a new accumulation window)."""
        self.__init__()  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Store-derived metrics (computed from LearningStore at read time)
# ---------------------------------------------------------------------------


def compute_org_metrics(
    org: str,
    skill_base_name: str,
    store: "LearningStore",
    credibility_store: "object | None" = None,
) -> dict:
    """Compute telemetry metrics for a skill family from the store.

    Returns a dict suitable for logging / dashboard output.  Does NOT modify
    any store state — read-only.

    Parameters
    ----------
    org:
        Org slug to compute metrics for.
    skill_base_name:
        Skill family name.
    store:
        LearningStore to query.
    credibility_store:
        Optional AuthorCredibilityStore; if provided, the per-author credibility
        map is included in the output.

    Returns
    -------
    dict
        Keys match the R4 metric spec (snake_case).
    """
    from learning_service.corroboration import (
        AuthorCredibilityStore,
        corroboration_weight,
        distinct_pr_count,
    )

    # Fetch all ideas (including history) for the skill family.
    all_org_ideas = store.list_all_ideas_for_org(org)
    skill_ideas = [i for i in all_org_ideas if i.skillBaseName == skill_base_name]

    # Authored vs inferred.
    authored_kinds = {"user_directive", "authored_import"}
    authored = [i for i in skill_ideas if (i.authorityKind or "") in authored_kinds or i.authored]
    inferred = [i for i in skill_ideas if i not in authored]
    total_ideas = len(skill_ideas)

    # Corroboration weights (over all ideas in the family).
    if credibility_store is None:
        cred_store = AuthorCredibilityStore()
    else:
        cred_store = credibility_store  # type: ignore[assignment]

    weights: list[float] = []
    distinct_pr_counts: list[int] = []
    per_author: dict[str, float] = {}

    for idea in skill_ideas:
        sources = store.list_idea_sources(org, idea.ideaId)
        w = corroboration_weight(sources, cred_store)
        weights.append(w)
        distinct_pr_counts.append(distinct_pr_count(sources))

        # Collect per-author credibilities.
        for src in sources:
            if src.authorId:
                cred = cred_store.get(src.authorId)
                per_author[src.authorId] = cred

    # Supersession metrics.
    supersede_events = 0
    unfold_count = 0
    revive_count = 0
    for idea in skill_ideas:
        events = store.list_verify_events(org, idea.ideaId)
        for evt in events:
            if evt.verdict == "supersede":
                supersede_events += 1
                if idea.foldedIntoRev is not None:
                    # Heuristic: if the idea was folded when superseded it triggered an unfold.
                    unfold_count += 1
            elif evt.verdict == "revive":
                revive_count += 1

    # Necessity demotions = ideas retired with invalidAt but no supersededBy.
    necessity_demotions = sum(
        1 for i in skill_ideas
        if i.invalidAt is not None and not i.supersededBy
    )

    return {
        "org": org,
        "skill_base_name": skill_base_name,
        # Corroboration
        "idea_count": total_ideas,
        "corroboration_weight_mean": (sum(weights) / len(weights)) if weights else 0.0,
        "corroboration_weight_max": max(weights) if weights else 0.0,
        "corroboration_weight_distribution": sorted(weights),
        "distinct_pr_count_mean": (
            sum(distinct_pr_counts) / len(distinct_pr_counts)
        ) if distinct_pr_counts else 0.0,
        "distinct_pr_distribution": sorted(distinct_pr_counts),
        "per_author_credibility": per_author,
        # Supersession
        "supersede_event_count": supersede_events,
        "unfold_count": unfold_count,
        "revive_count": revive_count,
        # Necessity
        "necessity_demotion_count": necessity_demotions,
        # Authored vs inferred
        "authored_count": len(authored),
        "inferred_count": len(inferred),
        "authored_inferred_ratio": len(authored) / total_ideas if total_ideas else 0.0,
    }


def to_json_dict(snapshot: TelemetrySnapshot) -> dict:
    """Serialise a TelemetrySnapshot to a plain JSON-safe dict.

    Suitable for CloudWatch, logging, or a dashboard endpoint.
    """
    return {
        "per_author_credibility": dict(snapshot.per_author_credibility),
        "corroboration": {
            "weight_distribution": list(snapshot.corroboration.weights),
            "mean_weight": snapshot.corroboration.mean_weight,
            "max_weight": snapshot.corroboration.max_weight,
            "distinct_pr_distribution": list(snapshot.corroboration.distinct_pr_counts),
            "mean_distinct_prs": snapshot.corroboration.mean_distinct_prs,
        },
        "anchor": {
            "symbol_count": snapshot.anchor.symbol_count,
            "file_fallback_count": snapshot.anchor.file_fallback_count,
            "total": snapshot.anchor.total,
            "symbol_resolution_rate": snapshot.anchor.symbol_resolution_rate,
        },
        "supersede": {
            "supersede_event_count": snapshot.supersede.supersede_event_count,
            "unfold_count": snapshot.supersede.unfold_count,
            "supersede_fp_rate": snapshot.supersede.supersede_fp_rate,
            "supersede_fp_sample_count": snapshot.supersede.supersede_fp_sample_count,
        },
        "revive_count": snapshot.revive_count,
        "necessity_demotion_count": snapshot.necessity_demotion_count,
        "nli": {
            "corroborate": snapshot.nli.corroborate,
            "refine": snapshot.nli.refine,
            "supersede": snapshot.nli.supersede,
            "neutral": snapshot.nli.neutral,
            "total": snapshot.nli.total,
            "judge_fallback_count": snapshot.nli.judge_fallback_count,
            "judge_fallback_rate": snapshot.nli.judge_fallback_rate,
            "distribution": snapshot.nli.distribution(),
        },
        "authored_inferred": {
            "authored_count": snapshot.authored_inferred.authored_count,
            "inferred_count": snapshot.authored_inferred.inferred_count,
            "total": snapshot.authored_inferred.total,
            "authored_inferred_ratio": snapshot.authored_inferred.authored_inferred_ratio,
        },
    }
