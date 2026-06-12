"""Admission gate (Operation 1) tests — fixture-driven, fully offline (008/U5).

The gate is the standalone FRONT stage of the R3 ingest path: messy raw input ->
one transferable, generalized schema'd atom + ``negative_scope``, BEFORE any
embedding. The judge runs in replay mode against fixtures written into a tmp dir
for the exact prompts the gate builds (``build_admission_gate_prompt`` is pure,
so tests compute the same prompt); embeddings come from a recording fake encoder.
Zero quota, zero subprocess calls.

## Conformance

Each R10/R14-gate invariant maps to the behavioral test that enforces it:

- gate runs before embed; key vector on GENERALIZED text, not raw
    -> test_gate_runs_before_embed
- typed-substitution strips instance trivia (file/repo/value -> placeholders)
    -> test_typed_substitution_strips_instance_trivia
- a non-empty negative_scope is emitted; altitude audit catches over-general
    -> test_negative_scope_emitted
- target-trivia -> lint_reject with NO downstream embed/insert (early exit)
    -> test_target_trivia_lint_rejected
- borderline -> rewrite_proposed exits non-zero; rewrite adopted only on
  --accept-rewrite re-entry (never silently)
    -> test_rewrite_proposed_requires_accept_flag
- scope-tag override is logged
    -> test_scope_tag_override_logged
"""

from __future__ import annotations

import logging

import pytest

from agent_families.config import (
    Config,
    EmbeddingConfig,
    JudgeConfig,
    LifecycleConfig,
    MergeConfig,
    RetrievalConfig,
    StoreConfig,
)
from agent_families.embedding import EmbeddingService
from agent_families.judge import ADMISSION_GATE_SCHEMA, write_fixture
from agent_families import pipeline
from agent_families.pipeline import (
    AdmittedAtom,
    LintRejected,
    RewriteProposed,
    StructuralValidationError,
    build_admission_gate_prompt,
    run_admission_gate,
)

DIM = 8
CFG = Config(
    embedding=EmbeddingConfig(model="fake-embedder", dim=DIM, device="cpu"),
    merge=MergeConfig(cosine_threshold=0.92),
    retrieval=RetrievalConfig(ann_top_k=10, relevance_floor=0.5),
    judge=JudgeConfig(model="sonnet", max_retries=1, bare=False),
    lifecycle=LifecycleConfig(active_cap=50),
    store=StoreConfig(busy_timeout_ms=5000),
)

# A hyper-specific debugging transcript naming concrete instance literals.
RAW = dict(
    precondition=(
        "While debugging the openemr repo, the file"
        " /var/www/openemr/sites/default/sqlconf.php throws on startup"
    ),
    action="Set $host to 127.0.0.1 and the port to 8300 in sqlconf.php",
    expected_outcome="The openemr container boots on port 8300",
)
RAW_LITERALS = (
    "/var/www/openemr/sites/default/sqlconf.php",
    "openemr",
    "8300",
    "127.0.0.1",
    "sqlconf.php",
)

# The generalized atom the gate emits: every instance literal replaced by a typed
# placeholder; carries a non-empty negative_scope.
GENERALIZED_ATOM = dict(
    precondition="An application's <CONFIG_FILE> throws on startup",
    action="Set the DB host to <HOST> and the port to <PORT> in <CONFIG_FILE>",
    expected_outcome="The service boots on the configured <PORT>",
    rationale="A misconfigured DB endpoint is the common startup-failure cause",
    negative_scope=(
        "Do not apply when the failure is a code-level exception unrelated to"
        " DB connectivity"
    ),
)


def make_config(**overrides) -> Config:
    return CFG


def envelope_for(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 950,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": output,
    }


def record_gate(fixtures_dir, prompt: str, output: dict) -> None:
    write_fixture(
        fixtures_dir, prompt, ADMISSION_GATE_SCHEMA, "sonnet", envelope_for(output)
    )


def admit_output(atom: dict, scope_value: str = "universal") -> dict:
    return {
        "outcome": "admit",
        "atom": dict(atom),
        "scope_tag": {
            "value": scope_value,
            "justification": "the generalized rule applies to any target",
        },
    }


def lint_reject_output(reason: str) -> dict:
    return {
        "outcome": "lint_reject",
        "reason": reason,
        "scope_tag": {"value": "", "justification": "rejected"},
    }


def rewrite_output(atom: dict) -> dict:
    return {
        "outcome": "rewrite_proposed",
        "atom": dict(atom),
        "scope_tag": {
            "value": "universal",
            "justification": "salvageable after generalization",
        },
    }


class RecordingEncoder:
    """Fake encoder: fixed vector, records every encoded text into a shared log."""

    def __init__(self, vector, log: list):
        self.vector = list(vector)
        self.log = log
        self.calls: list[str] = []

    def encode(self, text):
        self.calls.append(text)
        self.log.append(("embed", text))
        return list(self.vector)


def fixed_vector() -> list[float]:
    return [1.0] + [0.0] * (DIM - 1)


# --- tests --------------------------------------------------------------------


def test_gate_runs_before_embed(tmp_path, monkeypatch):
    """The gate's run_judge fires BEFORE embed, on the GENERALIZED text (R10)."""
    fix = tmp_path / "fixtures"
    prompt = build_admission_gate_prompt(
        RAW["precondition"], RAW["action"], RAW["expected_outcome"], None
    )
    record_gate(fix, prompt, admit_output(GENERALIZED_ATOM))

    order: list = []
    real_run_judge = pipeline.run_judge

    def spy_run_judge(*args, **kwargs):
        order.append(("judge", None))
        return real_run_judge(*args, **kwargs)

    monkeypatch.setattr(pipeline, "run_judge", spy_run_judge)

    encoder = RecordingEncoder(fixed_vector(), order)
    embedder = EmbeddingService(CFG.embedding, encoder=encoder)

    atom = run_admission_gate(
        CFG,
        precondition=RAW["precondition"],
        action=RAW["action"],
        expected_outcome=RAW["expected_outcome"],
        judge_fixtures_dir=fix,
    )
    # The downstream consumer (U6) embeds the KEY vector on the gate's output.
    embedder.embed_key(atom.precondition, atom.action)

    assert [kind for kind, _ in order] == ["judge", "embed"]
    embedded_text = encoder.calls[0]
    # Embedded the generalized text, never the raw input.
    for literal in RAW_LITERALS:
        assert literal not in embedded_text
    assert "<CONFIG_FILE>" in embedded_text


def test_typed_substitution_strips_instance_trivia(tmp_path):
    """A hyper-specific input emerges with typed placeholders; no raw literal leaks."""
    fix = tmp_path / "fixtures"
    prompt = build_admission_gate_prompt(
        RAW["precondition"], RAW["action"], RAW["expected_outcome"], None
    )
    record_gate(fix, prompt, admit_output(GENERALIZED_ATOM))

    atom = run_admission_gate(
        CFG,
        precondition=RAW["precondition"],
        action=RAW["action"],
        expected_outcome=RAW["expected_outcome"],
        judge_fixtures_dir=fix,
    )

    joined = " ".join(
        [
            atom.precondition,
            atom.action,
            atom.expected_outcome,
            atom.rationale or "",
            atom.negative_scope,
        ]
    )
    for literal in RAW_LITERALS:
        assert literal not in joined, f"raw literal {literal!r} leaked into the atom"
    # The gate did NOT echo the raw input — it returned the generalized atom.
    assert atom.precondition != RAW["precondition"]
    assert "<CONFIG_FILE>" in joined and "<PORT>" in joined


def test_negative_scope_emitted(tmp_path):
    """An admitted atom carries a non-empty negative_scope; altitude audit rejects."""
    fix = tmp_path / "fixtures"
    prompt = build_admission_gate_prompt(
        RAW["precondition"], RAW["action"], RAW["expected_outcome"], None
    )
    record_gate(fix, prompt, admit_output(GENERALIZED_ATOM))

    atom = run_admission_gate(
        CFG,
        precondition=RAW["precondition"],
        action=RAW["action"],
        expected_outcome=RAW["expected_outcome"],
        judge_fixtures_dir=fix,
    )
    assert isinstance(atom, AdmittedAtom)
    assert atom.negative_scope.strip()

    # An over-general atom is caught by the altitude audit -> lint_reject.
    over_general = dict(
        precondition="Something is wrong",
        action="Fix it",
        expected_outcome="It works",
    )
    og_prompt = build_admission_gate_prompt(
        over_general["precondition"],
        over_general["action"],
        over_general["expected_outcome"],
        None,
    )
    record_gate(
        fix,
        og_prompt,
        lint_reject_output("over-general: no precondition discriminates application"),
    )
    with pytest.raises(LintRejected):
        run_admission_gate(CFG, **over_general, judge_fixtures_dir=fix)


def test_target_trivia_lint_rejected(tmp_path):
    """Target-trivia -> lint_reject and NO downstream embed (the gate exits early)."""
    fix = tmp_path / "fixtures"
    trivia = dict(
        precondition="The openemr admin password is hunter2",
        action="Type hunter2 into the openemr login form",
        expected_outcome="You are logged into this specific openemr instance",
    )
    prompt = build_admission_gate_prompt(
        trivia["precondition"], trivia["action"], trivia["expected_outcome"], None
    )
    record_gate(
        fix,
        prompt,
        lint_reject_output("target-trivia: a single instance credential, not a rule"),
    )

    log: list = []
    encoder = RecordingEncoder(fixed_vector(), log)
    embedder = EmbeddingService(CFG.embedding, encoder=encoder)

    with pytest.raises(LintRejected):
        atom = run_admission_gate(CFG, **trivia, judge_fixtures_dir=fix)
        # Unreached: the gate raises before the consumer can embed.
        embedder.embed_key(atom.precondition, atom.action)

    assert encoder.calls == []  # no embed happened


def test_rewrite_proposed_requires_accept_flag(tmp_path):
    """rewrite_proposed exits non-zero; the rewrite is adopted only on re-entry."""
    fix = tmp_path / "fixtures"
    borderline = dict(
        precondition="In repo acme-web, the login page at /login is slow",
        action="Add an index on the users.email column in acme-web",
        expected_outcome="The /login page in acme-web loads faster",
    )
    proposed = dict(
        precondition="A login page backed by an unindexed lookup column is slow",
        action="Add a database index on the authentication lookup column",
        expected_outcome="Login latency drops to the indexed-lookup baseline",
        negative_scope="Do not apply when the column is already indexed",
    )
    b_prompt = build_admission_gate_prompt(
        borderline["precondition"],
        borderline["action"],
        borderline["expected_outcome"],
        None,
    )
    record_gate(fix, b_prompt, rewrite_output(proposed))

    # First pass (no --accept-rewrite): raises, carrying the proposed rewrite.
    with pytest.raises(RewriteProposed) as exc:
        run_admission_gate(CFG, **borderline, judge_fixtures_dir=fix)
    carried = exc.value.rewrite
    assert carried["precondition"] == proposed["precondition"]
    # The rewrite was NOT silently adopted as the borderline input.
    assert carried["precondition"] != borderline["precondition"]

    # Re-entry with the rewritten atom + accept_rewrite=True -> admitted.
    r_prompt = build_admission_gate_prompt(
        proposed["precondition"],
        proposed["action"],
        proposed["expected_outcome"],
        None,
    )
    record_gate(fix, r_prompt, admit_output(proposed))
    atom = run_admission_gate(
        CFG,
        precondition=proposed["precondition"],
        action=proposed["action"],
        expected_outcome=proposed["expected_outcome"],
        accept_rewrite=True,
        judge_fixtures_dir=fix,
    )
    assert atom.precondition == proposed["precondition"]
    assert atom.negative_scope == proposed["negative_scope"]


def test_scope_tag_override_logged(tmp_path, caplog):
    """The gate confirms/overrides the author's scope tag and logs the override."""
    fix = tmp_path / "fixtures"
    prompt = build_admission_gate_prompt(
        RAW["precondition"],
        RAW["action"],
        RAW["expected_outcome"],
        "target-specific",
    )
    record_gate(fix, prompt, admit_output(GENERALIZED_ATOM, scope_value="universal"))

    with caplog.at_level(logging.WARNING, logger="agent_families.pipeline"):
        atom = run_admission_gate(
            CFG,
            precondition=RAW["precondition"],
            action=RAW["action"],
            expected_outcome=RAW["expected_outcome"],
            scope_tag="target-specific",
            judge_fixtures_dir=fix,
        )
    assert atom.scope_tag == "universal"
    assert any("overrode author scope tag" in r.message for r in caplog.records)


def test_structural_validation_rejects_blank(tmp_path):
    """A blank structural field is bounced before any judge call (template gate)."""
    with pytest.raises(StructuralValidationError):
        run_admission_gate(
            CFG,
            precondition="   ",
            action="do something",
            expected_outcome="it works",
            judge_fixtures_dir=tmp_path,
        )
