"""plan-003 U5: the grader-side verified-oracle checker (R12's checker contract).

Fully offline: checker calls ride Phase 0's ``run_judge`` record/replay seam
with fixtures planted by the tests themselves (``write_fixture``) — zero
quota, no ``claude`` on PATH, no docker.

## Conformance

R12 checker-contract arms (plan-003 U5 Test scenarios: "checker pass and
contradiction paths") — invariant -> enforcing test:

- checker input = (question, answer, fresh-observation a11y snapshot,
  registry evidence for the mentioned FEATs); mentions are the retrieval
  mechanism and are mandatory
  -> ``test_retrieve_evidence_requires_mentions`` (empty mentions is a hard
  error), ``test_retrieve_evidence_unknown_feat_rejected`` (a mention with
  no registry row is a hard error),
  ``test_retrieve_evidence_loads_registry_a11y`` (the minted FEAT's captured
  a11y snapshots are what the checker validates against),
  ``test_checker_prompt_embeds_all_four_inputs``
- single-shot judge with default-fail framing
  -> ``test_checker_prompt_default_fail_framing`` (the prompt's wording) and
  ``test_check_answer_pass`` (one judge call, replayed)
- output = {verdict, contradiction: {claim, evidence_ref, observed}};
  the contradiction object is what the retry prompt embeds
  -> ``test_check_answer_fail_carries_contradiction`` (fail round-trips the
  full contradiction object),
  ``test_fail_without_contradiction_is_contract_violation`` and
  ``test_pass_with_contradiction_is_contract_violation`` (the pairing is
  enforced on the judge's schema-retry path, default-fail discipline)
- fixture-key discipline (Phase 0 R23): no volatile data in the prompt
  -> ``test_checker_prompt_carries_no_volatile_paths`` (evidence file paths
  never appear; unreadable evidence degrades to empty — the default-fail
  framing then fails the answer rather than crashing:
  ``test_retrieve_evidence_unreadable_file_degrades_empty``)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_families.grading.registry import FeatureCandidate, mint_feat
from agent_families.judge import JudgeSchemaViolation, write_fixture
from agent_families.pipeline.oracle_check import (
    CHECKER_SCHEMA,
    OracleCheckError,
    build_checker_prompt,
    check_answer,
    retrieve_registry_evidence,
    validate_checker_output,
)
from agent_families.store import Store

TARGET = "linkding"
DIGEST = "sha256:" + "cd" * 32
MODEL = "sonnet"

QUESTION = "Does the bookmark list support filtering by tag?"
ANSWER = "Yes - the list page shows a tag sidebar that filters on click."
OBSERVATION = {"role": "main", "name": "bookmarks", "children": ["tag-sidebar"]}
CONTRADICTION = {
    "claim": "the list page shows a tag sidebar",
    "evidence_ref": "FEAT-bookmark-tag-filter",
    "observed": "the registry evidence shows tag filtering via the search box",
}


# --- helpers -------------------------------------------------------------------


def make_store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "library.db")
    store.migrate()
    return store


def cand(key: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"user can {key.replace('-', ' ')}",
        route="bookmarks/urls.py",
        confirm_steps=(
            {"action": "goto", "selector": "", "args": {"url": "/bookmarks"}},
        ),
        scenario_steps=("Open the bookmarks page",),
        expected_outcome="the behavior is observable",
        tier="must",
    )


def write_evidence(tmp_path: Path, key: str, a11y) -> Path:
    """A registry evidence file in U3's ``confirm_candidate`` format."""
    path = tmp_path / "evidence" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "feat_key": key,
                "digest": DIGEST,
                "captured": [
                    {
                        "step": {"action": "goto", "selector": "", "args": {}},
                        "a11y": a11y,
                    }
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
        newline="\n",
    )
    return path


def mint(store: Store, tmp_path: Path, key: str, a11y=None) -> str:
    evidence = write_evidence(tmp_path, key, a11y or {"role": "list", "name": key})
    return mint_feat(store, cand(key), str(evidence), target=TARGET, digest=DIGEST)


def plant(fixtures_dir: Path, prompt: str, output: dict) -> None:
    write_fixture(
        fixtures_dir,
        prompt,
        CHECKER_SCHEMA,
        MODEL,
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "duration_ms": 40,
            "total_cost_usd": 0.001,
            "structured_output": output,
        },
    )


def checked(tmp_path: Path, evidence: list[dict], output: dict):
    fixtures = tmp_path / "fixtures"
    prompt = build_checker_prompt(QUESTION, ANSWER, OBSERVATION, evidence)
    plant(fixtures, prompt, output)
    return check_answer(
        QUESTION,
        ANSWER,
        OBSERVATION,
        evidence,
        model=MODEL,
        max_retries=0,
        mode="replay",
        fixtures_dir=fixtures,
    )


# --- mentioned-FEAT retrieval (the R12 retrieval mechanism) -----------------------


def test_retrieve_evidence_requires_mentions(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OracleCheckError, match="mandatory FEAT mentions"):
        retrieve_registry_evidence(store, [])


def test_retrieve_evidence_unknown_feat_rejected(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OracleCheckError, match="no registry row"):
        retrieve_registry_evidence(store, ["FEAT-never-minted"])


def test_retrieve_evidence_loads_registry_a11y(tmp_path):
    store = make_store(tmp_path)
    a11y = {"role": "list", "name": "bookmark-tag-filter", "items": 3}
    fid = mint(store, tmp_path, "bookmark-tag-filter", a11y)
    # duplicate mentions deduplicate; the captured snapshots come back
    rows = retrieve_registry_evidence(store, [fid, fid])
    assert [r["feat_id"] for r in rows] == [fid]
    assert rows[0]["a11y"] == [a11y]
    assert rows[0]["evidence_ref"].endswith("bookmark-tag-filter.json")


def test_retrieve_evidence_unreadable_file_degrades_empty(tmp_path):
    store = make_store(tmp_path)
    fid = mint_feat(
        store,
        cand("bookmark-archive"),
        str(tmp_path / "evidence" / "missing.json"),
        target=TARGET,
        digest=DIGEST,
    )
    rows = retrieve_registry_evidence(store, [fid])
    # nothing to confirm against -> the default-fail checker fails the
    # answer; retrieval itself never crashes the round-trip
    assert rows[0]["a11y"] == []


# --- the checker prompt (deterministic; default-fail framing) ---------------------


def test_checker_prompt_embeds_all_four_inputs(tmp_path):
    store = make_store(tmp_path)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    evidence = retrieve_registry_evidence(store, [fid])
    prompt = build_checker_prompt(QUESTION, ANSWER, OBSERVATION, evidence)
    assert QUESTION in prompt
    assert ANSWER in prompt
    assert json.dumps(OBSERVATION, sort_keys=True, ensure_ascii=False) in prompt
    assert fid in prompt
    assert "bookmark-tag-filter" in prompt  # the registry a11y payload


def test_checker_prompt_default_fail_framing():
    prompt = build_checker_prompt(QUESTION, ANSWER, OBSERVATION, [])
    assert "default to 'fail'" in prompt
    assert "MUST be 'fail'" in prompt


def test_checker_prompt_carries_no_volatile_paths(tmp_path):
    store = make_store(tmp_path)
    fid = mint(store, tmp_path, "bookmark-tag-filter")
    evidence = retrieve_registry_evidence(store, [fid])
    prompt = build_checker_prompt(QUESTION, ANSWER, OBSERVATION, evidence)
    # R23: replay keys hash the prompt — evidence file paths (tmp dirs) must
    # never appear in it
    assert str(tmp_path) not in prompt
    assert evidence[0]["evidence_ref"] not in prompt


# --- the verdict round-trip (pass and contradiction arms) -------------------------


def test_check_answer_pass(tmp_path):
    verdict = checked(tmp_path, [], {"verdict": "pass", "contradiction": None})
    assert verdict.passed
    assert verdict.verdict == "pass"
    assert verdict.contradiction is None
    assert verdict.attempts == 1


def test_check_answer_fail_carries_contradiction(tmp_path):
    verdict = checked(
        tmp_path, [], {"verdict": "fail", "contradiction": CONTRADICTION}
    )
    assert not verdict.passed
    # the contradiction object round-trips intact — it is what the
    # explorer's retry prompt embeds (R12)
    assert verdict.contradiction == CONTRADICTION


def test_fail_without_contradiction_is_contract_violation(tmp_path):
    with pytest.raises(JudgeSchemaViolation, match="contradiction"):
        checked(tmp_path, [], {"verdict": "fail", "contradiction": None})


def test_pass_with_contradiction_is_contract_violation(tmp_path):
    with pytest.raises(JudgeSchemaViolation, match="null"):
        checked(
            tmp_path, [], {"verdict": "pass", "contradiction": CONTRADICTION}
        )


def test_validate_checker_output_pairing():
    assert validate_checker_output({"verdict": "pass", "contradiction": None}) is None
    assert (
        validate_checker_output({"verdict": "fail", "contradiction": CONTRADICTION})
        is None
    )
    assert validate_checker_output({"verdict": "fail", "contradiction": None})
    assert validate_checker_output({"verdict": "pass", "contradiction": CONTRADICTION})
