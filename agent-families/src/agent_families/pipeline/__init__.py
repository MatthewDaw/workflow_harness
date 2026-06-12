"""add_idea pipeline: the complete registration flow per the decision spine (R5-R11, R14).

Stage order (plan-001 flow diagram): structural validation -> content-hash fast
path (checked against insights AND the merge log, so exact duplicates and
previously-merged ideas exit before any embedding or judging) -> embed with the
pinned local model (``search_document:``) -> cosine merge prefilter (judged,
never silently auto-merged, R8) -> ANN with relevance floor -> placement prompt,
or the cold-start taxonomy-listing prompt when no neighbor clears the floor ->
one transaction writing insight (status=quarantined, batch ID) + vec row +
membership + links (R7). Registration never mints a snapshot (R3).

Judge discipline (R6): one schema for all calls (:data:`JUDGE_SCHEMA`); each
call type carries an allowed-outcome SUBSET enforced application-side through
``extra_validate`` (:func:`outcome_validator`) — an out-of-subset outcome or a
dangling reference (e.g. ``append_to_skill`` naming a nonexistent skill) rides
the same feedback-retry-then-fail path as a schema violation, with no writes in
any failure mode. Prompts carry no volatile data — no timestamps, no absolute
paths, and cosine values are omitted entirely (R23 fixture-key stability).

Non-registration exits are typed exceptions (the CLI, U9, maps
:class:`RegistrationRejected` to a non-zero exit): the generalization lint never
silently rewrites — ``rewrite_proposed`` raises :class:`RewriteProposed` carrying
the rewrite, and re-running with ``accept_rewrite=True`` re-enters the pipeline
at the content-hash check (R10). A ``merge_discard`` whose target is retired
raises :class:`RetiredNearDuplicate` ("revive or override?", R14);
``override_retired=True`` admits the idea fresh via normal placement.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent_families.config import Config
from agent_families.embedding import EmbeddingService, ensure_pins
from agent_families.judge import (
    ADMISSION_GATE_SCHEMA,
    GATE_OUTCOMES,
    OUTCOMES,
    run_judge,
)
from agent_families.store import Store
from agent_families.vecindex import Neighbor, VecIndex

logger = logging.getLogger(__name__)

STRUCTURAL_FIELDS = ("precondition", "action", "expected_outcome")

# Per-call-type allowed-outcome subsets (R6). The merge-review call uses
# `no_placement` as its rejection verdict: "not a duplicate — fall through to
# normal placement" (R8). The taxonomy call is the KTD cold-start contract:
# new_skill or no_placement only.
MERGE_REVIEW_OUTCOMES = ("merge_discard", "no_placement")
PLACEMENT_OUTCOMES = (
    "append_to_skill",
    "new_skill",
    "contradiction_flag",
    "contradiction_supersede",
    "lint_reject",
    "rewrite_proposed",
    "no_placement",
)
TAXONOMY_OUTCOMES = ("new_skill", "no_placement")
CONTRADICTION_OUTCOMES = ("contradiction_flag", "contradiction_supersede")

_REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "precondition": {"type": "string"},
        "action": {"type": "string"},
        "expected_outcome": {"type": "string"},
    },
    "required": list(STRUCTURAL_FIELDS),
    "additionalProperties": False,
}

# The judge contract: one schema, all calls (plan-001 "Judge contract"). Subsets
# and reference integrity are enforced application-side via outcome_validator.
JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(OUTCOMES)},
        "target_skill_id": {"type": "integer"},
        "new_skill": {
            "type": "object",
            "properties": {
                "agent_id": {"type": "integer"},
                "name": {"type": "string"},
                "description": {"type": "string"},
            },
            "required": ["agent_id", "name", "description"],
            "additionalProperties": False,
        },
        "duplicate_of": {"type": "integer"},
        "supersedes": {"type": "integer"},
        "scope_tag": {
            "type": "object",
            "properties": {
                "value": {"type": "string"},
                "justification": {"type": "string"},
            },
            "required": ["value", "justification"],
            "additionalProperties": False,
        },
        "lint": {
            "type": "object",
            "properties": {
                "verdict": {"type": "string", "enum": ["pass", "reject", "rewrite"]},
                "reason": {"type": "string"},
                "rewrite": _REWRITE_SCHEMA,
            },
            "required": ["verdict"],
            "additionalProperties": False,
        },
        "confidence": {"type": "number"},
    },
    "required": ["outcome", "scope_tag", "lint", "confidence"],
    "additionalProperties": False,
}


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- exceptions ----------------------------------------------------------------


class PipelineError(Exception):
    """Base for every add_idea failure or non-registration exit."""


class StructuralValidationError(PipelineError):
    """The idea does not arrive in the structural template (flow exit B1)."""


class RegistrationRejected(PipelineError):
    """Base for judged non-registration exits; nothing was written (R7)."""


class LintRejected(RegistrationRejected):
    """The generalization lint rejected the idea (R10: exit non-zero with reason)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"generalization lint rejected this idea: {reason}")


class RewriteProposed(RegistrationRejected):
    """The lint proposed a rewrite; never applied silently (R10)."""

    def __init__(self, rewrite: dict) -> None:
        self.rewrite = dict(rewrite)
        super().__init__(
            "the generalization lint proposed a rewrite:\n"
            + build_idea_text(
                rewrite["precondition"], rewrite["action"], rewrite["expected_outcome"]
            )
            + "\nRe-run with `--accept-rewrite` to accept."
        )


class NoPlacement(RegistrationRejected):
    """The judge declined to place the idea anywhere."""


class RetiredNearDuplicate(RegistrationRejected):
    """The idea's judged near-duplicate is retired (R14: revive or override?)."""

    def __init__(self, insight_id: int) -> None:
        self.insight_id = insight_id
        super().__init__(
            f"near-duplicate insight {insight_id} was previously retired — revive it"
            " (`af revive`) or override with `--override-retired` to admit this idea"
            " fresh."
        )


# --- result ----------------------------------------------------------------------


@dataclass(frozen=True)
class AddIdeaResult:
    """A successful (exit-0) add_idea outcome.

    ``code``: ``registered`` (insight written, quarantined) | ``exact_duplicate``
    (content hash hit an existing insight) | ``previously_merged`` (content hash
    hit the merge log) | ``merged`` (judge said merge_discard; merge-log row
    written). For the duplicate/merge codes, ``insight_id`` is the EXISTING
    insight's ID (R5/R8).
    """

    code: str
    insight_id: int
    skill_id: int | None
    judge_outcome: str | None
    scope_tag: str | None
    batch_id: int | None
    message: str


# --- content identity --------------------------------------------------------------


def canonical_fields_json(
    precondition: str, action: str, expected_outcome: str
) -> str:
    """Canonical JSON of the structural fields; stored in merge_log rows (R8)."""
    return json.dumps(
        {
            "action": action,
            "expected_outcome": expected_outcome,
            "precondition": precondition,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def content_hash(precondition: str, action: str, expected_outcome: str) -> str:
    """sha256 over the canonical structural fields — the exact-duplicate key (R5)."""
    return hashlib.sha256(
        canonical_fields_json(precondition, action, expected_outcome).encode("utf-8")
    ).hexdigest()


def build_idea_text(precondition: str, action: str, expected_outcome: str) -> str:
    """The idea's full text: embedded at index time and quoted in judge prompts."""
    return (
        f"Precondition: {precondition}\n"
        f"Action: {action}\n"
        f"Expected outcome: {expected_outcome}"
    )


# --- prompt builders (pure, deterministic, no volatile data per R23) ----------------


def _skills_context(store: Store, insight_id: int) -> str:
    rows = store.conn.execute(
        "SELECT s.id AS skill_id, s.name AS skill_name,"
        "       a.id AS agent_id, a.name AS agent_name"
        " FROM skill_members m"
        " JOIN skills s ON s.id = m.skill_id"
        " JOIN agents a ON a.id = s.agent_id"
        " WHERE m.insight_id = ? ORDER BY s.id ASC",
        (insight_id,),
    ).fetchall()
    if not rows:
        return "unplaced"
    return "; ".join(
        f"skill {r['skill_id']} '{r['skill_name']}'"
        f" under agent {r['agent_id']} '{r['agent_name']}'"
        for r in rows
    )


def _neighbor_block(store: Store, neighbors: list[Neighbor]) -> str:
    blocks = []
    for n in neighbors:
        row = store.get_insight(n.insight_id)
        text = build_idea_text(
            row["precondition"], row["action"], row["expected_outcome"]
        )
        indented = "\n".join("  " + line for line in text.splitlines())
        blocks.append(
            f"- insight {n.insight_id} [status={n.status}]"
            f" ({_skills_context(store, n.insight_id)}):\n{indented}"
        )
    return "\n".join(blocks)


def _taxonomy_block(store: Store) -> str:
    lines: list[str] = []
    families = store.conn.execute(
        "SELECT id, name FROM families ORDER BY id ASC"
    ).fetchall()
    for fam in families:
        lines.append(f"- family {fam['id']}: {fam['name']}")
        agents = store.conn.execute(
            "SELECT id, name FROM agents WHERE family_id = ? ORDER BY id ASC",
            (fam["id"],),
        ).fetchall()
        for agent in agents:
            lines.append(f"  - agent {agent['id']}: {agent['name']}")
            skills = store.conn.execute(
                "SELECT id, name FROM skills WHERE agent_id = ? ORDER BY id ASC",
                (agent["id"],),
            ).fetchall()
            for skill in skills:
                lines.append(f"    - skill {skill['id']}: {skill['name']}")
    return "\n".join(lines) if lines else "(empty taxonomy)"


def _scope_line(author_scope_tag: str | None) -> str:
    return author_scope_tag if author_scope_tag is not None else "(none)"


def build_merge_prompt(
    store: Store, idea_text: str, candidates: list[Neighbor]
) -> str:
    """Merge-review prompt: prefilter hits are judged, never silently merged (R8)."""
    return (
        "You are the merge-review judge for an insight library. A newly submitted"
        " idea is a near-duplicate (by embedding similarity) of the existing"
        " insight(s) listed below. Decide whether it is a true duplicate.\n"
        "Allowed outcomes for this call: merge_discard (set duplicate_of to the"
        " duplicated insight's id) or no_placement (not a duplicate; the idea"
        " proceeds to normal placement).\n\n"
        f"New idea:\n{idea_text}\n\n"
        f"Near-duplicate candidates:\n{_neighbor_block(store, candidates)}"
    )


def build_placement_prompt(
    store: Store,
    idea_text: str,
    neighbors: list[Neighbor],
    author_scope_tag: str | None,
) -> str:
    """Placement prompt over the ANN neighbors that cleared the relevance floor."""
    return (
        "You are the placement judge for an insight library. Place the newly"
        " submitted idea relative to its nearest existing insights, apply the"
        " generalization lint (reject or rewrite ideas that name target"
        " internals), and confirm or override the author's scope tag.\n"
        "Allowed outcomes for this call: append_to_skill, new_skill,"
        " contradiction_flag, contradiction_supersede, lint_reject,"
        " rewrite_proposed, no_placement.\n"
        "For contradiction outcomes, set supersedes to the contradicted insight's"
        " id and still place the new idea (target_skill_id or new_skill).\n"
        f"Author-proposed scope tag: {_scope_line(author_scope_tag)}\n\n"
        f"New idea:\n{idea_text}\n\n"
        f"Nearest existing insights:\n{_neighbor_block(store, neighbors)}"
    )


def build_taxonomy_prompt(
    store: Store, idea_text: str, author_scope_tag: str | None
) -> str:
    """Cold-start prompt: no neighbor cleared the relevance floor (KTD cold start)."""
    return (
        "You are the placement judge for an insight library. No existing insight"
        " is sufficiently similar to the newly submitted idea, so place it"
        " against the taxonomy listing below.\n"
        "Allowed outcomes for this call: new_skill or no_placement.\n"
        f"Author-proposed scope tag: {_scope_line(author_scope_tag)}\n\n"
        f"New idea:\n{idea_text}\n\n"
        f"Taxonomy:\n{_taxonomy_block(store)}"
    )


# --- application-side outcome validation (R6) ----------------------------------------


def _placement_violation(store: Store, output: dict, outcome: str) -> str | None:
    skill_id = output.get("target_skill_id")
    new_skill = output.get("new_skill")
    if outcome == "append_to_skill" and skill_id is None:
        return "append_to_skill requires target_skill_id"
    if outcome == "new_skill" and new_skill is None:
        return "new_skill outcome requires the new_skill object"
    if skill_id is None and new_skill is None:
        return f"{outcome} requires a placement: set target_skill_id or new_skill"
    if skill_id is not None and new_skill is not None:
        return "set only one of target_skill_id / new_skill"
    if skill_id is not None:
        row = store.conn.execute(
            "SELECT 1 FROM skills WHERE id = ?", (skill_id,)
        ).fetchone()
        if row is None:
            return f"target_skill_id references nonexistent skill {skill_id}"
        return None
    agent_id = new_skill["agent_id"]
    if not new_skill["name"].strip():
        return "new_skill.name must be non-empty"
    row = store.conn.execute(
        "SELECT 1 FROM agents WHERE id = ?", (agent_id,)
    ).fetchone()
    if row is None:
        return f"new_skill.agent_id references nonexistent agent {agent_id}"
    taken = store.conn.execute(
        "SELECT id FROM skills WHERE agent_id = ? AND name = ?",
        (agent_id, new_skill["name"]),
    ).fetchone()
    if taken is not None:
        return (
            f"new_skill name '{new_skill['name']}' already exists under agent"
            f" {agent_id} (skill {taken['id']}); use append_to_skill instead"
        )
    return None


def outcome_validator(store: Store, allowed: tuple[str, ...]):
    """``extra_validate`` closure for run_judge: subset + reference integrity (R6).

    Any violation message it returns rides the judge's feedback-retry path and,
    after the configured retries, hard-fails with zero writes.
    """

    def _validate(output: dict) -> str | None:
        outcome = output.get("outcome")
        if outcome not in allowed:
            return (
                f"outcome '{outcome}' is not in the allowed subset for this call"
                f" type: {', '.join(allowed)}"
            )
        if outcome == "merge_discard":
            duplicate_of = output.get("duplicate_of")
            if duplicate_of is None:
                return "merge_discard requires duplicate_of"
            if store.get_insight(duplicate_of) is None:
                return f"duplicate_of references nonexistent insight {duplicate_of}"
        if outcome in ("append_to_skill", "new_skill", *CONTRADICTION_OUTCOMES):
            violation = _placement_violation(store, output, outcome)
            if violation is not None:
                return violation
        if outcome in CONTRADICTION_OUTCOMES:
            incumbent = output.get("supersedes")
            if incumbent is None:
                return f"{outcome} requires supersedes (the contradicted insight's id)"
            if store.get_insight(incumbent) is None:
                return f"supersedes references nonexistent insight {incumbent}"
        if outcome == "rewrite_proposed":
            rewrite = (output.get("lint") or {}).get("rewrite")
            if rewrite is None:
                return (
                    "rewrite_proposed requires lint.rewrite carrying the rewritten"
                    " structural fields"
                )
            for key in STRUCTURAL_FIELDS:
                if not str(rewrite.get(key, "")).strip():
                    return f"lint.rewrite.{key} must be non-empty"
        return None

    return _validate


# --- the pipeline -----------------------------------------------------------------------


def _validate_structure(precondition: str, action: str, expected_outcome: str) -> None:
    for name, value in (
        ("precondition", precondition),
        ("action", action),
        ("expected_outcome", expected_outcome),
    ):
        if not isinstance(value, str) or not value.strip():
            raise StructuralValidationError(
                f"structural field '{name}' must be a non-empty string — ideas"
                " arrive in the precondition / action / expected-outcome template"
                " (DESIGN §4); free prose is bounced to its author."
            )


def _resolve_scope_tag(output: dict, author_scope_tag: str | None) -> str | None:
    """The judge confirms or overrides the author's scope tag; overrides are logged."""
    judge_value = output["scope_tag"]["value"].strip() or None
    if judge_value is None:
        return author_scope_tag
    if author_scope_tag is not None and judge_value != author_scope_tag:
        logger.warning(
            "judge overrode author scope tag %r with %r (justification: %s)",
            author_scope_tag,
            judge_value,
            output["scope_tag"]["justification"],
        )
    return judge_value


# --- admission gate (Operation 1, R10) -------------------------------------------------


@dataclass(frozen=True)
class AdmittedAtom:
    """Operation 1's output: one generalized, transferable schema'd atom.

    The admission gate is the standalone FRONT stage of the R3 ingest path: it
    runs *before* any embedding so the key/full vectors (U6) are computed on this
    GENERALIZED text, never on the raw input (R10). ``negative_scope`` ("when NOT
    to apply") is mandatory; ``rationale`` ("because Z") is optional.
    """

    precondition: str
    action: str
    expected_outcome: str
    negative_scope: str
    scope_tag: str | None
    rationale: str | None = None


def build_admission_gate_prompt(
    precondition: str,
    action: str,
    expected_outcome: str,
    author_scope_tag: str | None,
) -> str:
    """Operation 1 prompt: extract-by-contrast -> generalize -> altitude-audit.

    Pure and volatile-data-free (R23) so the offline suite replays it byte-stably.
    """
    raw = build_idea_text(precondition, action, expected_outcome)
    return (
        "You are the admission gate (Operation 1) for an insight library. Turn the"
        " raw, possibly hyper-specific idea below into ONE transferable atom by"
        " applying three sub-stages in order:\n"
        "1. Extract-by-contrast: state the rule as a precondition / action /"
        " expected-outcome triple, dropping narration.\n"
        "2. Generalize-by-typed-substitution: strip instance trivia — file paths,"
        " repo names, literal values, host names — and replace each with a TYPED"
        " placeholder (e.g. <FILE>, <REPO>, <PORT>) so the atom transfers across"
        " targets. The admitted atom must name NO concrete instance literal.\n"
        "3. Decontextualize + altitude-audit: test the atom against one"
        " over-general misfire and one over-specific non-application, and emit"
        " negative_scope ('when NOT to apply'). If the input is target-trivia with"
        " no transferable rule (or fails the altitude audit), return lint_reject"
        " with a reason. If it is salvageable only by rewriting, return"
        " rewrite_proposed carrying the rewritten atom.\n"
        "Allowed outcomes for this call: admit (return the generalized atom),"
        " lint_reject (reason), rewrite_proposed (atom = the proposed rewrite)."
        " Confirm or override the author's scope tag.\n"
        f"Author-proposed scope tag: {_scope_line(author_scope_tag)}\n\n"
        f"Raw idea:\n{raw}"
    )


def gate_outcome_validator():
    """``extra_validate`` for the admission gate: per-verdict field requirements.

    Any violation rides run_judge's feedback-retry-then-fail path with zero side
    effects (R6). ``lint_reject`` carries a reason and no atom; ``admit`` and
    ``rewrite_proposed`` carry a fully-populated atom (incl. negative_scope).
    """

    def _validate(output: dict) -> str | None:
        outcome = output.get("outcome")
        if outcome not in GATE_OUTCOMES:
            return (
                f"outcome '{outcome}' is not an admission-gate verdict:"
                f" {', '.join(GATE_OUTCOMES)}"
            )
        if outcome == "lint_reject":
            if not str(output.get("reason", "")).strip():
                return "lint_reject requires a non-empty reason"
            return None
        atom = output.get("atom")
        if atom is None:
            return f"{outcome} requires the generalized atom object"
        for key in ("precondition", "action", "expected_outcome", "negative_scope"):
            if not str(atom.get(key, "")).strip():
                return f"atom.{key} must be non-empty for an admitted atom"
        return None

    return _validate


def run_admission_gate(
    config: Config,
    *,
    precondition: str,
    action: str,
    expected_outcome: str,
    scope_tag: str | None = None,
    accept_rewrite: bool = False,
    judge_mode: str | None = None,
    judge_fixtures_dir: str | Path | None = None,
) -> AdmittedAtom:
    """Operation 1: generalize raw input into a transferable atom, BEFORE embed.

    The single front-gate ``run_judge`` call of the R3 ingest path. On admission
    the returned :class:`AdmittedAtom` carries the GENERALIZED text — U6 embeds
    the key/full vectors on it, never on the raw input (R10). Raises
    :class:`LintRejected` (target-trivia / failed altitude audit) or
    :class:`RewriteProposed` (the proposed rewrite is never silently adopted;
    re-run it with ``accept_rewrite=True``), so the gate exits before any
    downstream embed/insert on a rejection (R7). ``accept_rewrite`` only skips the
    structural-template check on re-entry — the rewritten atom arrives already in
    template form, judge-authored.
    """
    if not accept_rewrite:
        _validate_structure(precondition, action, expected_outcome)
    prompt = build_admission_gate_prompt(
        precondition, action, expected_outcome, scope_tag
    )
    result = run_judge(
        prompt,
        ADMISSION_GATE_SCHEMA,
        model=config.judge.model,
        max_retries=config.judge.max_retries,
        bare=config.judge.bare,
        extra_validate=gate_outcome_validator(),
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )
    output = result.output
    outcome = output["outcome"]
    if outcome == "lint_reject":
        reason = (output.get("reason") or "").strip()
        raise LintRejected(
            reason or "the idea is target-trivia with no transferable rule"
        )
    if outcome == "rewrite_proposed":
        # Never silently adopted — surfaced for `--accept-rewrite` re-entry (R10).
        raise RewriteProposed(output["atom"])
    atom = output["atom"]
    final_scope_tag = _resolve_scope_tag(output, scope_tag)
    return AdmittedAtom(
        precondition=atom["precondition"],
        action=atom["action"],
        expected_outcome=atom["expected_outcome"],
        negative_scope=atom["negative_scope"],
        scope_tag=final_scope_tag,
        rationale=(atom.get("rationale") or None),
    )


def add_idea(
    store: Store,
    vec: VecIndex,
    embedder: EmbeddingService,
    config: Config,
    *,
    precondition: str,
    action: str,
    expected_outcome: str,
    batch_label: str,
    scope_tag: str | None = None,
    accept_rewrite: bool = False,
    override_retired: bool = False,
    judge_mode: str | None = None,
    judge_fixtures_dir: str | Path | None = None,
) -> AddIdeaResult:
    """Register one idea through the decision spine; returns only exit-0 outcomes.

    Non-registration exits raise: :class:`StructuralValidationError`,
    :class:`LintRejected`, :class:`RewriteProposed` (re-run the rewrite with
    ``accept_rewrite=True``, which re-enters at the content-hash check, R10),
    :class:`NoPlacement`, :class:`RetiredNearDuplicate` (R14), and any
    :class:`~agent_families.judge.JudgeError` — in every raising path zero rows
    are written (R7): all writes happen after the judge returns, inside one
    transaction. ``batch_label`` is resolved to a batch row at write time so a
    failed registration leaves no batch row either (R11).
    """
    if not accept_rewrite:
        # An accepted rewrite was authored by the judge in the structural
        # template; the flow re-enters at the content-hash check (R10).
        _validate_structure(precondition, action, expected_outcome)

    fields = {
        "precondition": precondition,
        "action": action,
        "expected_outcome": expected_outcome,
    }
    idea_hash = content_hash(precondition, action, expected_outcome)

    existing = store.find_insight_by_hash(idea_hash)
    if existing is not None:
        return AddIdeaResult(
            code="exact_duplicate",
            insight_id=existing["id"],
            skill_id=None,
            judge_outcome=None,
            scope_tag=existing["scope_tag"],
            batch_id=existing["batch_id"],
            message=f"exact duplicate of insight {existing['id']} (content hash)",
        )
    merged = store.find_merge_log_by_hash(idea_hash)
    if merged is not None:
        return AddIdeaResult(
            code="previously_merged",
            insight_id=merged["duplicate_of"],
            skill_id=None,
            judge_outcome=None,
            scope_tag=None,
            batch_id=merged["batch_id"],
            message=(
                f"previously judged a duplicate of insight {merged['duplicate_of']}"
                " (merge-log fast path)"
            ),
        )

    ensure_pins(store, config.embedding)
    idea_text = build_idea_text(precondition, action, expected_outcome)
    vector = embedder.embed_document(idea_text)
    # Add-idea dedup view: all statuses (R13), over the retrieval column the
    # legacy embed_document vector lives in. The U3 flattener fix lets us call
    # VecIndex.knn directly (the old out-of-band dedup-view helper is deleted).
    neighbors = vec.knn(
        vector, config.retrieval.ann_top_k, statuses=None, on="retrieval"
    )

    judge_kwargs = dict(
        model=config.judge.model,
        max_retries=config.judge.max_retries,
        bare=config.judge.bare,
        mode=judge_mode,
        fixtures_dir=judge_fixtures_dir,
    )

    # Merge prefilter: similarity (1 - cosine distance) at or above the threshold
    # short-circuits to merge review — judged, never silently merged (R8).
    merge_candidates = [
        n
        for n in neighbors
        if 1.0 - n.distance >= config.merge.cosine_threshold
    ]
    if merge_candidates:
        result = run_judge(
            build_merge_prompt(store, idea_text, merge_candidates),
            JUDGE_SCHEMA,
            extra_validate=outcome_validator(store, MERGE_REVIEW_OUTCOMES),
            **judge_kwargs,
        )
        if result.output["outcome"] == "merge_discard":
            duplicate_of = result.output["duplicate_of"]
            duplicate = store.get_insight(duplicate_of)
            if duplicate["status"] == "retired":
                if not override_retired:
                    raise RetiredNearDuplicate(duplicate_of)
                logger.info(
                    "overriding retired near-duplicate %d; admitting the idea fresh",
                    duplicate_of,
                )
                # fall through to placement (R14 override path)
            else:
                with store.transaction():
                    batch_id = store.ensure_batch(batch_label)
                    store.insert_merge_log(
                        content_hash=idea_hash,
                        structural_fields_json=canonical_fields_json(
                            precondition, action, expected_outcome
                        ),
                        duplicate_of=duplicate_of,
                        batch_id=batch_id,
                    )
                return AddIdeaResult(
                    code="merged",
                    insight_id=duplicate_of,
                    skill_id=None,
                    judge_outcome="merge_discard",
                    scope_tag=None,
                    batch_id=batch_id,
                    message=(
                        f"judged a duplicate of insight {duplicate_of}; discarded"
                        " with a merge-log row"
                    ),
                )
        # no_placement = merge rejected: fall through to normal placement (R8).

    placement_neighbors = [
        n
        for n in neighbors
        if 1.0 - n.distance >= config.retrieval.relevance_floor
    ]
    if placement_neighbors:
        prompt = build_placement_prompt(
            store, idea_text, placement_neighbors, scope_tag
        )
        allowed = PLACEMENT_OUTCOMES
    else:
        prompt = build_taxonomy_prompt(store, idea_text, scope_tag)
        allowed = TAXONOMY_OUTCOMES
    result = run_judge(
        prompt,
        JUDGE_SCHEMA,
        extra_validate=outcome_validator(store, allowed),
        **judge_kwargs,
    )
    output = result.output
    outcome = output["outcome"]

    if outcome == "no_placement":
        raise NoPlacement("judge declined to place this idea (no_placement)")
    if outcome == "lint_reject":
        reason = (output["lint"].get("reason") or "").strip()
        raise LintRejected(reason or "the idea names target internals")
    if outcome == "rewrite_proposed":
        raise RewriteProposed(output["lint"]["rewrite"])

    final_scope_tag = _resolve_scope_tag(output, scope_tag)
    supersedes_id = (
        output["supersedes"] if outcome == "contradiction_supersede" else None
    )
    incumbent_id = output["supersedes"] if outcome in CONTRADICTION_OUTCOMES else None

    # The single registration transaction (R7): insight + vec + membership +
    # links, all-or-nothing, status=quarantined, no snapshot minted (R3).
    with store.transaction():
        batch_id = store.ensure_batch(batch_label)
        new_skill = output.get("new_skill")
        if new_skill is not None:
            skill_id = store.create_skill(
                new_skill["agent_id"],
                new_skill["name"],
                new_skill["description"],
                created_batch_id=batch_id,
            )
        else:
            skill_id = output["target_skill_id"]
        insight_id = store.insert_insight(
            **fields,
            content_hash=idea_hash,
            scope_tag=final_scope_tag,
            status="quarantined",
            batch_id=batch_id,
            embedding_model=config.embedding.model,
            embedding_dim=config.embedding.dim,
            supersedes=supersedes_id,
        )
        vec.insert(insight_id, vector)
        store.append_member(skill_id, insight_id)
        if incumbent_id is not None:
            # Flag only: the incumbent stays active; retiring it is a manual
            # decision surfaced by `status` (R9 — automated retirement is a
            # Phase 3 ratchet-governance seam).
            store.conn.execute(
                "INSERT INTO contradictions"
                " (challenger_id, incumbent_id, status, opened_at)"
                " VALUES (?, ?, 'open', ?)",
                (insight_id, incumbent_id, _utcnow()),
            )

    return AddIdeaResult(
        code="registered",
        insight_id=insight_id,
        skill_id=skill_id,
        judge_outcome=outcome,
        scope_tag=final_scope_tag,
        batch_id=batch_id,
        message=(
            f"registered insight {insight_id} (quarantined) in skill {skill_id}"
            f" via {outcome}"
        ),
    )
