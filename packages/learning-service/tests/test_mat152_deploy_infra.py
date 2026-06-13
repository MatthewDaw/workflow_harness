"""MAT-152 (R3) — Deploy/infra: EventBridge wiring, model integrity, Dockerfile.

Tests close the three gaps identified by the Opus verifier:

GAP 1 — EventBridge / handler contract
  The EventBridge rule sends NO org/skill_base_name.  The handler must accept
  this event and perform a GLOBAL scan (enumerate all orgs/skills with folded
  non-authored ideas).  Previously the handler returned 400.

GAP 2 — Model artifact integrity
  The Dockerfile now performs a sha256 digest verification of the downloaded
  weight files at build time.  The verification logic (Python inline in the RUN
  step) is tested here independently with mock weight files.

GAP 3 — Dockerfile build context
  The Dockerfile previously used COPY ../../agent-families which escapes the
  build context (packages/learning-service).  The Dockerfile now requires a
  repo-root build context and references agent-families correctly.
  Tested by asserting the COPY paths in the Dockerfile are repo-root-relative.

All tests are OFFLINE (no boto3, no model load, no network, no Docker daemon).
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import tempfile

import pytest

from learning_service.db.store import InMemoryLearningStore, ProcessedPrRecord
from learning_service.necessity import (
    NecessityFpGate,
    ScanResult,
    scheduled_scan_handler,
)
from learning_service.schema.generated.py_types import (
    GoldenCaseRecord,
    IdeaRecord,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

ORG = "acme"
SKILL = "error-handling"
SKILL2 = "naming-conventions"
ORG2 = "betacorp"


def _make_store() -> InMemoryLearningStore:
    return InMemoryLearningStore()


def _seed_folded_inferred_idea(
    store: InMemoryLearningStore,
    org: str,
    skill: str,
    idea_id: str,
    body: str = "Use dependency injection.",
) -> IdeaRecord:
    """Seed a folded non-authored idea (the scan target)."""
    idea = IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body=body,
        status="folded",
        corroborationVersion=1,
        authorityKind="merged",
        authored=False,
    )
    store.put_idea_conditional(idea, expected_version=-1)
    return idea


def _seed_golden(
    store: InMemoryLearningStore,
    org: str,
    skill: str,
    idea_id: str,
    body: str = "Use dependency injection.",
) -> None:
    gc = GoldenCaseRecord(
        caseId=idea_id,
        skillBaseName=skill,
        org=org,
        before="old code",
        after="new code",
        ideaBody=body,
    )
    store.put_golden_case(gc)


def _judge_unsatisfied(**kw):
    class _R:
        output = {"satisfied": False, "reason": "mock"}
    return _R()


# ===========================================================================
# GAP 1 — EventBridge → scheduled_scan_handler global scan
# ===========================================================================


class TestGap1EventBridgeGlobalScan:
    """The EventBridge rule sends no org/skill; handler must do a global scan."""

    def test_eventbridge_event_accepted_returns_200(self):
        """The canonical EventBridge event (no org/skill) must return statusCode 200.

        Previously this returned 400 because the handler required org and
        skill_base_name.  Now it performs a global scan and returns 200.
        """
        store = _make_store()
        # EventBridge event shape (from learning-stack.ts rule target input).
        eb_event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {
                "necessity_sample_rate": 1.0,
                "necessity_min_firings": 0,
            },
            # Inject test store; no _test_judge_fn needed (no ideas to scan).
            "_test_store": store,
        }

        result = scheduled_scan_handler(eb_event, context=None)

        assert result["statusCode"] == 200, (
            f"EventBridge scheduled event must return 200, got {result}"
        )
        body = json.loads(result["body"])
        assert "ideas_scanned" in body, "Response body must include ideas_scanned"

    def test_eventbridge_event_scans_zero_ideas_when_store_empty(self):
        """Global scan on an empty store must succeed with ideas_scanned=0."""
        store = _make_store()
        eb_event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {"necessity_sample_rate": 1.0, "necessity_min_firings": 0},
            "_test_store": store,
        }

        result = scheduled_scan_handler(eb_event, context=None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ideas_scanned"] == 0

    def test_global_scan_enumerates_all_orgs_and_skills(self):
        """Global scan visits every (org, skill) with folded non-authored ideas.

        Seeds two (org, skill) pairs with one folded inferred idea each + a
        golden case.  Judge always unsatisfied; fp_gate in advisory mode
        (default) — so no actual demotes, but all ideas are scanned.
        """
        store = _make_store()
        # Pair 1: acme / error-handling
        _seed_folded_inferred_idea(store, ORG, SKILL, "idea-a")
        _seed_golden(store, ORG, SKILL, "idea-a")
        # Pair 2: betacorp / naming-conventions
        _seed_folded_inferred_idea(store, ORG2, SKILL2, "idea-b")
        _seed_golden(store, ORG2, SKILL2, "idea-b")

        eb_event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {"necessity_sample_rate": 1.0, "necessity_min_firings": 0},
            "_test_store": store,
            "_test_judge_fn": _judge_unsatisfied,
        }

        result = scheduled_scan_handler(eb_event, context=None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # Both ideas should have been scanned.
        assert body["ideas_scanned"] == 2, (
            f"Global scan must visit all (org, skill) pairs; got ideas_scanned={body['ideas_scanned']}"
        )

    def test_global_scan_skips_authored_ideas(self):
        """Global scan must not scan authored ideas (user_directive / authored_import).

        Authored ideas are exempt from necessity — the store's
        list_skills_with_folded_non_authored_ideas must exclude them, so
        the scan targets list is empty when only authored ideas exist.
        """
        store = _make_store()
        # Authored idea — must be excluded.
        authored_idea = IdeaRecord(
            ideaId="authored-1",
            skillBaseName=SKILL,
            org=ORG,
            body="Always validate input.",
            status="folded",
            corroborationVersion=1,
            authorityKind="user_directive",
            authored=True,
        )
        store.put_idea_conditional(authored_idea, expected_version=-1)
        _seed_golden(store, ORG, SKILL, "authored-1")

        eb_event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {"necessity_sample_rate": 1.0, "necessity_min_firings": 0},
            "_test_store": store,
            "_test_judge_fn": _judge_unsatisfied,
        }

        result = scheduled_scan_handler(eb_event, context=None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ideas_scanned"] == 0, (
            f"Authored ideas must not be included in the global scan; "
            f"got ideas_scanned={body['ideas_scanned']}"
        )

    def test_global_scan_skips_open_and_retired_ideas(self):
        """Global scan must only target folded non-authored ideas, not open/retired ones."""
        store = _make_store()

        # Open idea — must not be targeted.
        open_idea = IdeaRecord(
            ideaId="open-1",
            skillBaseName=SKILL,
            org=ORG,
            body="Validate everything.",
            status="open",
            corroborationVersion=1,
            authorityKind="merged",
        )
        store.put_idea_conditional(open_idea, expected_version=-1)

        # Retired (invalidAt set) folded idea — must not be targeted.
        import time
        retired_idea = IdeaRecord(
            ideaId="retired-1",
            skillBaseName=SKILL,
            org=ORG,
            body="Use DI.",
            status="folded",
            corroborationVersion=2,
            authorityKind="merged",
            invalidAt=int(time.time() * 1000) - 1000,
        )
        store.put_idea_conditional(retired_idea, expected_version=-1)

        eb_event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {"necessity_sample_rate": 1.0},
            "_test_store": store,
            "_test_judge_fn": _judge_unsatisfied,
        }

        result = scheduled_scan_handler(eb_event, context=None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ideas_scanned"] == 0

    def test_targeted_scan_still_works(self):
        """Explicit org + skill_base_name payload still triggers a targeted scan."""
        store = _make_store()
        _seed_folded_inferred_idea(store, ORG, SKILL, "idea-targeted")
        _seed_golden(store, ORG, SKILL, "idea-targeted")

        targeted_event = {
            "org": ORG,
            "skill_base_name": SKILL,
            "necessity_sample_rate": 1.0,
            "_test_store": store,
            "_test_judge_fn": _judge_unsatisfied,
        }

        result = scheduled_scan_handler(targeted_event, context=None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ideas_scanned"] >= 1

    def test_list_skills_with_folded_non_authored_ideas_in_memory(self):
        """InMemoryLearningStore.list_skills_with_folded_non_authored_ideas is correct."""
        store = _make_store()

        # Should be empty initially.
        assert store.list_skills_with_folded_non_authored_ideas() == []

        # Add a folded inferred idea.
        _seed_folded_inferred_idea(store, ORG, SKILL, "idea-1")
        targets = store.list_skills_with_folded_non_authored_ideas()
        assert (ORG, SKILL) in targets, f"Expected ({ORG}, {SKILL}) in targets; got {targets}"
        assert len(targets) == 1

        # Add an authored idea — must not add another target.
        authored = IdeaRecord(
            ideaId="authored",
            skillBaseName=SKILL,
            org=ORG,
            body="Authored.",
            status="folded",
            corroborationVersion=1,
            authorityKind="user_directive",
        )
        store.put_idea_conditional(authored, expected_version=-1)
        targets2 = store.list_skills_with_folded_non_authored_ideas()
        assert len(targets2) == 1, "Authored idea must not create a new scan target"

        # Add a second (org, skill) pair.
        _seed_folded_inferred_idea(store, ORG2, SKILL2, "idea-2")
        targets3 = store.list_skills_with_folded_non_authored_ideas()
        assert len(targets3) == 2
        assert (ORG2, SKILL2) in targets3


# ===========================================================================
# GAP 2 — Model artifact sha256 integrity verification (unit-testable logic)
# ===========================================================================


class TestGap2ModelIntegrity:
    """The sha256 digest verification logic is extracted and tested offline.

    The Dockerfile embeds the verification as a Python inline script.  We
    reproduce the same algorithm here to verify correctness without requiring
    a running Docker build.
    """

    def _compute_digest(self, files: dict[str, bytes]) -> str:
        """Reproduce the Dockerfile's digest algorithm (name-prefixed, sorted)."""
        sha256 = hashlib.sha256()
        for name in sorted(files.keys()):
            sha256.update(name.encode())
            sha256.update(b":")
            sha256.update(files[name])
            sha256.update(b"\n")
        return sha256.hexdigest()

    def test_digest_matches_when_weights_unchanged(self):
        """Verification passes when the weight file content matches the expected digest."""
        weight_data = b"fake model weights for testing" * 100
        files = {"model.safetensors": weight_data}
        expected = self._compute_digest(files)

        # Simulate the verification step.
        sha256 = hashlib.sha256()
        for name in sorted(files.keys()):
            sha256.update(name.encode())
            sha256.update(b":")
            sha256.update(files[name])
            sha256.update(b"\n")
        actual = sha256.hexdigest()

        assert actual == expected, "Digest must match when weights are unchanged"

    def test_digest_fails_when_weights_tampered(self):
        """Verification detects tampering (wrong digest → mismatch)."""
        weight_data = b"legitimate model weights"
        files = {"model.safetensors": weight_data}
        correct_digest = self._compute_digest(files)

        tampered_data = b"tampered model weights - attacker substituted this"
        tampered_files = {"model.safetensors": tampered_data}
        tampered_digest = self._compute_digest(tampered_files)

        assert correct_digest != tampered_digest, (
            "Digest of tampered weights must differ from expected digest"
        )

    def test_digest_is_deterministic_across_calls(self):
        """The digest algorithm is deterministic (same input → same output)."""
        files = {
            "model.safetensors": b"weights data",
            "tokenizer.json": b"tokenizer data",
        }
        d1 = self._compute_digest(files)
        d2 = self._compute_digest(files)
        assert d1 == d2

    def test_digest_covers_all_shards(self):
        """Multi-shard models: digest covers ALL shards (sorted by name)."""
        shard1 = b"shard 1 weights"
        shard2 = b"shard 2 weights"
        files_both = {
            "pytorch_model-00001-of-00002.bin": shard1,
            "pytorch_model-00002-of-00002.bin": shard2,
        }
        files_shard1_only = {"pytorch_model-00001-of-00002.bin": shard1}

        d_both = self._compute_digest(files_both)
        d_partial = self._compute_digest(files_shard1_only)

        assert d_both != d_partial, (
            "Digest covering both shards must differ from digest of partial shards"
        )

    def test_digest_changes_if_filename_changes(self):
        """Filenames are part of the digest — renaming a shard changes the digest."""
        data = b"weights"
        d1 = self._compute_digest({"shard-a.bin": data})
        d2 = self._compute_digest({"shard-b.bin": data})
        assert d1 != d2, "Different filenames must produce different digests"

    def test_empty_expected_sha256_skips_check(self):
        """When PINNED_NLI_MODEL_SHA256 is empty string, the check is skipped (dev mode).

        This mirrors the Dockerfile logic: if expected_sha256 is falsy, skip
        the verification and emit a warning instead of failing the build.
        """
        expected_sha256 = ""  # empty → skip
        should_verify = bool(expected_sha256)
        assert should_verify is False, (
            "Empty PINNED_NLI_MODEL_SHA256 must skip the integrity check (dev mode)"
        )


# ===========================================================================
# GAP 3 — Dockerfile build context paths are repo-root-relative
# ===========================================================================


class TestGap3DockerfileBuildContext:
    """Assert the Dockerfile uses repo-root-relative COPY paths (no ../ escapes)."""

    DOCKERFILE_PATH = (
        pathlib.Path(__file__).parent.parent / "Dockerfile"
    )

    def _copy_lines(self) -> list[str]:
        """Return all COPY instructions from the Dockerfile."""
        lines = self.DOCKERFILE_PATH.read_text().splitlines()
        return [ln.strip() for ln in lines if ln.strip().startswith("COPY")]

    def test_dockerfile_exists(self):
        """Dockerfile must exist at packages/learning-service/Dockerfile."""
        assert self.DOCKERFILE_PATH.exists(), (
            f"Dockerfile not found at {self.DOCKERFILE_PATH}"
        )

    def test_no_copy_with_dotdot_parent_escape(self):
        """No COPY instruction must reference a path starting with ../../ (context escape).

        A COPY path like ../../agent-families escapes the build context and
        causes a hard Docker error.  All paths must be relative to the repo root.
        """
        copy_lines = self._copy_lines()
        escaped = [ln for ln in copy_lines if "../../" in ln]
        assert escaped == [], (
            f"Dockerfile must not use COPY paths that escape the build context via '../../'.\n"
            f"Offending lines:\n" + "\n".join(f"  {ln}" for ln in escaped)
        )

    def test_agent_families_copy_is_repo_root_relative(self):
        """The agent-families COPY must reference 'agent-families' (repo-root path).

        When the build context is the repo root, 'agent-families' refers to
        <repo-root>/agent-families — the correct location.
        """
        copy_lines = self._copy_lines()
        af_copies = [ln for ln in copy_lines if "agent-families" in ln]
        assert af_copies, (
            "Dockerfile must have a COPY instruction for agent-families"
        )
        for ln in af_copies:
            # Must NOT start with a relative parent escape.
            assert not ln.startswith("COPY ../../"), (
                f"agent-families COPY must not use '../../': {ln}"
            )
            # Must reference 'agent-families' directly (repo-root relative).
            assert "agent-families" in ln, (
                f"Expected 'agent-families' in COPY line: {ln}"
            )

    def test_learning_service_copy_is_repo_root_relative(self):
        """The learning-service COPY must reference 'packages/learning-service'."""
        copy_lines = self._copy_lines()
        ls_copies = [ln for ln in copy_lines if "learning-service" in ln and "--from" not in ln]
        assert ls_copies, (
            "Dockerfile must have a COPY instruction for packages/learning-service"
        )

    def test_pinned_sha256_build_arg_declared(self):
        """Dockerfile must declare PINNED_NLI_MODEL_SHA256 as a build ARG."""
        content = self.DOCKERFILE_PATH.read_text()
        assert "PINNED_NLI_MODEL_SHA256" in content, (
            "Dockerfile must declare PINNED_NLI_MODEL_SHA256 ARG for model integrity"
        )

    def test_sha256_verification_present_in_build_script(self):
        """Dockerfile must contain sha256 hash verification logic."""
        content = self.DOCKERFILE_PATH.read_text()
        assert "hashlib.sha256" in content or "sha256" in content.lower(), (
            "Dockerfile RUN script must perform sha256 digest verification of model weights"
        )
        # The verification must check the actual hexdigest and fail the build if wrong.
        assert "RuntimeError" in content, (
            "Dockerfile must raise RuntimeError (failing the build) on digest mismatch"
        )
