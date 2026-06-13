"""NLI runner: the local cross-encoder seam owning every contradiction/entailment
classify call (R9), mirroring :mod:`agent_families.judge`'s record/replay discipline.

All NLI traffic flows through :func:`classify`, which is fully testable offline via
a record/replay fixture cache:

- ``replay`` (default): the ``{label, confidence}`` verdict comes from JSON fixtures
  keyed by request hash; a missing fixture is a hard failure naming the hash. Zero
  model load, zero download, zero quota — this is what the offline suite runs on.
- ``record``: the real local model is loaded and run and the verdict (plus the raw
  logits it derived from) is written to a fixture for deliberate refresh.
- ``passthrough``: the real model is loaded and run with no fixture interaction.

The mode comes from the ``AF_NLI_MODE`` env var (or an explicit argument); the
fixture directory from ``AF_NLI_FIXTURES``. Fixture keys are
``sha256(sorted-json(model, premise, hypothesis))`` — so a model swap (or any
premise/hypothesis change) misses the existing fixture rather than silently
reusing a verdict computed under a different head.

Determinism (R9): ``cross-encoder/nli-deberta-v3-base`` is loaded ``device="cpu"``
with a fixed model id, so its logits are reproducible enough for record/replay
fixture stability across machines. The hard-coded ``{contradiction:0, entailment:1,
neutral:2}`` label map is asserted at model LOAD against the model's own
``id2label`` — a model whose head emits a different class order (or not exactly 3
logits) fails preflight before any ``classify`` verdict, so a silent model swap can
never reorder labels and flip every verdict.

Replay returns the verdict stored in the fixture verbatim, so it is byte-identical
across runs; the softmax/label-map derivation runs only at record/passthrough time,
where the real logits are available.

Manual record-mode smoke (documented, not in CI): with the model cached locally
(``af init`` pre-fetches it; ~400 MB, honors ``HF_HOME``), from agent-families/ set
``AF_NLI_MODE=record`` and ``AF_NLI_FIXTURES=tests/fixtures/nli``, call
:func:`classify` once per pair, inspect the new fixtures, and commit them
deliberately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

MODE_ENV = "AF_NLI_MODE"
FIXTURES_ENV = "AF_NLI_FIXTURES"
MODES = ("replay", "record", "passthrough")
DEFAULT_FIXTURES_DIR = Path("tests") / "fixtures" / "nli"

# The recommended local cross-encoder (R9): 3-class NLI, CPU, ~400 MB, no quota.
DEFAULT_MODEL = "cross-encoder/nli-deberta-v3-base"

# The three NLI labels in head/logit order. The index of each label IS the logit
# column the model emits it on; this ordering is asserted against the loaded
# model's own id2label at preflight so it can never silently drift.
LABEL_ORDER = ("contradiction", "entailment", "neutral")
EXPECTED_ID2LABEL = {0: "contradiction", 1: "entailment", 2: "neutral"}

_OFFLINE_HINT = (
    "If this is a cache miss while offline (HF_HUB_OFFLINE=1), the model must be"
    " pre-fetched first: unset HF_HUB_OFFLINE and run `af init` once with network"
    " access (~400 MB download, honors HF_HOME)."
)


class NliError(Exception):
    """Base for every NLI-runner failure."""


class NliUnavailable(NliError):
    """The local NLI model is missing, broken, or has an unexpected label head."""


class NliFixtureMissing(NliError):
    """Replay mode found no recorded fixture for the request hash."""


@dataclass(frozen=True)
class NliResult:
    """A 3-class NLI verdict plus the fixture key it was addressed by."""

    label: str
    confidence: float
    request_hash: str


# --- request identity and fixtures ------------------------------------------


def request_hash(model: str, premise: str, hypothesis: str) -> str:
    """Stable fixture key: sha256 over canonical JSON of (model, premise, hypothesis).

    The model id is part of the key so fixtures are not reusable across models —
    a different head produces different logits, hence a different verdict.
    """
    canonical = json.dumps(
        {"model": model, "premise": premise, "hypothesis": hypothesis},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_fixture(
    fixtures_dir: Path,
    model: str,
    premise: str,
    hypothesis: str,
    envelope: dict,
) -> Path:
    """Persist a verdict envelope under its request hash; returns the path.

    Written with sorted keys and ``\\n`` newlines so committed fixtures are
    byte-stable across platforms.
    """
    fixtures_dir = Path(fixtures_dir)
    fixtures_dir.mkdir(parents=True, exist_ok=True)
    h = request_hash(model, premise, hypothesis)
    path = fixtures_dir / f"{h}.json"
    body = json.dumps(
        {
            "request_hash": h,
            "request": {
                "model": model,
                "premise": premise,
                "hypothesis": hypothesis,
            },
            "envelope": envelope,
        },
        sort_keys=True,
        indent=2,
        ensure_ascii=False,
    )
    path.write_text(body + "\n", encoding="utf-8", newline="\n")
    return path


def _load_fixture(fixtures_dir: Path, h: str) -> dict:
    path = Path(fixtures_dir) / f"{h}.json"
    if not path.exists():
        raise NliFixtureMissing(
            f"no recorded NLI fixture for request hash {h} in {fixtures_dir}"
            f" (mode=replay). Refresh fixtures deliberately with {MODE_ENV}=record."
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["envelope"]


# --- model load + label-map assertion ----------------------------------------

# Encoders that loaded and passed the label-map assertion, keyed by model id.
_encoder_cache: dict[str, object] = {}


def _id2label(encoder) -> dict[int, str]:
    """Read the loaded model's class-index → label-name mapping.

    CrossEncoder exposes the HF config at ``encoder.config``; fall back to the
    wrapped ``encoder.model.config`` for older layouts.
    """
    config = getattr(encoder, "config", None)
    if config is None:
        config = getattr(getattr(encoder, "model", None), "config", None)
    id2label = getattr(config, "id2label", None) if config is not None else None
    if id2label is None:
        raise NliUnavailable(
            "NLI encoder exposes no id2label mapping; cannot verify the label order"
            " — refusing to classify with an unverifiable head."
        )
    return dict(id2label)


def _assert_label_map(encoder) -> None:
    """Assert the model head is exactly the 3-class map this seam hard-codes (R9).

    Raised at LOAD, before any verdict: a model whose head emits a different class
    order (or not exactly 3 logits) cannot silently reorder labels and flip every
    contradiction/entailment verdict.
    """
    id2label = _id2label(encoder)
    try:
        normalized = {int(k): str(v).strip().lower() for k, v in id2label.items()}
    except (TypeError, ValueError) as exc:
        raise NliUnavailable(
            f"NLI model has a non-integer-keyed label map {id2label!r};"
            f" expected {EXPECTED_ID2LABEL!r}."
        ) from exc
    if normalized != EXPECTED_ID2LABEL:
        raise NliUnavailable(
            "NLI model label head does not match the hard-coded 3-class map.\n"
            f"  expected: {EXPECTED_ID2LABEL}\n"
            f"  model has: {normalized}\n"
            "A different class order would silently flip every contradiction/"
            "entailment verdict, so this is fatal at load (likely a model swap)."
        )


def _load_encoder(model: str, *, _encoder=None):
    """Load (or reuse) the CPU cross-encoder, asserting its label head at load.

    An injected ``_encoder`` (tests) is asserted but never cached, so distinct
    fakes do not bleed across tests.
    """
    if _encoder is not None:
        _assert_label_map(_encoder)
        return _encoder
    if model in _encoder_cache:
        return _encoder_cache[model]
    try:
        from sentence_transformers import CrossEncoder
    except ImportError as exc:
        raise NliUnavailable(
            "sentence-transformers is not installed; run `uv sync` in"
            " agent-families/."
        ) from exc
    try:
        encoder = CrossEncoder(model, device="cpu")
    except Exception as exc:
        raise NliUnavailable(
            f"could not load NLI model '{model}': {exc}\n" + _OFFLINE_HINT
        ) from exc
    _assert_label_map(encoder)
    _encoder_cache[model] = encoder
    return encoder


def preflight(model: str, *, _encoder=None) -> None:
    """Force the model load + label-map assertion (cached per process per model).

    Quota-free and offline-friendly by design — the only cost is the one-time CPU
    model load, and the only failure mode is a head that is not the expected
    3-class map.
    """
    _load_encoder(model, _encoder=_encoder)


def reset_encoder_cache() -> None:
    """Forget cached encoders (test hook)."""
    _encoder_cache.clear()


# --- logit → verdict ---------------------------------------------------------


def _softmax(logits: list[float]) -> list[float]:
    m = max(logits)
    exps = [math.exp(x - m) for x in logits]
    total = sum(exps)
    return [e / total for e in exps]


def _predict_logits(encoder, premise: str, hypothesis: str) -> list[float]:
    raw = encoder.predict([(premise, hypothesis)])
    row = raw[0]
    logits = [float(x) for x in row]
    if len(logits) != 3:
        raise NliUnavailable(
            f"NLI model returned {len(logits)} logits, expected exactly 3"
            " (likely a model swap or a non-NLI head)."
        )
    return logits


def _verdict_from_logits(logits: list[float], h: str) -> NliResult:
    probs = _softmax(logits)
    idx = max(range(len(probs)), key=lambda i: probs[i])
    return NliResult(label=LABEL_ORDER[idx], confidence=probs[idx], request_hash=h)


# --- mode/fixture resolution -------------------------------------------------


def _resolve_mode(mode: str | None) -> str:
    resolved = mode or os.environ.get(MODE_ENV) or "replay"
    if resolved not in MODES:
        raise NliError(
            f"invalid NLI mode '{resolved}' (from {MODE_ENV} or argument);"
            f" valid modes: {', '.join(MODES)}"
        )
    return resolved


def _resolve_fixtures_dir(fixtures_dir: str | Path | None) -> Path:
    if fixtures_dir is not None:
        return Path(fixtures_dir)
    env = os.environ.get(FIXTURES_ENV)
    if env:
        return Path(env)
    return DEFAULT_FIXTURES_DIR


# --- the seam -----------------------------------------------------------------


def classify(
    premise: str,
    hypothesis: str,
    *,
    model: str = DEFAULT_MODEL,
    mode: str | None = None,
    fixtures_dir: str | Path | None = None,
    _encoder=None,
) -> NliResult:
    """Classify ``(premise, hypothesis)`` into contradiction/entailment/neutral.

    In ``replay`` (default) the verdict is read verbatim from the fixture keyed by
    :func:`request_hash` — no model load, byte-identical across runs. In
    ``record``/``passthrough`` the local CPU cross-encoder is loaded (its label
    head asserted at load), run, and the verdict derived from the raw logits via a
    fixed softmax + the hard-coded label order.
    """
    resolved_mode = _resolve_mode(mode)
    h = request_hash(model, premise, hypothesis)
    if resolved_mode == "replay":
        fixtures = _resolve_fixtures_dir(fixtures_dir)
        envelope = _load_fixture(fixtures, h)
        result = NliResult(
            label=envelope["label"],
            confidence=envelope["confidence"],
            request_hash=h,
        )
        logger.info(
            "nli ok (replay): hash=%s label=%s confidence=%s",
            h,
            result.label,
            result.confidence,
        )
        return result

    encoder = _load_encoder(model, _encoder=_encoder)
    logits = _predict_logits(encoder, premise, hypothesis)
    result = _verdict_from_logits(logits, h)
    if resolved_mode == "record":
        fixtures = _resolve_fixtures_dir(fixtures_dir)
        write_fixture(
            fixtures,
            model,
            premise,
            hypothesis,
            {
                "label": result.label,
                "confidence": result.confidence,
                "logits": logits,
            },
        )
    logger.info(
        "nli ok (%s): hash=%s label=%s confidence=%s",
        resolved_mode,
        h,
        result.label,
        result.confidence,
    )
    return result
