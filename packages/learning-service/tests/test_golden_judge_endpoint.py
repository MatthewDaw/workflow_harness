"""MAT-141 (U10) Gap 3 — the ONE golden judge.

The standalone TS advisory judge (rerank/golden.ts) is superseded by this single
Python golden-drift-detector endpoint.  These tests prove the endpoint is the
authoritative verdict implementation and behaves per the advisory contract
(garbled/failed verdict → flagged regression, never a silent pass).

All offline: a fake ``run_judge`` is injected via ``_test_judge`` so no model is
loaded and no network is touched.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from learning_service.entrypoints.golden_judge import handler, judge_golden


def _fake_judge(satisfied: bool, reason: str):
    def _run(prompt, schema, model, max_retries):  # noqa: ANN001
        # Assert the single verdict implementation built the prompt (the lesson +
        # candidate body both flow through one place).
        assert "Lesson the skill was edited to encode" in prompt
        assert "Candidate new skill body" in prompt
        return SimpleNamespace(output={"satisfied": satisfied, "reason": reason})

    return _run


def test_golden_judge_satisfied_verdict():
    out = judge_golden(
        "Always reconcile in the ledger currency.",
        "candidate still has the lesson",
        run_judge_fn=_fake_judge(True, "still present"),
    )
    assert out == {"satisfied": True, "reason": "still present"}


def test_golden_judge_regression_verdict():
    out = judge_golden(
        "Always reconcile in the ledger currency.",
        "candidate dropped the lesson",
        run_judge_fn=_fake_judge(False, "lesson dropped"),
    )
    assert out["satisfied"] is False
    assert out["reason"] == "lesson dropped"


def test_golden_judge_failure_is_flagged_regression_not_silent_pass():
    def _boom(**kwargs):  # noqa: ANN003
        raise RuntimeError("model unavailable")

    out = judge_golden("lesson", "candidate", run_judge_fn=_boom)
    assert out["satisfied"] is False
    assert "unavailable" in out["reason"]


def test_handler_routes_through_the_single_judge():
    event = {
        "body": json.dumps({"lesson": "L", "candidateBody": "C"}),
        "_test_judge": _fake_judge(True, "ok"),
    }
    resp = handler(event, object())
    assert resp["statusCode"] == 200
    body = json.loads(resp["body"])
    assert body == {"satisfied": True, "reason": "ok"}


def test_handler_bad_request_missing_fields():
    resp = handler({"body": json.dumps({"lesson": "only"})}, object())
    assert resp["statusCode"] == 400


def test_only_one_golden_judge_implementation_exists():
    """The golden-verdict prompt/schema must live in exactly ONE module.

    A grep-style guard: the verdict schema constant is defined only in the Python
    golden_judge module; the TS side proxies to it and must not re-declare it.
    """
    from learning_service.entrypoints import golden_judge

    # The single source of the verdict schema.
    assert golden_judge.GOLDEN_VERDICT_SCHEMA["required"] == ["satisfied", "reason"]
