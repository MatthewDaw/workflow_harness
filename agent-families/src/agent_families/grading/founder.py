"""Grader-side founder simulator — the degradation generator half (plan-007 U5).

A greenfield episode replaces the explorer's perfect oracle with a *founder*: a
persona who answers from a logged, seeded **degradation** of the FEAT+DEC
registries — ignorant where the log says so, never untruthful (DESIGN §9's
perfect-oracle reason is preserved by degrading *knowledge*, never *truthfulness*;
KTD3). The full registry stays the hidden grading oracle, so elicitation quality
becomes measurable.

This module builds the founder *model*: per (target registries, seed, degradation
params) it deterministically partitions every registry ref into

- ``intact``  — the founder knows it plainly,
- ``blurred`` — the founder knows it only vaguely (a cached, entailment-linted
  JTBD-level blur), or
- ``dropped`` — the founder never thought about it (scripted ignorance lands in
  U6's session half).

Determinism is load-bearing (replays reproduce, benchmarks are pinned): the same
``(registry snapshot, seed, params)`` yields byte-identical ``founder_models`` +
``founder_knowledge`` rows AND identical blur prose. Blur prose is cached
**target-side** (``founder_blur_cache``), keyed by the registry entry's content
digest among other things, so a re-extraction visibly misses the cache and an
identical input never re-pays prose variance into a benchmark run (KTD3).

The blur judge and the entailment lint are injectable seams (the
``improvement.DesignJudgeFn`` precedent): the live bindings wrap :func:`run_judge`
through the standard record/replay fixtures; the offline suite injects scripted
fakes, so the suite passes with zero quota and no ``claude`` on PATH.

The founder *session* (answer / adjudicate / accept / UAT) is U6 and lives beside
this in the same file when it lands; this unit is the generator only.
"""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from agent_families.judge import run_judge

if TYPE_CHECKING:
    from agent_families.config import GreenfieldConfig
    from agent_families.store import Store

# The blur prompt-set version is part of the target-side cache key (KTD3): a
# prompt-set change is an instrument event that visibly invalidates the cache,
# re-baselining benchmarks rather than silently drifting prose on the same seed.
PROMPT_SET_VERSION = "founder-blur-v1"

# Tier ordering for the deterministic core-loop ranking (KTD3 — "core-loop = the
# top-N JTBD-linked FEATs from registry links, not a judge call"). A FEAT's most
# core active manifest tier ranks it; ties break on the permanent FEAT id.
_TIER_PRIORITY = {"must": 0, "should": 1, "free": 2}


class FounderError(Exception):
    """Founder-model misuse or a broken invariant, with an actionable message."""


# --- registry entries (the degradation substrate) --------------------------------


@dataclass(frozen=True)
class RegistryEntry:
    """One degradable registry ref — a FEAT or a DEC — with the behavioral text
    a blur generalizes and a content digest that keys the blur cache.

    ``entry_digest`` is a digest of the entry's *content* (not the target image
    digest): a registry re-extraction that changes the behavior text changes the
    digest, so the blur cache misses and regenerates (KTD3's cache-key rule).
    ``is_core`` marks core-loop membership (FEATs only; the stratification guard
    protects these and they are excluded from recovery denominators).
    """

    ref_kind: str  # "feat" | "dec"
    ref_id: str
    description: str
    entry_digest: str
    tier: str  # "must"|"should"|"free" for feat; "" for dec
    is_core: bool


def entry_content_digest(payload: dict) -> str:
    """A stable content digest of a registry entry (the blur-cache key half that
    re-extraction invalidates). Canonical JSON so it is order-insensitive."""
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _feat_entries(store: Store, target: str, core_loop_n: int) -> list[RegistryEntry]:
    """Confirmed, target-scoped FEATs with an active scenario manifest, ranked
    into a deterministic core-loop (KTD3).

    A FEAT's behavioral text and tier come from its most-core active manifest
    (``author_manifest``'s shape: key/steps/expected_outcome/tier). Confirmed
    FEATs with no active manifest are skipped — there is nothing to blur."""
    rows = store.conn.execute(
        "SELECT id FROM trace_feat"
        " WHERE target = ? AND status = 'confirmed' ORDER BY id",
        (target,),
    ).fetchall()
    raw: list[tuple[str, str, str, str]] = []  # (feat_id, description, tier, digest)
    for row in rows:
        fid = row["id"]
        manifests = store.conn.execute(
            "SELECT manifest_json, tier FROM scenario_manifests"
            " WHERE feat_id = ? AND status = 'active'",
            (fid,),
        ).fetchall()
        best: tuple[int, str, str, str] | None = None
        for man in manifests:
            try:
                payload = json.loads(man["manifest_json"])
            except (TypeError, ValueError):
                payload = {}
            tier = man["tier"]
            priority = _TIER_PRIORITY.get(tier, len(_TIER_PRIORITY))
            description = (
                str(payload.get("expected_outcome") or "").strip()
                or " ".join(str(s) for s in payload.get("steps", []))
                or fid.removeprefix("FEAT-").replace("-", " ")
            )
            digest = entry_content_digest(
                {"kind": "feat", "manifest": payload, "tier": tier}
            )
            cand = (priority, tier, description, digest)
            if best is None or cand[0] < best[0]:
                best = cand
        if best is None:
            continue
        raw.append((fid, best[2], best[1], best[3]))

    # Core-loop ranking: most-core tier first, FEAT id as the stable tiebreak.
    ranked = sorted(
        raw, key=lambda r: (_TIER_PRIORITY.get(r[2], len(_TIER_PRIORITY)), r[0])
    )
    core_ids = {r[0] for r in ranked[: max(core_loop_n, 0)]}
    return [
        RegistryEntry(
            ref_kind="feat",
            ref_id=fid,
            description=description,
            entry_digest=digest,
            tier=tier,
            is_core=fid in core_ids,
        )
        for (fid, description, tier, digest) in raw
    ]


def _dec_entries(store: Store, target: str) -> list[RegistryEntry]:
    """Confirmed, target-scoped DECs. DECs are never core-loop members — the
    stratification guard is a JTBD-linked-FEAT concern (KTD3)."""
    rows = store.conn.execute(
        "SELECT id, category, description FROM trace_dec"
        " WHERE target = ? AND status = 'confirmed' ORDER BY id",
        (target,),
    ).fetchall()
    entries: list[RegistryEntry] = []
    for row in rows:
        description = str(row["description"] or "").strip() or row["id"]
        digest = entry_content_digest(
            {"kind": "dec", "category": row["category"], "description": description}
        )
        entries.append(
            RegistryEntry(
                ref_kind="dec",
                ref_id=row["id"],
                description=description,
                entry_digest=digest,
                tier="",
                is_core=False,
            )
        )
    return entries


def read_registry_entries(
    store: Store, target: str, *, core_loop_n: int
) -> tuple[RegistryEntry, ...]:
    """Every confirmed FEAT+DEC for ``target``, ref-id-sorted, with core-loop
    membership resolved (KTD3). The DEC table is required: a greenfield founder
    over a FEAT-only registry would have no decisions to be ignorant of, which is
    a setup error, not a silent empty set."""
    feats = _feat_entries(store, target, core_loop_n)
    decs = _dec_entries(store, target)
    if not feats and not decs:
        raise FounderError(
            f"no confirmed registry entries for target {target!r}: a founder"
            " model needs an extracted FEAT+DEC registry (run pre-research first)"
        )
    entries = feats + decs
    return tuple(sorted(entries, key=lambda e: e.ref_id))


# --- degradation params + seeding (deterministic) --------------------------------


def degradation_params_hash(config: GreenfieldConfig) -> str:
    """A stable hash of *only* the params that shape the model (drop/blur rates,
    core-loop N). Pinned into ``founder_models`` and the blur cache key so a
    severity-param change re-baselines, never silently mixing two regimes."""
    payload = {
        "drop_rate": config.drop_rate,
        "blur_rate": config.blur_rate,
        "core_loop_n": config.core_loop_n,
    }
    return entry_content_digest(payload)


def training_seed(episode_id: int) -> int:
    """Training-world seed: derived from the episode id so a replay reproduces
    the persona (DESIGN §9). The **benchmark** path overrides this with
    ``config.benchmark_seed`` (KTD8 — the two schemes coexist explicitly; this
    rule is training-world only). The caller (U7/U13b) picks which to pass."""
    if episode_id < 0:
        raise FounderError(f"episode_id must be non-negative, got {episode_id}")
    return episode_id


def _rng(*parts: object) -> random.Random:
    """A deterministic RNG seeded from a sha256 over the parts — stable across
    processes (unlike ``hash()``, which is salted), so replays reproduce."""
    material = ":".join(str(p) for p in parts)
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest, "big"))


# --- blur seams: the judge + the entailment lint ---------------------------------


@dataclass(frozen=True)
class BlurRequest:
    """One blur-generation request handed to the blur seam. ``attempt`` rises on
    each regeneration after a lint failure (live, the judge prompt varies with
    it; the offline fake may vary its draft by it)."""

    entry: RegistryEntry
    attempt: int
    goal_statement: str


# The blur seam (improvement.DesignJudgeFn precedent): one request -> JTBD-level
# blur prose. Live = run_judge over build_blur_prompt; offline = a scripted fake.
BlurFn = Callable[[BlurRequest], str]

# The entailment seam: does the registry entry ENTAIL the blur (no contradiction)?
# True = passes; False = the blur asserts something the registry contradicts.
# Live = run_judge over build_entailment_prompt; offline = a scripted fake.
EntailmentFn = Callable[[RegistryEntry, str], bool]


# Generic JTBD-level vocabulary a blur is allowed to share with a registry entry
# without it counting as a leak. Anything longer/more specific that the blur
# echoes from the entry's own description is registry-distinctive phrasing — the
# generalization lint's vocabulary discipline (KTD3: recovery must measure
# elicitation, not blur leakiness). v1 list; grow-by-exception.
_GENERIC_WORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "into", "your",
        "user", "users", "item", "items", "data", "page", "pages", "list",
        "lists", "view", "views", "when", "where", "have", "has", "are", "can",
        "create", "delete", "update", "edit", "manage", "settings", "app",
        "able", "some", "thing", "things", "show", "shown", "shows", "see",
        "want", "wants", "need", "needs", "work", "works", "working", "made",
        "make", "makes", "set", "sets", "get", "gets", "uses", "used", "use",
    }
)


def _tokens(text: str) -> list[str]:
    out: list[str] = []
    word = []
    for ch in text.lower():
        if ch.isalnum():
            word.append(ch)
        elif word:
            out.append("".join(word))
            word = []
    if word:
        out.append("".join(word))
    return out


def blur_vocab_leak(entry: RegistryEntry, blur: str) -> str | None:
    """The deterministic half of the blur lint: a blur must not echo a
    registry-distinctive token from the entry's own description (leak control,
    KTD3). Returns the first leaked token, or ``None`` when the blur stays at
    JTBD-level vocabulary. Generic words (:data:`_GENERIC_WORDS`) and short
    tokens are allowed; distinctive ones are not."""
    entry_tokens = {
        t for t in _tokens(entry.description) if len(t) >= 4 and t not in _GENERIC_WORDS
    }
    for token in _tokens(blur):
        if token in entry_tokens:
            return token
    return None


def lint_blur(
    entry: RegistryEntry, blur: str, entailment_fn: EntailmentFn
) -> tuple[bool, str]:
    """Both blur-lint arms (KTD3): the deterministic vocab/leak guard, then the
    judge entailment check (registry entry must entail the blur — a blur may
    *omit* detail, never *assert* a contradiction). Returns ``(passed, reason)``."""
    text = blur.strip()
    if not text:
        return False, "empty blur"
    leak = blur_vocab_leak(entry, text)
    if leak is not None:
        return False, f"vocab leak: registry-distinctive token {leak!r}"
    if not entailment_fn(entry, text):
        return False, "registry entry does not entail blur (contradiction)"
    return True, "pass"


# --- live judge bindings (offline suite injects fakes instead) --------------------

FOUNDER_BLUR_SCHEMA = {
    "type": "object",
    "properties": {"blur": {"type": "string"}},
    "required": ["blur"],
    "additionalProperties": False,
}

FOUNDER_ENTAILMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "entails": {"type": "boolean"},
    },
    "required": ["reasoning", "entails"],
    "additionalProperties": False,
}


def build_blur_prompt(request: BlurRequest) -> str:
    """The blur-generation brief: produce a vague, JTBD-level recollection of one
    registry entry that the founder *half-remembers* — omitting specifics, never
    inventing or contradicting them, never echoing the entry's own distinctive
    wording (leak control, KTD3)."""
    retry = (
        ""
        if request.attempt == 0
        else (
            f"\n\nYour previous {request.attempt} attempt(s) failed the lint"
            " (either a contradiction of the entry, or distinctive wording from"
            " it leaked through). Generalize harder this time."
        )
    )
    return (
        "You are degrading one registry entry into how a non-expert FOUNDER would"
        " vaguely recall it — they know roughly what they want but not the"
        " specifics. Produce a single short, vague, JTBD-level sentence.\n"
        "Rules: OMIT specifics (exact numbers, durations, mechanisms); NEVER"
        " assert anything the entry does not say; NEVER use the entry's own"
        " distinctive words — stay at plain everyday vocabulary.\n\n"
        f"Founder's overall goal: {request.goal_statement}\n"
        f"Registry entry ({request.entry.ref_kind} {request.entry.ref_id}):"
        f" {request.entry.description}"
        f"{retry}"
    )


def build_entailment_prompt(entry: RegistryEntry, blur: str) -> str:
    """The entailment-lint brief: does the registry entry ENTAIL the blur? A blur
    that merely omits detail is entailed (passes); a blur that asserts anything
    the entry contradicts is not (fails). Default to NOT entailed when unsure."""
    return (
        "Decide whether the REGISTRY ENTRY entails the BLUR. The blur is allowed"
        " to be vaguer and to omit detail — that still counts as entailed. The"
        " blur is NOT entailed if it asserts anything the entry contradicts or"
        " does not support (e.g. entry says 'purged after 30 days', blur says"
        " 'within a week' -> NOT entailed; blur says 'cleaned up eventually' ->"
        " entailed). When unsure, answer not entailed.\n"
        "Reason briefly, then answer.\n\n"
        f"REGISTRY ENTRY: {entry.description}\n"
        f"BLUR: {blur}"
    )


def blur_fn_via_judge(
    *,
    model: str,
    max_retries: int,
    mode: str | None = None,
    fixtures_dir: str | None = None,
    bare: bool = False,
) -> BlurFn:
    """The live blur seam: wrap :func:`run_judge` over :func:`build_blur_prompt`
    through the standard record/replay fixtures. The offline suite injects a
    scripted fake instead, so it never reaches this."""

    def _fn(request: BlurRequest) -> str:
        result = run_judge(
            build_blur_prompt(request),
            FOUNDER_BLUR_SCHEMA,
            model,
            max_retries=max_retries,
            bare=bare,
            mode=mode,
            fixtures_dir=fixtures_dir,
        )
        return str(result.output["blur"])

    return _fn


def entailment_fn_via_judge(
    *,
    model: str,
    max_retries: int,
    mode: str | None = None,
    fixtures_dir: str | None = None,
    bare: bool = False,
) -> EntailmentFn:
    """The live entailment seam: wrap :func:`run_judge` over
    :func:`build_entailment_prompt`. Offline injects a scripted fake."""

    def _fn(entry: RegistryEntry, blur: str) -> bool:
        result = run_judge(
            build_entailment_prompt(entry, blur),
            FOUNDER_ENTAILMENT_SCHEMA,
            model,
            max_retries=max_retries,
            bare=bare,
            mode=mode,
            fixtures_dir=fixtures_dir,
        )
        return bool(result.output["entails"])

    return _fn


# --- the founder model -----------------------------------------------------------


@dataclass(frozen=True)
class FounderKnowledgeRow:
    """One resolved knowledge row: the founder's state on one registry ref, plus
    the cached blur text when ``state == 'blurred'`` and whether the
    stratification guard protects this ref (metric-exclusion flag, KTD3)."""

    ref_kind: str
    ref_id: str
    state: str  # FOUNDER_KNOWLEDGE_STATES
    blur_text: str | None
    guard_protected: bool


@dataclass(frozen=True)
class FounderModel:
    """A generated founder model for one greenfield episode: the goal statement
    (always intact) plus the per-ref degradation log. ``guard_protected`` is the
    core-loop ref set excluded from recovery-rate denominators (KTD3)."""

    episode_id: int
    target: str
    seed: int
    params_hash: str
    goal_statement: str
    knowledge: tuple[FounderKnowledgeRow, ...]
    guard_protected: tuple[str, ...]


def goal_statement_for(target: str, core_entries: Sequence[RegistryEntry]) -> str:
    """The founder's always-intact JTBD-level goal, derived deterministically
    from the core-loop entries' behaviors (KTD3 — derived from the registry
    summary; always intact, never degraded)."""
    if not core_entries:
        return f"I want a working {target} that does its core job for me."
    phrases = [e.description.strip().rstrip(".") for e in core_entries]
    return f"I want a {target} that lets me: {'; '.join(phrases)}."


def _cache_lookup(
    store: Store,
    *,
    target: str,
    entry: RegistryEntry,
    seed: int,
    params_hash: str,
    prompt_set_version: str,
):
    return store.conn.execute(
        "SELECT id, blur_text, lint_verdict FROM founder_blur_cache"
        " WHERE target = ? AND ref_kind = ? AND ref_id = ? AND entry_digest = ?"
        " AND seed = ? AND params_hash = ? AND prompt_set_version = ?",
        (
            target,
            entry.ref_kind,
            entry.ref_id,
            entry.entry_digest,
            seed,
            params_hash,
            prompt_set_version,
        ),
    ).fetchone()


def _cache_insert(
    store: Store,
    *,
    target: str,
    entry: RegistryEntry,
    seed: int,
    params_hash: str,
    prompt_set_version: str,
    blur_text: str,
    lint_verdict: str,
) -> int:
    cur = store.conn.execute(
        "INSERT INTO founder_blur_cache"
        " (target, ref_kind, ref_id, entry_digest, seed, params_hash,"
        "  prompt_set_version, blur_text, lint_verdict)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            target,
            entry.ref_kind,
            entry.ref_id,
            entry.entry_digest,
            seed,
            params_hash,
            prompt_set_version,
            blur_text,
            lint_verdict,
        ),
    )
    return int(cur.lastrowid)


def _resolve_blur(
    store: Store,
    entry: RegistryEntry,
    *,
    target: str,
    seed: int,
    params_hash: str,
    prompt_set_version: str,
    goal_statement: str,
    blur_fn: BlurFn,
    entailment_fn: EntailmentFn,
    max_blur_attempts: int,
) -> tuple[str, str | None, int | None]:
    """Resolve one ``blurred`` entry into ``(state, blur_text, blur_id)``.

    Cache-first (target-side, KTD3): a cached ``pass`` reuses byte-identical
    prose forever; a cached fallback (``drop``) reproduces the drop decision.
    On a miss, generate + lint up to ``max_blur_attempts`` times; on persistent
    failure cache the drop decision and fall the entry back to ``dropped`` (a
    blur the registry refuses to entail is never spoken)."""
    cached = _cache_lookup(
        store,
        target=target,
        entry=entry,
        seed=seed,
        params_hash=params_hash,
        prompt_set_version=prompt_set_version,
    )
    if cached is not None:
        if cached["lint_verdict"] == "pass":
            return "blurred", cached["blur_text"], int(cached["id"])
        return "dropped", None, None

    last = ""
    for attempt in range(max_blur_attempts):
        draft = blur_fn(
            BlurRequest(entry=entry, attempt=attempt, goal_statement=goal_statement)
        )
        last = draft
        passed, reason = lint_blur(entry, draft, entailment_fn)
        if passed:
            blur_id = _cache_insert(
                store,
                target=target,
                entry=entry,
                seed=seed,
                params_hash=params_hash,
                prompt_set_version=prompt_set_version,
                blur_text=draft.strip(),
                lint_verdict="pass",
            )
            return "blurred", draft.strip(), blur_id

    # Persistent lint failure: cache the drop decision (so it is deterministic
    # and visible) and fall the entry back to dropped.
    _cache_insert(
        store,
        target=target,
        entry=entry,
        seed=seed,
        params_hash=params_hash,
        prompt_set_version=prompt_set_version,
        blur_text=last.strip(),
        lint_verdict="drop",
    )
    return "dropped", None, None


def generate_founder_model(
    store: Store,
    *,
    episode_id: int,
    target: str,
    seed: int,
    config: GreenfieldConfig,
    blur_fn: BlurFn,
    entailment_fn: EntailmentFn,
    prompt_set_version: str = PROMPT_SET_VERSION,
    max_blur_attempts: int = 2,
) -> FounderModel:
    """Generate and persist one greenfield episode's founder model (KTD3).

    Deterministic given ``(target registry snapshot, seed, params)``: the state
    partition is a seeded draw, the stratification guard's rescue is seed-sampled,
    and blur prose is target-side cached (so a second episode on the same inputs
    reproduces it byte-for-byte without re-calling the blur seam). Writes
    ``founder_models`` + ``founder_knowledge``; returns the in-memory model with
    the guard-protected ref set (excluded from recovery denominators).
    """
    if max_blur_attempts < 1:
        raise FounderError("max_blur_attempts must be >= 1")
    existing = store.conn.execute(
        "SELECT 1 FROM founder_models WHERE episode_id = ?", (episode_id,)
    ).fetchone()
    if existing is not None:
        raise FounderError(
            f"a founder model already exists for episode {episode_id};"
            " generation is once-per-episode (regenerate into a fresh episode)"
        )

    entries = read_registry_entries(store, target, core_loop_n=config.core_loop_n)
    params_hash = degradation_params_hash(config)
    core_entries = tuple(e for e in entries if e.is_core)
    core_ids = tuple(e.ref_id for e in core_entries)
    goal_statement = goal_statement_for(target, core_entries)

    # 1. Seeded state draw (stable entry order).
    draw = _rng(target, seed, params_hash, "states")
    states: dict[str, str] = {}
    for entry in entries:
        r = draw.random()
        if r < config.drop_rate:
            states[entry.ref_id] = "dropped"
        elif r < config.drop_rate + config.blur_rate:
            states[entry.ref_id] = "blurred"
        else:
            states[entry.ref_id] = "intact"

    # 2. Resolve blurs (cache-first; persistent lint failure -> dropped).
    blur_ids: dict[str, int | None] = {}
    blur_texts: dict[str, str | None] = {}
    for entry in entries:
        if states[entry.ref_id] != "blurred":
            blur_ids[entry.ref_id] = None
            blur_texts[entry.ref_id] = None
            continue
        state, blur_text, blur_id = _resolve_blur(
            store,
            entry,
            target=target,
            seed=seed,
            params_hash=params_hash,
            prompt_set_version=prompt_set_version,
            goal_statement=goal_statement,
            blur_fn=blur_fn,
            entailment_fn=entailment_fn,
            max_blur_attempts=max_blur_attempts,
        )
        states[entry.ref_id] = state
        blur_ids[entry.ref_id] = blur_id
        blur_texts[entry.ref_id] = blur_text

    # 3. Stratification guard (KTD3): >=1 core item must stay non-dropped. Checked
    # AFTER blur resolution so a blur-fallback-to-dropped can't sneak past it. The
    # rescued item is forced INTACT (so it is never at risk of fallback) and which
    # one is seed-sampled (deterministic).
    if core_ids and all(states[cid] == "dropped" for cid in core_ids):
        rescue = _rng(target, seed, params_hash, "guard").choice(list(core_ids))
        states[rescue] = "intact"
        blur_ids[rescue] = None
        blur_texts[rescue] = None

    # 4. Persist the model + knowledge log atomically.
    knowledge: list[FounderKnowledgeRow] = []
    with store.transaction():
        store.conn.execute(
            "INSERT INTO founder_models"
            " (episode_id, target, seed, params_hash, goal_statement)"
            " VALUES (?, ?, ?, ?, ?)",
            (episode_id, target, seed, params_hash, goal_statement),
        )
        for entry in entries:
            store.conn.execute(
                "INSERT INTO founder_knowledge"
                " (episode_id, ref_kind, ref_id, state, blur_id)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    episode_id,
                    entry.ref_kind,
                    entry.ref_id,
                    states[entry.ref_id],
                    blur_ids[entry.ref_id],
                ),
            )
            knowledge.append(
                FounderKnowledgeRow(
                    ref_kind=entry.ref_kind,
                    ref_id=entry.ref_id,
                    state=states[entry.ref_id],
                    blur_text=blur_texts[entry.ref_id],
                    guard_protected=entry.is_core,
                )
            )

    return FounderModel(
        episode_id=episode_id,
        target=target,
        seed=seed,
        params_hash=params_hash,
        goal_statement=goal_statement,
        knowledge=tuple(knowledge),
        guard_protected=core_ids,
    )
