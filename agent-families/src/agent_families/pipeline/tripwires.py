"""Shadow-mode tripwires and span-cost accounting (plan-002 U7, R16/R17).

Phase 1 tripwires OBSERVE and never kill (DESIGN section 17): per Ralph
iteration the orchestrator hands :class:`ShadowTripwires` an
orchestrator-derived diff summary (never a worker self-summary, plan KTD) and
the iteration's typed failure records, and two detector events are logged to
``tripwire_events`` — the deliverable is the threshold-setting dataset, not an
enforcement mechanism. :meth:`ShadowTripwires.observe_iteration` returns pure
data and raises nothing on a fired detector; callers MUST NOT branch on the
fired flags to stop a loop in Phase 1 (kill mode is a Phase 2+ decision made
FROM this dataset).

Detectors:

- ``diff_similarity``: the diff summary is embedded with the pinned nomic
  model through the Phase 0 :class:`~agent_families.embedding.EmbeddingService`
  (``search_document:`` prefix — indexing-side content, plan U7 approach), and
  the cosine against the SAME ticket's previous-iteration vector is logged
  with ``would_have_fired = similarity >= threshold``. Embedder model and dim
  are recorded on every event because cosine thresholds are not portable
  across embedders (section 17). The first iteration of a ticket has no
  predecessor and logs a NULL-similarity baseline event, so the dataset
  carries every iteration.
- ``no_progress``: the iteration's failure set is reduced to a canonical
  order-independent hash (:func:`canonical_failure_set_hash`);
  ``would_have_fired`` is true when a NON-EMPTY failure set repeats the
  previous iteration's hash exactly (an empty set is progress, not a stall).

Detector state (previous vector / previous hash, keyed per run+ticket) is
in-process only: a resumed orchestrator starts a fresh baseline, which is
honest — shadow events are observations, not durable loop state (R3's
checkpoint document carries nothing tripwire-related).

Tunables discipline: the similarity threshold is caller-supplied with NO
default, per U4's precedent — the Phase 0 config loader is frozen this wave,
so routing the value from ``thresholds.toml`` lands with the units that own
real stage wiring (U6/U8). Nothing in this module hardcodes a tunable.

Accounting (R16): :func:`run_totals` and :func:`ticket_totals` aggregate span
cost/turn/duration fields per run and per ticket from ``trace_span`` — the
same sums the orchestrator's settlement writes into the run row. Sums include
``cost_partial`` spans (killed/orphaned sessions whose usage was accumulated
best-effort from the captured stream) and report their count.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from agent_families.embedding import EmbeddingService
from agent_families.store import Store

DETECTOR_DIFF_SIMILARITY = "diff_similarity"
DETECTOR_NO_PROGRESS = "no_progress"

# The typed-failure identity fields (R13, DESIGN section 7) that make two
# failure sets "the same failures" for the no-progress detector. Row ids and
# timestamps are deliberately excluded: re-recording an identical failure must
# hash identically.
FAILURE_IDENTITY_FIELDS = (
    "failure_kind",
    "location",
    "expected",
    "observed",
    "repro_command",
)


class TripwireError(Exception):
    """Tripwire misuse (bad threshold, malformed inputs) with an actionable message."""


# --- canonical failure-set hashing (no-progress substrate) ---------------------


def canonical_failure_set_hash(failures: Iterable[Mapping]) -> str:
    """Order-independent sha256 over a set of typed failure records.

    Each record (a mapping or sqlite3.Row carrying the R13 fields) is reduced
    to canonical JSON over :data:`FAILURE_IDENTITY_FIELDS`; the encoded records
    are sorted before hashing, so reordering the same failures yields the same
    hash and any change to the set yields a different one.
    """
    canonical = sorted(
        json.dumps(
            {field: str(dict(record).get(field, "")) for field in FAILURE_IDENTITY_FIELDS},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        for record in failures
    )
    return hashlib.sha256("\n".join(canonical).encode("utf-8")).hexdigest()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# --- the shadow observer ---------------------------------------------------------


@dataclass(frozen=True)
class IterationObservation:
    """What one observed iteration logged — data for tests/reports, never a kill
    signal (Phase 1 shadow discipline)."""

    diff_event_id: int
    no_progress_event_id: int
    similarity: float | None
    diff_would_have_fired: bool
    failure_set_hash: str
    no_progress_would_have_fired: bool


class ShadowTripwires:
    """Per-iteration shadow detectors writing the section-17 threshold dataset.

    One instance per orchestrator process; state is keyed per (run, ticket) so
    interleaved tickets never cross-contaminate each other's baselines.
    """

    def __init__(
        self,
        store: Store,
        embedder: EmbeddingService,
        *,
        similarity_threshold: float,
    ) -> None:
        if not (0.0 <= float(similarity_threshold) <= 1.0):
            raise TripwireError(
                "similarity_threshold must be a cosine in [0.0, 1.0],"
                f" got {similarity_threshold} (tunable; route it from"
                " thresholds.toml at the call site — never hardcode it)"
            )
        self.store = store
        self.embedder = embedder
        self.similarity_threshold = float(similarity_threshold)
        self._prev_vector: dict[tuple[int, str], list[float]] = {}
        self._prev_hash: dict[tuple[int, str], str] = {}

    def observe_iteration(
        self,
        *,
        run_id: int,
        ticket_id: str,
        ralph_iteration: int,
        diff_summary: str,
        failures: Iterable[Mapping] = (),
        span_id: str | None = None,
    ) -> IterationObservation:
        """Log both detectors for one completed Ralph iteration; never raises
        on a fired detector and never kills anything (R17 shadow mode)."""
        key = (int(run_id), ticket_id)

        # Detector 1: consecutive-iteration diff similarity. The summary is
        # orchestrator-derived (mechanical, from the iteration diff) — workers
        # are untrusted producers and never describe their own progress.
        vector = self.embedder.embed_document(diff_summary)
        previous = self._prev_vector.get(key)
        similarity = _cosine(previous, vector) if previous is not None else None
        diff_fired = (
            similarity is not None and similarity >= self.similarity_threshold
        )
        diff_event_id = self.store.insert_tripwire_event(
            detector_kind=DETECTOR_DIFF_SIMILARITY,
            would_have_fired=diff_fired,
            span_id=span_id,
            run_id=run_id,
            ralph_iteration=ralph_iteration,
            similarity=similarity,
            embedding_model=self.embedder.model_name,
            embedding_dim=self.embedder.dim,
        )
        self._prev_vector[key] = vector

        # Detector 2: no progress — the same non-empty failure set, twice in a
        # row. An empty set never fires (no failures IS progress).
        failure_set_hash = canonical_failure_set_hash(list(failures))
        had_failures = failure_set_hash != canonical_failure_set_hash(())
        no_progress_fired = (
            had_failures and self._prev_hash.get(key) == failure_set_hash
        )
        no_progress_event_id = self.store.insert_tripwire_event(
            detector_kind=DETECTOR_NO_PROGRESS,
            would_have_fired=no_progress_fired,
            span_id=span_id,
            run_id=run_id,
            ralph_iteration=ralph_iteration,
            failure_set_hash=failure_set_hash,
        )
        self._prev_hash[key] = failure_set_hash

        return IterationObservation(
            diff_event_id=diff_event_id,
            no_progress_event_id=no_progress_event_id,
            similarity=similarity,
            diff_would_have_fired=diff_fired,
            failure_set_hash=failure_set_hash,
            no_progress_would_have_fired=no_progress_fired,
        )


# --- accounting (R16) --------------------------------------------------------------


@dataclass(frozen=True)
class SpanTotals:
    """Aggregated span cost/turn fields; ``cost_partial_spans`` counts spans
    whose usage was accumulated best-effort from a killed/orphaned stream."""

    cost_usd: float | None
    input_tokens: int | None
    output_tokens: int | None
    num_turns: int | None
    duration_ms: int | None
    cost_partial_spans: int


_TOTALS_SELECT = (
    "SUM(cost_usd) AS cost_usd,"
    " SUM(input_tokens) AS input_tokens,"
    " SUM(output_tokens) AS output_tokens,"
    " SUM(num_turns) AS num_turns,"
    " SUM(duration_ms) AS duration_ms,"
    " SUM(CASE WHEN cost_partial = 1 THEN 1 ELSE 0 END) AS partials"
)


def _totals_from_row(row) -> SpanTotals:
    return SpanTotals(
        cost_usd=row["cost_usd"],
        input_tokens=row["input_tokens"],
        output_tokens=row["output_tokens"],
        num_turns=row["num_turns"],
        duration_ms=row["duration_ms"],
        cost_partial_spans=row["partials"] or 0,
    )


def run_totals(store: Store, run_id: int) -> SpanTotals:
    """A run's span cost/turn sums — what settlement writes into the run row."""
    row = store.conn.execute(
        f"SELECT {_TOTALS_SELECT} FROM trace_span WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return _totals_from_row(row)


def ticket_totals(store: Store, run_id: int) -> dict[str | None, SpanTotals]:
    """Per-ticket span aggregation for a run (R16 "per ticket and per run").

    Spans without a ticket (planner, run-level judges) group under ``None`` so
    the per-ticket breakdown always sums to :func:`run_totals`.
    """
    rows = store.conn.execute(
        f"SELECT ticket_id, {_TOTALS_SELECT} FROM trace_span"
        " WHERE run_id = ? GROUP BY ticket_id",
        (run_id,),
    ).fetchall()
    return {row["ticket_id"]: _totals_from_row(row) for row in rows}
