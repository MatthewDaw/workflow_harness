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
    # R11 cut-over: cosine_threshold removed from toml; the live path uses candidate_floor
    assert cfg.merge.cosine_threshold is None
    assert cfg.merge.candidate_floor == pytest.approx(0.80)
    assert cfg.retrieval.ann_top_k == 10
    assert 0.0 <= cfg.retrieval.relevance_floor <= 1.0
    assert cfg.judge.model == "sonnet"
    assert cfg.judge.max_retries == 3
    assert cfg.judge.bare is False
    assert cfg.lifecycle.active_cap == 50
    assert cfg.store.busy_timeout_ms >= 0
    # Plan 007: the shipped template carries the [greenfield] section
    assert 0.0 <= cfg.greenfield.drop_rate <= 1.0
    assert 0.0 <= cfg.greenfield.blur_rate <= 1.0
    assert cfg.greenfield.core_loop_n > 0
    assert cfg.greenfield.rotation_fraction == pytest.approx(0.0)
    assert cfg.greenfield.gate_k >= 0
    assert cfg.greenfield.grace_window >= 0
    assert cfg.greenfield.seed_occupancy_cap > 0
    assert cfg.greenfield.benchmark_seed >= 0


def test_minimal_valid_config_round_trips(tmp_path):
    path = _write(
        tmp_path,
        """
        [embedding]
        model = "nomic-ai/nomic-embed-text-v1.5"
        dim = 768
        device = "cpu"
        [merge]
        candidate_floor = 0.80
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
    # R11 cut-over: cosine_threshold removed; only candidate_floor remains
    assert cfg.merge.cosine_threshold is None
    assert cfg.merge.candidate_floor == pytest.approx(0.80)
    # [greenfield] is optional — its absence falls back to documented defaults
    assert cfg.greenfield.core_loop_n == 5
    assert cfg.greenfield.rotation_fraction == pytest.approx(0.0)
    assert cfg.greenfield.benchmark_seed == 1234


# --- greenfield section: optional, validated when present (Plan 007 U1) ------


_BASE_SECTIONS = """
[embedding]
model = "nomic-ai/nomic-embed-text-v1.5"
dim = 768
device = "cpu"
[merge]
candidate_floor = 0.80
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
"""

_GREENFIELD_OK = """
[greenfield]
drop_rate = 0.25
blur_rate = 0.4
core_loop_n = 6
rotation_fraction = 0.25
gate_k = 3
grace_window = 4
seed_occupancy_cap = 12
benchmark_seed = 99
"""


def test_greenfield_section_loads_when_present(tmp_path):
    path = _write(tmp_path, _BASE_SECTIONS + _GREENFIELD_OK)
    cfg = load_config(path)
    assert cfg.greenfield.drop_rate == pytest.approx(0.25)
    assert cfg.greenfield.core_loop_n == 6
    assert cfg.greenfield.rotation_fraction == pytest.approx(0.25)
    assert cfg.greenfield.gate_k == 3
    assert cfg.greenfield.seed_occupancy_cap == 12
    assert cfg.greenfield.benchmark_seed == 99


def test_greenfield_unknown_key_names_the_key(tmp_path):
    path = _write(
        tmp_path,
        _BASE_SECTIONS + _GREENFIELD_OK + "bogus_greenfield_key = 1\n",
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "bogus_greenfield_key" in str(exc.value)


def test_greenfield_out_of_range_drop_rate_errors(tmp_path):
    path = _write(
        tmp_path,
        _BASE_SECTIONS + _GREENFIELD_OK.replace("drop_rate = 0.25", "drop_rate = 1.5"),
    )
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "drop_rate" in str(exc.value)


def test_greenfield_partial_section_missing_key_errors(tmp_path):
    """When present, the section is fully validated — a missing key is an error."""
    path = _write(tmp_path, _BASE_SECTIONS + "[greenfield]\ndrop_rate = 0.2\n")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "greenfield" in str(exc.value)


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
        candidate_floor = 0.80
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
        candidate_floor = 0.80
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
        ("merge", "candidate_floor"): "0.80",
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
        candidate_floor = {overrides[("merge", "candidate_floor")]}
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


def test_candidate_floor_above_one_errors(tmp_path):
    # R11 cut-over: cosine_threshold removed; candidate_floor is now the validated key.
    path = _base_with(tmp_path, "merge", "candidate_floor", "1.5")
    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "candidate_floor" in str(exc.value)


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
    # Phase 0 shipped nine commands; 003 U9 adds the `trace` and `episode`
    # command groups (the human reflector's tools) — 11 in total.
    assert len(SUBCOMMANDS) == 11
    phase0 = {
        "init", "add-idea", "promote", "revert", "retire",
        "revive", "render", "export", "status",
    }
    assert phase0 <= set(SUBCOMMANDS)
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
