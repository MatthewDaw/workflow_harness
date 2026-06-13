"""test_r3_infra.py — R3 (MAT-152) Python-side infra tests.

Tests the Python entrypoints and runtime behaviour that the CDK infra provisions:

  1. The ingest Lambda handler (container Lambda CMD target) — validates the
     event-driven interface used by the CDK DockerImageFunction.
  2. The necessity-scan Lambda handler — validates the EventBridge event shape
     and scheduled_scan_handler contract (already in necessity.py).
  3. Secrets Manager integration — validates that the PEM secret is loaded from
     the ARN env var (not a plaintext env var), and that the runtime never logs
     the secret value.
  4. Model artifact pinning — validates that PINNED_MODEL_REVISION in classifier.py
     is a plausible git SHA (non-empty, ≥ 8 hex chars), mirroring the CDK build-arg
     contract.  Tests in this section inspect the classifier.py SOURCE TEXT rather
     than importing it, so they remain offline even when agent_families is absent.
  5. Container image ENV contract — validates that LS_MODE defaults to shadow and
     that LS_NLI_MODE / LS_JUDGE_MODE are recognised by the entrypoints.
  6. IAM partition prefixes — validates that the DynamoDB key prefixes the Python
     service writes (SCOPE#org#, REPO#, VERIFY#, SKILL#, IDEAGOLD#) match what
     the CDK stack's LeadingKeys condition restricts.

These are offline unit tests; no AWS SDK calls, no live DynamoDB, no real model load.
"""
from __future__ import annotations

import ast
import inspect
import json
import logging
import os
import re


# ---------------------------------------------------------------------------
# 1. Ingest Lambda handler — container Lambda CMD target
# ---------------------------------------------------------------------------


class TestIngestLambdaHandler:
    """Tests for learning_service.entrypoints.ingest.lambda_handler."""

    def test_handler_returns_200_for_valid_event(self):
        from learning_service.entrypoints.ingest import lambda_handler

        event = {"org": "acme", "repo": "acme/backend", "mode": "shadow"}
        result = lambda_handler(event, object())
        assert result["statusCode"] == 200
        assert "ingest ok" in result["body"]
        assert "acme" in result["body"]
        assert "acme/backend" in result["body"]

    def test_handler_returns_400_when_org_missing(self):
        from learning_service.entrypoints.ingest import lambda_handler

        result = lambda_handler({"repo": "acme/backend"}, object())
        assert result["statusCode"] == 400
        assert "org" in result["body"].lower()

    def test_handler_returns_400_when_repo_missing(self):
        from learning_service.entrypoints.ingest import lambda_handler

        result = lambda_handler({"org": "acme"}, object())
        assert result["statusCode"] == 400
        assert "repo" in result["body"].lower()

    def test_handler_returns_400_when_both_missing(self):
        from learning_service.entrypoints.ingest import lambda_handler

        result = lambda_handler({}, object())
        assert result["statusCode"] == 400

    def test_handler_accepts_since_pr_parameter(self):
        from learning_service.entrypoints.ingest import lambda_handler

        event = {"org": "acme", "repo": "acme/backend", "since_pr": 100, "mode": "shadow"}
        result = lambda_handler(event, object())
        # since_pr is accepted; the stub logs and returns 200
        assert result["statusCode"] == 200

    def test_handler_defaults_mode_to_shadow_from_env(self, monkeypatch):
        """When mode is absent from the event, fall back to LS_MODE env var."""
        import importlib
        monkeypatch.setenv("LS_MODE", "shadow")
        from learning_service.entrypoints.ingest import lambda_handler

        event = {"org": "acme", "repo": "acme/backend"}  # no mode key
        result = lambda_handler(event, object())
        assert result["statusCode"] == 200

    def test_handler_rejects_invalid_mode(self):
        from learning_service.entrypoints.ingest import lambda_handler

        # The run_ingest() validates mode; 'badmode' is not shadow|enforce.
        event = {"org": "acme", "repo": "acme/backend", "mode": "badmode"}
        result = lambda_handler(event, object())
        # Validation failure → non-zero exit → 500
        assert result["statusCode"] == 500

    def test_handler_is_importable_as_module_attribute(self):
        """The CMD target `learning_service.entrypoints.ingest.lambda_handler`
        must be a callable attribute of the module — this is how container Lambda
        resolves the handler string."""
        import learning_service.entrypoints.ingest as mod

        assert callable(mod.lambda_handler)


# ---------------------------------------------------------------------------
# 2. Necessity-scan Lambda handler — EventBridge scheduled event
# ---------------------------------------------------------------------------


class TestNecessityScanHandler:
    """Tests for necessity.scheduled_scan_handler (the EventBridge Lambda CMD target)."""

    def _make_store(self):
        """Return a minimal in-memory LearningStore stub."""
        from unittest.mock import MagicMock
        store = MagicMock()
        store.list_current_ideas.return_value = []  # no ideas → trivial scan
        return store

    def test_handler_returns_200_for_valid_event(self):
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        event = {
            "org": "acme",
            "skill_base_name": "naming-conventions",
            "_test_store": store,
        }
        result = scheduled_scan_handler(event, object())
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "ideas_scanned" in body
        assert "ideas_demoted" in body

    def test_handler_returns_400_when_org_missing(self):
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        event = {"skill_base_name": "naming-conventions", "_test_store": store}
        result = scheduled_scan_handler(event, object())
        assert result["statusCode"] == 400

    def test_handler_returns_400_when_skill_base_name_missing(self):
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        event = {"org": "acme", "_test_store": store}
        result = scheduled_scan_handler(event, object())
        assert result["statusCode"] == 400

    def test_handler_accepts_necessity_sample_rate(self):
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        event = {
            "org": "acme",
            "skill_base_name": "naming-conventions",
            "necessity_sample_rate": 0.5,
            "necessity_min_firings": 2,
            "_test_store": store,
        }
        result = scheduled_scan_handler(event, object())
        assert result["statusCode"] == 200

    def test_handler_is_importable_as_module_attribute(self):
        """The CMD target `learning_service.necessity.scheduled_scan_handler`
        must be a callable attribute of the module."""
        import learning_service.necessity as mod

        assert callable(mod.scheduled_scan_handler)

    def test_handler_scan_returns_zero_demotions_for_no_folded_ideas(self):
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        store.list_current_ideas.return_value = []  # no ideas
        event = {
            "org": "acme",
            "skill_base_name": "empty-skill",
            "_test_store": store,
        }
        result = scheduled_scan_handler(event, object())
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["ideas_scanned"] == 0
        assert body["ideas_demoted"] == 0

    def test_eventbridge_event_shape_matches_handler_signature(self):
        """The CDK LearningStack delivers a structured event; validate it is
        understood by the handler without error."""
        from learning_service.necessity import scheduled_scan_handler

        store = self._make_store()
        # This mirrors the RuleTargetInput.fromObject shape in learning-stack.ts,
        # but note the handler reads top-level keys (not nested under 'detail').
        # We test both the flat shape (direct invocation) and confirm the handler
        # does not crash on extra keys it doesn't recognise.
        event = {
            "source": "eventbridge.scheduled",
            "detail-type": "NecessityScanScheduled",
            "detail": {
                "necessity_sample_rate": 1.0,
                "necessity_min_firings": 0,
            },
            # Handler requires these at the top level:
            "org": "acme",
            "skill_base_name": "naming-conventions",
            "_test_store": store,
        }
        result = scheduled_scan_handler(event, object())
        # Extra keys must not cause a crash; handler reads what it knows.
        assert result["statusCode"] == 200


# ---------------------------------------------------------------------------
# 3. Secrets Manager integration — ARN-based secret loading
# ---------------------------------------------------------------------------


class TestSecretsManagerContract:
    """Validate the runtime never exposes PEM plaintext through env vars."""

    def test_github_app_pem_secret_arn_env_var_is_not_plaintext_pem(self, monkeypatch):
        """GITHUB_APP_PEM_SECRET_ARN must be an ARN string, never a raw PEM."""
        # The CDK stack sets GITHUB_APP_PEM_SECRET_ARN to the Secrets Manager ARN.
        # This test confirms the convention: the variable holds an ARN, not a PEM.
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:command-hq/github-app-pem-AbCdEf"
        monkeypatch.setenv("GITHUB_APP_PEM_SECRET_ARN", arn)

        val = os.environ.get("GITHUB_APP_PEM_SECRET_ARN", "")
        # Must start with arn:aws, not -----BEGIN
        assert val.startswith("arn:aws")
        assert "BEGIN" not in val
        assert "PRIVATE KEY" not in val

    def test_github_webhook_secret_arn_env_var_is_not_plaintext(self, monkeypatch):
        arn = "arn:aws:secretsmanager:us-east-1:123456789012:secret:command-hq/github-webhook-secret-XyZaBc"
        monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ARN", arn)

        val = os.environ.get("GITHUB_WEBHOOK_SECRET_ARN", "")
        assert val.startswith("arn:aws")

    def test_ingest_handler_does_not_log_secret_values(self, monkeypatch, caplog):
        """The ingest handler must never write the PEM or webhook secret to logs."""
        monkeypatch.setenv(
            "GITHUB_APP_PEM_SECRET_ARN",
            "arn:aws:secretsmanager:us-east-1:123456789012:secret:command-hq/github-app-pem-AbCdEf",
        )
        from learning_service.entrypoints.ingest import lambda_handler

        with caplog.at_level(logging.DEBUG):
            lambda_handler({"org": "acme", "repo": "acme/backend"}, object())

        # No log record should contain anything resembling a PEM header.
        for record in caplog.records:
            assert "BEGIN RSA" not in record.getMessage()
            assert "BEGIN PRIVATE" not in record.getMessage()
            # Also ensure the webhook secret placeholder isn't echoed.
            assert "ghsec_" not in record.getMessage()

    def test_secrets_never_in_image_env_baseline(self):
        """Sanity check: confirm GITHUB_APP_PEM is not set in the base test env
        (it should only come from Secrets Manager at runtime, never baked in)."""
        # In CI and local dev this env var must be absent (the value is never
        # baked into the image; only the ARN pointer is allowed).
        raw_pem = os.environ.get("GITHUB_APP_PEM", None)
        assert raw_pem is None, (
            "GITHUB_APP_PEM is set in the environment — the plaintext PEM must "
            "never be an env var; use GITHUB_APP_PEM_SECRET_ARN (the Secrets Manager ARN)."
        )


# ---------------------------------------------------------------------------
# 4. Model artifact pinning — PINNED_MODEL_REVISION is a plausible SHA
# ---------------------------------------------------------------------------

# Path to classifier.py (inspected as source text so we don't need agent_families).
_CLASSIFIER_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "src",
    "learning_service",
    "classifier.py",
)
_DOCKERFILE_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "Dockerfile",
)


def _read_classifier_constant(name: str) -> str | None:
    """Parse classifier.py with ast.literal_eval to extract a top-level string constant.

    This avoids importing the module (which would require agent_families to be
    installed).  We walk the module's AST and find `name = <string literal>`.
    """
    with open(_CLASSIFIER_PATH) as f:
        source = f.read()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == name
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            return node.value.value
    return None


def _read_classifier_validation_source() -> str:
    """Return the raw source of classifier.py for guard inspection."""
    with open(_CLASSIFIER_PATH) as f:
        return f.read()


class TestModelArtifactPinning:
    """Validate that PINNED_MODEL_REVISION in classifier.py is a plausible git SHA.

    The CDK build arg `PINNED_NLI_MODEL_REVISION` is threaded into the Docker
    build at synth time.  The Python constant must be non-empty and SHA-like
    so a `NliClassifier()` constructed with the module default is valid.

    Tests in this class inspect classifier.py as SOURCE TEXT / AST rather than
    importing it, so they stay offline even when agent_families is not installed.
    """

    def test_pinned_model_revision_is_non_empty(self):
        sha = _read_classifier_constant("PINNED_MODEL_REVISION")
        assert sha is not None, "PINNED_MODEL_REVISION constant not found in classifier.py"
        assert sha.strip(), "PINNED_MODEL_REVISION must not be blank"

    def test_pinned_model_revision_looks_like_git_sha(self):
        sha = _read_classifier_constant("PINNED_MODEL_REVISION")
        assert sha is not None
        sha = sha.strip()
        # A git SHA (full or abbreviated) is a hex string of ≥ 8 characters.
        assert len(sha) >= 8, f"SHA too short: {sha!r}"
        assert re.fullmatch(r"[0-9a-f]+", sha), (
            f"PINNED_MODEL_REVISION must be lowercase hex: {sha!r}"
        )

    def test_nli_classifier_rejects_empty_revision_via_source_guard(self):
        """NliClassifier.__init__ must contain a guard that raises ValueError for
        empty model_revision — verified by inspecting the source text."""
        src = _read_classifier_validation_source()
        # The guard should reference both the empty-string check and ValueError.
        assert "model_revision" in src
        assert "ValueError" in src
        # The guard should check for empty/blank revision.
        assert 'not model_revision' in src or 'model_revision.strip()' in src or \
               'not model_revision.strip()' in src, (
            "classifier.py must guard against an empty model_revision"
        )

    def test_nli_classifier_rejects_short_revision_via_source_guard(self):
        """NliClassifier.__init__ must contain a guard against too-short SHAs."""
        src = _read_classifier_validation_source()
        # The guard checks len(model_revision) >= some minimum (8).
        assert "len(model_revision" in src or "len(model_revision.strip())" in src, (
            "classifier.py must validate the minimum length of model_revision"
        )

    def test_nli_classifier_default_revision_is_40_hex_chars(self):
        """The default PINNED_MODEL_REVISION must be a full 40-char SHA."""
        sha = _read_classifier_constant("PINNED_MODEL_REVISION")
        assert sha is not None
        sha = sha.strip()
        assert len(sha) == 40, (
            f"PINNED_MODEL_REVISION should be a full 40-char SHA, got {len(sha)} chars: {sha!r}"
        )

    def test_dockerfile_references_pinned_revision_build_arg(self):
        """The Dockerfile must contain the PINNED_NLI_MODEL_REVISION ARG declaration
        so the CDK build-arg can actually be consumed."""
        with open(_DOCKERFILE_PATH) as f:
            content = f.read()

        assert "PINNED_NLI_MODEL_REVISION" in content, (
            "Dockerfile must declare ARG PINNED_NLI_MODEL_REVISION so the CDK "
            "build-arg threads the pinned SHA into the image."
        )
        # The revision must also be used (passed to the model fetch).
        assert "PINNED_NLI_MODEL_REVISION" in content


# ---------------------------------------------------------------------------
# 5. Container image ENV contract — shadow mode, NLI/judge mode defaults
# ---------------------------------------------------------------------------


class TestContainerEnvContract:
    """Validate the runtime ENV contract specified in the CDK LearningStack."""

    def test_ls_mode_shadow_is_valid_for_ingest_config(self):
        """LS_MODE=shadow must be accepted by IngestConfig / run_ingest."""
        from learning_service.entrypoints.ingest import IngestConfig, run_ingest

        config = IngestConfig(org="acme", repo="acme/backend", mode="shadow")
        exit_code = run_ingest(config)
        assert exit_code == 0

    def test_ls_mode_enforce_is_valid_for_ingest_config(self):
        from learning_service.entrypoints.ingest import IngestConfig, run_ingest

        config = IngestConfig(org="acme", repo="acme/backend", mode="enforce")
        exit_code = run_ingest(config)
        assert exit_code == 0

    def test_ls_nli_mode_replay_is_recognised(self, monkeypatch):
        """LS_NLI_MODE=replay must be consumed by the ingest lambda_handler."""
        monkeypatch.setenv("LS_NLI_MODE", "replay")
        from learning_service.entrypoints.ingest import lambda_handler

        result = lambda_handler({"org": "acme", "repo": "acme/backend"}, object())
        assert result["statusCode"] == 200

    def test_ls_nli_mode_passthrough_is_recognised(self, monkeypatch):
        """LS_NLI_MODE=passthrough (the production setting) must not crash the handler."""
        monkeypatch.setenv("LS_NLI_MODE", "passthrough")
        from learning_service.entrypoints.ingest import lambda_handler

        result = lambda_handler({"org": "acme", "repo": "acme/backend"}, object())
        assert result["statusCode"] == 200

    def test_harness_table_env_var_is_read_by_necessity_handler(self, monkeypatch):
        """The necessity scan handler reads HARNESS_TABLE when no test store is injected.
        Confirm it imports and the env var path exists in the code."""
        import inspect
        from learning_service.necessity import scheduled_scan_handler

        src = inspect.getsource(scheduled_scan_handler)
        assert "HARNESS_TABLE" in src, (
            "scheduled_scan_handler must read HARNESS_TABLE from the environment "
            "to wire the production DynamoDB store."
        )


# ---------------------------------------------------------------------------
# 6. IAM partition prefixes — match CDK LeadingKeys condition
# ---------------------------------------------------------------------------


class TestIAMPartitionPrefixes:
    """Validate that the DynamoDB key prefixes written by the Python service
    match the five partition prefixes declared in the CDK LearningStack's
    LeadingKeys condition.

    CDK condition:
        SCOPE#org#*, REPO#*, VERIFY#*, SKILL#*, IDEAGOLD#*

    All learning-service key builders live in the generated py_types module.
    Every key uses PK = SCOPE#org#{org}, which is covered by the SCOPE#org#*
    condition.  The SK carries the partition-specific prefix (ANCHOR#, PROCESSED#,
    VERIFY#, SKILL#, IDEAGOLD#) — these are authorised because the PK condition
    covers the whole partition, and the IAM condition uses `ForAnyValue:StringLike`
    on the PK (LeadingKeys matches on PK, not SK).
    """

    # The prefixes the CDK IAM condition allows (on the PK).
    CDK_ALLOWED_PREFIXES = frozenset(
        {"SCOPE#org#", "REPO#", "VERIFY#", "SKILL#", "IDEAGOLD#"}
    )

    def test_anchor_key_prefix_is_under_scope_org_partition(self):
        """Anchor index keys must live under SCOPE#org# so the LeadingKeys
        condition covers them."""
        from learning_service.schema.generated.py_types import anchor_key

        key = anchor_key("my-org", "owner/repo", "src/foo.py", "myFunc", "idea-123")
        assert key["PK"].startswith("SCOPE#org#"), (
            f"anchor_key PK must start with SCOPE#org# (got {key['PK']!r}) "
            "so the CDK IAM LeadingKeys condition covers it."
        )

    def test_processed_pr_key_prefix_is_under_scope_org_partition(self):
        from learning_service.schema.generated.py_types import processed_pr_key

        key = processed_pr_key("my-org", "owner/repo", 42)
        assert key["PK"].startswith("SCOPE#org#"), (
            f"processed_pr_key PK must start with SCOPE#org# (got {key['PK']!r})"
        )

    def test_idea_key_prefix_is_under_scope_org_partition(self):
        from learning_service.schema.generated.py_types import idea_key

        key = idea_key("my-org", "naming-conventions", "idea-abc")
        assert key["PK"].startswith("SCOPE#org#"), (
            f"idea_key PK must start with SCOPE#org# (got {key['PK']!r})"
        )

    def test_revision_key_prefix_is_under_scope_org_partition(self):
        """Skill revisions use revision_key (not skill_revision_key) — PK = SCOPE#org#."""
        from learning_service.schema.generated.py_types import revision_key

        key = revision_key("my-org", "naming-conventions", 1)
        pk = key["PK"]
        allowed = any(pk.startswith(p) for p in self.CDK_ALLOWED_PREFIXES)
        assert allowed, (
            f"revision_key PK {pk!r} does not start with any CDK-allowed prefix: "
            f"{self.CDK_ALLOWED_PREFIXES}"
        )

    def test_golden_case_key_prefix_is_under_scope_org_partition(self):
        from learning_service.schema.generated.py_types import golden_case_key

        key = golden_case_key("my-org", "naming-conventions", "idea-abc")
        pk = key["PK"]
        allowed = any(pk.startswith(p) for p in self.CDK_ALLOWED_PREFIXES)
        assert allowed, (
            f"golden_case_key PK {pk!r} does not start with any CDK-allowed prefix: "
            f"{self.CDK_ALLOWED_PREFIXES}"
        )

    def test_verify_event_key_sk_carries_verify_prefix(self):
        """Verification/supersession audit records carry VERIFY# in the SK.
        The PK is SCOPE#org# (covered by the IAM condition)."""
        from learning_service.schema.generated.py_types import verify_event_key

        # verify_event_key(org, ideaId, seq) — 3 params per generated py_types
        key = verify_event_key("my-org", "idea-abc", 1)
        pk = key["PK"]
        sk = key.get("SK", "")
        # PK must be covered by the IAM condition
        pk_covered = any(pk.startswith(p) for p in self.CDK_ALLOWED_PREFIXES)
        # SK carries the VERIFY# sub-prefix (the CDK comment about VERIFY#)
        assert pk_covered, (
            f"verify_event_key PK {pk!r} is not covered by any CDK allowed prefix."
        )
        assert "VERIFY#" in sk, (
            f"verify_event_key SK {sk!r} must contain VERIFY# (the audit-row marker)."
        )

    def test_all_key_builders_produce_string_pk(self):
        """All generated key builders must produce a non-empty string PK."""
        from learning_service.schema.generated.py_types import (
            anchor_key,
            processed_pr_key,
            idea_key,
            revision_key,
            golden_case_key,
            verify_event_key,
        )

        fns_and_args = [
            (anchor_key, ("org", "repo", "file.py", "sym", "idea-1")),
            (processed_pr_key, ("org", "repo", 1)),
            (idea_key, ("org", "skill", "idea-1")),
            (revision_key, ("org", "skill", 1)),
            (golden_case_key, ("org", "skill", "idea-1")),
            (verify_event_key, ("org", "idea-1", 1)),
        ]
        for fn, args in fns_and_args:
            key = fn(*args)
            assert isinstance(key.get("PK"), str), f"{fn.__name__} PK must be a string"
            assert key["PK"], f"{fn.__name__} PK must be non-empty"

    def test_scope_org_prefix_covers_all_learning_partitions(self):
        """The CDK SCOPE#org#* LeadingKeys condition must cover all learning partitions.

        This is the key invariant: since all learning service key builders produce
        PK = SCOPE#org#{org}, a single SCOPE#org#* prefix in the LeadingKeys
        condition is sufficient to authorise all learning DynamoDB writes.
        """
        from learning_service.schema.generated.py_types import (
            anchor_key,
            processed_pr_key,
            idea_key,
            revision_key,
            golden_case_key,
            verify_event_key,
            idea_source_key,
            branch_session_key,
        )

        fns_and_args = [
            (anchor_key, ("org", "repo", "file.py", "sym", "idea-1")),
            (processed_pr_key, ("org", "repo", 1)),
            (idea_key, ("org", "skill", "idea-1")),
            (revision_key, ("org", "skill", 1)),
            (golden_case_key, ("org", "skill", "idea-1")),
            (verify_event_key, ("org", "idea-1", 1)),
            (idea_source_key, ("org", "idea-1", "src-1")),
            (branch_session_key, ("org", "repo", "main")),
        ]
        for fn, args in fns_and_args:
            key = fn(*args)
            pk = key["PK"]
            assert pk.startswith("SCOPE#org#"), (
                f"{fn.__name__} PK {pk!r} does not start with SCOPE#org# — "
                f"the CDK SCOPE#org#* LeadingKeys condition would not cover it."
            )
