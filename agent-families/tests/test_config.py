"""U1 tests: thresholds config loader + CLI subcommand surface."""

from __future__ import annotations

import io
import textwrap
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from agent_families.cli import SUBCOMMANDS, build_parser
from agent_families.config import ConfigError, load_config

REPO_THRESHOLDS = Path(__file__).resolve().parent.parent / "thresholds.toml"


def _write(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "thresholds.toml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


# --- valid config loads with all defaults ----------------------------------


def test_shipped_template_loads_with_defaults():
    cfg = load_config(REPO_THRESHOLDS)
    assert cfg.embedding.model == "nomic-ai/nomic-embed-text-v1.5"
    assert cfg.embedding.dim == 768
    assert cfg.embedding.device == "cpu"
    assert cfg.merge.cosine_threshold == pytest.approx(0.92)
    assert cfg.retrieval.ann_top_k == 10
    assert 0.0 <= cfg.retrieval.relevance_floor <= 1.0
    assert cfg.judge.model == "sonnet"
    assert cfg.judge.max_retries == 3
    assert cfg.judge.bare is False
    assert cfg.lifecycle.active_cap == 50
    assert cfg.store.busy_timeout_ms >= 0


def test_minimal_valid_config_round_trips(tmp_path):
    path = _write(
        tmp_path,
        """
        [embedding]
        model = "nomic-ai/nomic-embed-text-v1.5"
        dim = 768
        device = "cpu"
        [merge]
        cosine_threshold = 0.9
        [retrieval]
        ann_top_k = 10
        relevance_floor = 0.5
        [judge]
        model = "sonnet"
        max_retries = 3
        bare = false
        [lifecycle]
        active_cap = 50
        [store]
        busy_timeout_ms = 5000
        """,
    )
    cfg = load_config(path)
    assert cfg.merge.cosine_threshold == pytest.approx(0.9)


# --- unknown key errors with the key named ---------------------------------


def test_unknown_key_in_section_names_the_key(tmp_path):
    path = _write(
        tmp_path,
        """
        [embedding]
        model = "m"
        dim = 768
        device = "cpu"
        bogus_key = 1
        [merge]
        cosine_threshold = 0.92
        [retrieval]
        ann_top_k = 10
        relevance_floor = 0.5
        [judge]
        model = "sonnet"
        max_retries = 3
        bare = false
        [lifecycle]
        active_cap = 50
        [store]
        busy_timeout_ms = 5000
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "bogus_key" in str(exc.value)


def test_unknown_section_names_the_section(tmp_path):
    path = _write(
        tmp_path,
        """
        [embedding]
        model = "m"
        dim = 768
        device = "cpu"
        [merge]
        cosine_threshold = 0.92
        [retrieval]
        ann_top_k = 10
        relevance_floor = 0.5
        [judge]
        model = "sonnet"
        max_retries = 3
        bare = false
        [lifecycle]
        active_cap = 50
        [store]
        busy_timeout_ms = 5000
        [mystery]
        x = 1
        """,
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "mystery" in str(exc.value)


# --- out-of-range value errors ----------------------------------------------


def _base_with(tmp_path: Path, section: str, key: str, value: str) -> Path:
    defaults = {
        ("merge", "cosine_threshold"): "0.92",
        ("lifecycle", "active_cap"): "50",
    }
    overrides = dict(defaults)
    overrides[(section, key)] = value
    return _write(
        tmp_path,
        f"""
        [embedding]
        model = "m"
        dim = 768
        device = "cpu"
        [merge]
        cosine_threshold = {overrides[("merge", "cosine_threshold")]}
        [retrieval]
        ann_top_k = 10
        relevance_floor = 0.5
        [judge]
        model = "sonnet"
        max_retries = 3
        bare = false
        [lifecycle]
        active_cap = {overrides[("lifecycle", "active_cap")]}
        [store]
        busy_timeout_ms = 5000
        """,
    )


def test_cosine_threshold_above_one_errors(tmp_path):
    path = _base_with(tmp_path, "merge", "cosine_threshold", "1.5")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "cosine_threshold" in str(exc.value)


def test_negative_active_cap_errors(tmp_path):
    path = _base_with(tmp_path, "lifecycle", "active_cap", "-1")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "active_cap" in str(exc.value)


def test_bool_for_numeric_field_errors(tmp_path):
    path = _base_with(tmp_path, "lifecycle", "active_cap", "true")
    with pytest.raises(ConfigError):
        load_config(path)


# --- missing file directs to af init ----------------------------------------


def test_missing_file_directs_to_af_init(tmp_path):
    missing = tmp_path / "nope" / "thresholds.toml"
    with pytest.raises(ConfigError) as exc:
        load_config(missing)
    assert "af init" in str(exc.value)


# --- af --help lists all nine subcommands -----------------------------------


def test_help_lists_all_nine_subcommands():
    parser = build_parser()
    assert len(SUBCOMMANDS) == 9
    buf = io.StringIO()
    with redirect_stdout(buf):
        parser.print_help()
    help_text = buf.getvalue()
    for name in SUBCOMMANDS:
        assert name in help_text


def test_parser_requires_a_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
