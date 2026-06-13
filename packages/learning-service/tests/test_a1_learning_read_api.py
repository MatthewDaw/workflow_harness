"""A1 / MAT-154 — Learning read API (Python side).

Tests the two read endpoints:
  - candidate-learnings: current set only, corroboration-gated, excludes invalidAt
  - all-ideas: all ideas (current set by default; history=true includes invalidAt)

Also includes ``test_web_read_dto_contract`` which validates the JSON-DTO shape
on both the Python side (this file) and mirrors the TypeScript contract shape
in the TS-side test (see packages/web/src/test/learningReadApi.test.ts).

All tests are offline — they use InMemoryLearningStore, no boto3 / moto.
"""
from __future__ import annotations

import json
import time
import uuid

import pytest

from learning_service.db.store import InMemoryLearningStore
from learning_service.entrypoints.learning_reads import (
    CORROBORATION_K,
    all_ideas,
    candidate_learnings,
    handle_all_ideas,
    handle_candidate_learnings,
    handler,
)
from learning_service.schema.generated.py_types import IdeaRecord


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

ORG = "acme"
SKILL = "hq-add-skill"


def _idea(
    idea_id: str = None,
    body: str = "use owner/repo not bare name",
    status: str = "open",
    corroboration_version: int = 0,
    invalid_at: int | None = None,
    folded_into_rev: int | None = None,
    authored: bool | None = None,
    authority_kind: str | None = None,
    superseded_by: str | None = None,
    supersedes: list[str] | None = None,
    refines: str | None = None,
) -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id or str(uuid.uuid4()),
        skillBaseName=SKILL,
        org=ORG,
        body=body,
        status=status,
        corroborationVersion=corroboration_version,
        invalidAt=invalid_at,
        foldedIntoRev=folded_into_rev,
        authored=authored,
        authorityKind=authority_kind,
        supersededBy=superseded_by,
        supersedes=supersedes or [],
        refines=refines,
    )


def _event(
    skill: str = SKILL,
    org: str = ORG,
    path_suffix: str = "/candidate-learnings",
    history: str | None = None,
    store: InMemoryLearningStore | None = None,
) -> dict:
    qs: dict[str, str] = {"org": org}
    if history is not None:
        qs["history"] = history
    ev: dict = {
        "rawPath": f"/skills/{skill}{path_suffix}",
        "pathParameters": {"name": skill},
        "queryStringParameters": qs,
        "headers": {},
    }
    if store is not None:
        ev["_test_store"] = store
    return ev


# ---------------------------------------------------------------------------
# candidate-learnings: current set only, corroboration-gated, excludes invalidAt
# ---------------------------------------------------------------------------

class TestCandidateLearnings:
    def test_returns_only_open_ideas_meeting_k(self):
        """candidate-learnings only surfaces open ideas with count >= K."""
        store = InMemoryLearningStore()
        corroborated = _idea("corr-1", corroboration_version=CORROBORATION_K)
        not_enough = _idea("low-1", corroboration_version=CORROBORATION_K - 1)
        store.put_idea(corroborated)
        store.put_idea(not_enough)

        resp = handle_candidate_learnings(_event(store=store), object())
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["learnings"]]
        assert "corr-1" in ids
        assert "low-1" not in ids

    def test_excludes_invalid_at_ideas(self):
        """Retired ideas (invalidAt set) must NOT appear in candidate-learnings."""
        store = InMemoryLearningStore()
        active = _idea("active-1", corroboration_version=CORROBORATION_K)
        retired = _idea("retired-1", corroboration_version=CORROBORATION_K, invalid_at=int(time.time() * 1000))
        store.put_idea(active)
        store.put_idea(retired)

        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["learnings"]]
        assert "active-1" in ids
        assert "retired-1" not in ids

    def test_excludes_folded_ideas(self):
        """Folded ideas must NOT appear in candidate-learnings (lesson is already in skill body)."""
        store = InMemoryLearningStore()
        open_idea = _idea("open-1", corroboration_version=CORROBORATION_K)
        folded = _idea("folded-1", status="folded", corroboration_version=CORROBORATION_K, folded_into_rev=3)
        store.put_idea(open_idea)
        store.put_idea(folded)

        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["learnings"]]
        assert "open-1" in ids
        assert "folded-1" not in ids

    def test_sorted_strongest_first(self):
        """Results are sorted by corroboration count descending."""
        store = InMemoryLearningStore()
        weak = _idea("weak", corroboration_version=CORROBORATION_K)
        strong = _idea("strong", corroboration_version=CORROBORATION_K + 5)
        store.put_idea(weak)
        store.put_idea(strong)

        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["learnings"]]
        assert ids[0] == "strong"
        assert ids[1] == "weak"

    def test_capped_at_5(self):
        """candidate-learnings caps results at 5."""
        store = InMemoryLearningStore()
        for i in range(10):
            store.put_idea(_idea(f"idea-{i}", corroboration_version=CORROBORATION_K))

        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        assert len(body["learnings"]) <= 5

    def test_empty_store_returns_empty_list(self):
        store = InMemoryLearningStore()
        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        assert body == {"learnings": []}

    def test_bad_request_missing_org(self):
        store = InMemoryLearningStore()
        ev = {
            "rawPath": f"/skills/{SKILL}/candidate-learnings",
            "pathParameters": {"name": SKILL},
            "queryStringParameters": {},  # no org
            "headers": {},
            "_test_store": store,
        }
        resp = handle_candidate_learnings(ev, object())
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# all-ideas: current set + optional history
# ---------------------------------------------------------------------------

class TestAllIdeas:
    def test_returns_all_current_ideas_including_folded(self):
        """all-ideas returns open AND folded ideas (no corroboration gate)."""
        store = InMemoryLearningStore()
        open_idea = _idea("open-1", corroboration_version=0)
        folded = _idea("folded-1", status="folded", corroboration_version=CORROBORATION_K, folded_into_rev=2)
        store.put_idea(open_idea)
        store.put_idea(folded)

        resp = handle_all_ideas(_event(path_suffix="/all-ideas", store=store), object())
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["ideas"]]
        assert "open-1" in ids
        assert "folded-1" in ids

    def test_excludes_invalid_at_by_default(self):
        """all-ideas excludes retired ideas by default (current set only)."""
        store = InMemoryLearningStore()
        active = _idea("active-1", corroboration_version=0)
        retired = _idea("retired-1", corroboration_version=0, invalid_at=int(time.time() * 1000))
        store.put_idea(active)
        store.put_idea(retired)

        resp = handle_all_ideas(_event(path_suffix="/all-ideas", store=store), object())
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["ideas"]]
        assert "active-1" in ids
        assert "retired-1" not in ids

    def test_history_true_includes_retired(self):
        """?history=true includes ideas with invalidAt set."""
        store = InMemoryLearningStore()
        active = _idea("active-1", corroboration_version=0)
        retired = _idea("retired-1", corroboration_version=0, invalid_at=int(time.time() * 1000))
        store.put_idea(active)
        store.put_idea(retired)

        resp = handle_all_ideas(
            _event(path_suffix="/all-ideas", history="true", store=store), object()
        )
        body = json.loads(resp["body"])
        ids = [i["ideaId"] for i in body["ideas"]]
        assert "active-1" in ids
        assert "retired-1" in ids

    def test_no_corroboration_gate(self):
        """all-ideas has no corroboration gate — all counts surface."""
        store = InMemoryLearningStore()
        zero = _idea("zero-votes", corroboration_version=0)
        store.put_idea(zero)

        resp = handle_all_ideas(_event(path_suffix="/all-ideas", store=store), object())
        body = json.loads(resp["body"])
        assert any(i["ideaId"] == "zero-votes" for i in body["ideas"])

    def test_includes_history_fields_in_dto(self):
        """all-ideas DTOs include invalidAt, supersededBy, supersedes, refines."""
        store = InMemoryLearningStore()
        idea = _idea(
            "rich-1",
            superseded_by="other-idea",
            supersedes=["old-1", "old-2"],
            refines="parent-idea",
        )
        store.put_idea(idea)

        resp = handle_all_ideas(_event(path_suffix="/all-ideas", store=store), object())
        body = json.loads(resp["body"])
        dto = next(i for i in body["ideas"] if i["ideaId"] == "rich-1")
        assert "invalidAt" in dto
        assert "supersededBy" in dto
        assert dto["supersededBy"] == "other-idea"
        assert dto["supersedes"] == ["old-1", "old-2"]
        assert dto["refines"] == "parent-idea"

    def test_bad_request_missing_org(self):
        store = InMemoryLearningStore()
        ev = {
            "rawPath": f"/skills/{SKILL}/all-ideas",
            "pathParameters": {"name": SKILL},
            "queryStringParameters": {},  # no org
            "headers": {},
            "_test_store": store,
        }
        resp = handle_all_ideas(ev, object())
        assert resp["statusCode"] == 400


# ---------------------------------------------------------------------------
# Unified handler dispatch
# ---------------------------------------------------------------------------

class TestUnifiedHandler:
    def test_dispatch_candidate_learnings(self):
        store = InMemoryLearningStore()
        store.put_idea(_idea("idea-1", corroboration_version=CORROBORATION_K))
        ev = _event(path_suffix="/candidate-learnings", store=store)
        resp = handler(ev, object())
        assert resp["statusCode"] == 200
        assert "learnings" in json.loads(resp["body"])

    def test_dispatch_all_ideas(self):
        store = InMemoryLearningStore()
        store.put_idea(_idea("idea-1", corroboration_version=0))
        ev = _event(path_suffix="/all-ideas", store=store)
        resp = handler(ev, object())
        assert resp["statusCode"] == 200
        assert "ideas" in json.loads(resp["body"])

    def test_dispatch_unknown_path_returns_404(self):
        ev = {"rawPath": "/skills/hq-add-skill/unknown", "_test_store": InMemoryLearningStore()}
        resp = handler(ev, object())
        assert resp["statusCode"] == 404


# ---------------------------------------------------------------------------
# JSON-DTO contract test (test_web_read_dto_contract — both sides)
#
# This test validates the exact JSON shape that the Python API returns and the
# TS web layer (packages/web/src/api/learningApi.ts) expects to parse.
# The TS side mirrors this in packages/web/src/test/learningReadApi.test.ts.
# ---------------------------------------------------------------------------

class TestWebReadDtoContract:
    """Validate the JSON-DTO shape on the Python side of the contract."""

    def test_web_read_dto_contract_candidate_learnings(self):
        """Python API returns the exact DTO shape the TS web expects for candidate-learnings."""
        store = InMemoryLearningStore()
        idea = IdeaRecord(
            ideaId="idea-contract-1",
            skillBaseName=SKILL,
            org=ORG,
            body="Always use owner/repo not bare repo name.",
            status="open",
            corroborationVersion=CORROBORATION_K,
            foldedIntoRev=None,
            authored=None,
            authorityKind="merged",
        )
        store.put_idea(idea)

        resp = handle_candidate_learnings(_event(store=store), object())
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])

        # Envelope shape
        assert "learnings" in body
        assert isinstance(body["learnings"], list)

        # DTO field contract (the TS LearningIdeaDto interface must match these keys)
        dto = body["learnings"][0]
        required_keys = {
            "ideaId", "skillBaseName", "body", "status",
            "corroborationCount", "foldedIntoRev", "authored", "authorityKind",
        }
        assert required_keys <= set(dto.keys()), (
            f"Missing DTO keys: {required_keys - set(dto.keys())}"
        )
        # Values
        assert dto["ideaId"] == "idea-contract-1"
        assert dto["body"] == "Always use owner/repo not bare repo name."
        assert dto["status"] == "open"
        assert isinstance(dto["corroborationCount"], int)
        assert dto["corroborationCount"] >= CORROBORATION_K
        assert dto["authorityKind"] == "merged"

    def test_web_read_dto_contract_all_ideas(self):
        """Python API returns the exact DTO shape the TS web expects for all-ideas."""
        store = InMemoryLearningStore()
        now = int(time.time() * 1000)
        idea = IdeaRecord(
            ideaId="idea-contract-2",
            skillBaseName=SKILL,
            org=ORG,
            body="Use tree-sitter for anchor extraction.",
            status="open",
            corroborationVersion=1,
            foldedIntoRev=None,
            authored=None,
            authorityKind="merged",
            invalidAt=None,
            supersededBy=None,
            supersedes=[],
            refines=None,
        )
        store.put_idea(idea)

        resp = handle_all_ideas(_event(path_suffix="/all-ideas", store=store), object())
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])

        assert "ideas" in body
        assert isinstance(body["ideas"], list)

        dto = body["ideas"][0]
        required_keys = {
            "ideaId", "skillBaseName", "body", "status",
            "corroborationCount", "foldedIntoRev", "authored", "authorityKind",
            "invalidAt", "supersededBy", "supersedes", "refines",
        }
        assert required_keys <= set(dto.keys()), (
            f"Missing DTO keys: {required_keys - set(dto.keys())}"
        )
        assert dto["ideaId"] == "idea-contract-2"
        assert dto["invalidAt"] is None
        assert dto["supersedes"] == []
        assert dto["supersededBy"] is None

    def test_candidate_learnings_never_exposes_invalid_at(self):
        """candidate-learnings DTO must NOT include invalidAt — security contract."""
        store = InMemoryLearningStore()
        idea = _idea("guard-1", corroboration_version=CORROBORATION_K)
        store.put_idea(idea)

        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        for dto in body["learnings"]:
            assert "invalidAt" not in dto, (
                "candidate-learnings must NOT expose invalidAt — it is a history/HQ field"
            )

    def test_dto_corroboration_count_is_integer(self):
        """corroborationCount in the DTO must always be an integer (not float, not None)."""
        store = InMemoryLearningStore()
        store.put_idea(_idea("int-check", corroboration_version=3))
        resp = handle_candidate_learnings(_event(store=store), object())
        body = json.loads(resp["body"])
        for dto in body["learnings"]:
            assert isinstance(dto["corroborationCount"], int), (
                f"corroborationCount must be int, got {type(dto['corroborationCount'])}"
            )


# ---------------------------------------------------------------------------
# Pure logic tests (no store I/O)
# ---------------------------------------------------------------------------

class TestPureLogic:
    def test_candidate_learnings_pure(self):
        """Pure candidateLearnings() mirrors the TS version's filter/sort/cap behaviour."""
        ideas = [
            _idea("a", corroboration_version=5),
            _idea("b", corroboration_version=1),  # below K
            _idea("c", corroboration_version=3),
            _idea("d", corroboration_version=CORROBORATION_K, invalid_at=123456),  # retired
            _idea("e", status="folded", corroboration_version=CORROBORATION_K),  # folded
        ]
        result = candidate_learnings(ideas, k=CORROBORATION_K, cap=10)
        ids = [r["ideaId"] for r in result]
        assert "a" in ids
        assert "c" in ids
        assert "b" not in ids     # below K
        assert "d" not in ids     # retired
        assert "e" not in ids     # folded
        assert ids[0] == "a"      # strongest first

    def test_all_ideas_pure_excludes_retired_by_default(self):
        ideas = [
            _idea("curr", invalid_at=None),
            _idea("ret", invalid_at=999),
        ]
        result = all_ideas(ideas, include_history=False)
        ids = [r["ideaId"] for r in result]
        assert "curr" in ids
        assert "ret" not in ids

    def test_all_ideas_pure_includes_retired_with_history(self):
        ideas = [
            _idea("curr", invalid_at=None),
            _idea("ret", invalid_at=999),
        ]
        result = all_ideas(ideas, include_history=True)
        ids = [r["ideaId"] for r in result]
        assert "curr" in ids
        assert "ret" in ids
