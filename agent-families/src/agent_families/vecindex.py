"""Vector index: sqlite-vec vec0 storage and KNN with status-visibility joins.

The vec0 virtual table lives in the same DB file as the relational schema so a
vector row commits in the same transaction as its insight row — that one fact is
what makes R7's registration atomicity a one-line rule. Inserts therefore refuse
to run outside :meth:`Store.transaction`.

Visibility (R13) is a query-time join, never a vec mutation: lifecycle operations
flip insight status and the join picks it up; vec rows are written once at
registration and never deleted or rewritten. ``statuses=None`` is the add-idea
dedup view (all statuses, including retired — that is what triggers R14's
revive-or-override prompt); narrower views pass an explicit status tuple.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from agent_families.store import Store

VEC_TABLE = "insight_vectors"


class VecIndexError(Exception):
    """Raised on index misuse (dim drift, out-of-transaction insert, missing ext)."""


@dataclass(frozen=True)
class Neighbor:
    insight_id: int
    distance: float  # cosine distance: 0 = identical direction, 1 = orthogonal
    status: str


class VecIndex:
    """vec0-backed KNN over insight embeddings, joined to insights for status."""

    def __init__(self, store: Store, dim: int) -> None:
        self.store = store
        self.dim = int(dim)
        self._load_extension()

    # --- setup -----------------------------------------------------------------

    def _load_extension(self) -> None:
        try:
            import sqlite_vec
        except ImportError as exc:
            raise VecIndexError(
                "sqlite-vec is not installed; run `uv sync` in agent-families/."
            ) from exc
        conn = self.store.conn
        try:
            conn.execute("SELECT vec_version()")
            return  # already loaded on this connection
        except sqlite3.OperationalError:
            pass
        try:
            conn.enable_load_extension(True)
        except AttributeError as exc:
            raise VecIndexError(
                "this Python's sqlite3 module was built without loadable-extension"
                " support, which sqlite-vec requires."
            ) from exc
        try:
            sqlite_vec.load(conn)
        finally:
            conn.enable_load_extension(False)

    def migrate(self) -> None:
        """Create the vec0 table if absent; dim is fixed at creation (pinned, R22)."""
        self.store.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
            "  insight_id INTEGER PRIMARY KEY,"
            f"  embedding float[{self.dim}] distance_metric=cosine"
            ")"
        )

    # --- writes ------------------------------------------------------------------

    def insert(self, insight_id: int, vector) -> None:
        """Store an insight's embedding; must share the insight row's transaction (R7)."""
        if not self.store.in_transaction:
            raise VecIndexError(
                "VecIndex.insert must run inside Store.transaction() — the vec row"
                " commits atomically with its insight row (R7)."
            )
        import sqlite_vec

        self._check_dim(vector)
        self.store.conn.execute(
            f"INSERT INTO {VEC_TABLE} (insight_id, embedding) VALUES (?, ?)",
            (insight_id, sqlite_vec.serialize_float32(list(vector))),
        )

    # --- reads --------------------------------------------------------------------

    def knn(
        self,
        vector,
        k: int,
        statuses: tuple[str, ...] | None = None,
    ) -> list[Neighbor]:
        """K nearest neighbors by cosine distance, joined to insights for status.

        The status filter applies AFTER the KNN takes its k, so filtered views can
        return fewer than k rows; callers needing exact-k-after-filter over-fetch
        (Phase 1+ runtime-retrieval seam — Phase 0's only ANN caller is add-idea,
        which wants the unfiltered dedup view anyway).
        """
        import sqlite_vec

        self._check_dim(vector)
        sql = (
            "SELECT v.insight_id AS insight_id, v.distance AS distance,"
            "       i.status AS status"
            f" FROM (SELECT insight_id, distance FROM {VEC_TABLE}"
            "        WHERE embedding MATCH ? AND k = ?"
            "        ORDER BY distance) v"
            " JOIN insights i ON i.id = v.insight_id"
        )
        params: list = [sqlite_vec.serialize_float32(list(vector)), int(k)]
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" WHERE i.status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY v.distance ASC, v.insight_id ASC"
        rows = self.store.conn.execute(sql, params).fetchall()
        return [
            Neighbor(
                insight_id=row["insight_id"],
                distance=float(row["distance"]),
                status=row["status"],
            )
            for row in rows
        ]

    def count(self) -> int:
        """Total vec rows; lifecycle ops must never change this (R13 invariant)."""
        row = self.store.conn.execute(f"SELECT COUNT(*) AS n FROM {VEC_TABLE}").fetchone()
        return row["n"]

    # --- internals -------------------------------------------------------------------

    def _check_dim(self, vector) -> None:
        if len(vector) != self.dim:
            raise VecIndexError(
                f"vector has {len(vector)} dims but the index is pinned to"
                f" {self.dim} (thresholds.toml [embedding] dim)."
            )
