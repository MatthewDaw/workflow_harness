"""Grader settlement and report (plan-003 U7, R7 dual-app half, R20-R23).

Settlement is the episode's one measurement event: the target resets to seed,
both apps come up on the static port table (the clone dev server held open by
the orchestrator — ``DevServer.hold_open()``, R7), the full registry-anchored
scenario rubric executes on both DOMs through the U4 harness, and the results
settle into SCEN rows plus the human reflector's settlement report.

The judge protocol (R20) is layered deterministic-first:

1. **Deterministic tier.** Harness-level outcomes never reach a judge: a
   target-side ``invalid`` scenario is apparatus defect (excluded from the
   score denominator); a clone ``feature_absent`` or ``failed`` execution is a
   deterministic fail; two completed executions whose final accessibility
   trees normalize to an empty structural diff are a deterministic pass.
2. **Single judgment.** Exactly one LLM judgment per scenario *comparison*,
   on the normalized a11y-tree diff — binary verdict + CoT reasoning +
   self-reported confidence, default-fail framing, through the judge seam
   (record/replay; the offline suite runs on fixtures with zero quota).
3. **Panel.** Only on low confidence does the comparison escalate to a panel
   of ``panel_size`` independent re-judgments (distinct prompts, hence
   distinct fixture keys); majority vote decides and a tie falls to fail
   (default-fail). Votes, tally, and disagreement land in the judge metadata.

Screenshots are archived as evidence by the U4 harness — they are **never**
judge input; judges see only normalized a11y-diff payloads, and exactly those
payloads persist on the SCEN row (``judge_input_json``, R22) so replay
re-judging has the original inputs.

The baseline property checklist (R21) probes the clone's own endpoints —
target-independent: server-side validation, authz-on-direct-access, no stack
traces. The metamorphic tier (R21) runs target-free on the clone through the
same scenario harness: create-then-list, edit-then-revert identity, refresh
idempotence — identity checks compare the U4 fingerprint at two declared
steps, so within-check data mutation cannot mask a structural divergence.

The settlement report (R23) is a product requirement, not a log: overall
score + tier breakdown over the FULL denominator (§10 full-coverage
fairness), with scenarios for FEATs never mentioned in any MSG broken out as
the unreached-frontier bucket (budget-vs-capability made visible); failed
scenarios each embed the named trace command (``af trace chain <SCEN>`` — the
attribution join lives in the CLI, U9, not in this renderer); a dedicated
UAT-accepted-but-scenario-failed section carries the typed tag (attribution
rule deferred to Plan 4); instrument health (mutation-audit results) is a
report section populated by U8's caller. Every scenario row carries a
ready-to-paste trace-query CLI command.

Behavior tunables (judge model, retries, panel size) are caller-supplied via
:class:`SettleConfig` per the established U3/U6/target-env precedent —
nothing here hardcodes one. The report dict carries no timestamps or
volatile data, so :func:`render_report` is byte-deterministic from a
synthetic episode fixture (the U7 verification); ``created_at`` lives only
in the database column.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from agent_families.grading.scenarios import (
    Driver,
    HealTelemetry,
    ResolutionCache,
    ResolveFn,
    ScenarioManifest,
    ScenarioResult,
    _collapse_rows,
    _skeleton,
    execute_scenario,
    fingerprint,
)
from agent_families.grading.target_env import Http, _http_request
from agent_families.judge import run_judge
from agent_families.pipeline.devserver import DevServer
from agent_families.store import Store, StoreError

logger = logging.getLogger(__name__)

# --- vocabulary -----------------------------------------------------------------

# Comparison verdicts on the SCEN row: pass/fail are scoreable; `invalid`
# (target-side hard failure, R19) is excluded from the score denominator.
SCEN_VERDICTS = ("pass", "fail", "invalid")

CONFIDENCES = ("high", "low")

# The typed tag for the report's dedicated UAT-divergence section (R23);
# Plan 4 owns the attribution rule, Phase 2 only surfaces and tags.
UAT_DIVERGENCE_TAG = "uat_accepted_scenario_failed"

# Baseline property checklist kinds (R21) — target-independent clone probes.
BASELINE_PROBE_KINDS = (
    "server_side_validation",
    "authz_direct_access",
    "no_stack_trace",
)

# Statuses that count as authz blocking direct access (R21): redirects to
# login, explicit denials, or existence-hiding 404 — anything but serving.
_AUTHZ_BLOCKING_STATUSES = frozenset({301, 302, 303, 307, 308, 401, 403, 404})

# Server-error stack-trace markers (R21): python tracebacks and node frames.
STACK_TRACE_MARKERS = (
    "Traceback (most recent call last)",
    "UnhandledPromiseRejection",
    "    at ",
)

# Metamorphic tier kinds (R21). Identity kinds compare the U4 fingerprint at
# two declared steps; presence kinds rely on the manifest's deterministic
# post-assertions.
METAMORPHIC_KINDS = (
    "create_then_list",
    "edit_then_revert_identity",
    "refresh_idempotence",
)
_IDENTITY_KINDS = frozenset({"edit_then_revert_identity", "refresh_idempotence"})

_SCEN_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SettleError(Exception):
    """Base for every settlement failure."""


# --- config ------------------------------------------------------------------------


@dataclass(frozen=True)
class SettleConfig:
    """Judge plumbing for scenario comparisons. Model/retries come from the
    [judge] config section via the caller (never hardcoded); ``panel_size``
    is the low-confidence escalation width (R20); mode/fixtures ride the
    record/replay seam exactly like every other judge call."""

    model: str
    max_retries: int
    panel_size: int
    bare: bool = False
    mode: str | None = None
    fixtures_dir: Path | None = None

    def __post_init__(self) -> None:
        if self.panel_size < 2:
            raise SettleError(
                f"panel_size must be >= 2 (a panel of one is a re-ask, not a"
                f" panel — R20), got {self.panel_size}"
            )


# --- normalized a11y diff (R20: judges see diffs, never screenshots) -----------------


def _flat_shapes(a11y_tree: dict) -> list[str]:
    """Flatten the U4 normalized structural skeleton into sorted path strings
    (one per interactive/landmark/row shape, parents encoded as prefixes)."""
    shapes = _collapse_rows(_skeleton(a11y_tree, in_row=False))
    out: list[str] = []

    def walk(shape: list, prefix: str) -> None:
        first, second, inner = shape
        if first == "row":
            label = f"{prefix}row[{second}]"
        else:
            label = f"{prefix}{first}" + (f":{second}" if second else "")
        out.append(label)
        for child in inner:
            walk(child, label + ">")

    for shape in shapes:
        walk(shape, "")
    return sorted(out)


def a11y_diff(target_tree: dict, clone_tree: dict) -> dict:
    """Structural diff between two normalized skeletons (R20 judge input).

    Multiset difference over flattened shape paths: data mutation (row
    contents, counts, dates) is already normalized away by U4's fingerprint
    discipline, so an empty diff means structural behavioral equivalence.
    """
    target_shapes = Counter(_flat_shapes(target_tree))
    clone_shapes = Counter(_flat_shapes(clone_tree))
    return {
        "target_only": sorted((target_shapes - clone_shapes).elements()),
        "clone_only": sorted((clone_shapes - target_shapes).elements()),
    }


# --- comparison judge (R20) ----------------------------------------------------------

COMPARISON_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "confidence": {"type": "string", "enum": list(CONFIDENCES)},
    },
    "required": ["reasoning", "verdict", "confidence"],
    "additionalProperties": False,
}


def comparison_prompt(
    title: str, expected_outcome: str, tier: str, diff: dict
) -> str:
    """The single-shot comparison prompt: binary + CoT, default-fail framing,
    no volatile data (fixture keys hash the prompt — judge-seam discipline)."""
    diff_json = json.dumps(
        diff, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return (
        "You compare ONE behavioral scenario's outcome on a legacy target app"
        " against a rebuilt clone, using ONLY the normalized accessibility-"
        "tree diff below (shapes present on one app and missing on the"
        " other).\n"
        "Default to fail: if the diff shows any difference relevant to the"
        " expected outcome, or you are unsure, the verdict is fail. Respond"
        ' "pass" ONLY when the clone\'s behavior is clearly equivalent for'
        " this scenario.\n"
        "Reason step by step in 'reasoning' first, then give the binary"
        " verdict (pass or fail) and your confidence (high or low).\n\n"
        f"Scenario: {title}\n"
        f"Expected outcome: {expected_outcome}\n"
        f"Tolerance tier: {tier}\n"
        f"Accessibility diff: {diff_json}"
    )


def panelist_prompt(index: int, size: int, base_prompt: str) -> str:
    """Panelist framing (R20): a distinct prompt per panelist — independent
    re-judgment, and a distinct fixture key through the replay seam."""
    return (
        f"Panelist {index} of {size}: give your independent judgment of the"
        f" comparison below.\n\n{base_prompt}"
    )


def _judge_once(prompt: str, config: SettleConfig) -> dict:
    result = run_judge(
        prompt,
        COMPARISON_SCHEMA,
        config.model,
        max_retries=config.max_retries,
        bare=config.bare,
        mode=config.mode,
        fixtures_dir=config.fixtures_dir,
    )
    return result.output


# --- scenario comparison (R20/R22) ----------------------------------------------------


@dataclass(frozen=True)
class ScenarioVerdict:
    """One settled scenario comparison, ready to persist as a SCEN row."""

    scenario_id: str
    feat_id: str
    tier: str
    verdict: str  # SCEN_VERDICTS
    judge_mode: str  # SCEN_JUDGE_MODES
    judge_metadata: dict
    judge_input: dict  # the a11y-diff payload (R22); {} when deterministic
    failure: str | None
    evidence_stale: bool


def compare_scenario(
    manifest: ScenarioManifest,
    target_result: ScenarioResult,
    clone_result: ScenarioResult,
    config: SettleConfig,
) -> ScenarioVerdict:
    """Settle one scenario: deterministic assertions first, one judge
    comparison on the a11y diff, panel only on low confidence (R20)."""
    stale = target_result.evidence_stale

    def deterministic(verdict: str, failure: str | None, reason: str):
        return ScenarioVerdict(
            scenario_id=manifest.scenario_id,
            feat_id=manifest.feat_id,
            tier=manifest.tier,
            verdict=verdict,
            judge_mode="deterministic",
            judge_metadata={"reason": reason},
            judge_input={},
            failure=failure,
            evidence_stale=stale,
        )

    # Deterministic tier (R20): harness outcomes never reach a judge.
    if target_result.status != "completed":
        # Target-side hard failure: broken apparatus, not a clone defect (R19).
        return deterministic(
            "invalid",
            f"target_invalid: {target_result.failure_reason}",
            "target-side hard failure marks the scenario invalid (R19)",
        )
    if clone_result.status == "feature_absent":
        return deterministic(
            "fail",
            "feature_absent",
            "first unresolvable step on the clone: not built (R18)",
        )
    if clone_result.status != "completed":
        return deterministic(
            "fail",
            clone_result.failure_reason,
            "clone execution failed deterministically (built wrong)",
        )

    target_tree = target_result.steps[-1].evidence.a11y_tree
    clone_tree = clone_result.steps[-1].evidence.a11y_tree
    diff = a11y_diff(target_tree, clone_tree)
    if not diff["target_only"] and not diff["clone_only"]:
        return deterministic(
            "pass", None, "final accessibility trees structurally identical"
        )

    # One LLM judgment per scenario comparison, on the diff (R20). The exact
    # payload the judge saw persists on the SCEN row (R22).
    judge_input = {
        "diff": diff,
        "expected_outcome": manifest.expected_outcome,
        "tier": manifest.tier,
        "title": manifest.title,
    }
    base_prompt = comparison_prompt(
        manifest.title, manifest.expected_outcome, manifest.tier, diff
    )
    initial = _judge_once(base_prompt, config)
    if initial["confidence"] == "high":
        verdict = initial["verdict"]
        return ScenarioVerdict(
            scenario_id=manifest.scenario_id,
            feat_id=manifest.feat_id,
            tier=manifest.tier,
            verdict=verdict,
            judge_mode="single",
            judge_metadata={
                "confidence": initial["confidence"],
                "reasoning": initial["reasoning"],
            },
            judge_input=judge_input,
            failure="judged_different" if verdict == "fail" else None,
            evidence_stale=stale,
        )

    # Panel escalation (R20): low confidence -> panel_size independent
    # re-judgments; majority vote, tie falls to fail (default-fail).
    votes = [
        _judge_once(panelist_prompt(k, config.panel_size, base_prompt), config)
        for k in range(1, config.panel_size + 1)
    ]
    passes = sum(1 for v in votes if v["verdict"] == "pass")
    verdict = "pass" if passes * 2 > config.panel_size else "fail"
    verdicts_seen = {v["verdict"] for v in votes}
    metadata = {
        "initial": {
            "confidence": initial["confidence"],
            "reasoning": initial["reasoning"],
            "verdict": initial["verdict"],
        },
        "votes": [
            {"confidence": v["confidence"], "verdict": v["verdict"]}
            for v in votes
        ],
        "tally": {"fail": len(votes) - passes, "pass": passes},
        "disagreement": len(verdicts_seen) > 1,
    }
    logger.info(
        "panel on %s: %d/%d pass -> %s (disagreement=%s)",
        manifest.scenario_id,
        passes,
        config.panel_size,
        verdict,
        metadata["disagreement"],
    )
    return ScenarioVerdict(
        scenario_id=manifest.scenario_id,
        feat_id=manifest.feat_id,
        tier=manifest.tier,
        verdict=verdict,
        judge_mode="panel",
        judge_metadata=metadata,
        judge_input=judge_input,
        failure="judged_different" if verdict == "fail" else None,
        evidence_stale=stale,
    )


# --- baseline property checklist (R21) -------------------------------------------------


@dataclass(frozen=True)
class BaselineProbe:
    """One target-independent probe against the clone's own endpoints."""

    kind: str
    path: str
    method: str = "GET"
    payload: dict | None = None

    def __post_init__(self) -> None:
        if self.kind not in BASELINE_PROBE_KINDS:
            raise SettleError(
                f"baseline probe kind must be one of"
                f" {', '.join(BASELINE_PROBE_KINDS)}, got {self.kind!r}"
            )
        if not self.path.startswith("/"):
            raise SettleError(
                f"baseline probe path must start with '/', got {self.path!r}"
            )
        if self.kind == "server_side_validation" and self.payload is None:
            raise SettleError(
                "server_side_validation probes need an invalid 'payload' to"
                " submit (R21)"
            )


def run_baseline_probes(
    base_url: str, probes: Sequence[BaselineProbe], http: Http | None = None
) -> list[dict]:
    """Execute the baseline property checklist against the clone (R21).

    ``http`` is the injectable seam (target_env precedent): ``(method, url,
    payload=...) -> (status, body)``.
    """
    http = http or _http_request
    results: list[dict] = []
    for probe in probes:
        url = base_url.rstrip("/") + probe.path
        status, body = http(probe.method, url, payload=probe.payload)
        if probe.kind == "no_stack_trace":
            marker = next((m for m in STACK_TRACE_MARKERS if m in body), None)
            passed = marker is None
            detail = (
                "no stack-trace markers in response body"
                if passed
                else f"stack-trace marker in response body: {marker!r}"
            )
        elif probe.kind == "authz_direct_access":
            passed = status in _AUTHZ_BLOCKING_STATUSES
            detail = (
                "direct access blocked"
                if passed
                else f"protected path served status {status} without auth"
            )
        else:  # server_side_validation
            passed = 400 <= status < 500
            detail = (
                "invalid payload rejected with a 4xx"
                if passed
                else f"invalid payload produced status {status}, expected 4xx"
            )
        results.append(
            {
                "detail": detail,
                "method": probe.method,
                "passed": passed,
                "path": probe.path,
                "probe": probe.kind,
                "status": status,
            }
        )
    return results


# --- metamorphic tier (R21) -------------------------------------------------------------


@dataclass(frozen=True)
class MetamorphicCheck:
    """One target-free metamorphic check, executed on the clone through the
    U4 harness. Identity kinds declare the two step indices whose post-step
    fingerprints must match; create-then-list relies on the manifest's own
    deterministic post-assertions."""

    name: str
    kind: str
    manifest: ScenarioManifest
    identity_steps: tuple[int, int] | None = None

    def __post_init__(self) -> None:
        if self.kind not in METAMORPHIC_KINDS:
            raise SettleError(
                f"metamorphic kind must be one of {', '.join(METAMORPHIC_KINDS)},"
                f" got {self.kind!r}"
            )
        if self.kind in _IDENTITY_KINDS:
            steps = self.identity_steps
            n = len(self.manifest.steps)
            if (
                steps is None
                or len(steps) != 2
                or not all(0 <= i < n for i in steps)
                or steps[0] == steps[1]
            ):
                raise SettleError(
                    f"identity check {self.name!r} needs identity_steps ="
                    f" two distinct step indices in 0..{n - 1}"
                )
        elif not any(s.post_assertion for s in self.manifest.steps):
            raise SettleError(
                f"create_then_list check {self.name!r} needs at least one"
                " deterministic post_assertion in its manifest (R21)"
            )


def run_metamorphic_checks(
    checks: Sequence[MetamorphicCheck],
    driver: Driver,
    cache: ResolutionCache,
    resolve: ResolveFn,
    *,
    telemetry: HealTelemetry | None = None,
) -> list[dict]:
    """Run the metamorphic tier on the clone — no target involved (R21)."""
    results: list[dict] = []
    for check in checks:
        result = execute_scenario(
            check.manifest, "clone", driver, cache, resolve, telemetry=telemetry
        )
        if result.status != "completed":
            passed = False
            detail = f"execution {result.status}: {result.failure_reason}"
        elif check.kind in _IDENTITY_KINDS:
            i, j = check.identity_steps
            fp_a = fingerprint(result.steps[i].evidence.a11y_tree)
            fp_b = fingerprint(result.steps[j].evidence.a11y_tree)
            passed = fp_a == fp_b
            detail = (
                f"fingerprints at steps {i} and {j} "
                + ("match" if passed else "diverge")
            )
        else:
            passed = True
            detail = "completed with all post-assertions holding"
        results.append(
            {
                "detail": detail,
                "kind": check.kind,
                "name": check.name,
                "passed": passed,
                "scenario_id": check.manifest.scenario_id,
            }
        )
    return results


# --- SCEN persistence (R22) ---------------------------------------------------------------


def scen_id(episode_id: int, scenario_id: str) -> str:
    """Deterministic, globally unique SCEN id: episode-prefixed slug (IDs are
    globally unique, episode-scoped only for display — 003 R1)."""
    slug = _SCEN_SLUG_RE.sub("-", scenario_id).strip("-")
    return f"SCEN-e{episode_id}-{slug}"


def write_scen_rows(
    store: Store,
    episode_id: int,
    snapshot_id: int,
    verdicts: Sequence[ScenarioVerdict],
) -> dict[str, str]:
    """Persist settled verdicts as SCEN rows keyed (episode, snapshot), with
    tier, verdict, judge metadata, and the judge-input a11y-diff payloads
    (R22 — replay re-judging needs the original inputs). Atomic; returns
    ``{scenario_id: SCEN id}``."""
    ids: dict[str, str] = {}
    with store.transaction():
        for v in verdicts:
            sid = scen_id(episode_id, v.scenario_id)
            store.conn.execute(
                "INSERT INTO trace_scen (id, feat_id, result, evidence,"
                " episode_id, snapshot_id, tier, judge_mode,"
                " judge_metadata_json, judge_input_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    v.feat_id,
                    v.verdict,
                    v.failure or "",
                    int(episode_id),
                    int(snapshot_id),
                    v.tier,
                    v.judge_mode,
                    json.dumps(
                        v.judge_metadata, sort_keys=True, ensure_ascii=False
                    ),
                    json.dumps(
                        v.judge_input, sort_keys=True, ensure_ascii=False
                    ),
                ),
            )
            ids[v.scenario_id] = sid
    return ids


def unreached_feat_ids(store: Store, target: str) -> set[str]:
    """FEATs never mentioned in ANY MSG for the target (R23's unreached-
    frontier bucket — same mention join as R10's coverage audit)."""
    rows = store.conn.execute(
        "SELECT f.id FROM trace_feat f"
        " WHERE f.target = ? AND f.status = 'confirmed'"
        " AND NOT EXISTS (SELECT 1 FROM trace_msg_mentions m"
        "                 WHERE m.feat_id = f.id)",
        (target,),
    ).fetchall()
    return {row["id"] for row in rows}


# --- report assembly (R23) ------------------------------------------------------------------


def _trace_command(sid: str) -> str:
    # The attribution join lives once, in the CLI (U9's `af trace chain`),
    # not in this renderer (R23) — the report only embeds the command.
    return f"af trace chain {sid}"


def assemble_report(
    *,
    episode_id: int,
    snapshot_id: int,
    target: str,
    verdicts: Sequence[ScenarioVerdict],
    scen_ids: Mapping[str, str],
    baseline: Sequence[dict] = (),
    metamorphic: Sequence[dict] = (),
    unreached_feats: set[str] = frozenset(),
    uat_accepted_feats: set[str] = frozenset(),
    instrument_health: dict | None = None,
) -> dict:
    """Assemble the settlement report (R23) — the human reflector's entry
    point. Pure function of its inputs, no timestamps: rendering is
    deterministic (the U7 verification)."""
    rows: list[dict] = []
    for v in verdicts:
        sid = scen_ids[v.scenario_id]
        tags: list[str] = []
        if v.feat_id in unreached_feats:
            tags.append("unreached_frontier")
        if v.verdict == "fail" and v.feat_id in uat_accepted_feats:
            tags.append(UAT_DIVERGENCE_TAG)
        if v.evidence_stale:
            tags.append("evidence_stale")
        rows.append(
            {
                "failure": v.failure,
                "feat_id": v.feat_id,
                "judge_mode": v.judge_mode,
                "scen_id": sid,
                "scenario_id": v.scenario_id,
                "tags": tags,
                "tier": v.tier,
                "trace_command": _trace_command(sid),
                "verdict": v.verdict,
            }
        )

    scoreable = [v for v in verdicts if v.verdict != "invalid"]
    passed = sum(1 for v in scoreable if v.verdict == "pass")
    by_tier: dict[str, dict] = {}
    for v in scoreable:
        bucket = by_tier.setdefault(v.tier, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if v.verdict == "pass":
            bucket["passed"] += 1
    overall = passed / len(scoreable) if scoreable else 0.0

    failed_rows = [r for r in rows if r["verdict"] == "fail"]
    return {
        "baseline": list(baseline),
        "commands": {"report": f"af episode report {episode_id}"},
        "episode_id": episode_id,
        "evidence_stale_feats": sorted(
            {v.feat_id for v in verdicts if v.evidence_stale}
        ),
        "failed_scenarios": failed_rows,
        "instrument_health": (
            dict(instrument_health)
            if instrument_health is not None
            else {
                "mutation_audits": [],
                "note": "no mutation audits recorded for this episode (U8"
                " populates this section)",
            }
        ),
        "metamorphic": list(metamorphic),
        "scenarios": rows,
        "score": {
            "by_tier": by_tier,
            "invalid_excluded": len(verdicts) - len(scoreable),
            "overall": overall,
            "passed": passed,
            "scoreable": len(scoreable),
        },
        "snapshot_id": snapshot_id,
        "target": target,
        "uat_divergence": [
            r for r in rows if UAT_DIVERGENCE_TAG in r["tags"]
        ],
        "unreached_frontier": {
            "feat_ids": sorted(unreached_feats),
            "note": "full-denominator score (§10 full-coverage fairness);"
            " these FEATs were never mentioned in any increment —"
            " budget-vs-capability, not clone failure",
            "scen_ids": sorted(
                r["scen_id"] for r in rows if "unreached_frontier" in r["tags"]
            ),
        },
    }


def render_report(report: dict) -> str:
    """Canonical report text: sorted keys, ``\\n`` newlines — byte-stable
    across platforms and runs (the U7 determinism verification)."""
    return (
        json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    )


def store_report(store: Store, episode_id: int, report: dict) -> int:
    """Persist the report (R23 storage) and stamp the episode settled."""
    episode = store.get_episode(episode_id)
    if episode is None:
        raise StoreError(f"episode {episode_id} does not exist")
    now = _utcnow()
    with store.transaction():
        cur = store.conn.execute(
            "INSERT INTO settlement_reports"
            " (episode_id, score, report_json, created_at)"
            " VALUES (?, ?, ?, ?)",
            (episode_id, report["score"]["overall"], render_report(report), now),
        )
        store.conn.execute(
            "UPDATE episodes SET settled_at = ? WHERE id = ?",
            (now, episode_id),
        )
    return cur.lastrowid


# --- the settlement (R7 dual-app + R20-R23 assembly) -------------------------------------------


@dataclass(frozen=True)
class SettlementPlan:
    """Everything a settlement measures, declared up front."""

    episode_id: int
    snapshot_id: int
    target: str
    manifests: tuple[ScenarioManifest, ...]
    baseline_probes: tuple[BaselineProbe, ...] = ()
    metamorphic_checks: tuple[MetamorphicCheck, ...] = ()
    uat_accepted_feats: frozenset[str] = frozenset()


@dataclass
class SettlementOutcome:
    """The settlement's full yield: the report plus its persisted keys."""

    report: dict
    scen_ids: dict[str, str] = field(default_factory=dict)
    verdicts: list[ScenarioVerdict] = field(default_factory=list)


def run_settlement(
    plan: SettlementPlan,
    *,
    store: Store,
    config: SettleConfig,
    reset_target,
    drivers: Mapping[str, Driver],
    cache: ResolutionCache,
    resolve: ResolveFn,
    clone_server: DevServer | None = None,
    clone_url: str | None = None,
    http: Http | None = None,
    telemetry: HealTelemetry | None = None,
    evidence_dir: Path | None = None,
    instrument_health: dict | None = None,
) -> SettlementOutcome:
    """Execute one full settlement: target reset -> both apps -> rubric ->
    baseline -> metamorphic -> SCEN rows + report (R7, R20-R23).

    ``reset_target`` is the R6 reset-to-seed hook (settlement-start reset);
    ``drivers`` must carry both apps (the static port table put them up,
    R7); when ``clone_server`` is provided it must be running AND held open
    (``DevServer.hold_open()``) — the orchestrator owns it across UAT and
    settlement, and an unheld server could vanish mid-rubric.
    """
    for app in ("target", "clone"):
        if app not in drivers:
            raise SettleError(
                f"settlement needs drivers for both apps (R7); missing {app!r}"
            )
    if clone_server is not None:
        if clone_server.port is None:
            raise SettleError(
                "clone dev server is not running; settlement needs both apps"
                " up (R7)"
            )
        if not clone_server.held:
            raise SettleError(
                "clone dev server must be held open across UAT and settlement"
                " (R7): call hold_open() before settling"
            )
        clone_url = clone_server.url
    if plan.baseline_probes and clone_url is None:
        raise SettleError(
            "baseline probes need the clone's URL: pass clone_server or"
            " clone_url (R21)"
        )

    # Reset-to-seed at settlement start (R6): all cached target-side evidence
    # and fingerprints are defined against post-seed state.
    reset_target()
    telemetry = telemetry or HealTelemetry()

    verdicts: list[ScenarioVerdict] = []
    for manifest in plan.manifests:
        target_result = execute_scenario(
            manifest,
            "target",
            drivers["target"],
            cache,
            resolve,
            telemetry=telemetry,
            evidence_dir=evidence_dir,
        )
        clone_result = execute_scenario(
            manifest,
            "clone",
            drivers["clone"],
            cache,
            resolve,
            telemetry=telemetry,
            evidence_dir=evidence_dir,
        )
        verdicts.append(
            compare_scenario(manifest, target_result, clone_result, config)
        )

    baseline = run_baseline_probes(clone_url, plan.baseline_probes, http) if (
        plan.baseline_probes
    ) else []
    metamorphic = run_metamorphic_checks(
        plan.metamorphic_checks,
        drivers["clone"],
        cache,
        resolve,
        telemetry=telemetry,
    )

    scen_ids = write_scen_rows(
        store, plan.episode_id, plan.snapshot_id, verdicts
    )
    report = assemble_report(
        episode_id=plan.episode_id,
        snapshot_id=plan.snapshot_id,
        target=plan.target,
        verdicts=verdicts,
        scen_ids=scen_ids,
        baseline=baseline,
        metamorphic=metamorphic,
        unreached_feats=unreached_feat_ids(store, plan.target),
        uat_accepted_feats=set(plan.uat_accepted_feats),
        instrument_health=instrument_health,
    )
    store_report(store, plan.episode_id, report)
    logger.info(
        "episode %d settled: score %.3f over %d scoreable scenario(s)",
        plan.episode_id,
        report["score"]["overall"],
        report["score"]["scoreable"],
    )
    return SettlementOutcome(
        report=report, scen_ids=scen_ids, verdicts=verdicts
    )
