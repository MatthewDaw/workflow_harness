"""``af`` command-line entry point — the nine Phase 0 commands wired end to end.

U9 (R18, R19, R21, R24, R22 init half): ``init`` solves cold start — it writes the
provenance-commented thresholds template, applies schema migrations, seeds the four
DESIGN §3 families (planner, worker, verifier, context-retriever) each with one
generic specialist agent, records the embedding pins, and pre-fetches the pinned
embedding model with progress and offline guidance. A second ``init`` is a safe
no-op. ``status`` reports counts by status, pending batches, open contradiction
flags, empty skills, the current snapshot ID, the embedder + dim pins, and a config
summary (R21).

Every command exits non-zero with an actionable message on failure: known failure
types (config, store, pipeline, judge, lifecycle, rendering, embedding, vec-index)
are printed as ``af <command>: <message>`` on stderr with exit code 1. The R10
discipline rides this path — ``rewrite_proposed`` surfaces the judge's rewrite and
the ``--accept-rewrite`` hint in the error message, never an interactive prompt.

The library lives in a directory (``--dir``, default ``.``) holding
``thresholds.toml``, ``library.db``, and ``compiled/`` for delta-patch documents.
``render`` writes rendering BYTES to stdout (notes go to stderr) so byte-stability
(R15) survives the console. The judge record/replay mode and fixtures directory
come from the ``AF_JUDGE_MODE`` / ``AF_JUDGE_FIXTURES`` env vars (R23).

Phase 2 (plan-003 U9) adds the human reflector's toolset: ``add-idea`` gains
``--episode/--scenario/--ticket`` provenance (003 R27, validated before any judge
call); ``trace`` is the navigation surface the settlement report embeds —
``trace chain <SCEN>`` carries the SCEN -> FEAT -> requests -> tickets -> spans
attribution join (003 R26), plus ``spans``/``iterations``/``evidence``/
``transcript``; and ``episode report``/``episode status`` inspect a settled
episode. Driving a live episode (the explorer + grader stack against linkding)
is the documented procedure in ``README.md``; the offline pipeline exercises the
same episode loop with scripted fakes (``tests/test_e2e_episode.py``).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from agent_families.config import (
    DEFAULT_CONFIG_FILENAME,
    Config,
    ConfigError,
    load_config,
)
from agent_families.embedding import (
    META_DIM_KEY,
    META_MODEL_KEY,
    EmbeddingError,
    EmbeddingService,
    ensure_pins,
)
from agent_families.export import export_skills
from agent_families.judge import JudgeError
from agent_families.lifecycle import (
    LifecycleError,
    empty_skills,
    promote_batch,
    retire_insight,
    retire_skill,
    revert_batch,
    revive_insight,
    revive_skill,
    skills_created_by_reverted_batches,
)
from agent_families.pipeline import PipelineError, add_idea
from agent_families.rendering import Renderer, RenderingError
from agent_families.store import (
    DEFAULT_DB_FILENAME,
    STATUSES,
    Store,
    StoreError,
)
from agent_families.vecindex import VecIndex, VecIndexError

SUBCOMMANDS: dict[str, str] = {
    "init": "Seed families/agents, write thresholds.toml, migrate schema, prefetch embedder.",
    "add-idea": "Register a hand-written idea through the routing pipeline.",
    "promote": "Promote a batch's quarantined insights to active.",
    "revert": "Revert a batch's insights to retired.",
    "retire": "Retire an insight or skill by id.",
    "revive": "Revive a retired insight or skill by id.",
    "render": "Render a skill (concatenation, or --compile for delta-patch).",
    "export": "Export skills as Claude Code SKILL.md files.",
    "status": "Report library counts, batches, flags, snapshot, and config.",
    "trace": "Trace-query an episode: chain/spans/iterations/evidence/transcript (003 R26).",
    "episode": "Inspect an episode: report/status (003 R23/R26).",
}

COMPILED_DIRNAME = "compiled"

# The four DESIGN §3 pipeline families, each seeded with one generic specialist
# agent (R19). Names are stable identifiers consumed by later phases.
FAMILY_SEEDS: tuple[tuple[str, str], ...] = (
    ("planner", "Turn a request + Q&A into a fleshed-out, ticketed feature list."),
    ("worker", "Take a ticket plus retrieved context and code it."),
    (
        "verifier",
        "Check work against conventions, requirements, unit correctness, and"
        " integration.",
    ),
    (
        "context-retriever",
        "Answer questions from the codebase, the internet, or (in training) the"
        " human simulator; never routes work itself.",
    ),
)

# Written verbatim by `af init` (R19/R20). Must stay byte-identical to the
# committed template at agent-families/thresholds.toml — test_e2e pins the two
# together so they cannot drift.
DEFAULT_THRESHOLDS_TOML = """\
# thresholds.toml — agent-families tunables, one file, provenance-commented.
#
# Discipline (DESIGN §17): every threshold lives here, never hardcoded in source.
# Each entry records (a) its default's PROVENANCE — the paper or decision it comes
# from — and (b) its TUNING METRIC — what you measure to move it. Decisions made by
# the system are logged with the threshold value that produced them, so changing a
# value reveals what would have flipped.
#
# This file is the template written verbatim by `af init`. The loader is fail-fast:
# unknown sections/keys and out-of-range values are hard errors (no silent defaults).

[embedding]
# Local embedder, pinned. Cosine thresholds are NOT portable across models — a model
# swap is a deliberate migration that re-embeds and recalibrates every cosine
# threshold against the hand-labeled pair set (DESIGN §13, §17).
# PROVENANCE: §13 stack decision — nomic-embed-text-v1.5 via sentence-transformers.
model = "nomic-ai/nomic-embed-text-v1.5"
# PROVENANCE: §13 — 768-dim, pinned once; Matryoshka truncation deliberately unused
# in Phase 0. TUNING METRIC: re-pin only on a full re-embed migration.
dim = 768
# PROVENANCE: KTD — CPUExecutionProvider pinned so ONNX float output is reproducible
# enough for record/replay fixture stability across machines.
device = "cpu"

[merge]
# Cosine prefilter for the merge-review judge call: candidates at or above this
# similarity are JUDGED (never silently auto-merged).
# PROVENANCE: SkillRouter (arXiv 2603.22455) — "cosine>0.92 merge".
# TUNING METRIC: duplicate-pair precision/recall on ~50 hand-labeled pairs (§13/§17).
cosine_threshold = 0.92

[retrieval]
# Approximate-nearest-neighbor breadth handed to the placement judge.
# PROVENANCE: R5 — "ANN top-10 across all statuses".
# TUNING METRIC: placement-accuracy vs prompt size.
ann_top_k = 10
# Minimum cosine similarity for an ANN candidate to count as a real neighbor. Below
# the floor for all candidates, the judge gets the cold-start taxonomy listing instead
# of a neighbor list (KTD cold-start).
# PROVENANCE: Phase-0 provisional default (no paper) — calibrate with the labeled pair
# set. TUNING METRIC: fraction of registrations correctly routed taxonomy-vs-neighbor.
relevance_floor = 0.5

[judge]
# Headless `claude -p` model pin for routing/merge/taxonomy/compile calls.
# PROVENANCE: §15 model tiers — volume routing on Sonnet.
model = "sonnet"
# Schema-violation retries: a malformed or out-of-subset judge response is fed back
# this many times before a hard fail with NO writes (R6).
# PROVENANCE: default. TUNING METRIC: schema-violation recovery rate vs wasted quota.
max_retries = 3
# `--bare` skips OAuth and would silently require an API key, breaking the
# subscription-only constraint. OFF until empirically verified against subscription
# auth (KTD / Risks). Treated as a tested toggle, never a default.
bare = false

[lifecycle]
# Active-set cap per skill. Phase 0 promotion is unconditional (the cap-tournament
# admission is a marked Phase-3 seam); the value is carried now so the seam reads it.
# PROVENANCE: §17 "active cap ~50"; Skill Shadowing (2605.24050) — selection collapses
# as libraries grow (21% drop at 202 skills).
# TUNING METRIC: routing selection accuracy vs active library size.
active_cap = 50

[store]
# SQLite busy-timeout backstop for the single-writer promotion queue (R4): concurrent
# BEGIN IMMEDIATE writers block up to this long rather than failing immediately.
# PROVENANCE: default backstop. TUNING METRIC: lifecycle-op contention under load.
busy_timeout_ms = 5000
"""

# Failure types mapped to `af <command>: <message>` + exit 1. Anything else is a
# bug and should traceback loudly.
_FAILURES = (
    ConfigError,
    StoreError,
    EmbeddingError,
    VecIndexError,
    JudgeError,
    PipelineError,
    LifecycleError,
    RenderingError,
)


# --- shared plumbing ---------------------------------------------------------


def _embedding_service(config: Config) -> EmbeddingService:
    """Construct the production embedder; tests monkeypatch this seam."""
    return EmbeddingService(config.embedding)


def _session_batch_label() -> str:
    """Default batch identity: one batch per CLI session/invocation (R11)."""
    return datetime.now(timezone.utc).strftime("session-%Y%m%dT%H%M%SZ")


def _open_library(args: argparse.Namespace) -> tuple[Config, Store]:
    """Load config and open the library DB, failing actionably if uninitialized."""
    root = Path(args.dir)
    config = load_config(root / DEFAULT_CONFIG_FILENAME)
    db = root / DEFAULT_DB_FILENAME
    if not db.exists():
        raise StoreError(
            f"no library database at {db}. Run `af init` in this directory first."
        )
    return config, Store(db, busy_timeout_ms=config.store.busy_timeout_ms)


def _write_stdout_bytes(content: bytes) -> None:
    """Write rendering bytes to stdout without newline translation (R15)."""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        sys.stdout.flush()
        buffer.write(content)
        buffer.flush()
    else:  # pragma: no cover - non-buffer stdout (rare embedding hosts)
        sys.stdout.write(content.decode("utf-8"))


# --- init (R19, R22 init half) -------------------------------------------------


def _seed_taxonomy(store: Store, active_cap: int) -> int:
    """Seed the four families + generic agents if absent; returns families created."""
    created = 0
    with store.transaction():
        for name, charter in FAMILY_SEEDS:
            row = store.conn.execute(
                "SELECT id FROM families WHERE name = ?", (name,)
            ).fetchone()
            if row is not None:
                continue
            family_id = store.create_family(name, charter=charter)
            store.create_agent(
                family_id,
                "generalist",
                description=f"Generic {name} specialist (seeded by `af init`).",
                active_cap=active_cap,
            )
            created += 1
    return created


def _cmd_init(args: argparse.Namespace) -> int:
    root = Path(args.dir)
    root.mkdir(parents=True, exist_ok=True)
    config_path = root / DEFAULT_CONFIG_FILENAME
    if config_path.exists():
        print(f"keeping existing {config_path}")
    else:
        with open(config_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(DEFAULT_THRESHOLDS_TOML)
        print(f"wrote {config_path}")
    config = load_config(config_path)

    with Store(
        root / DEFAULT_DB_FILENAME, busy_timeout_ms=config.store.busy_timeout_ms
    ) as store:
        store.migrate()
        vec = VecIndex(store, config.embedding.dim)
        vec.migrate()
        created = _seed_taxonomy(store, config.lifecycle.active_cap)
        ensure_pins(store, config.embedding)
    if created:
        names = ", ".join(name for name, _ in FAMILY_SEEDS)
        print(f"seeded {created} families ({names}), one generic agent each")
    else:
        print("taxonomy already seeded; left untouched")

    print(
        f"pre-fetching embedding model '{config.embedding.model}'"
        " (first run downloads ~0.5 GB; honors HF_HOME)..."
    )
    _embedding_service(config).ensure_ready()
    print("embedding model ready.")
    print(f"init complete (library at {root.resolve()}).")
    return 0


# --- add-idea (R5-R11, R14) ------------------------------------------------------


def _cmd_add_idea(args: argparse.Namespace) -> int:
    config, store = _open_library(args)
    with store:
        # Idea provenance (003 R27): validate every evidence ref BEFORE spending a
        # judge call or an embedding — an unknown --episode is a fast, actionable
        # failure, and the FK targets are confirmed to exist before any write.
        _validate_idea_provenance(store, args)
        vec = VecIndex(store, config.embedding.dim)
        embedder = _embedding_service(config)
        batch_label = args.batch
        if batch_label is None:
            batch_label = _session_batch_label()
            print(f"batch label: {batch_label} (per-session default)")
        result = add_idea(
            store,
            vec,
            embedder,
            config,
            precondition=args.precondition,
            action=args.action,
            expected_outcome=args.expected_outcome,
            batch_label=batch_label,
            scope_tag=args.scope_tag,
            accept_rewrite=args.accept_rewrite,
            override_retired=args.override_retired,
        )
        print(result.message)
        # Provenance columns are stamped only on a freshly REGISTERED insight; a
        # duplicate/merge resolves to a pre-existing row whose provenance stays as
        # first written. The Phase 3 reflector populates these mechanically; here
        # the human supplies them by hand (003 R27).
        if result.code == "registered" and (
            args.episode is not None
            or args.scenario is not None
            or args.ticket is not None
        ):
            with store.transaction():
                store.conn.execute(
                    "UPDATE insights SET episode_id = ?, evidence_scenario_id = ?,"
                    " evidence_ticket_id = ? WHERE id = ?",
                    (args.episode, args.scenario, args.ticket, result.insight_id),
                )
            print(
                f"  provenance: episode={args.episode} scenario={args.scenario}"
                f" ticket={args.ticket}"
            )
    return 0


def _validate_idea_provenance(store: Store, args: argparse.Namespace) -> None:
    """Fail fast and actionably if any --episode/--scenario/--ticket ref is unknown
    (003 R27). Done before the pipeline runs so no quota is spent on a bad ref."""
    if args.episode is not None and store.get_episode(args.episode) is None:
        raise PipelineError(
            f"unknown episode {args.episode}: `add-idea --episode` needs an episode"
            " that exists (see `af episode status`)."
        )
    if args.scenario is not None and store.conn.execute(
        "SELECT 1 FROM trace_scen WHERE id = ?", (args.scenario,)
    ).fetchone() is None:
        raise PipelineError(
            f"unknown scenario {args.scenario!r} (--scenario evidence ref);"
            " settle an episode first or check `af episode report`."
        )
    if args.ticket is not None and store.conn.execute(
        "SELECT 1 FROM trace_tkt WHERE id = ?", (args.ticket,)
    ).fetchone() is None:
        raise PipelineError(
            f"unknown ticket {args.ticket!r} (--ticket evidence ref)."
        )


# --- lifecycle (R12) ---------------------------------------------------------------


def _cmd_promote(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        result = promote_batch(store, args.batch)
        print(
            f"promoted batch '{args.batch}': {len(result.insight_ids)} insight(s)"
            f" -> active (snapshot {result.snapshot_id})"
        )
    return 0


def _cmd_revert(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        result = revert_batch(store, args.batch)
        print(
            f"reverted batch '{args.batch}': {len(result.insight_ids)} insight(s)"
            f" -> retired (snapshot {result.snapshot_id})"
        )
        for skill_id in result.removed_skill_ids:
            print(f"removed batch-created skill {skill_id} (no surviving members)")
        for flag_id in result.closed_contradiction_ids:
            print(f"closed contradiction flag {flag_id}")
    return 0


def _cmd_retire(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        if args.insight is not None:
            result = retire_insight(store, args.insight)
            print(f"retired insight {args.insight} (snapshot {result.snapshot_id})")
        else:
            result = retire_skill(store, args.skill)
            print(
                f"retired skill {args.skill}: {len(result.insight_ids)} member"
                f" insight(s) -> retired (snapshot {result.snapshot_id})"
            )
    return 0


def _cmd_revive(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        if args.insight is not None:
            result = revive_insight(store, args.insight)
            print(f"revived insight {args.insight} (snapshot {result.snapshot_id})")
        else:
            result = revive_skill(store, args.skill)
            print(
                f"revived skill {args.skill}: {len(result.insight_ids)} member"
                f" insight(s) -> active (snapshot {result.snapshot_id})"
            )
    return 0


# --- render / export (R15-R17) --------------------------------------------------------


def _cmd_render(args: argparse.Namespace) -> int:
    config, store = _open_library(args)
    with store:
        renderer = Renderer(store, compiled_dir=Path(args.dir) / COMPILED_DIRNAME)
        if args.compile:
            doc = renderer.compile_skill(
                args.skill,
                model=config.judge.model,
                max_retries=config.judge.max_retries,
                snapshot_id=args.snapshot,
                bare=config.judge.bare,
            )
            _write_stdout_bytes(doc.content)
            print(f"compiled document written to {doc.path}", file=sys.stderr)
        else:
            rendering = renderer.render_concat(
                args.skill,
                snapshot_id=args.snapshot,
                include_quarantined=args.include_quarantined,
            )
            _write_stdout_bytes(rendering.content)
            if rendering.empty:
                print(
                    f"note: skill {args.skill} has no visible members at snapshot"
                    f" {rendering.snapshot_id} (empty rendering)",
                    file=sys.stderr,
                )
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        renderer = Renderer(store, compiled_dir=Path(args.dir) / COMPILED_DIRNAME)
        try:
            report = export_skills(renderer, args.out, skill_ids=args.skill)
        except ValueError as exc:
            raise RenderingError(str(exc)) from exc
        for entry in report.exported:
            suffix = " [compiled]" if entry.compiled else ""
            print(
                f"exported skill {entry.skill_id} ({entry.name!r}) ->"
                f" {entry.path}{suffix}"
            )
        for notice in report.notices:
            print(f"notice: {notice}")
        if not report.exported and not report.skipped:
            print("no skills to export.")
    return 0


# --- status (R21) ----------------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    config, store = _open_library(args)
    with store:
        lines: list[str] = []
        lines.append(f"library: {store.path}")
        lines.append(f"snapshot: {store.current_snapshot_id()}")
        model = store.get_meta(META_MODEL_KEY) or config.embedding.model
        dim = store.get_meta(META_DIM_KEY) or str(config.embedding.dim)
        lines.append(f"embedder: {model} (dim {dim})")

        lines.append("taxonomy:")
        for fam in store.conn.execute(
            "SELECT id, name FROM families ORDER BY id ASC"
        ).fetchall():
            n_agents = store.conn.execute(
                "SELECT COUNT(*) AS n FROM agents WHERE family_id = ?", (fam["id"],)
            ).fetchone()["n"]
            n_skills = store.conn.execute(
                "SELECT COUNT(*) AS n FROM skills s JOIN agents a ON a.id = s.agent_id"
                " WHERE a.family_id = ?",
                (fam["id"],),
            ).fetchone()["n"]
            lines.append(
                f"  - family {fam['name']}: {n_agents} agent(s), {n_skills} skill(s)"
            )

        counts = {status: 0 for status in STATUSES}
        total = 0
        for row in store.conn.execute(
            "SELECT status, COUNT(*) AS n FROM insights GROUP BY status"
        ).fetchall():
            counts[row["status"]] = row["n"]
            total += row["n"]
        lines.append(
            f"insights: total={total}"
            f" quarantined={counts['quarantined']} active={counts['active']}"
            f" dormant={counts['dormant']} retired={counts['retired']}"
        )

        pending = store.conn.execute(
            "SELECT b.label AS label, COUNT(*) AS n FROM insights i"
            " JOIN batches b ON b.id = i.batch_id"
            " WHERE i.status = 'quarantined' GROUP BY b.id ORDER BY b.id ASC"
        ).fetchall()
        if pending:
            lines.append("pending batches:")
            for row in pending:
                lines.append(f"  - {row['label']}: {row['n']} quarantined insight(s)")
        else:
            lines.append("pending batches: none")

        flags = store.conn.execute(
            "SELECT id, challenger_id, incumbent_id FROM contradictions"
            " WHERE status = 'open' ORDER BY id ASC"
        ).fetchall()
        if flags:
            lines.append(f"open contradiction flags: {len(flags)}")
            for row in flags:
                lines.append(
                    f"  - flag {row['id']}: insight {row['challenger_id']}"
                    f" contradicts insight {row['incumbent_id']}"
                )
        else:
            lines.append("open contradiction flags: none")

        empties = empty_skills(store)
        lines.append(
            "empty skills: " + (", ".join(map(str, empties)) if empties else "none")
        )
        flagged = skills_created_by_reverted_batches(store)
        lines.append(
            "skills created by reverted batches: "
            + (", ".join(map(str, flagged)) if flagged else "none")
        )

        lines.append(
            f"config: merge.cosine_threshold={config.merge.cosine_threshold}"
            f" retrieval.relevance_floor={config.retrieval.relevance_floor}"
            f" retrieval.ann_top_k={config.retrieval.ann_top_k}"
            f" judge.model={config.judge.model}"
            f" judge.max_retries={config.judge.max_retries}"
            f" lifecycle.active_cap={config.lifecycle.active_cap}"
            f" store.busy_timeout_ms={config.store.busy_timeout_ms}"
        )
        print("\n".join(lines))
    return 0


# --- trace queries (003 R26) ---------------------------------------------------------
#
# The trace CLI is the human reflector's navigation surface and the commands the
# settlement report embeds. `af trace chain <SCEN>` carries the attribution join
# (SCEN -> FEAT -> requests -> requirements -> tickets -> spans); it lives HERE,
# once, where Plan 4's Stage A will reuse it (003 R23) — never in the report
# renderer, which only embeds the command string.


def _placeholders(count: int) -> str:
    return ",".join("?" * count)


def _require_scen(store: Store, scen_id: str):
    row = store.conn.execute(
        "SELECT * FROM trace_scen WHERE id = ?", (scen_id,)
    ).fetchone()
    if row is None:
        raise StoreError(
            f"no scenario {scen_id!r}. Settled scenarios are listed in"
            " `af episode report <episode>`."
        )
    return row


def _cmd_trace_chain(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        scen = _require_scen(store, args.scen)
        lines = [f"chain for {scen['id']}:"]
        lines.append(
            f"  verdict: {scen['result']} (tier {scen['tier']},"
            f" judge {scen['judge_mode']})"
        )
        if scen["evidence"]:
            lines.append(f"  failure: {scen['evidence']}")
        lines.append(
            f"  episode: {scen['episode_id']}  snapshot: {scen['snapshot_id']}"
        )
        feat = store.conn.execute(
            "SELECT * FROM trace_feat WHERE id = ?", (scen["feat_id"],)
        ).fetchone()
        if feat is None:
            lines.append(f"  feature: {scen['feat_id']} (no registry row)")
        else:
            lines.append(
                f"  feature: {feat['id']} (target {feat['target']},"
                f" digest {feat['digest']}, status {feat['status']})"
            )

        msg_ids = [
            r["msg_id"]
            for r in store.conn.execute(
                "SELECT msg_id FROM trace_msg_mentions WHERE feat_id = ?"
                " ORDER BY msg_id",
                (scen["feat_id"],),
            ).fetchall()
        ]
        lines.append(
            "  requests (MSG mentions): " + (", ".join(msg_ids) or "none")
        )
        req_rows = (
            store.conn.execute(
                "SELECT id, source_msg_id FROM trace_req WHERE source_msg_id IN"
                f" ({_placeholders(len(msg_ids))}) ORDER BY id",
                msg_ids,
            ).fetchall()
            if msg_ids
            else []
        )
        lines.append(
            "  requirements: "
            + (
                ", ".join(
                    f"{r['id']} (from {r['source_msg_id']})" for r in req_rows
                )
                or "none"
            )
        )
        req_ids = [r["id"] for r in req_rows]
        tkt_rows = (
            store.conn.execute(
                "SELECT DISTINCT t.id AS id, t.status AS status"
                " FROM trace_tkt_covers c JOIN trace_tkt t ON t.id = c.tkt_id"
                f" WHERE c.req_id IN ({_placeholders(len(req_ids))})"
                " ORDER BY t.id",
                req_ids,
            ).fetchall()
            if req_ids
            else []
        )
        lines.append(
            "  tickets: "
            + (
                ", ".join(f"{r['id']} ({r['status']})" for r in tkt_rows)
                or "none"
            )
        )
        tkt_ids = [r["id"] for r in tkt_rows]
        span_rows = (
            store.conn.execute(
                "SELECT s.id AS id, s.family AS family, s.agent AS agent,"
                " s.status AS status, s.run_id AS run_id,"
                " r.episode_id AS episode_id, r.increment_index AS increment_index"
                " FROM trace_span s LEFT JOIN runs r ON r.id = s.run_id"
                f" WHERE s.ticket_id IN ({_placeholders(len(tkt_ids))})"
                " ORDER BY s.id",
                tkt_ids,
            ).fetchall()
            if tkt_ids
            else []
        )
        lines.append(
            "  spans: "
            + (
                ", ".join(
                    f"{r['id']} ({r['family']}/{r['agent']}, run {r['run_id']},"
                    f" increment {r['increment_index']}, {r['status']})"
                    for r in span_rows
                )
                or "none"
            )
        )
        print("\n".join(lines))
    return 0


def _cmd_trace_spans(args: argparse.Namespace) -> int:
    if args.increment is not None and args.episode is None:
        raise StoreError("--increment requires --episode")
    _config, store = _open_library(args)
    with store:
        if args.ticket is not None:
            header = f"spans for ticket {args.ticket}:"
            rows = store.conn.execute(
                "SELECT s.*, r.episode_id AS r_episode, r.increment_index AS r_inc"
                " FROM trace_span s LEFT JOIN runs r ON r.id = s.run_id"
                " WHERE s.ticket_id = ? ORDER BY s.id",
                (args.ticket,),
            ).fetchall()
        elif args.increment is not None:
            header = f"spans for episode {args.episode} increment {args.increment}:"
            rows = store.conn.execute(
                "SELECT s.*, r.episode_id AS r_episode, r.increment_index AS r_inc"
                " FROM trace_span s JOIN runs r ON r.id = s.run_id"
                " WHERE r.episode_id = ? AND r.increment_index = ? ORDER BY s.id",
                (args.episode, args.increment),
            ).fetchall()
        else:
            header = f"spans for episode {args.episode}:"
            rows = store.conn.execute(
                "SELECT s.*, r.episode_id AS r_episode, r.increment_index AS r_inc"
                " FROM trace_span s JOIN runs r ON r.id = s.run_id"
                " WHERE r.episode_id = ? ORDER BY s.id",
                (args.episode,),
            ).fetchall()
        lines = [header]
        if not rows:
            lines.append("  (none)")
        for s in rows:
            lines.append(
                f"  {s['id']}: {s['family']}/{s['agent']} ticket {s['ticket_id']}"
                f" run {s['run_id']} increment {s['r_inc']}"
                f" iter {s['ralph_iteration']} {s['status']} cost {s['cost_usd']}"
            )
        print("\n".join(lines))
    return 0


def _cmd_trace_iterations(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        rows = store.conn.execute(
            "SELECT * FROM trace_span WHERE ticket_id = ?"
            " ORDER BY ralph_iteration, id",
            (args.ticket,),
        ).fetchall()
        lines = [f"iterations for ticket {args.ticket}:"]
        if not rows:
            lines.append("  (no spans)")
        for s in rows:
            files = ", ".join(json.loads(s["files_json"] or "[]")) or "(none)"
            lines.append(
                f"  iter {s['ralph_iteration']} {s['id']} [{s['status']}]: {files}"
            )
        print("\n".join(lines))
    return 0


def _cmd_trace_evidence(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        scen = _require_scen(store, args.scen)
        lines = [
            f"evidence for {scen['id']}:",
            f"  verdict: {scen['result']}  failure: {scen['evidence'] or '(none)'}",
            f"  judge_metadata: {scen['judge_metadata_json']}",
            f"  judge_input: {scen['judge_input_json']}",
        ]
        print("\n".join(lines))
    return 0


def _cmd_trace_transcript(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        span = store.conn.execute(
            "SELECT * FROM trace_span WHERE id = ?", (args.span,)
        ).fetchone()
        if span is None:
            raise StoreError(f"no span {args.span!r}.")
        refs = json.loads(span["artifact_refs_json"] or "[]")
        files = json.loads(span["files_json"] or "[]")
        lines = [
            f"transcript artifacts for {span['id']} (run {span['run_id']},"
            f" status {span['status']}):",
            "  artifact_refs: " + (", ".join(refs) or "none"),
            "  files: " + (", ".join(files) or "none"),
        ]
        print("\n".join(lines))
    return 0


# --- episode inspection (003 R23/R26) ------------------------------------------------


def _cmd_episode_report(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        row = store.conn.execute(
            "SELECT report_json FROM settlement_reports WHERE episode_id = ?",
            (args.episode,),
        ).fetchone()
        if row is None:
            raise StoreError(
                f"episode {args.episode} has no settlement report yet"
                f" (see `af episode status {args.episode}`)."
            )
        # The stored report is the byte-stable canonical render (R23); emit it
        # verbatim so it round-trips through the console unchanged.
        _write_stdout_bytes(row["report_json"].encode("utf-8"))
    return 0


def _cmd_episode_status(args: argparse.Namespace) -> int:
    _config, store = _open_library(args)
    with store:
        ep = store.get_episode(args.episode)
        if ep is None:
            raise StoreError(f"episode {args.episode} does not exist.")
        lines = [
            f"episode {ep['id']}: target {ep['target']} digest {ep['digest']}",
            f"  status: {ep['status']}",
            f"  snapshot: {ep['snapshot_id']}",
            f"  budget: max_increments={ep['max_increments']}"
            f" cost_ceiling_usd={ep['cost_ceiling_usd']}",
            f"  created_at: {ep['created_at']}"
            f"  settled_at: {ep['settled_at'] or '(not settled)'}",
        ]
        runs = store.conn.execute(
            "SELECT id, increment_index, status, acceptance, total_cost_usd"
            " FROM runs WHERE episode_id = ? ORDER BY increment_index, id",
            (args.episode,),
        ).fetchall()
        lines.append(f"  increments: {len(runs)}")
        for r in runs:
            lines.append(
                f"    increment {r['increment_index']}: run {r['id']}"
                f" {r['status']} acceptance={r['acceptance'] or '-'}"
                f" cost={r['total_cost_usd']}"
            )
        rep = store.conn.execute(
            "SELECT score FROM settlement_reports WHERE episode_id = ?",
            (args.episode,),
        ).fetchone()
        if rep is not None:
            lines.append(
                f"  settlement score: {rep['score']}"
                f" (run `af episode report {args.episode}`)"
            )
        else:
            lines.append("  settlement: none")
        print("\n".join(lines))
    return 0


# --- parser and entry point ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="af",
        description="agent-families — self-improving skill library (Phase 0 core).",
    )
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")
    subparsers.required = True

    def sub(name: str, handler) -> argparse.ArgumentParser:
        p = subparsers.add_parser(
            name, help=SUBCOMMANDS[name], description=SUBCOMMANDS[name]
        )
        p.add_argument(
            "--dir",
            default=".",
            help="library directory (default: current directory)",
        )
        p.set_defaults(_handler=handler)
        return p

    sub("init", _cmd_init)

    p = sub("add-idea", _cmd_add_idea)
    p.add_argument("--precondition", required=True, help="structural field 1/3")
    p.add_argument("--action", required=True, help="structural field 2/3")
    p.add_argument("--expected-outcome", required=True, help="structural field 3/3")
    p.add_argument("--scope-tag", help="author-proposed scope tag")
    p.add_argument(
        "--batch", help="batch label (default: a per-session batch, R11)"
    )
    p.add_argument(
        "--accept-rewrite",
        action="store_true",
        help="re-enter the pipeline with a judge-proposed rewrite (R10)",
    )
    p.add_argument(
        "--override-retired",
        action="store_true",
        help="admit the idea fresh although its near-duplicate is retired (R14)",
    )
    p.add_argument(
        "--episode",
        type=int,
        help="provenance: the episode this idea reflects on (003 R27)",
    )
    p.add_argument(
        "--scenario", help="provenance: evidence SCEN id (optional, 003 R27)"
    )
    p.add_argument(
        "--ticket", help="provenance: evidence TKT id (optional, 003 R27)"
    )

    p = sub("promote", _cmd_promote)
    p.add_argument("--batch", required=True, help="batch label to promote")
    p = sub("revert", _cmd_revert)
    p.add_argument("--batch", required=True, help="batch label to revert")

    for name, handler in (("retire", _cmd_retire), ("revive", _cmd_revive)):
        p = sub(name, handler)
        group = p.add_mutually_exclusive_group(required=True)
        group.add_argument("--insight", type=int, help="insight id")
        group.add_argument("--skill", type=int, help="skill id (all members)")

    p = sub("render", _cmd_render)
    p.add_argument("--skill", type=int, required=True, help="skill id")
    p.add_argument(
        "--snapshot", type=int, help="render as of this snapshot (default: current)"
    )
    p.add_argument(
        "--include-quarantined",
        action="store_true",
        help="include quarantined members (computed fresh, never cached)",
    )
    p.add_argument(
        "--compile",
        action="store_true",
        help="delta-patch compile via the judge (writes under <dir>/compiled/)",
    )

    p = sub("export", _cmd_export)
    p.add_argument("--out", required=True, help="output directory for SKILL.md trees")
    p.add_argument(
        "--skill",
        type=int,
        action="append",
        help="skill id to export (repeatable; default: all skills)",
    )

    sub("status", _cmd_status)

    def _dir_arg(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--dir", default=".", help="library directory (default: current directory)"
        )
        return p

    # --- trace queries (003 R26) -------------------------------------------------
    trace = subparsers.add_parser(
        "trace", help=SUBCOMMANDS["trace"], description=SUBCOMMANDS["trace"]
    )
    trace_sub = trace.add_subparsers(
        dest="trace_command", metavar="<trace-command>"
    )
    trace_sub.required = True

    tc = _dir_arg(
        trace_sub.add_parser(
            "chain",
            help="attribution chain for a settled scenario"
            " (SCEN -> FEAT -> requests -> tickets -> spans)",
        )
    )
    tc.add_argument("scen", metavar="SCEN", help="scenario id (SCEN-...)")
    tc.set_defaults(_handler=_cmd_trace_chain, command="trace")

    tspans = _dir_arg(
        trace_sub.add_parser(
            "spans", help="list spans by ticket, episode, or increment"
        )
    )
    tspans_target = tspans.add_mutually_exclusive_group(required=True)
    tspans_target.add_argument("--ticket", help="ticket id (TKT-...)")
    tspans_target.add_argument("--episode", type=int, help="episode id")
    tspans.add_argument(
        "--increment", type=int, help="increment index (with --episode)"
    )
    tspans.set_defaults(_handler=_cmd_trace_spans, command="trace")

    titer = _dir_arg(
        trace_sub.add_parser(
            "iterations", help="per-iteration span diffs (files touched) for a ticket"
        )
    )
    titer.add_argument("ticket", metavar="TKT", help="ticket id (TKT-...)")
    titer.set_defaults(_handler=_cmd_trace_iterations, command="trace")

    tev = _dir_arg(
        trace_sub.add_parser(
            "evidence", help="judge inputs/metadata for a settled scenario"
        )
    )
    tev.add_argument("scen", metavar="SCEN", help="scenario id (SCEN-...)")
    tev.set_defaults(_handler=_cmd_trace_evidence, command="trace")

    ttr = _dir_arg(
        trace_sub.add_parser(
            "transcript", help="artifact/transcript refs for a span"
        )
    )
    ttr.add_argument("span", metavar="SPAN", help="span id (SPAN-...)")
    ttr.set_defaults(_handler=_cmd_trace_transcript, command="trace")

    # --- episode inspection (003 R23/R26) ----------------------------------------
    episode = subparsers.add_parser(
        "episode", help=SUBCOMMANDS["episode"], description=SUBCOMMANDS["episode"]
    )
    episode_sub = episode.add_subparsers(
        dest="episode_command", metavar="<episode-command>"
    )
    episode_sub.required = True

    erep = _dir_arg(
        episode_sub.add_parser(
            "report", help="print an episode's stored settlement report"
        )
    )
    erep.add_argument("episode", type=int, help="episode id")
    erep.set_defaults(_handler=_cmd_episode_report, command="episode")

    est = _dir_arg(
        episode_sub.add_parser(
            "status", help="episode status, increments, and settlement state"
        )
    )
    est.add_argument("episode", type=int, help="episode id")
    est.set_defaults(_handler=_cmd_episode_status, command="episode")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "_handler", None)
    if handler is None:  # pragma: no cover - argparse enforces a subcommand
        parser.print_help()
        return 2
    try:
        return handler(args)
    except _FAILURES as exc:
        print(f"af {args.command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
