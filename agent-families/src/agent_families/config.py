"""Thresholds config: TOML -> typed dataclass, fail-fast.

One file (``thresholds.toml``) carries every tunable. The loader rejects unknown
sections/keys (naming the offender) and out-of-range values; a missing file points
the user at ``af init``. Source has no hardcoded tunables — everything flows from here
(DESIGN §17).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_FILENAME = "thresholds.toml"

_KNOWN_SECTIONS = ("embedding", "merge", "retrieval", "judge", "lifecycle", "store")


class ConfigError(Exception):
    """Raised when thresholds.toml is missing, malformed, or out of range."""


@dataclass(frozen=True)
class EmbeddingConfig:
    model: str
    dim: int
    device: str


@dataclass(frozen=True)
class MergeConfig:
    cosine_threshold: float


@dataclass(frozen=True)
class RetrievalConfig:
    ann_top_k: int
    relevance_floor: float


@dataclass(frozen=True)
class JudgeConfig:
    model: str
    max_retries: int
    bare: bool


@dataclass(frozen=True)
class LifecycleConfig:
    active_cap: int


@dataclass(frozen=True)
class StoreConfig:
    busy_timeout_ms: int


@dataclass(frozen=True)
class Config:
    embedding: EmbeddingConfig
    merge: MergeConfig
    retrieval: RetrievalConfig
    judge: JudgeConfig
    lifecycle: LifecycleConfig
    store: StoreConfig


# --- parsing helpers -------------------------------------------------------


def _require_table(data: dict, name: str) -> dict:
    if name not in data:
        raise ConfigError(f"missing required [{name}] section in thresholds.toml")
    section = data[name]
    if not isinstance(section, dict):
        raise ConfigError(f"[{name}] must be a table in thresholds.toml")
    return dict(section)


def _take(section: dict, sect: str, key: str, typ: type):
    if key not in section:
        raise ConfigError(f"[{sect}] missing required key '{key}' in thresholds.toml")
    val = section.pop(key)
    # bool is an int subclass — reject it for numeric fields, and reject ints for bool.
    if isinstance(val, bool) and typ is not bool:
        raise ConfigError(f"[{sect}] '{key}' must be {typ.__name__}, got bool")
    if typ is float and isinstance(val, int) and not isinstance(val, bool):
        val = float(val)
    if not isinstance(val, typ):
        got = type(val).__name__
        raise ConfigError(f"[{sect}] '{key}' must be {typ.__name__}, got {got}")
    return val


def _reject_extra(section: dict, sect: str) -> None:
    if section:
        key = sorted(section)[0]
        raise ConfigError(
            f"unknown key '{key}' in [{sect}] section of thresholds.toml"
        )


def _unit_interval(sect: str, key: str, val: float) -> None:
    if not (0.0 <= val <= 1.0):
        raise ConfigError(f"[{sect}] '{key}' must be in [0.0, 1.0], got {val}")


def _positive_int(sect: str, key: str, val: int) -> None:
    if val <= 0:
        raise ConfigError(f"[{sect}] '{key}' must be a positive integer, got {val}")


def _non_negative_int(sect: str, key: str, val: int) -> None:
    if val < 0:
        raise ConfigError(f"[{sect}] '{key}' must be >= 0, got {val}")


def _non_empty_str(sect: str, key: str, val: str) -> None:
    if not val.strip():
        raise ConfigError(f"[{sect}] '{key}' must be a non-empty string")


# --- loader ----------------------------------------------------------------


def load_config(path: str | Path) -> Config:
    """Load and validate thresholds.toml at ``path``.

    Raises :class:`ConfigError` (never a partial Config) on any problem.
    """
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"config file not found: {path}\n"
            "Run `af init` to create thresholds.toml in this directory."
        )

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from exc

    extra_sections = set(data) - set(_KNOWN_SECTIONS)
    if extra_sections:
        raise ConfigError(
            f"unknown section '[{sorted(extra_sections)[0]}]' in thresholds.toml"
        )

    emb = _require_table(data, "embedding")
    model = _take(emb, "embedding", "model", str)
    _non_empty_str("embedding", "model", model)
    dim = _take(emb, "embedding", "dim", int)
    _positive_int("embedding", "dim", dim)
    device = _take(emb, "embedding", "device", str)
    _non_empty_str("embedding", "device", device)
    _reject_extra(emb, "embedding")

    mrg = _require_table(data, "merge")
    cosine_threshold = _take(mrg, "merge", "cosine_threshold", float)
    _unit_interval("merge", "cosine_threshold", cosine_threshold)
    _reject_extra(mrg, "merge")

    ret = _require_table(data, "retrieval")
    ann_top_k = _take(ret, "retrieval", "ann_top_k", int)
    _positive_int("retrieval", "ann_top_k", ann_top_k)
    relevance_floor = _take(ret, "retrieval", "relevance_floor", float)
    _unit_interval("retrieval", "relevance_floor", relevance_floor)
    _reject_extra(ret, "retrieval")

    jdg = _require_table(data, "judge")
    judge_model = _take(jdg, "judge", "model", str)
    _non_empty_str("judge", "model", judge_model)
    max_retries = _take(jdg, "judge", "max_retries", int)
    _non_negative_int("judge", "max_retries", max_retries)
    bare = _take(jdg, "judge", "bare", bool)
    _reject_extra(jdg, "judge")

    lif = _require_table(data, "lifecycle")
    active_cap = _take(lif, "lifecycle", "active_cap", int)
    _positive_int("lifecycle", "active_cap", active_cap)
    _reject_extra(lif, "lifecycle")

    sto = _require_table(data, "store")
    busy_timeout_ms = _take(sto, "store", "busy_timeout_ms", int)
    _non_negative_int("store", "busy_timeout_ms", busy_timeout_ms)
    _reject_extra(sto, "store")

    return Config(
        embedding=EmbeddingConfig(model=model, dim=dim, device=device),
        merge=MergeConfig(cosine_threshold=cosine_threshold),
        retrieval=RetrievalConfig(
            ann_top_k=ann_top_k, relevance_floor=relevance_floor
        ),
        judge=JudgeConfig(model=judge_model, max_retries=max_retries, bare=bare),
        lifecycle=LifecycleConfig(active_cap=active_cap),
        store=StoreConfig(busy_timeout_ms=busy_timeout_ms),
    )
