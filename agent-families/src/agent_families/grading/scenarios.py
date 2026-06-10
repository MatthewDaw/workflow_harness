"""Scenario harness: resolve, cache, replay, heal (plan-003 U4, R16-R19).

Behavioral scenarios must execute on two different DOMs (target and clone) at
training-loop cost. The economics rest on a three-part discipline:

- **Manifests** (R16) are JSON apparatus authored at FEAT-mint time: NL steps,
  an expected outcome, and a tolerance tier (``must``/``should``/``free``).
- **Resolution** (R17) maps one NL step + one accessibility tree to a single
  structured Playwright action via the judge seam (single-shot, default-fail,
  typed ``element_absent``/``ambiguous`` outcomes — R18). Every resolution is
  cached under ``SHA256(step, URL pattern, a11y fingerprint)`` scoped
  (scenario, app); replay executes cached actions as plain Playwright with
  **zero LLM calls** and captures per-step evidence (a11y snapshot +
  screenshot).
- **Fingerprint normalization** (R17, load-bearing): the fingerprint hashes a
  normalized structural skeleton — roles + accessible names of
  interactive/landmark elements only, repeated content rows collapsed to a
  count-free shape, text content/dates/numeric counts stripped — so routine
  data mutation does not invalidate caches. This is what lets the target-side
  cache accumulate hits forever while scenarios mutate state.

Failure attribution (R18): the first ``element_absent`` step on the **clone**
fails the scenario as ``feature_absent``, skips the remaining steps, attempts
no healing, and captures the stopping snapshot — separating "not built" from
"built wrong" exactly as the attribution tree needs. ``ambiguous`` is a
distinct outcome and lands in the "built wrong" channel (``failed``).

Self-healing (R19): a failed replay step re-resolves that step only, updates
the cache, and must pair with a deterministic post-assertion. Heal telemetry
is per-app. Within-settlement state mutation on the target never registers as
a heal — the fingerprint normalization absorbs it, so a mutated-but-
structurally-identical page is a plain cache hit, and a structurally-changed
page is a plain cache miss. Only a fingerprint-stable re-resolution (cache hit
whose action fails) counts as a heal. A target-side heal flags the FEAT's
evidence ``stale`` (surfaced by the settlement report, U7); a target-side hard
failure marks the scenario ``invalid``, and :func:`score_denominator` excludes
``invalid`` scenarios from the score denominator.

Test discipline: the resolver is exercised offline through the judge seam's
replay fixtures and scripted fakes; a11y trees come from committed static
fixtures, captured once and never re-captured in CI. Fixture refresh is a
deliberate recorded operation tied to seed-manifest or target-pin changes
(Phase 1's canary-test pattern).

Documented live smoke (the U4 verification — manual, requires Docker + the
primed linkding stack + ``playwright`` installed + the claude CLI logged in)::

    1. Boot and seed linkding (target_env one-command bring-up).
    2. Open a Playwright page logged into linkding, wrap it in
       :class:`PlaywrightDriver`, and run one manifest through
       :func:`execute_scenario` with a file-backed :class:`ResolutionCache`
       and ``make_judge_resolver(ResolverConfig(model=..., max_retries=...,
       mode="passthrough"))`` — every step resolves live and caches.
    3. Re-run the same scenario against the same cache file with a resolver
       that fails the test if called (the second run must be all cache hits):
       zero LLM calls, scenario ``completed``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from agent_families.judge import run_judge
from agent_families.store import SCENARIO_TIERS

logger = logging.getLogger(__name__)

# --- vocabulary ----------------------------------------------------------------

APPS = ("target", "clone")

RESOLVER_OUTCOMES = ("resolved", "element_absent", "ambiguous")

# Scenario execution statuses (R18/R19). `completed` means every step executed
# and every declared post-assertion held; the outcome *comparison* judgment is
# settlement's job (U7), not the harness's.
SCENARIO_STATUSES = ("completed", "failed", "feature_absent", "invalid")

STEP_STATUSES = ("cached", "resolved", "healed", "failed", "skipped")

# Deterministic post-assertion kinds (R19's heal pairing requirement).
POST_ASSERTION_KINDS = ("url_contains", "node_present", "node_absent")

# The closed action vocabulary the resolver may emit and the drivers execute.
ALLOWED_ACTIONS = ("goto", "click", "fill", "press", "check", "uncheck", "select")

# Roles whose (role, normalized name) survive into the fingerprint skeleton.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "textbox",
        "searchbox",
        "checkbox",
        "radio",
        "combobox",
        "listbox",
        "option",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "tab",
        "switch",
        "slider",
        "spinbutton",
    }
)
LANDMARK_ROLES = frozenset(
    {
        "banner",
        "navigation",
        "main",
        "contentinfo",
        "complementary",
        "form",
        "search",
        "region",
        "dialog",
        "alertdialog",
        "menubar",
        "menu",
        "tablist",
        "toolbar",
    }
)
# Content-row roles: repeated rows collapse to a count-free shape and the
# names inside them (bookmark titles, URLs, dates) are data, not structure.
ROW_ROLES = frozenset({"listitem", "row", "article"})

_DIGIT_RUN_RE = re.compile(r"\d+")
_NAME_SEPARATORS_RE = re.compile(r"[\s/:\-,.()\[\]]+")


class ScenarioError(Exception):
    """Base for every scenario-harness failure."""


class ManifestError(ScenarioError):
    """A scenario manifest is malformed (apparatus problems are loud, R16)."""


class ActionExecutionError(ScenarioError):
    """A driver could not execute a structured action (the heal trigger, R19)."""


# --- manifests (R16) -------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioStep:
    """One NL step, optionally paired with a deterministic post-assertion."""

    text: str
    post_assertion: dict | None = None


@dataclass(frozen=True)
class ScenarioManifest:
    """The R16 manifest: NL steps, expected outcome, tolerance tier."""

    scenario_id: str
    feat_id: str
    title: str
    steps: tuple[ScenarioStep, ...]
    expected_outcome: str
    tier: str


def _parse_post_assertion(raw, where: str) -> dict:
    if not isinstance(raw, dict):
        raise ManifestError(f"{where}: post_assertion must be an object")
    kind = raw.get("kind")
    if kind not in POST_ASSERTION_KINDS:
        raise ManifestError(
            f"{where}: post_assertion kind must be one of"
            f" {', '.join(POST_ASSERTION_KINDS)}, got {kind!r}"
        )
    if kind == "url_contains":
        if not isinstance(raw.get("value"), str) or not raw["value"]:
            raise ManifestError(
                f"{where}: url_contains post_assertion needs a non-empty 'value'"
            )
    else:
        for key in ("role", "name"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ManifestError(
                    f"{where}: {kind} post_assertion needs a non-empty '{key}'"
                )
    return dict(raw)


def parse_manifest(data: dict) -> ScenarioManifest:
    """Validate one manifest object (the ``manifest_json`` column format)."""
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    scenario_id = data.get("scenario_id")
    if not isinstance(scenario_id, str) or not scenario_id.strip():
        raise ManifestError("manifest needs a non-empty string 'scenario_id'")
    feat_id = data.get("feat_id")
    if not isinstance(feat_id, str) or not feat_id.startswith("FEAT-"):
        raise ManifestError(
            f"manifest 'feat_id' must be a FEAT-* id, got {feat_id!r}"
        )
    title = data.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ManifestError("manifest needs a non-empty string 'title'")
    tier = data.get("tier")
    if tier not in SCENARIO_TIERS:
        raise ManifestError(
            f"manifest 'tier' must be one of {', '.join(SCENARIO_TIERS)},"
            f" got {tier!r}"
        )
    expected = data.get("expected_outcome")
    if not isinstance(expected, str) or not expected.strip():
        raise ManifestError("manifest needs a non-empty string 'expected_outcome'")
    raw_steps = data.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ManifestError("manifest needs a non-empty 'steps' list")
    steps: list[ScenarioStep] = []
    for i, raw in enumerate(raw_steps):
        where = f"manifest {scenario_id} step #{i}"
        if isinstance(raw, str):
            text, post = raw, None
        elif isinstance(raw, dict):
            text = raw.get("step")
            post = raw.get("post_assertion")
            if post is not None:
                post = _parse_post_assertion(post, where)
        else:
            raise ManifestError(f"{where}: must be a string or an object")
        if not isinstance(text, str) or not text.strip():
            raise ManifestError(f"{where}: needs non-empty NL step text")
        steps.append(ScenarioStep(text=text.strip(), post_assertion=post))
    return ScenarioManifest(
        scenario_id=scenario_id.strip(),
        feat_id=feat_id,
        title=title.strip(),
        steps=tuple(steps),
        expected_outcome=expected.strip(),
        tier=tier,
    )


def load_manifest_json(text: str) -> ScenarioManifest:
    """Parse a manifest from its JSON text (the ``manifest_json`` column)."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest is not valid JSON: {exc}") from exc
    return parse_manifest(data)


# --- fingerprint normalization (R17, load-bearing) -------------------------------


def _normalize_name(name) -> str:
    """Strip numeric counts and date digits from an accessible name.

    Digit runs vanish entirely (counts, dates, IDs), separators collapse to
    single spaces, case folds — so "Bookmarks (12)" and "Bookmarks (7)"
    normalize identically while "Add bookmark" stays distinct from "Search".
    """
    if not isinstance(name, str):
        return ""
    stripped = _DIGIT_RUN_RE.sub("", name)
    return _NAME_SEPARATORS_RE.sub(" ", stripped).strip().lower()


def _skeleton(node, *, in_row: bool) -> list:
    """Normalized structural shapes for one a11y node (list — nodes that are
    neither interactive, landmark, nor row are transparent: their children
    bubble up and their own text content is dropped)."""
    if not isinstance(node, dict):
        return []
    role = node.get("role")
    children = node.get("children") or []
    if role in ROW_ROLES:
        inner: list = []
        for child in children:
            inner.extend(_skeleton(child, in_row=True))
        return [["row", role, inner]]
    inner = []
    for child in children:
        inner.extend(_skeleton(child, in_row=in_row))
    if role in INTERACTIVE_ROLES or role in LANDMARK_ROLES:
        # Inside a content row, names are data (titles, URLs), not structure.
        name = "" if in_row else _normalize_name(node.get("name"))
        return [[role, name, inner]]
    return inner


def _collapse_rows(shapes: list) -> list:
    """Collapse repeated content rows to a count-free shape: among siblings,
    identical row shapes keep one occurrence; recursion handles nesting."""
    collapsed = []
    seen_rows: list = []
    for shape in shapes:
        kind, role, inner = shape
        shape = [kind, role, _collapse_rows(inner)]
        if kind == "row":
            if shape in seen_rows:
                continue
            seen_rows.append(shape)
        collapsed.append(shape)
    return collapsed


def fingerprint(a11y_tree: dict) -> str:
    """SHA256 over the normalized structural skeleton of an a11y tree (R17)."""
    skeleton = _collapse_rows(_skeleton(a11y_tree, in_row=False))
    canonical = json.dumps(
        skeleton, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def url_pattern(url: str) -> str:
    """Normalize a URL to its cache-key pattern: path only (query dropped),
    numeric path segments — entity IDs — replaced by ``{n}``."""
    path = urlsplit(url).path or "/"
    segments = [
        "{n}" if segment.isdigit() else segment for segment in path.split("/")
    ]
    return "/".join(segments) or "/"


# --- resolution cache (R17) -------------------------------------------------------


def cache_key(step_text: str, url_pat: str, a11y_fingerprint: str) -> str:
    """``SHA256(step, URL pattern, a11y fingerprint)`` — the R17 cache key."""
    canonical = json.dumps(
        {
            "fingerprint": a11y_fingerprint,
            "step": step_text,
            "url_pattern": url_pat,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_action(action: dict) -> None:
    if not isinstance(action, dict):
        raise ScenarioError(f"action must be an object, got {type(action).__name__}")
    kind = action.get("action")
    if kind not in ALLOWED_ACTIONS:
        raise ScenarioError(
            f"action kind must be one of {', '.join(ALLOWED_ACTIONS)}, got {kind!r}"
        )
    if not isinstance(action.get("selector"), str):
        raise ScenarioError("action needs a string 'selector'")
    if not isinstance(action.get("args"), list):
        raise ScenarioError("action needs an 'args' list")


class ResolutionCache:
    """Per-(scenario, app) resolution cache, optionally file-backed.

    The persistent form is part of the per-target apparatus: JSON with sorted
    keys and ``\\n`` newlines so the committed/cached file is byte-stable
    across platforms (Windows KTD).
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else None
        self._data: dict[str, dict[str, dict]] = {}
        if self.path is not None and self.path.exists():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise ScenarioError(
                    f"resolution cache is not valid JSON: {self.path}: {exc}"
                ) from exc
            if not isinstance(loaded, dict):
                raise ScenarioError(
                    f"resolution cache must be a JSON object: {self.path}"
                )
            self._data = loaded

    @staticmethod
    def _scope(scenario_id: str, app: str) -> str:
        return f"{scenario_id}::{app}"

    def get(self, scenario_id: str, app: str, key: str) -> dict | None:
        action = self._data.get(self._scope(scenario_id, app), {}).get(key)
        return dict(action) if action is not None else None

    def put(self, scenario_id: str, app: str, key: str, action: dict) -> None:
        _validate_action(action)
        scope = self._scope(scenario_id, app)
        self._data.setdefault(scope, {})[key] = dict(action)
        self._save()

    def _save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(self._data, sort_keys=True, indent=2, ensure_ascii=False)
        self.path.write_text(body + "\n", encoding="utf-8", newline="\n")


# --- constrained resolver (R17/R18, judge seam mechanics) -------------------------

RESOLVER_SCHEMA = {
    "type": "object",
    "properties": {
        "outcome": {"type": "string", "enum": list(RESOLVER_OUTCOMES)},
        "action": {
            "type": ["object", "null"],
            "properties": {
                "action": {"type": "string", "enum": list(ALLOWED_ACTIONS)},
                "selector": {"type": "string"},
                "args": {"type": "array"},
            },
            "required": ["action", "selector", "args"],
            "additionalProperties": False,
        },
        "reason": {"type": "string"},
    },
    "required": ["outcome", "action", "reason"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Resolution:
    """One typed resolver outcome (R17/R18)."""

    outcome: str
    action: dict | None
    reason: str


# A resolver maps (NL step text, a11y tree) -> Resolution. The judge-backed
# implementation below is the production path; tests inject scripted fakes
# through the same seam (target_env's runner/http precedent).
ResolveFn = Callable[[str, dict], Resolution]


@dataclass(frozen=True)
class ResolverConfig:
    """Judge-seam plumbing for the resolver. Model/retries come from the
    [judge] config section via the caller (never hardcoded); mode/fixtures
    ride the record/replay seam exactly like every other judge call."""

    model: str
    max_retries: int
    bare: bool = False
    mode: str | None = None
    fixtures_dir: Path | None = None


def resolver_prompt(step_text: str, a11y_tree: dict) -> str:
    """The single-shot resolver prompt: default-fail framing, no volatile data
    (fixture keys hash the prompt — R23 discipline)."""
    tree_json = json.dumps(
        a11y_tree, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    actions = ", ".join(ALLOWED_ACTIONS)
    return (
        "You resolve ONE natural-language UI step to ONE structured browser"
        " action, using only the accessibility tree below.\n"
        "Default to failure: if no element in the tree clearly matches the"
        ' step, respond with outcome "element_absent" and action null. If more'
        " than one element plausibly matches and the step does not"
        ' disambiguate, respond with outcome "ambiguous" and action null.'
        ' Respond with outcome "resolved" ONLY when exactly one element'
        " clearly matches.\n"
        f"Allowed action kinds: {actions}. The selector must uniquely identify"
        " the element (prefer role/name-based selectors).\n\n"
        f"Step: {step_text}\n"
        f"Accessibility tree: {tree_json}"
    )


def _validate_resolution(output: dict) -> str | None:
    """``extra_validate`` for the resolver call: outcome/action coherence."""
    outcome = output.get("outcome")
    action = output.get("action")
    if outcome == "resolved":
        if action is None:
            return "outcome 'resolved' requires a non-null action"
        if not action.get("selector") and action.get("action") != "goto":
            return "resolved action needs a non-empty selector (except goto)"
    elif action is not None:
        return f"outcome '{outcome}' requires action to be null"
    return None


def make_judge_resolver(config: ResolverConfig) -> ResolveFn:
    """The production resolver: one NL step + a11y tree -> one structured call
    through :func:`run_judge` (single-shot, schema-forced, default-fail)."""

    def resolve(step_text: str, a11y_tree: dict) -> Resolution:
        result = run_judge(
            resolver_prompt(step_text, a11y_tree),
            RESOLVER_SCHEMA,
            config.model,
            max_retries=config.max_retries,
            bare=config.bare,
            extra_validate=_validate_resolution,
            mode=config.mode,
            fixtures_dir=config.fixtures_dir,
        )
        output = result.output
        return Resolution(
            outcome=output["outcome"],
            action=output["action"],
            reason=output["reason"],
        )

    return resolve


# --- drivers ----------------------------------------------------------------------


@dataclass(frozen=True)
class Observation:
    """One browse observation: the page's a11y tree and its URL."""

    a11y_tree: dict
    url: str


class Driver(Protocol):
    """The replay executor seam. Offline tests script it; live execution uses
    :class:`PlaywrightDriver`. ``execute`` raises :class:`ActionExecutionError`
    on failure (the R19 heal trigger)."""

    def observe(self) -> Observation: ...

    def execute(self, action: dict) -> None: ...

    def screenshot(self) -> bytes: ...


class PlaywrightDriver:
    """Plain-Playwright executor for live replay (zero LLM calls, R17).

    Duck-typed over a Playwright sync ``Page`` — this module never imports
    playwright, so the offline suite carries no dependency on it; the live
    smoke constructs the page (see the module docstring).
    """

    def __init__(self, page) -> None:
        self._page = page

    def observe(self) -> Observation:
        tree = self._page.accessibility.snapshot() or {}
        return Observation(a11y_tree=tree, url=self._page.url)

    def execute(self, action: dict) -> None:
        _validate_action(action)
        kind = action["action"]
        selector = action["selector"]
        args = action["args"]
        try:
            if kind == "goto":
                self._page.goto(args[0])
            elif kind == "click":
                self._page.click(selector)
            elif kind == "fill":
                self._page.fill(selector, args[0])
            elif kind == "press":
                self._page.press(selector, args[0])
            elif kind == "check":
                self._page.check(selector)
            elif kind == "uncheck":
                self._page.uncheck(selector)
            elif kind == "select":
                self._page.select_option(selector, args[0])
        except ScenarioError:
            raise
        except Exception as exc:
            raise ActionExecutionError(
                f"{kind} on {selector!r} failed: {exc}"
            ) from exc

    def screenshot(self) -> bytes:
        return self._page.screenshot()


# --- post-assertions (R19) ---------------------------------------------------------


def _tree_contains(node, role: str, name: str) -> bool:
    if not isinstance(node, dict):
        return False
    if node.get("role") == role and node.get("name") == name:
        return True
    return any(
        _tree_contains(child, role, name) for child in node.get("children") or []
    )


def check_post_assertion(assertion: dict, observation: Observation) -> bool:
    """Evaluate one deterministic post-assertion against an observation."""
    kind = assertion.get("kind")
    if kind == "url_contains":
        return assertion["value"] in observation.url
    if kind == "node_present":
        return _tree_contains(
            observation.a11y_tree, assertion["role"], assertion["name"]
        )
    if kind == "node_absent":
        return not _tree_contains(
            observation.a11y_tree, assertion["role"], assertion["name"]
        )
    raise ScenarioError(f"unknown post_assertion kind: {kind!r}")


# --- execution results and telemetry ------------------------------------------------


@dataclass(frozen=True)
class StepEvidence:
    """Per-step evidence (R17): a11y snapshot + URL + screenshot bytes."""

    a11y_tree: dict
    url: str
    screenshot: bytes


@dataclass(frozen=True)
class HealEvent:
    """One self-heal: a fingerprint-stable re-resolution after a failed replay
    step (R19). ``app`` is the telemetry channel."""

    app: str
    scenario_id: str
    step_index: int
    fingerprint: str
    old_action: dict
    new_action: dict


class HealTelemetry:
    """Per-app heal channels (R19): clone-side healing is expected churn;
    target-side healing flags evidence staleness for the settlement report."""

    def __init__(self) -> None:
        self.channels: dict[str, list[HealEvent]] = {app: [] for app in APPS}

    def record(self, event: HealEvent) -> None:
        self.channels[event.app].append(event)


@dataclass(frozen=True)
class StepResult:
    index: int
    text: str
    status: str
    action: dict | None
    resolver_calls: int
    evidence: StepEvidence | None
    failure: str | None


@dataclass
class ScenarioResult:
    """One scenario execution on one app. ``evidence_stale`` is the R19
    target-side-heal flag the settlement report surfaces against the FEAT's
    registry evidence; ``invalid`` results are excluded from the score
    denominator (:func:`score_denominator`)."""

    scenario_id: str
    feat_id: str
    app: str
    status: str
    steps: list[StepResult] = field(default_factory=list)
    resolver_calls: int = 0
    heal_events: list[HealEvent] = field(default_factory=list)
    evidence_stale: bool = False
    failure_reason: str | None = None


def score_denominator(results: list[ScenarioResult]) -> list[ScenarioResult]:
    """The scoreable subset: ``invalid`` scenarios (target-side hard failures,
    R19) are apparatus defects, not clone defects — excluded from the score
    denominator."""
    return [r for r in results if r.status != "invalid"]


def _write_evidence(
    evidence_dir: Path, scenario_id: str, app: str, index: int, ev: StepEvidence
) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{scenario_id}-{app}-step{index}")
    snapshot = json.dumps(
        {"a11y_tree": ev.a11y_tree, "url": ev.url},
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    (evidence_dir / f"{safe}.json").write_text(
        snapshot + "\n", encoding="utf-8", newline="\n"
    )
    (evidence_dir / f"{safe}.png").write_bytes(ev.screenshot)


# --- the harness (R17/R18/R19) -------------------------------------------------------


def execute_scenario(
    manifest: ScenarioManifest,
    app: str,
    driver: Driver,
    cache: ResolutionCache,
    resolve: ResolveFn,
    *,
    telemetry: HealTelemetry | None = None,
    evidence_dir: Path | None = None,
) -> ScenarioResult:
    """Execute one manifest on one app: cached replay, resolution on miss,
    healing on failed replay (R17-R19).

    Per step: observe -> fingerprint -> cache lookup. A hit executes as plain
    replay (zero LLM calls). A miss resolves once through ``resolve``. A hit
    whose execution fails triggers exactly one heal — a fingerprint-stable
    re-resolution that must pair with the step's deterministic post-assertion.
    Any failure on the **target** marks the scenario ``invalid`` (apparatus
    defect, R19); on the **clone**, ``element_absent`` short-circuits as
    ``feature_absent`` with the stopping snapshot and no healing (R18), and
    everything else is ``failed`` (built wrong).
    """
    if app not in APPS:
        raise ScenarioError(f"app must be one of {', '.join(APPS)}, got {app!r}")

    result = ScenarioResult(
        scenario_id=manifest.scenario_id,
        feat_id=manifest.feat_id,
        app=app,
        status="completed",
    )

    for i, step in enumerate(manifest.steps):
        obs = driver.observe()
        fp = fingerprint(obs.a11y_tree)
        key = cache_key(step.text, url_pattern(obs.url), fp)
        cached = cache.get(manifest.scenario_id, app, key)

        step_calls = 0
        step_status: str | None = None
        action: dict | None = None
        failure: str | None = None
        absent = False

        if cached is None:
            # Cache miss: one constrained resolution. On the target this is
            # either first contact or a structural change — a plain miss,
            # never a heal (R19).
            resolution = resolve(step.text, obs.a11y_tree)
            step_calls += 1
            if resolution.outcome == "element_absent":
                failure, absent = "element_absent", True
            elif resolution.outcome == "ambiguous":
                failure = "ambiguous_match"
            else:
                action = resolution.action
                cache.put(manifest.scenario_id, app, key, action)
                try:
                    driver.execute(action)
                    step_status = "resolved"
                except ActionExecutionError as exc:
                    failure = f"action_failed: {exc}"
        else:
            action = cached
            try:
                driver.execute(action)
                step_status = "cached"
            except ActionExecutionError:
                # Failed replay step -> heal: re-resolve THIS step only at the
                # same fingerprint, update the cache, and pair with the step's
                # deterministic post-assertion (R19).
                if step.post_assertion is None:
                    failure = "heal_requires_post_assertion"
                else:
                    resolution = resolve(step.text, obs.a11y_tree)
                    step_calls += 1
                    if resolution.outcome == "element_absent":
                        failure, absent = "element_absent", True
                    elif resolution.outcome == "ambiguous":
                        failure = "ambiguous_match"
                    else:
                        cache.put(
                            manifest.scenario_id, app, key, resolution.action
                        )
                        try:
                            driver.execute(resolution.action)
                            step_status = "healed"
                            event = HealEvent(
                                app=app,
                                scenario_id=manifest.scenario_id,
                                step_index=i,
                                fingerprint=fp,
                                old_action=action,
                                new_action=resolution.action,
                            )
                            result.heal_events.append(event)
                            if telemetry is not None:
                                telemetry.record(event)
                            if app == "target":
                                # Target-side heal: the FEAT's cached evidence
                                # no longer matches reality (R19).
                                result.evidence_stale = True
                                logger.warning(
                                    "target-side heal on %s step %d: FEAT %s"
                                    " evidence flagged stale",
                                    manifest.scenario_id,
                                    i,
                                    manifest.feat_id,
                                )
                            action = resolution.action
                        except ActionExecutionError as exc:
                            failure = f"heal_failed: {exc}"

        result.resolver_calls += step_calls

        if failure is None and step.post_assertion is not None:
            if not check_post_assertion(step.post_assertion, driver.observe()):
                failure = "post_assertion_failed"

        # Per-step evidence (R17); on failure this is the stopping snapshot
        # (R18 requires it for the feature_absent short-circuit).
        post_obs = driver.observe()
        evidence = StepEvidence(
            a11y_tree=post_obs.a11y_tree,
            url=post_obs.url,
            screenshot=driver.screenshot(),
        )
        if evidence_dir is not None:
            _write_evidence(evidence_dir, manifest.scenario_id, app, i, evidence)

        if failure is not None:
            result.steps.append(
                StepResult(
                    index=i,
                    text=step.text,
                    status="failed",
                    action=action,
                    resolver_calls=step_calls,
                    evidence=evidence,
                    failure=failure,
                )
            )
            if app == "target":
                # Any target-side hard failure: the scenario itself is broken
                # apparatus -> invalid, excluded from the denominator (R19).
                result.status = "invalid"
            elif absent:
                # First unresolvable step on the clone -> not built (R18).
                result.status = "feature_absent"
            else:
                result.status = "failed"
            result.failure_reason = failure
            for j in range(i + 1, len(manifest.steps)):
                result.steps.append(
                    StepResult(
                        index=j,
                        text=manifest.steps[j].text,
                        status="skipped",
                        action=None,
                        resolver_calls=0,
                        evidence=None,
                        failure=None,
                    )
                )
            return result

        result.steps.append(
            StepResult(
                index=i,
                text=step.text,
                status=step_status,
                action=action,
                resolver_calls=step_calls,
                evidence=evidence,
                failure=None,
            )
        )

    return result
