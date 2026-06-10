"""Calibration instruments (plan-003 U8, R24 + R25 bootstrap half).

Measure the verifier before anything trusts it (DESIGN §16 places this in
Phase 2 explicitly). Two instruments live here:

**Mutation-seeded verifier audits (R24).** Mutant fixtures are hand-authored
known-bad diffs against the pinned template stack
(``agent-families/fixtures/mutants/<id>/mutant.diff`` + ``mutant.json``),
each shipping a **witness**: a repro envelope (the R14 CHK shape — command,
workspace-relative cwd, timeout, expected_exit) that demonstrates the bad
behavior on the mutated build. An audit injects the mutant build into the
verify queue — it stages the bad diff into a disposable workspace and presents
that workspace to the verifier seam exactly as a ticket's iteration output
would be — but the witness executes FIRST: a witness that does not reproduce
the bad behavior disqualifies the mutant (the equivalent-mutant guard) and
**blocks the audit** — the verifier is never scored, never flagged. Only a
witnessed mutant can convict: a verifier that passes one is flagged
``false_pass`` and every verdict it produced since the last clean audit is
marked ``suspect``. Suspect marks are recorded and surfaced (settlement-report
instrument health) ONLY — enforcement is Phase 3.

The audit bookkeeping (append-only audit log, per-verifier clean-audit
watermark, accumulated suspect verdict ids) persists in the store's ``meta``
table (the U5 plan-document precedent — no new DDL outside the migration
list). Verdicts are trace_chk rows; the "since the last clean audit" window
is their monotonic rowid (CHK rows are never deleted). Phase 2 runs a single
verifier role, so the verdict window is global; ``verifier_id`` scopes the
audit bookkeeping and per-verifier verdict attribution arrives with Phase 3's
enforcement.

**Frozen replay set — bootstrap persistence only (R25).** A utility persists
hand-verified SCEN verdict pairs *with their judge-input payloads* (R22 —
replay re-judging needs the original inputs, not screenshots) as the frozen
set: a byte-stable JSON file (sorted keys, ``\\n`` newlines). The re-judging
cadence, drift tolerance, and ``instrument_suspect`` propagation ship in
Plan 4 with their consumer (control charts); in Phase 2 the human is the loop.

Behavior tunables (audit cadence, frozen-set size N) are caller-supplied via
:class:`CalibrateConfig` per the established U3/U6/U7 precedent — routing
them from ``thresholds.toml`` is the run-assembly wiring's job; nothing here
hardcodes one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from agent_families.pipeline.orchestrator import VerifierResult
from agent_families.store import Store

logger = logging.getLogger(__name__)

# --- vocabulary -----------------------------------------------------------------

# Audit outcomes (R24): `caught` = the verifier failed the witnessed mutant (a
# clean audit); `false_pass` = it passed one (flagged + suspect marking);
# `disqualified` = the witness did not reproduce, the mutant is disqualified
# and the verifier was never scored (the equivalent-mutant guard).
AUDIT_OUTCOMES = ("caught", "false_pass", "disqualified")

# R25 protocol floor, not a tunable: the frozen set is a Phase 2 exit
# criterion at N >= 20 (Plan 4's SPC bootstrap depends on it); the config
# supplies N, the requirement bounds it (the SettleConfig.panel_size pattern).
FROZEN_SET_SIZE_FLOOR = 20

FROZEN_SET_VERSION = 1

# Judge modes whose SCEN rows are re-judgeable: a deterministic verdict never
# saw a judge and carries no judge input — nothing to freeze (R25).
_REJUDGEABLE_MODES = frozenset({"single", "panel"})

# meta keys (the planning.py plan-document precedent: bookkeeping in `meta`).
META_AUDIT_LOG = "calibration:mutation_audit_log"


def _watermark_key(verifier_id: str) -> str:
    return f"calibration:clean_watermark:{verifier_id}"


def _suspect_key(verifier_id: str) -> str:
    return f"calibration:suspect_chks:{verifier_id}"


# Windows drive / UNC prefixes anywhere in a witness command — the R14 "no
# absolute paths" repro discipline (the ticket_loop pattern).
_WIN_ABS_IN_COMMAND = re.compile(r"(?:^|[\s\"'=])(?:[A-Za-z]:[\\/]|\\\\)")

_MUTANT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class CalibrateError(Exception):
    """Calibration misuse or invariant breach with an actionable message."""


# --- config ----------------------------------------------------------------------


@dataclass(frozen=True)
class CalibrateConfig:
    """The two calibration tunables, caller-supplied (no hidden defaults).

    ``audit_cadence``: a mutation audit is due every N verifier verdicts
    (R24 "injected into the verify queue on a config cadence").
    ``frozen_set_min_size``: the R25 N — the frozen set is bootstrap-complete
    at this many hand-verified pairs (Phase 2 exit criterion, >= 20).
    """

    audit_cadence: int
    frozen_set_min_size: int

    def __post_init__(self) -> None:
        if self.audit_cadence < 1:
            raise CalibrateError(
                f"audit_cadence must be >= 1 verdict between audits,"
                f" got {self.audit_cadence}"
            )
        if self.frozen_set_min_size < FROZEN_SET_SIZE_FLOOR:
            raise CalibrateError(
                f"frozen_set_min_size must be >= {FROZEN_SET_SIZE_FLOOR}"
                f" (R25: the Phase 2 exit criterion is at least"
                f" {FROZEN_SET_SIZE_FLOOR} hand-verified pairs),"
                f" got {self.frozen_set_min_size}"
            )


# --- mutant fixtures (R24) ----------------------------------------------------------


@dataclass(frozen=True)
class WitnessEnvelope:
    """The mutant's witness: an R14-shaped repro envelope that demonstrates
    the bad behavior on the mutated build (``expected_exit`` is what the
    command yields when the bug is present)."""

    command: str
    cwd: str
    timeout: float
    expected_exit: int

    def __post_init__(self) -> None:
        if not self.command.strip():
            raise CalibrateError("witness command must be non-empty")
        if _WIN_ABS_IN_COMMAND.search(self.command):
            raise CalibrateError(
                f"witness command must not contain absolute paths (R14 repro"
                f" discipline), got: {self.command}"
            )
        if not self.cwd.strip():
            raise CalibrateError(
                "witness cwd must be non-empty ('.' for the workspace root)"
            )
        if (
            PureWindowsPath(self.cwd).is_absolute()
            or self.cwd.startswith(("/", "\\"))
            or (len(self.cwd) >= 2 and self.cwd[1] == ":")
        ):
            raise CalibrateError(
                f"witness cwd must be workspace-relative, got absolute"
                f" path '{self.cwd}'"
            )
        if ".." in PureWindowsPath(self.cwd).parts:
            raise CalibrateError(
                f"witness cwd must stay inside the workspace, got '{self.cwd}'"
            )
        if self.timeout <= 0:
            raise CalibrateError(
                f"witness timeout must be positive, got {self.timeout}"
            )
        if self.expected_exit < 0:
            raise CalibrateError(
                f"witness expected_exit must be >= 0, got {self.expected_exit}"
            )


@dataclass(frozen=True)
class MutantFixture:
    """One hand-authored known-bad diff against the template stack, with the
    witness that proves it is not an equivalent mutant."""

    mutant_id: str
    description: str
    diff_text: str
    witness: WitnessEnvelope

    def __post_init__(self) -> None:
        if not _MUTANT_ID_RE.match(self.mutant_id):
            raise CalibrateError(
                f"mutant_id must be a lowercase slug ([a-z0-9._-]),"
                f" got {self.mutant_id!r}"
            )
        if not self.description.strip():
            raise CalibrateError(
                f"mutant {self.mutant_id}: description must say what the bad"
                " diff breaks (hand-authored fixtures carry their rationale)"
            )
        if "diff --git" not in self.diff_text:
            raise CalibrateError(
                f"mutant {self.mutant_id}: mutant.diff must be a git unified"
                " diff against the template stack"
            )


def load_mutant(mutant_dir: Path | str) -> MutantFixture:
    """Load one mutant fixture directory (``mutant.json`` + ``mutant.diff``);
    the directory name is the mutant id."""
    mutant_dir = Path(mutant_dir)
    meta_path = mutant_dir / "mutant.json"
    diff_path = mutant_dir / "mutant.diff"
    for path, what in ((meta_path, "mutant.json"), (diff_path, "mutant.diff")):
        if not path.is_file():
            raise CalibrateError(
                f"mutant fixture {mutant_dir.name!r} is missing {what}"
                f" (expected at {path})"
            )
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrateError(f"invalid JSON in {meta_path}: {exc}") from exc
    unknown = set(meta) - {"description", "witness"}
    if unknown:
        raise CalibrateError(
            f"mutant {mutant_dir.name}: unknown key {sorted(unknown)[0]!r}"
            " in mutant.json (expected description, witness)"
        )
    witness_raw = meta.get("witness")
    if not isinstance(witness_raw, dict):
        raise CalibrateError(
            f"mutant {mutant_dir.name}: mutant.json needs a 'witness' object"
            " {command, cwd, timeout, expected_exit} (R24 — every mutant"
            " ships with a witness)"
        )
    try:
        witness = WitnessEnvelope(**witness_raw)
    except TypeError as exc:
        raise CalibrateError(
            f"mutant {mutant_dir.name}: malformed witness envelope: {exc}"
        ) from exc
    return MutantFixture(
        mutant_id=mutant_dir.name,
        description=str(meta.get("description", "")),
        diff_text=diff_path.read_text(encoding="utf-8"),
        witness=witness,
    )


def load_mutants(fixtures_root: Path | str) -> tuple[MutantFixture, ...]:
    """Every mutant fixture under ``fixtures_root``, sorted by id."""
    fixtures_root = Path(fixtures_root)
    if not fixtures_root.is_dir():
        raise CalibrateError(
            f"mutant fixtures directory does not exist: {fixtures_root}"
        )
    dirs = sorted(p for p in fixtures_root.iterdir() if p.is_dir())
    if not dirs:
        raise CalibrateError(
            f"no mutant fixtures under {fixtures_root} — the audit instrument"
            " needs at least one hand-authored mutant (R24)"
        )
    return tuple(load_mutant(d) for d in dirs)


# --- staging and the witness (R24) -----------------------------------------------


def stage_mutant(mutant: MutantFixture, workspace_root: Path | str) -> None:
    """Apply the mutant's bad diff to a disposable workspace.

    ``--ignore-whitespace`` tolerates CRLF working trees (the template is
    committed LF but ``core.autocrlf`` checkouts and template *copies* may be
    CRLF on Windows); the diff itself is authored against the LF blob.
    """
    workspace_root = Path(workspace_root)
    if not workspace_root.is_dir():
        raise CalibrateError(
            f"mutant workspace does not exist: {workspace_root}"
        )
    patch_path = workspace_root / f".af-mutant-{mutant.mutant_id}.diff"
    with open(patch_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(mutant.diff_text)
    try:
        proc = subprocess.run(
            [
                "git", "apply", "--ignore-whitespace", "--whitespace=nowarn",
                patch_path.name,
            ],
            cwd=str(workspace_root),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        patch_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        raise CalibrateError(
            f"mutant {mutant.mutant_id} failed to apply in {workspace_root}"
            f" (exit {proc.returncode}): {proc.stderr.strip()} — the fixture"
            " has drifted from the template stack; refresh the hand-authored"
            " diff"
        )


# Witness executor seam: (witness, workspace_root) -> observed exit code, or
# None when the command timed out (a hung witness demonstrates nothing).
RunWitness = Callable[[WitnessEnvelope, Path], int | None]


def run_witness(
    witness: WitnessEnvelope, workspace_root: Path | str
) -> int | None:
    """Execute the witness envelope in the (mutated) workspace.

    The command is a shell-pasteable string (the CHK repro convention);
    utf-8 capture per the Windows KTD. Returns the observed exit code, or
    None on timeout.
    """
    cwd = Path(workspace_root) / witness.cwd
    try:
        proc = subprocess.run(
            witness.command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=witness.timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning(
            "witness timed out after %.1fs: %s", witness.timeout,
            witness.command,
        )
        return None
    return proc.returncode


# --- the audit (R24) -----------------------------------------------------------------

# The verifier seam under audit: the mutated workspace in, a VerifierResult
# out. TicketLoop.verifier closes over its own config/context; adapting it
# onto this shape is the run-assembly wiring's job.
AuditVerifier = Callable[[Path], VerifierResult]


@dataclass(frozen=True)
class MutationAuditResult:
    """One scored (or blocked) mutation audit."""

    mutant_id: str
    verifier_id: str
    outcome: str  # AUDIT_OUTCOMES
    witness_exit: int | None
    suspect_chk_ids: tuple[str, ...]
    detail: str

    @property
    def flagged(self) -> bool:
        return self.outcome == "false_pass"


def _max_chk_rowid(store: Store) -> int:
    row = store.conn.execute(
        "SELECT COALESCE(MAX(rowid), 0) AS v FROM trace_chk"
    ).fetchone()
    return row["v"]


def last_clean_watermark(store: Store, verifier_id: str) -> int:
    """The trace_chk rowid watermark of the verifier's last clean (caught)
    audit; 0 before any clean audit (every verdict is then in the window)."""
    raw = store.get_meta(_watermark_key(verifier_id))
    return int(raw) if raw is not None else 0


def suspect_chk_ids(store: Store, verifier_id: str) -> tuple[str, ...]:
    """Every CHK id ever marked suspect for this verifier (accumulated;
    recorded and surfaced only — enforcement is Phase 3)."""
    raw = store.get_meta(_suspect_key(verifier_id))
    return tuple(json.loads(raw)) if raw is not None else ()


def audit_log(store: Store, verifier_id: str | None = None) -> list[dict]:
    """The append-only mutation-audit log, optionally filtered by verifier."""
    raw = store.get_meta(META_AUDIT_LOG)
    records: list[dict] = json.loads(raw) if raw is not None else []
    if verifier_id is None:
        return records
    return [r for r in records if r["verifier_id"] == verifier_id]


def audit_due(store: Store, verifier_id: str, config: CalibrateConfig) -> bool:
    """True when ``audit_cadence`` verdicts accumulated since the verifier
    was last *scored* (caught or false_pass — a disqualified mutant never
    scored it, so it does not reset the clock)."""
    scored = [
        r
        for r in audit_log(store, verifier_id)
        if r["outcome"] in ("caught", "false_pass")
    ]
    baseline = scored[-1]["chk_watermark"] if scored else 0
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_chk WHERE rowid > ?", (baseline,)
    ).fetchone()
    return row["n"] >= config.audit_cadence


def _record_audit(
    store: Store,
    result: MutationAuditResult,
    *,
    chk_watermark: int,
    advance_watermark: bool,
) -> None:
    with store.transaction():
        records = audit_log(store)
        records.append(
            {
                "chk_watermark": chk_watermark,
                "detail": result.detail,
                "mutant_id": result.mutant_id,
                "outcome": result.outcome,
                "suspect_chk_ids": list(result.suspect_chk_ids),
                "verifier_id": result.verifier_id,
                "witness_exit": result.witness_exit,
            }
        )
        store.set_meta(META_AUDIT_LOG, json.dumps(records, sort_keys=True))
        if advance_watermark:
            store.set_meta(
                _watermark_key(result.verifier_id), str(chk_watermark)
            )
        if result.suspect_chk_ids:
            merged = sorted(
                set(suspect_chk_ids(store, result.verifier_id))
                | set(result.suspect_chk_ids)
            )
            store.set_meta(
                _suspect_key(result.verifier_id), json.dumps(merged)
            )


def run_mutation_audit(
    store: Store,
    mutant: MutantFixture,
    workspace_root: Path | str,
    verifier: AuditVerifier,
    *,
    verifier_id: str,
    witness_runner: RunWitness | None = None,
    stage: bool = True,
) -> MutationAuditResult:
    """Run one mutation-seeded verifier audit (R24).

    Stages the bad diff into ``workspace_root`` (a disposable checkout of the
    build under audit), executes the witness FIRST — the equivalent-mutant
    guard: a witness observing anything but its expected exit disqualifies
    the mutant and blocks the audit (the verifier is never invoked) — then
    scores the verifier on the mutant build. A false-pass flags the verifier
    and marks every verdict since its last clean audit ``suspect``; a catch
    is a clean audit and advances the watermark (clears the window).
    """
    workspace_root = Path(workspace_root)
    if not workspace_root.is_dir():
        raise CalibrateError(
            f"audit workspace does not exist: {workspace_root}"
        )
    if not verifier_id.strip():
        raise CalibrateError(
            "verifier_id must be non-empty (audit bookkeeping is"
            " per-verifier)"
        )
    if stage:
        stage_mutant(mutant, workspace_root)
    runner = witness_runner or run_witness
    observed = runner(mutant.witness, workspace_root)
    watermark_now = _max_chk_rowid(store)

    if observed != mutant.witness.expected_exit:
        # Equivalent-mutant guard: the bad behavior did not reproduce, so the
        # mutant proves nothing — disqualify it, never score the verifier.
        result = MutationAuditResult(
            mutant_id=mutant.mutant_id,
            verifier_id=verifier_id,
            outcome="disqualified",
            witness_exit=observed,
            suspect_chk_ids=(),
            detail=(
                f"witness expected exit {mutant.witness.expected_exit},"
                f" observed {observed}: mutant disqualified, audit blocked"
                " (equivalent-mutant guard, R24)"
            ),
        )
        _record_audit(
            store, result, chk_watermark=watermark_now, advance_watermark=False
        )
        logger.warning(
            "mutant %s disqualified (witness exit %s != %d)",
            mutant.mutant_id, observed, mutant.witness.expected_exit,
        )
        return result

    verdict = verifier(workspace_root)
    if verdict.passed:
        # False-pass: flag, and mark every verdict since the last clean audit.
        window_start = last_clean_watermark(store, verifier_id)
        rows = store.conn.execute(
            "SELECT id FROM trace_chk WHERE rowid > ? ORDER BY rowid",
            (window_start,),
        ).fetchall()
        suspects = tuple(row["id"] for row in rows)
        result = MutationAuditResult(
            mutant_id=mutant.mutant_id,
            verifier_id=verifier_id,
            outcome="false_pass",
            witness_exit=observed,
            suspect_chk_ids=suspects,
            detail=(
                f"verifier passed witnessed mutant {mutant.mutant_id}:"
                f" flagged; {len(suspects)} verdict(s) since the last clean"
                " audit marked suspect (recorded and surfaced only —"
                " enforcement is Phase 3)"
            ),
        )
        _record_audit(
            store, result, chk_watermark=watermark_now, advance_watermark=False
        )
        logger.warning(
            "verifier %s FALSE-PASSED mutant %s; %d verdict(s) marked suspect",
            verifier_id, mutant.mutant_id, len(suspects),
        )
        return result

    result = MutationAuditResult(
        mutant_id=mutant.mutant_id,
        verifier_id=verifier_id,
        outcome="caught",
        witness_exit=observed,
        suspect_chk_ids=(),
        detail=(
            f"verifier failed witnessed mutant {mutant.mutant_id}: clean"
            " audit; suspect window cleared"
        ),
    )
    _record_audit(
        store, result, chk_watermark=watermark_now, advance_watermark=True
    )
    logger.info(
        "verifier %s caught mutant %s (clean audit)", verifier_id,
        mutant.mutant_id,
    )
    return result


def instrument_health(store: Store, verifier_id: str | None = None) -> dict:
    """The settlement report's instrument-health section (R23/R24): audit
    records, flagged verifiers, and per-verifier suspect verdict ids. No
    timestamps or volatile data — the report must render deterministically
    from its inputs (the U7 discipline)."""
    records = audit_log(store, verifier_id)
    audits = [
        {
            "detail": r["detail"],
            "mutant_id": r["mutant_id"],
            "outcome": r["outcome"],
            "verifier_id": r["verifier_id"],
            "witness_exit": r["witness_exit"],
        }
        for r in records
    ]
    flagged = sorted(
        {r["verifier_id"] for r in records if r["outcome"] == "false_pass"}
    )
    verifier_ids = sorted({r["verifier_id"] for r in records})
    return {
        "flagged_verifiers": flagged,
        "mutation_audits": audits,
        "suspect_chk_ids": {
            vid: list(suspect_chk_ids(store, vid)) for vid in verifier_ids
        },
    }


# --- frozen replay set: bootstrap persistence (R25) ------------------------------------


def persist_frozen_set(
    store: Store,
    scen_ids: Sequence[str],
    path: Path | str,
    *,
    verified_by: str,
) -> dict:
    """Persist hand-verified SCEN verdict pairs (with their judge-input
    payloads, R22) as the frozen replay set — bootstrap only; the re-judging
    harness ships in Plan 4 with its consumer.

    Merges with an existing frozen set at ``path`` (the bootstrap accumulates
    across settlements toward N); a same-id pair must be byte-identical —
    a conflicting re-persist is a hard error, never a silent overwrite.
    """
    if not verified_by.strip():
        raise CalibrateError(
            "verified_by must name who hand-verified these pairs (the frozen"
            " set's provenance, R25)"
        )
    ids = sorted(set(scen_ids))
    if not ids:
        raise CalibrateError(
            "persist_frozen_set needs at least one SCEN id"
        )
    pairs: dict[str, dict] = {}
    for sid in ids:
        row = store.conn.execute(
            "SELECT * FROM trace_scen WHERE id = ?", (sid,)
        ).fetchone()
        if row is None:
            raise CalibrateError(f"SCEN row {sid} does not exist")
        if row["judge_mode"] not in _REJUDGEABLE_MODES:
            raise CalibrateError(
                f"SCEN {sid} settled deterministically"
                f" (judge_mode={row['judge_mode']!r}) — no judge was involved"
                " and there is nothing to re-judge; the frozen set holds"
                " judged pairs only (R25)"
            )
        judge_input = json.loads(row["judge_input_json"])
        if not judge_input:
            raise CalibrateError(
                f"SCEN {sid} carries no judge-input payload — replay"
                " re-judging needs the original inputs (R22)"
            )
        pairs[sid] = {
            "episode_id": row["episode_id"],
            "feat_id": row["feat_id"],
            "judge_input": judge_input,
            "judge_mode": row["judge_mode"],
            "scen_id": sid,
            "snapshot_id": row["snapshot_id"],
            "tier": row["tier"],
            "verdict": row["result"],
            "verified_by": verified_by,
        }

    path = Path(path)
    merged: dict[str, dict] = {}
    if path.exists():
        for pair in load_frozen_set(path)["pairs"]:
            merged[pair["scen_id"]] = pair
    for sid, pair in pairs.items():
        existing = merged.get(sid)
        if existing is not None and existing != pair:
            raise CalibrateError(
                f"frozen set at {path} already holds a different pair for"
                f" {sid} — hand-verified verdicts are append-only, never"
                " silently overwritten (R25)"
            )
        merged[sid] = pair

    doc = {
        "pairs": [merged[k] for k in sorted(merged)],
        "version": FROZEN_SET_VERSION,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(
            json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False)
            + "\n"
        )
    logger.info(
        "frozen replay set at %s now holds %d pair(s)", path, len(merged)
    )
    return doc


def load_frozen_set(path: Path | str) -> dict:
    """Load and shape-check a persisted frozen replay set."""
    path = Path(path)
    if not path.is_file():
        raise CalibrateError(f"frozen replay set not found: {path}")
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CalibrateError(f"invalid JSON in {path}: {exc}") from exc
    if doc.get("version") != FROZEN_SET_VERSION:
        raise CalibrateError(
            f"frozen set at {path} has version {doc.get('version')!r},"
            f" expected {FROZEN_SET_VERSION}"
        )
    pairs = doc.get("pairs")
    if not isinstance(pairs, list):
        raise CalibrateError(f"frozen set at {path} has no 'pairs' list")
    required = {
        "episode_id", "feat_id", "judge_input", "judge_mode", "scen_id",
        "snapshot_id", "tier", "verdict", "verified_by",
    }
    for index, pair in enumerate(pairs):
        missing = required - set(pair)
        if missing:
            raise CalibrateError(
                f"frozen set pair [{index}] is missing"
                f" {sorted(missing)[0]!r} (corrupt frozen set at {path})"
            )
    return doc


def frozen_set_ready(frozen: dict, config: CalibrateConfig) -> bool:
    """True once the frozen set holds the configured N hand-verified pairs
    (the Phase 2 exit criterion, R25)."""
    return len(frozen["pairs"]) >= config.frozen_set_min_size
