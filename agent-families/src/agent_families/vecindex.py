"""Vector index: sqlite-vec vec0 storage and KNN with status-visibility joins.

The vec0 virtual table lives in the same DB file as the relational schema so a
vector row commits in the same transaction as its insight row — that one fact is
what makes R7's registration atomicity a one-line rule. Inserts therefore refuse
to run outside :meth:`Store.transaction`.

**Three-vector shape (R5/R8).** Each insight owns one row carrying its clustering
vectors in a single multi-column vec0 table, so every vector of an insight
commits in the same transaction (the R7 atomicity invariant — no half-written
insight is reachable). The columns:

- ``key_embedding`` — the clustering vector over the rule's *identity*
  (precondition + action). ``knn(on="key")`` collides contradictions on the rule
  they share, which is what lets NLI separate a duplicate from a negation.
- ``full_embedding`` — the clustering vector over the whole atom.
- ``embedding`` — the legacy search-document / retrieval vector, exposed as
  ``knn(on="retrieval")``. (The dedicated v1 retrieval column is deferred to
  plan 010; until then this legacy column carries the retrieval vector and keeps
  the demoted retrieval/maintenance readers working — demote, never drop, R6.)

Visibility (R13) is a query-time join, never a vec mutation: lifecycle operations
flip insight status and the join picks it up; vec rows are written once at
registration and never deleted or rewritten. ``statuses=None`` is the add-idea
dedup view (all statuses, including retired — that is what triggers R14's
revive-or-override prompt); narrower views pass an explicit status tuple.

The vec0 KNN query uses ``LIMIT`` inside the subquery rather than the ``k = ?``
form: with an outer ``ORDER BY`` and a join, SQLite's query flattener merges the
outer ordering into the vec0 subquery and vec0 rejects the resulting second
``ORDER BY distance``. A ``LIMIT``-carrying subquery cannot be flattened into a
join, so the ordering stays put. (This is the documented flattener fix that the
deleted ``pipeline._knn_dedup_view`` helper worked around out-of-band.)
"""

from __future__ import annotations

import sqlite3
import struct
from dataclasses import dataclass

from agent_families.store import Store

VEC_TABLE = "insight_vectors"

# on= → physical column. Validated allow-list: the column is NEVER interpolated
# from caller input, only chosen from this map (no SQL injection surface, R8).
_VIEW_COLUMN = {
    "key": "key_embedding",
    "full": "full_embedding",
    "retrieval": "embedding",
}


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
        """Create the multi-column vec0 table if absent; dim is fixed at creation."""
        self.store.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
            "  insight_id INTEGER PRIMARY KEY,"
            f"  embedding float[{self.dim}] distance_metric=cosine,"
            f"  key_embedding float[{self.dim}] distance_metric=cosine,"
            f"  full_embedding float[{self.dim}] distance_metric=cosine"
            ")"
        )

    # --- writes ------------------------------------------------------------------

    def insert(
        self,
        insight_id: int,
        key_vector,
        full_vector=None,
        retrieval_vector=None,
    ) -> None:
        """Store an insight's vectors atomically with its insight row (R5/R7).

        New (R3) form — ``insert(id, key_vector, full_vector)`` — writes the key
        and full clustering vectors; the retrieval column falls back to the full
        vector unless ``retrieval_vector`` is given (the dedicated retrieval
        vector arrives in plan 010).

        Legacy single-vector form — ``insert(id, vector)`` (``full_vector`` left
        ``None``) — fans the one vector into all three columns. The pre-R3 write
        path and the deferred retrieval/maintenance readers still call this form;
        it is preserved until the pipeline is rewritten to embed key+full (plan
        008 U6).

        vec0 requires every declared column non-NULL per row, so all three are
        always written. The dims are validated *before* the single ``INSERT`` is
        issued: a partial/mismatched call raises and leaves ZERO rows for this
        insight_id (no half-written vector — the R7 atomicity invariant).
        """
        if not self.store.in_transaction:
            raise VecIndexError(
                "VecIndex.insert must run inside Store.transaction() — the vec row"
                " commits atomically with its insight row (R7)."
            )
        import sqlite_vec

        if full_vector is None:  # legacy single-vector fan-out
            key_v = full_v = retr_v = list(key_vector)
        else:
            key_v = list(key_vector)
            full_v = list(full_vector)
            retr_v = list(retrieval_vector) if retrieval_vector is not None else full_v

        # Validate every column up front: any bad dim aborts before the write, so
        # a rejected partial insert can never leave a row behind.
        self._check_dim(key_v)
        self._check_dim(full_v)
        self._check_dim(retr_v)

        self.store.conn.execute(
            f"INSERT INTO {VEC_TABLE}"
            " (insight_id, embedding, key_embedding, full_embedding)"
            " VALUES (?, ?, ?, ?)",
            (
                insight_id,
                sqlite_vec.serialize_float32(retr_v),
                sqlite_vec.serialize_float32(key_v),
                sqlite_vec.serialize_float32(full_v),
            ),
        )

    # --- reads --------------------------------------------------------------------

    def knn(
        self,
        vector,
        k: int,
        statuses: tuple[str, ...] | None = None,
        *,
        on: str = "key",
    ) -> list[Neighbor]:
        """K nearest neighbors by cosine distance over the ``on`` column.

        ``on`` selects which vector column to search — ``"key"`` (rule identity),
        ``"full"`` (whole atom), or ``"retrieval"`` (legacy/search-document) —
        from a fixed allow-list; an unknown value raises rather than reaching SQL,
        so the column is never string-interpolated from caller input (R8).

        The status filter applies AFTER the KNN takes its k, so filtered views can
        return fewer than k rows; callers needing exact-k-after-filter over-fetch.
        ``statuses=None`` is the add-idea dedup view (all statuses).
        """
        import sqlite_vec

        try:
            column = _VIEW_COLUMN[on]
        except KeyError:
            raise VecIndexError(
                f"unknown knn view {on!r}; expected one of {sorted(_VIEW_COLUMN)}"
            ) from None

        self._check_dim(vector)
        # LIMIT in the subquery (not `k = ?`) blocks the flattener from merging the
        # outer ORDER BY into the vec0 KNN and tripping its single-ORDER-BY rule.
        sql = (
            "SELECT v.insight_id AS insight_id, v.distance AS distance,"
            "       i.status AS status"
            f" FROM (SELECT insight_id, distance FROM {VEC_TABLE}"
            f"        WHERE {column} MATCH ?"
            "        ORDER BY distance LIMIT ?) v"
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

    def get_vector(self, insight_id: int, on: str = "full") -> list[float]:
        """Read back the stored vector for ``insight_id`` from the ``on`` column.

        The graph build (009 R4/R5) needs the raw clustering vectors to weight
        edges by Tanimoto, so this reverses :meth:`insert`. The column comes from
        the same fixed allow-list as :meth:`knn` (never interpolated, R8). The
        vec0 column is stored as raw little-endian float32 (the exact bytes
        ``sqlite_vec.serialize_float32`` produced), so ``struct.unpack`` recovers
        the values without the precision loss of ``vec_to_json``'s 6-decimal text.
        """
        try:
            column = _VIEW_COLUMN[on]
        except KeyError:
            raise VecIndexError(
                f"unknown vector view {on!r}; expected one of {sorted(_VIEW_COLUMN)}"
            ) from None
        row = self.store.conn.execute(
            f"SELECT {column} AS blob FROM {VEC_TABLE} WHERE insight_id = ?",
            (insight_id,),
        ).fetchone()
        if row is None:
            raise VecIndexError(f"no vec row for insight_id {insight_id}")
        blob = row["blob"]
        return list(struct.unpack(f"{self.dim}f", blob))

    def all_neighbors(
        self,
        k: int,
        *,
        on: str = "full",
        statuses: tuple[str, ...] | None = ("active",),
    ) -> dict[int, list[Neighbor]]:
        """Per-node kNN over every visible insight on the ``on`` column (009 R5).

        Returns ``{insight_id: [k nearest neighbors, self dropped]}`` for every
        insight whose status is in ``statuses`` (``None`` = all statuses) and that
        carries a vec row. v1 is the brute-force per-node loop the plan specifies:
        for each node we run :meth:`knn` on its own stored vector and drop the
        self-edge (the node always matches itself at distance 0). We over-fetch
        ``k + 1`` so dropping self still leaves up to ``k`` true neighbors.

        Deterministic: nodes are visited in ascending id and :meth:`knn` already
        breaks distance ties by ascending insight_id, so the mapping is byte-stable
        for a fixed library snapshot. This is the input to
        :func:`agent_families.reflector.graphbuild.build_similarity_graph`.
        """
        # Node set: visible insights that actually have a vector row. The status
        # join mirrors knn's so every returned neighbor is itself a node.
        sql = (
            f"SELECT v.insight_id AS insight_id FROM {VEC_TABLE} v"
            " JOIN insights i ON i.id = v.insight_id"
        )
        params: list = []
        if statuses is not None:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" WHERE i.status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY v.insight_id ASC"
        node_ids = [r["insight_id"] for r in self.store.conn.execute(sql, params)]

        result: dict[int, list[Neighbor]] = {}
        for node_id in node_ids:
            vector = self.get_vector(node_id, on=on)
            hits = self.knn(vector, k + 1, statuses=statuses, on=on)
            neighbors = [n for n in hits if n.insight_id != node_id][:k]
            result[node_id] = neighbors
        return result

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
