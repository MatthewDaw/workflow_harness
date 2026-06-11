"""Grader-side FEAT registry: pre-research + runtime-confirmed mint (plan-003 U3).

"Source proposes, runtime confirms" (R8): pre-research enumerates candidate
behaviors from the pinned source checkout, the routes in ``urls.py``, and UI
traversal observations — but a FEAT row exists ONLY once the candidate has
been confirmed on the *running* app with captured evidence (an a11y snapshot;
screenshots ride along as refs). A source-only feature (dead code, disabled
flag) is never minted.

FEAT identity discipline (R9): IDs are derived deterministically from the
candidate's stable behavior key (``FEAT-<key>``), so a registry refresh can
never renumber; the store's triggers (003 U1) forbid ID updates and row
deletes outright, so IDs are never reused — a vanished feature flips to
``deprecated`` (its scenario manifests archive, its frontier entry drops,
MSG history stays valid). Every row is stamped with the target image digest;
the digest pin itself is enforced at setup by :mod:`.target_env` (U2).

Source acquisition is pinned to the image: ``git clone --depth 1 --branch
v1.45.0`` into ``agent-families/targets/linkding/source/`` (gitignored via
the sibling ``.gitignore``), and the checkout's exact tag is verified against
the tag half of the image pin — the digest half is U2's hard setup error, so
a drifted source and a drifted image are both loud.

The pre-research session runs through Phase 1's ``run_session`` seam with a
**grader profile**: Read/Grep scoped to the source checkout (the session
cwd) plus Bash scoped to caller-named source-inspection commands. There is
deliberately NO browse tool — UI evidence flows through the
orchestrator-mediated browse channel (the R15 structural-containment
pattern), an injectable ``browse`` callable here so the offline suite runs
on scripted fakes with zero quota and no docker.

Newly-discovered flow (R10): the explorer enqueues an observation WITH its
UI evidence -> a grader-side confirmation step mints the FEAT row + its
scenario manifest(s) -> only then is the feature mentionable (the
``trace_msg_mentions`` FK enforces it mechanically). :class:`DiscoveryQueue`
is in-process in this unit; the explorer/episode units (U5/U6) own wiring it
into their loops.

Live verification procedure (documented, NOT in CI — needs Docker, a
logged-in claude CLI, and a Playwright-backed browse executor): bring the
target up and seed it (U2), ``ensure_source_checkout(...)``, run
``run_pre_research(...)`` live with a grader profile, then
``refresh_registry(...)`` with the live browse channel and assert
``registry_in_expected_range(store, "linkding")`` — the expected surface is
~18 areas / 40–60 testable behaviors (:data:`EXPECTED_BEHAVIOR_RANGE`, R8).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from agent_families.grading.target_env import (
    LINKDING_IMAGE_TAG,
    Runner,
)
from agent_families.pipeline.planning import LintFinding, SEVERITY_ERROR
from agent_families.pipeline.sessions import RoleProfile, run_session
from agent_families.reflector.stage_a import PROBE_TAXONOMY
from agent_families.store import DEC_STATUSES, FRONTIER_STATUSES, SCENARIO_TIERS, WORLDS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agent_families.store import Store

logger = logging.getLogger(__name__)

# --- pins and expectations -----------------------------------------------------

SOURCE_REPO_URL = "https://github.com/sissbruecker/linkding.git"
# The source tag is the tag half of the image pin (R9): image
# sissbruecker/linkding:1.45.0@sha256:... <-> source tag v1.45.0. Re-pinning
# either is a deliberate apparatus migration that re-runs pre-research.
SOURCE_TAG = f"v{LINKDING_IMAGE_TAG}"

# R8's expected linkding surface — plan-pinned verification values (protocol
# constants for the documented live procedure, not behavior tunables).
EXPECTED_AREA_COUNT = 18
EXPECTED_BEHAVIOR_RANGE = (40, 60)

# Stable behavior keys: permanent identity, FEAT-<key> forever (R9).
_KEY_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

# Process-hygiene constant (not a behavior tunable): a shallow clone of the
# pinned tag over a normal connection finishes well inside this.
_GIT_TIMEOUT_S = 600.0


class RegistryError(Exception):
    """Registry misuse or a broken invariant, with an actionable message."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- seams ----------------------------------------------------------------------

# The orchestrator-mediated browse channel (R15 pattern): one structured
# browse request ``{action, selector, args}`` in, one observation out:
# ``{"status": "ok" | "element_absent" | "error", "a11y": <snapshot>,
#   "screenshot_ref": <optional str>}``. Live, this is Playwright driven by
# the orchestrator; offline it is a scripted fake.
Browse = Callable[[dict], dict]


def _run_git(argv: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - argv is module-constructed
            list(argv),
            capture_output=True,
            encoding="utf-8",  # Windows KTD: never the locale codepage
            errors="replace",
            timeout=_GIT_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise RegistryError(
            "git is not installed or not on PATH; source acquisition needs it"
            " (plan-003 U3)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RegistryError(
            f"git command timed out after {_GIT_TIMEOUT_S}s:"
            f" {subprocess.list2cmdline(list(argv))}"
        ) from exc


# --- source acquisition (pinned) -------------------------------------------------


def ensure_source_checkout(
    dest: str | Path,
    *,
    runner: Runner | None = None,
    repo_url: str = SOURCE_REPO_URL,
    tag: str = SOURCE_TAG,
) -> Path:
    """Clone (depth 1, pinned tag) or verify the target source checkout.

    The checkout must sit at exactly ``tag`` — the tag half of the image pin
    whose digest half :mod:`.target_env` enforces at setup (R9). A drifted
    checkout is a hard error, never silently re-used.
    """
    run = runner or _run_git
    dest = Path(dest)
    if not (dest / ".git").exists():
        result = run(
            ["git", "clone", "--depth", "1", "--branch", tag, repo_url, str(dest)]
        )
        if result.returncode != 0:
            raise RegistryError(
                f"pinned source clone failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )
    result = run(["git", "-C", str(dest), "describe", "--tags", "--exact-match"])
    if result.returncode != 0:
        raise RegistryError(
            f"source checkout at {dest} is not at an exact tag (exit"
            f" {result.returncode}): {result.stderr.strip()} — expected {tag},"
            " the tag half of the pinned image digest (R9); re-clone, don't"
            " patch in place"
        )
    found = result.stdout.strip().splitlines()[-1].strip() if result.stdout.strip() else ""
    if found != tag:
        raise RegistryError(
            f"source checkout at {dest} is at tag {found!r} but the image"
            f" digest pin expects {tag} (R9): the registry enumerated from a"
            " mismatched source would silently drift from the running target."
            " Re-clone the pinned tag, or re-pin image + source together and"
            " re-run registry pre-research."
        )
    return dest


# --- candidates -------------------------------------------------------------------


@dataclass(frozen=True)
class FeatureCandidate:
    """One source-proposed testable behavior, pending runtime confirmation.

    ``key`` is the permanent identity (``FEAT-<key>``); ``confirm_steps`` are
    the structured browse requests the grader executes on the running app to
    capture evidence; ``scenario_steps`` are the NL steps the mint-time
    scenario manifest carries (U4's format: NL steps + expected outcome +
    tolerance tier).
    """

    key: str
    area: str
    behavior: str
    route: str
    confirm_steps: tuple[dict, ...]
    scenario_steps: tuple[str, ...]
    expected_outcome: str
    tier: str

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key):
            raise RegistryError(
                f"candidate key {self.key!r} must be kebab-case"
                " ([a-z0-9]+(-[a-z0-9]+)*): keys are permanent FEAT identity"
                " (R9)"
            )
        for field_name in ("area", "behavior", "route", "expected_outcome"):
            if not str(getattr(self, field_name)).strip():
                raise RegistryError(
                    f"candidate {self.key!r}: '{field_name}' must be non-empty"
                )
        if self.tier not in SCENARIO_TIERS:
            raise RegistryError(
                f"candidate {self.key!r}: tier {self.tier!r} must be one of"
                f" {SCENARIO_TIERS}"
            )
        if not self.confirm_steps:
            raise RegistryError(
                f"candidate {self.key!r}: confirm_steps must be non-empty —"
                " runtime confirmation is what minting means (R8)"
            )
        for i, step in enumerate(self.confirm_steps):
            if (
                not isinstance(step, dict)
                or not str(step.get("action", "")).strip()
                or "selector" not in step
                or not isinstance(step.get("args"), dict)
            ):
                raise RegistryError(
                    f"candidate {self.key!r}: confirm step #{i} must be a"
                    " browse request {{action, selector, args}} (R15 shape)"
                )
        if not self.scenario_steps or not all(
            isinstance(s, str) and s.strip() for s in self.scenario_steps
        ):
            raise RegistryError(
                f"candidate {self.key!r}: scenario_steps must be non-empty NL"
                " steps (the mint-time manifest, R16)"
            )


def feat_id(key: str) -> str:
    """The permanent FEAT ID for a behavior key — derivation, never allocation,
    so a refresh cannot renumber (R9)."""
    if not _KEY_RE.fullmatch(key):
        raise RegistryError(f"invalid behavior key {key!r}")
    return f"FEAT-{key}"


def candidate_from_payload(payload: dict) -> FeatureCandidate:
    return FeatureCandidate(
        key=payload["key"],
        area=payload["area"],
        behavior=payload["behavior"],
        route=payload["route"],
        confirm_steps=tuple(dict(s) for s in payload["confirm_steps"]),
        scenario_steps=tuple(payload["scenario_steps"]),
        expected_outcome=payload["expected_outcome"],
        tier=payload["tier"],
    )


def candidates_from_output(output: dict) -> list[FeatureCandidate]:
    """Pre-research structured output -> validated candidates, unique keys."""
    raw = output.get("candidates") or []
    if not raw:
        raise RegistryError(
            "pre-research proposed zero candidates; the linkding surface is"
            f" ~{EXPECTED_AREA_COUNT} areas (R8) — an empty enumeration is a"
            " session failure, not a finding"
        )
    candidates = [candidate_from_payload(p) for p in raw]
    seen: set[str] = set()
    for cand in candidates:
        if cand.key in seen:
            raise RegistryError(
                f"duplicate candidate key {cand.key!r}: keys are permanent"
                " FEAT identity and must be unique (R9)"
            )
        seen.add(cand.key)
    return candidates


# --- decision candidates (007 KTD1: DEC is a sibling of FEAT) ----------------------

# The known DEC categories ARE the probe-question taxonomy (007 KTD1 — "category
# keys into the probe-question taxonomy"): one shared vocabulary, not a parallel
# list. A decision whose category is not in here is FLAGGED (proposed), never
# rejected — §17 grow-by-exception. Clustering DEC categories across targets is
# the taxonomy's data-driven seeding substrate.
KNOWN_DEC_CATEGORIES: frozenset[str] = frozenset(PROBE_TAXONOMY)


@dataclass(frozen=True)
class DecisionCandidate:
    """One source-proposed design decision, pending runtime confirmation.

    Mirrors :class:`FeatureCandidate` (007 KTD1: same instruments, second
    question) but DECs do not carry scenario manifests or frontier entries —
    DEC does not enter the frontier ledger; its coverage is a lint + settlement
    concern. ``key`` is the permanent identity (``DEC-<key>``); ``confirm_steps``
    are the structured browse requests the grader runs on the running app to
    observe the decision in force; ``route`` is the source evidence the
    candidate came from.
    """

    key: str
    category: str
    description: str
    route: str
    confirm_steps: tuple[dict, ...]

    def __post_init__(self) -> None:
        if not _KEY_RE.fullmatch(self.key):
            raise RegistryError(
                f"decision key {self.key!r} must be kebab-case"
                " ([a-z0-9]+(-[a-z0-9]+)*): keys are permanent DEC identity"
                " (007 KTD1)"
            )
        for field_name in ("category", "description", "route"):
            if not str(getattr(self, field_name)).strip():
                raise RegistryError(
                    f"decision {self.key!r}: '{field_name}' must be non-empty"
                )
        if not self.confirm_steps:
            raise RegistryError(
                f"decision {self.key!r}: confirm_steps must be non-empty —"
                " runtime confirmation is what minting means (007 KTD1: source"
                " proposes, runtime confirms)"
            )
        for i, step in enumerate(self.confirm_steps):
            if (
                not isinstance(step, dict)
                or not str(step.get("action", "")).strip()
                or "selector" not in step
                or not isinstance(step.get("args"), dict)
            ):
                raise RegistryError(
                    f"decision {self.key!r}: confirm step #{i} must be a"
                    " browse request {{action, selector, args}} (R15 shape)"
                )

    @property
    def in_taxonomy(self) -> bool:
        """Whether this decision's category is a known probe-taxonomy category
        (007 KTD1). An out-of-taxonomy category is flagged, never rejected."""
        return self.category in KNOWN_DEC_CATEGORIES


def dec_id(key: str) -> str:
    """The permanent DEC ID for a decision key — derivation, never allocation,
    so a refresh cannot renumber (007 KTD1, the FEAT identity discipline)."""
    if not _KEY_RE.fullmatch(key):
        raise RegistryError(f"invalid decision key {key!r}")
    return f"DEC-{key}"


def decision_from_payload(payload: dict) -> DecisionCandidate:
    return DecisionCandidate(
        key=payload["key"],
        category=payload["category"],
        description=payload["description"],
        route=payload["route"],
        confirm_steps=tuple(dict(s) for s in payload["confirm_steps"]),
    )


def decisions_from_output(output: dict) -> list[DecisionCandidate]:
    """Pre-research structured output -> validated decisions, unique keys.

    The ``decisions`` array is optional in the contract (a FEAT-only
    enumeration is legal): a missing or empty array yields no decisions, not an
    error. Unlike candidates, an empty decision set is NOT a session failure
    here — the granularity/coverage discipline lives downstream.
    """
    raw = output.get("decisions") or []
    decisions = [decision_from_payload(p) for p in raw]
    seen: set[str] = set()
    for dec in decisions:
        if dec.key in seen:
            raise RegistryError(
                f"duplicate decision key {dec.key!r}: keys are permanent DEC"
                " identity and must be unique (007 KTD1)"
            )
        seen.add(dec.key)
    return decisions


def out_of_taxonomy_categories(
    decisions: Sequence[DecisionCandidate],
) -> tuple[str, ...]:
    """The novel DEC categories in a decision set — the §17 grow-by-exception
    telemetry. Out-of-taxonomy categories are surfaced (for taxonomy growth),
    never used to reject a decision; first-seen order, de-duplicated."""
    flagged: list[str] = []
    for dec in decisions:
        if not dec.in_taxonomy and dec.category not in flagged:
            flagged.append(dec.category)
    return tuple(flagged)


# --- the pre-research session (grader profile over run_session) -------------------

_BROWSE_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string"},
        "selector": {"type": "string"},
        "args": {"type": "object"},
    },
    "required": ["action", "selector", "args"],
    "additionalProperties": False,
}

_CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "area": {"type": "string"},
        "behavior": {"type": "string"},
        "route": {"type": "string"},
        "confirm_steps": {"type": "array", "items": _BROWSE_STEP_SCHEMA},
        "scenario_steps": {"type": "array", "items": {"type": "string"}},
        "expected_outcome": {"type": "string"},
        "tier": {"type": "string", "enum": list(SCENARIO_TIERS)},
    },
    "required": [
        "key",
        "area",
        "behavior",
        "route",
        "confirm_steps",
        "scenario_steps",
        "expected_outcome",
        "tier",
    ],
    "additionalProperties": False,
}

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "category": {"type": "string"},
        "description": {"type": "string"},
        "route": {"type": "string"},
        "confirm_steps": {"type": "array", "items": _BROWSE_STEP_SCHEMA},
    },
    "required": ["key", "category", "description", "route", "confirm_steps"],
    "additionalProperties": False,
}

PRE_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {"type": "array", "items": _CANDIDATE_SCHEMA},
        # The DEC sibling array (007 KTD1): the same pre-research pass, a second
        # output table. Optional in the contract (NOT in `required`) so a
        # FEAT-only enumeration stays valid — an empty/absent `decisions` array
        # is legal; a real target is expected to yield non-empty.
        "decisions": {"type": "array", "items": _DECISION_SCHEMA},
    },
    "required": ["candidates"],
    "additionalProperties": False,
}

_READ_TOOLS = ("Read", "Glob", "Grep", "LS")


def grader_profile(
    *,
    model: str,
    max_turns: int,
    timeout_s: float,
    source_commands: tuple[str, ...] = (),
) -> RoleProfile:
    """The pre-research session's surface (R8): read tools scoped to the
    source checkout (the session cwd) plus Bash scoped to the caller-named
    source-inspection commands. No write tools, and deliberately NO browse
    tool — UI evidence is orchestrator-mediated (R15's structural
    containment), so there is nothing browse-shaped to misuse in-session.
    """
    if not all(c.strip() for c in source_commands):
        raise RegistryError(
            "grader_profile source_commands must be non-empty command names"
        )
    allowed = (
        *_READ_TOOLS,
        *(f"Bash({command}:*)" for command in source_commands),
    )
    return RoleProfile(
        role="grader",
        model=model,
        max_turns=max_turns,
        timeout_s=timeout_s,
        tools=None,
        allowed_tools=allowed,
    )


def build_pre_research_prompt(
    *, target: str, source_root: str | Path, ui_observations: str = ""
) -> str:
    """The enumeration brief: source + ``urls.py`` routes + UI traversal.

    UI-traversal observations are gathered by the orchestrator over the
    browse channel and embedded as text — the session itself never browses.
    """
    ui_section = (
        "## UI traversal observations (captured on the running app)\n\n"
        f"{ui_observations.strip()}\n\n"
        if ui_observations.strip()
        else ""
    )
    lo, hi = EXPECTED_BEHAVIOR_RANGE
    return (
        f"You are the grader's registry pre-researcher for the target"
        f" '{target}'. Your working directory is the pinned source checkout"
        f" ({source_root}). Enumerate every distinct, testable USER-FACING"
        " behavior by reading the source, the URL routes (urls.py files),"
        " and the UI traversal observations below.\n\n"
        f"{ui_section}"
        "Rules:\n"
        f"- Expect roughly {EXPECTED_AREA_COUNT} feature areas and between"
        f" {lo} and {hi} testable behaviors for this target; substantially"
        " fewer means you missed areas, substantially more means you are"
        " splitting one behavior into UI micro-steps.\n"
        "- Each candidate's `key` is PERMANENT identity (FEAT-<key>): a short"
        " kebab-case slug of the form <area>-<behavior>, stable across"
        " re-research runs. Never encode incidental details in it.\n"
        "- `confirm_steps`: the minimal structured browse requests"
        " ({action, selector, args}) an orchestrator can execute on the"
        " RUNNING app to observe the behavior exists. Features that cannot"
        " be confirmed on the running app (dead code, disabled flags) will"
        " NOT be minted — source proposes, runtime confirms.\n"
        "- `scenario_steps`: natural-language steps for the behavioral"
        " scenario manifest (one short imperative sentence per step), with"
        " `expected_outcome` stating what a correct app shows.\n"
        "- `tier`: 'must' for core behaviors a clone must reproduce,"
        " 'should' for important-but-tolerant ones, 'free' for cosmetic or"
        " incidental ones.\n"
        "- `route`: the source evidence (file/route) the candidate came"
        " from.\n\n"
        "Then answer a SECOND question about the same app: what did it"
        " *decide*? Enumerate the resolved design DECISIONS this app embodies"
        " — the choices a greenfield builder would otherwise have to make"
        " (auth model, multi-tenancy, deletion semantics, empty-state"
        " behavior, ...) — into a `decisions` array. Each decision:\n"
        "- `key`: PERMANENT identity (DEC-<key>), a short kebab-case slug"
        " stable across re-research runs.\n"
        "- `category`: the probe-question category this decision falls under"
        f" (known categories: {', '.join(sorted(PROBE_TAXONOMY))}); propose a"
        " new one only when none fits (grow-by-exception).\n"
        "- GRANULARITY RUBRIC: one decision = one independently-reversible"
        " choice. 'soft delete + 30-day purge + admin override' is THREE"
        " decisions, not one — split compound choices.\n"
        "- `description`: a behavioral description of the resolved choice"
        " (what the running app does), not its source-level mechanism.\n"
        "- `confirm_steps`: the minimal structured browse requests"
        " ({action, selector, args}) an orchestrator can run on the RUNNING"
        " app to observe the decision in force. Source proposes, runtime"
        " confirms — a decision you cannot observe live is NOT recorded.\n"
        "- `route`: the source evidence (file/route) the decision came from."
        "\n\n"
        "Return structured output only:"
        " {\"candidates\": [...], \"decisions\": [...]} matching the provided"
        " schema (an empty `decisions` array is legal but expected to be"
        " non-empty for a real target)."
    )


def validate_pre_research_output(output: dict) -> str | None:
    """``run_session`` extra-validate hook: candidate AND decision shapes +
    unique keys (007 KTD1 — DEC rides the same pass)."""
    try:
        candidates_from_output(output)
        decisions_from_output(output)
    except (RegistryError, KeyError, TypeError) as exc:
        return f"pre-research output invalid: {exc}"
    return None


@dataclass(frozen=True)
class PreResearch:
    """One pre-research pass's two output arrays (007 KTD1): the FEAT candidates
    and the DEC siblings extracted by the same session."""

    candidates: tuple[FeatureCandidate, ...]
    decisions: tuple[DecisionCandidate, ...]


def run_pre_research_full(
    profile: RoleProfile,
    *,
    target: str,
    source_root: str | Path,
    transcript_path: str | Path,
    max_retries: int,
    ui_observations: str = "",
    store: Store | None = None,
    run_id: int | None = None,
    mode: str | None = None,
    script_path: str | Path | None = None,
) -> PreResearch:
    """One pre-research session over the ``run_session`` seam -> FEAT candidates
    AND DEC siblings (007 KTD1: same instruments, second output table).

    ``source_root`` becomes the session cwd, scoping the grader's Read/Grep
    to the pinned checkout. Offline this rides the scripted fake (R7
    discipline); live it is one headless subscription session.
    """
    prompt = build_pre_research_prompt(
        target=target, source_root=source_root, ui_observations=ui_observations
    )
    result = run_session(
        prompt,
        PRE_RESEARCH_SCHEMA,
        profile,
        transcript_path=transcript_path,
        max_retries=max_retries,
        workspace_root=source_root,
        store=store,
        run_id=run_id,
        agent="grader",
        extra_validate=validate_pre_research_output,
        mode=mode,
        script_path=script_path,
    )
    return PreResearch(
        candidates=tuple(candidates_from_output(result.output)),
        decisions=tuple(decisions_from_output(result.output)),
    )


def run_pre_research(
    profile: RoleProfile,
    *,
    target: str,
    source_root: str | Path,
    transcript_path: str | Path,
    max_retries: int,
    ui_observations: str = "",
    store: Store | None = None,
    run_id: int | None = None,
    mode: str | None = None,
    script_path: str | Path | None = None,
) -> list[FeatureCandidate]:
    """The FEAT-only view of one pre-research pass (back-compat): the candidate
    list. Callers needing the DEC siblings call :func:`run_pre_research_full`."""
    return list(
        run_pre_research_full(
            profile,
            target=target,
            source_root=source_root,
            transcript_path=transcript_path,
            max_retries=max_retries,
            ui_observations=ui_observations,
            store=store,
            run_id=run_id,
            mode=mode,
            script_path=script_path,
        ).candidates
    )


# --- runtime confirmation ("source proposes, runtime confirms") -------------------


def _confirm_via_browse(
    confirm_steps: Sequence[dict], browse: Browse, *, label: str
) -> list[dict] | None:
    """Run confirm steps on the running app; return the captured observations,
    or ``None`` the moment any step fails to observe the behavior.

    Shared by FEAT (:func:`confirm_candidate`) and DEC
    (:func:`confirm_decision`) confirmation — "source proposes, runtime
    confirms" is one discipline. An ``ok`` observation without an a11y payload
    is treated as unconfirmed: evidence IS the confirmation (R8).
    """
    captured: list[dict] = []
    for step in confirm_steps:
        observation = browse(dict(step))
        if not isinstance(observation, dict) or observation.get("status") != "ok":
            status = (
                observation.get("status")
                if isinstance(observation, dict)
                else "malformed-observation"
            )
            logger.info("%s unconfirmed at step %r: %s", label, step, status)
            return None
        if not observation.get("a11y"):
            logger.info(
                "%s: step %r returned ok but no a11y snapshot — evidence-less"
                " confirmation is no confirmation (R8)",
                label,
                step,
            )
            return None
        captured.append(
            {
                "step": dict(step),
                "a11y": observation["a11y"],
                "screenshot_ref": observation.get("screenshot_ref"),
            }
        )
    return captured


def confirm_candidate(
    candidate: FeatureCandidate,
    browse: Browse,
    *,
    evidence_dir: str | Path,
    digest: str,
) -> str | None:
    """Execute the candidate's confirm steps on the running app; capture
    evidence.

    Returns the evidence ref (a written a11y-snapshot file stamped with the
    digest) on success, or ``None`` when any step fails to observe the
    behavior — an unconfirmed candidate is simply not minted (R8). An ``ok``
    observation without an a11y payload is treated as unconfirmed: evidence
    IS the confirmation.
    """
    captured = _confirm_via_browse(
        candidate.confirm_steps, browse, label=f"candidate {candidate.key}"
    )
    if captured is None:
        return None
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{candidate.key}.json"
    payload = {
        "feat_key": candidate.key,
        "behavior": candidate.behavior,
        "digest": digest,
        "captured": captured,
        "captured_at": _utcnow(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return str(path)


def confirm_decision(
    candidate: DecisionCandidate,
    browse: Browse,
    *,
    evidence_dir: str | Path,
    digest: str,
) -> str | None:
    """Execute a decision's confirm steps on the running app; capture evidence
    (007 KTD1: source proposes, runtime confirms — entry by entry).

    Returns the evidence ref (a written a11y-snapshot file, DEC-id-named to
    avoid colliding with the FEAT capture's bare-key filename) on success, or
    ``None`` when any step fails to observe the decision in force — a
    source-only decision is simply not minted.
    """
    captured = _confirm_via_browse(
        candidate.confirm_steps, browse, label=f"decision {candidate.key}"
    )
    if captured is None:
        return None
    evidence_dir = Path(evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / f"{dec_id(candidate.key)}.json"
    payload = {
        "dec_key": candidate.key,
        "category": candidate.category,
        "description": candidate.description,
        "digest": digest,
        "captured": captured,
        "captured_at": _utcnow(),
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return str(path)


# --- mint, deprecate, refresh ------------------------------------------------------


def author_manifest(candidate: FeatureCandidate, fid: str) -> str:
    """The mint-time scenario manifest (U4's format: NL steps, expected
    outcome, tolerance tier — R16)."""
    return json.dumps(
        {
            "feat": fid,
            "key": candidate.key,
            "steps": list(candidate.scenario_steps),
            "expected_outcome": candidate.expected_outcome,
            "tier": candidate.tier,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def mint_feat(
    store: Store,
    candidate: FeatureCandidate,
    evidence_ref: str,
    *,
    target: str,
    digest: str,
    frontier_status: str = "unexplored",
) -> str:
    """Mint one runtime-confirmed FEAT: registry row + scenario manifest +
    frontier entry, atomically. Only after this does the feature become
    mentionable (the ``trace_msg_mentions`` FK) and authorable (the
    ``scenario_manifests`` FK) — R10's mint path.
    """
    if not evidence_ref.strip():
        raise RegistryError(
            f"minting {candidate.key!r} requires captured runtime evidence"
            " (R8): confirm on the running app first"
        )
    if frontier_status not in FRONTIER_STATUSES:
        raise RegistryError(
            f"unknown frontier status {frontier_status!r}"
            f" (expected one of {FRONTIER_STATUSES})"
        )
    fid = feat_id(candidate.key)
    with store.transaction():
        existing = store.conn.execute(
            "SELECT id FROM trace_feat WHERE id = ?", (fid,)
        ).fetchone()
        if existing is not None:
            raise RegistryError(
                f"{fid} already exists; FEAT ids are append-only and never"
                " reused (R9) — refresh the registry instead of re-minting"
            )
        store.conn.execute(
            "INSERT INTO trace_feat (id, evidence_ref, target, digest, status)"
            " VALUES (?, ?, ?, ?, 'confirmed')",
            (fid, evidence_ref, target, digest),
        )
        store.conn.execute(
            "INSERT INTO scenario_manifests"
            " (feat_id, tier, manifest_json, status, created_at)"
            " VALUES (?, ?, ?, 'active', ?)",
            (fid, candidate.tier, author_manifest(candidate, fid), _utcnow()),
        )
        store.conn.execute(
            "INSERT INTO frontier (feat_id, status) VALUES (?, ?)",
            (fid, frontier_status),
        )
    logger.info("minted %s (%s) with evidence %s", fid, candidate.behavior, evidence_ref)
    return fid


def deprecate_feat(store: Store, fid: str) -> None:
    """A vanished feature deprecates, never deletes (R9): scenarios archive,
    the frontier entry drops, and MSG mention history stays queryable
    (the FEAT row — and so the FK target — persists forever)."""
    with store.transaction():
        row = store.conn.execute(
            "SELECT status FROM trace_feat WHERE id = ?", (fid,)
        ).fetchone()
        if row is None:
            raise RegistryError(f"{fid} does not exist; nothing to deprecate")
        store.conn.execute(
            "UPDATE trace_feat SET status = 'deprecated' WHERE id = ?", (fid,)
        )
        store.conn.execute(
            "UPDATE scenario_manifests SET status = 'archived' WHERE feat_id = ?",
            (fid,),
        )
        store.conn.execute("DELETE FROM frontier WHERE feat_id = ?", (fid,))
    logger.info("deprecated %s (scenarios archived, frontier entry dropped)", fid)


@dataclass(frozen=True)
class RegistryRefresh:
    """One refresh outcome: FEAT ids minted/reconfirmed/deprecated, plus the
    candidate KEYS that failed runtime confirmation (never minted)."""

    minted: tuple[str, ...]
    reconfirmed: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    deprecated: tuple[str, ...]


def refresh_registry(
    store: Store,
    candidates: Sequence[FeatureCandidate],
    browse: Browse,
    *,
    target: str,
    digest: str,
    evidence_dir: str | Path,
) -> RegistryRefresh:
    """Reconcile the registry with a freshly-confirmed candidate set.

    Confirmation (the slow, browse-driven half) runs FIRST, outside any
    transaction; the reconcile then commits atomically: new keys mint, known
    keys re-stamp evidence + digest (a previously-deprecated key that
    reappears flips back — same ID, same identity, never a new number), and
    confirmed FEATs whose key vanished from the candidate set deprecate.
    """
    keys = [c.key for c in candidates]
    if len(set(keys)) != len(keys):
        raise RegistryError(
            "duplicate candidate keys in refresh: keys are permanent FEAT"
            " identity and must be unique (R9)"
        )

    evidences: dict[str, str] = {}
    unconfirmed: list[str] = []
    for cand in candidates:
        evidence = confirm_candidate(
            cand, browse, evidence_dir=evidence_dir, digest=digest
        )
        if evidence is None:
            unconfirmed.append(cand.key)
        else:
            evidences[cand.key] = evidence

    minted: list[str] = []
    reconfirmed: list[str] = []
    deprecated: list[str] = []
    with store.transaction():
        for cand in candidates:
            if cand.key not in evidences:
                continue
            fid = feat_id(cand.key)
            row = store.conn.execute(
                "SELECT status FROM trace_feat WHERE id = ?", (fid,)
            ).fetchone()
            if row is None:
                mint_feat(
                    store,
                    cand,
                    evidences[cand.key],
                    target=target,
                    digest=digest,
                )
                minted.append(fid)
                continue
            if row["status"] == "deprecated":
                # the feature came back: same ID forever, manifests reactivate
                store.conn.execute(
                    "UPDATE scenario_manifests SET status = 'active'"
                    " WHERE feat_id = ?",
                    (fid,),
                )
            store.conn.execute(
                "UPDATE trace_feat SET evidence_ref = ?, target = ?,"
                " digest = ?, status = 'confirmed' WHERE id = ?",
                (evidences[cand.key], target, digest, fid),
            )
            store.conn.execute(
                "INSERT INTO frontier (feat_id)"
                " SELECT ? WHERE NOT EXISTS"
                " (SELECT 1 FROM frontier WHERE feat_id = ?)",
                (fid, fid),
            )
            reconfirmed.append(fid)

        confirmed_ids = {feat_id(key) for key in evidences}
        rows = store.conn.execute(
            "SELECT id FROM trace_feat WHERE target = ? AND status = 'confirmed'"
            " ORDER BY id",
            (target,),
        ).fetchall()
        for row in rows:
            if row["id"] not in confirmed_ids:
                deprecate_feat(store, row["id"])
                deprecated.append(row["id"])

    return RegistryRefresh(
        minted=tuple(minted),
        reconfirmed=tuple(reconfirmed),
        unconfirmed=tuple(unconfirmed),
        deprecated=tuple(deprecated),
    )


# --- decision mint / deprecate / refresh (007 KTD1) -------------------------------


def mint_dec(
    store: Store,
    candidate: DecisionCandidate,
    evidence_ref: str,
    *,
    target: str,
    digest: str,
) -> str:
    """Mint one runtime-confirmed DEC: the registry row only (007 KTD1 — DEC
    does NOT enter the frontier ledger and carries no scenario manifest; its
    coverage is a lint + settlement concern). Ids are append-only and never
    reused (the FEAT identity discipline)."""
    if not evidence_ref.strip():
        raise RegistryError(
            f"minting {candidate.key!r} requires captured runtime evidence"
            " (007 KTD1): confirm on the running app first"
        )
    did = dec_id(candidate.key)
    with store.transaction():
        existing = store.conn.execute(
            "SELECT id FROM trace_dec WHERE id = ?", (did,)
        ).fetchone()
        if existing is not None:
            raise RegistryError(
                f"{did} already exists; DEC ids are append-only and never"
                " reused (007 KTD1) — refresh the registry instead of re-minting"
            )
        store.conn.execute(
            "INSERT INTO trace_dec"
            " (id, target, digest, category, description, evidence_ref, status)"
            " VALUES (?, ?, ?, ?, ?, ?, 'confirmed')",
            (
                did,
                target,
                digest,
                candidate.category,
                candidate.description,
                evidence_ref,
            ),
        )
    logger.info("minted %s (%s) with evidence %s", did, candidate.category, evidence_ref)
    return did


def deprecate_dec(store: Store, did: str) -> None:
    """A vanished decision deprecates, never deletes (007 KTD1): the DEC row —
    and so the mention FK target — persists forever, MSG history stays
    queryable."""
    with store.transaction():
        row = store.conn.execute(
            "SELECT status FROM trace_dec WHERE id = ?", (did,)
        ).fetchone()
        if row is None:
            raise RegistryError(f"{did} does not exist; nothing to deprecate")
        store.conn.execute(
            "UPDATE trace_dec SET status = 'deprecated' WHERE id = ?", (did,)
        )
    logger.info("deprecated %s", did)


@dataclass(frozen=True)
class DecisionRefresh:
    """One DEC refresh outcome: ids minted/reconfirmed/deprecated, plus the
    candidate KEYS that failed runtime confirmation (never minted), and the
    out-of-taxonomy categories flagged for §17 grow-by-exception."""

    minted: tuple[str, ...]
    reconfirmed: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    deprecated: tuple[str, ...]
    out_of_taxonomy: tuple[str, ...]


def refresh_decisions(
    store: Store,
    decisions: Sequence[DecisionCandidate],
    browse: Browse,
    *,
    target: str,
    digest: str,
    evidence_dir: str | Path,
) -> DecisionRefresh:
    """Reconcile the decision registry with a freshly-confirmed candidate set,
    parallel to :func:`refresh_registry`: confirmation runs FIRST (browse-driven,
    outside any transaction); the reconcile then commits atomically — new keys
    mint, known keys re-stamp evidence + digest (a deprecated key that reappears
    flips back, same id), confirmed DECs whose key vanished deprecate. Source
    proposes, runtime confirms: unconfirmable decisions are reported, never
    minted."""
    keys = [d.key for d in decisions]
    if len(set(keys)) != len(keys):
        raise RegistryError(
            "duplicate decision keys in refresh: keys are permanent DEC"
            " identity and must be unique (007 KTD1)"
        )

    evidences: dict[str, str] = {}
    unconfirmed: list[str] = []
    for dec in decisions:
        evidence = confirm_decision(
            dec, browse, evidence_dir=evidence_dir, digest=digest
        )
        if evidence is None:
            unconfirmed.append(dec.key)
        else:
            evidences[dec.key] = evidence

    minted: list[str] = []
    reconfirmed: list[str] = []
    deprecated: list[str] = []
    with store.transaction():
        for dec in decisions:
            if dec.key not in evidences:
                continue
            did = dec_id(dec.key)
            row = store.conn.execute(
                "SELECT status FROM trace_dec WHERE id = ?", (did,)
            ).fetchone()
            if row is None:
                mint_dec(
                    store, dec, evidences[dec.key], target=target, digest=digest
                )
                minted.append(did)
                continue
            store.conn.execute(
                "UPDATE trace_dec SET evidence_ref = ?, target = ?, digest = ?,"
                " category = ?, description = ?, status = 'confirmed'"
                " WHERE id = ?",
                (
                    evidences[dec.key],
                    target,
                    digest,
                    dec.category,
                    dec.description,
                    did,
                ),
            )
            reconfirmed.append(did)

        confirmed_ids = {dec_id(key) for key in evidences}
        rows = store.conn.execute(
            "SELECT id FROM trace_dec WHERE target = ? AND status = 'confirmed'"
            " ORDER BY id",
            (target,),
        ).fetchall()
        for row in rows:
            if row["id"] not in confirmed_ids:
                deprecate_dec(store, row["id"])
                deprecated.append(row["id"])

    return DecisionRefresh(
        minted=tuple(minted),
        reconfirmed=tuple(reconfirmed),
        unconfirmed=tuple(unconfirmed),
        deprecated=tuple(deprecated),
        out_of_taxonomy=out_of_taxonomy_categories(decisions),
    )


# --- the newly-discovered queue (R10) ----------------------------------------------


@dataclass(frozen=True)
class Discovery:
    """One explorer observation awaiting grader-side confirmation."""

    candidate: FeatureCandidate
    ui_evidence: str


@dataclass(frozen=True)
class DiscoveryResult:
    minted: tuple[str, ...]
    unconfirmed: tuple[str, ...]
    already_known: tuple[str, ...]


class DiscoveryQueue:
    """R10's newly-discovered flow: enqueue (with UI evidence) -> incremental
    grader-side confirmation -> mint -> only then mentionable (FK-enforced).

    In-process in this unit; the explorer/episode units (U5/U6) own durable
    wiring into their loops.
    """

    def __init__(self) -> None:
        self._pending: list[Discovery] = []

    @property
    def pending(self) -> tuple[Discovery, ...]:
        return tuple(self._pending)

    def enqueue(self, candidate: FeatureCandidate, *, ui_evidence: str) -> None:
        if not ui_evidence.strip():
            raise RegistryError(
                "a newly-discovered observation must carry the explorer's UI"
                " evidence (R10) — discovery without evidence is hearsay"
            )
        self._pending.append(Discovery(candidate=candidate, ui_evidence=ui_evidence))

    def drain(
        self,
        store: Store,
        browse: Browse,
        *,
        target: str,
        digest: str,
        evidence_dir: str | Path,
    ) -> DiscoveryResult:
        """Confirm and mint every queued discovery; minted FEATs enter the
        frontier as ``newly-discovered``. Unconfirmable observations are
        dropped (reported, never minted); already-minted keys are skipped."""
        minted: list[str] = []
        unconfirmed: list[str] = []
        already_known: list[str] = []
        for discovery in self._pending:
            cand = discovery.candidate
            fid = feat_id(cand.key)
            row = store.conn.execute(
                "SELECT id FROM trace_feat WHERE id = ?", (fid,)
            ).fetchone()
            if row is not None:
                already_known.append(fid)
                continue
            evidence = confirm_candidate(
                cand, browse, evidence_dir=evidence_dir, digest=digest
            )
            if evidence is None:
                unconfirmed.append(cand.key)
                continue
            mint_feat(
                store,
                cand,
                evidence,
                target=target,
                digest=digest,
                frontier_status="newly-discovered",
            )
            minted.append(fid)
        self._pending.clear()
        return DiscoveryResult(
            minted=tuple(minted),
            unconfirmed=tuple(unconfirmed),
            already_known=tuple(already_known),
        )


# --- registry queries ----------------------------------------------------------------


def registry_rows(store: Store, target: str) -> list[sqlite3.Row]:
    """All FEAT rows for a target (confirmed AND deprecated), id-ordered."""
    return store.conn.execute(
        "SELECT * FROM trace_feat WHERE target = ? ORDER BY id", (target,)
    ).fetchall()


def registry_in_expected_range(store: Store, target: str) -> bool:
    """R8's live-verification check: confirmed-behavior count within the
    expected 40–60 range for the linkding surface."""
    row = store.conn.execute(
        "SELECT COUNT(*) AS n FROM trace_feat"
        " WHERE target = ? AND status = 'confirmed'",
        (target,),
    ).fetchone()
    lo, hi = EXPECTED_BEHAVIOR_RANGE
    return lo <= row["n"] <= hi


def decision_rows(store: Store, target: str) -> list[sqlite3.Row]:
    """All DEC rows for a target (confirmed AND deprecated), id-ordered."""
    return store.conn.execute(
        "SELECT * FROM trace_dec WHERE target = ? ORDER BY id", (target,)
    ).fetchall()


# --- DEC-coverage lint (007 KTD1, mentioned-only v1) ------------------------------

# A store-backed lint (it joins persisted rows), deliberately BESIDE the pure
# plan-dict `lint_plan` rather than inside it — its data lives in the store, not
# the planner output. The §7 typed-failure shape (LintFinding) is shared so the
# DEC-coverage failure feeds the same Ralph feedback channel as the plan lints.
LINT_DEC_COVERAGE = "dec_coverage"


def _dec_is_resolved(store: Store, msg_ids: Sequence[str]) -> bool:
    """Whether a DEC mentioned by any of ``msg_ids`` was surfaced — resolved by
    a question, an ASSUME, or a proposal (007 KTD1's named join, msg-keyed v1).

    v1 resolution is keyed on the mentioning MSG, the only cross-table link the
    schema gives uniformly at this phase: a question (the mention MSG is a
    ``qa_log`` question/answer slot), an ASSUME (the mention MSG confirmed a
    ``trace_assume`` row), or a proposal (a ``trace_proposal`` resolving such an
    ASSUME). Richer per-DEC adjudication-keyed resolution lands with U6's
    ``trace_proposal_adjudication`` table.
    """
    placeholders = ",".join("?" * len(msg_ids))
    msgs = tuple(msg_ids)
    # resolved by a question: the mention MSG is a qa_log question/answer slot.
    if store.conn.execute(
        f"SELECT 1 FROM qa_log WHERE question_msg_id IN ({placeholders})"
        f" OR answer_msg_id IN ({placeholders}) LIMIT 1",
        (*msgs, *msgs),
    ).fetchone():
        return True
    # resolved by an ASSUME: the mention MSG confirmed an assumption.
    if store.conn.execute(
        f"SELECT 1 FROM trace_assume WHERE confirmed_by_msg IN ({placeholders})"
        " LIMIT 1",
        msgs,
    ).fetchone():
        return True
    # resolved by a proposal: a proposal resolving an ASSUME the mention MSG
    # confirmed (proposal -> assume -> msg, the schema's only msg-reachable
    # proposal link in v1).
    if store.conn.execute(
        "SELECT 1 FROM trace_proposal p"
        " JOIN trace_assume t ON t.id = p.linked_assume_id"
        f" WHERE t.confirmed_by_msg IN ({placeholders}) LIMIT 1",
        msgs,
    ).fetchone():
        return True
    return False


def lint_dec_coverage(
    store: Store, *, world: str, msg_ids: Iterable[str]
) -> list[LintFinding]:
    """Every DEC *mentioned in the increment's MSGs* must be resolved by a
    question, a proposal, or an ASSUME (007 KTD1, mentioned-only v1).

    World-gated: a no-op in brownfield (the explorer is a perfect oracle that
    never raises a decision the founder hadn't thought of — DEC coverage is a
    greenfield concern). ``msg_ids`` is the increment's MSG set (the
    orchestrator/episode that owns the increment supplies it). An UNMENTIONED
    decision is ignored — v1 scopes to mentioned DECs only; the
    category-mandatory list is deferred (Scope decision 4).

    Returns one :class:`LintFinding` per mentioned-but-unsurfaced DEC; an empty
    list means coverage holds.
    """
    if world not in WORLDS:
        raise RegistryError(
            f"unknown world {world!r}; expected one of {WORLDS}"
        )
    if world == "brownfield":
        return []
    scope = tuple(dict.fromkeys(msg_ids))  # stable de-dup
    if not scope:
        return []
    placeholders = ",".join("?" * len(scope))
    rows = store.conn.execute(
        "SELECT DISTINCT m.dec_id AS dec_id, m.msg_id AS msg_id"
        " FROM trace_msg_dec_mentions m"
        " JOIN trace_dec d ON d.id = m.dec_id"
        f" WHERE m.msg_id IN ({placeholders}) AND d.status = 'confirmed'"
        " ORDER BY m.dec_id, m.msg_id",
        scope,
    ).fetchall()

    mentioned: dict[str, list[str]] = {}
    for row in rows:
        mentioned.setdefault(row["dec_id"], []).append(row["msg_id"])

    findings: list[LintFinding] = []
    for did in sorted(mentioned):
        dec_msgs = mentioned[did]
        if not _dec_is_resolved(store, dec_msgs):
            findings.append(
                LintFinding(
                    LINT_DEC_COVERAGE,
                    SEVERITY_ERROR,
                    f"decision {did}",
                    "a mentioned decision resolved by a question, a proposal,"
                    " or an ASSUME",
                    f"{did} mentioned in {', '.join(sorted(dec_msgs))} but never"
                    " surfaced (no question/proposal/ASSUME)",
                )
            )
    return findings
