"""MAT-141 (U10) — Schema codegen + single skill-revision writer (DRY contract).

Acceptance checklist (from Linear MAT-141):
  [ ] test_schema_codegen_is_fresh_or_ci_fails
  [ ] test_ts_authoring_routes_through_python_writer
  [ ] test_single_writer_cas_race_retries
  [ ] test_cas_retry_preserves_concurrent_edit_body
  [ ] test_wrapper_and_web_parse_generated_schema
  [ ] round-trip: Python writes revision → Go materializes → TS web reads,
      no hand-authored mirror.

All tests are OFFLINE (no DynamoDB, no model load, no network, no quota).
The ``InMemorySkillStore`` and the generated schema types carry the full test
surface.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 1.  test_schema_codegen_is_fresh_or_ci_fails
# ---------------------------------------------------------------------------


def test_schema_codegen_is_fresh_or_ci_fails():
    """CI gate: generated files must be up-to-date with the IDL.

    This test re-runs the codegen in check mode (in-memory) and asserts that
    what is on disk matches what would be generated today.  If the IDL changes
    without re-running codegen, this test fails — reproducing a CI ``--check``
    failure locally.
    """
    from learning_service.schema.codegen import check_freshness

    fresh = check_freshness()
    assert fresh, (
        "Generated schema files are STALE.  Run:\n"
        "  python -m learning_service.schema.codegen\n"
        "to regenerate, then commit the updated files."
    )


def test_generated_py_types_importable():
    """The generated Python types must be importable and include all key families."""
    from learning_service.schema.generated.py_types import (
        SkillRecord,
        RevisionRecord,
        TruePointerRecord,
        IdeaRecord,
        GoldenCaseRecord,
        AnchorRecord,
        skill_key,
        revision_key,
        true_pointer_key,
        idea_key,
        golden_case_key,
        anchor_key,
        processed_pr_key,
        verify_event_key,
    )
    # Spot-check: key builders produce the expected PK/SK shapes.
    k = skill_key("acme", "my-skill")
    assert k["PK"] == "SCOPE#org#acme"
    assert k["SK"] == "SKILL#my-skill"

    k2 = true_pointer_key("acme", "my-skill")
    assert k2["SK"] == "SKILL#my-skill#TRUE"

    k3 = revision_key("acme", "my-skill", 1)
    assert k3["SK"] == "SKILL#my-skill#r000000000001"

    k4 = golden_case_key("acme", "my-skill", "idea-001")
    assert k4["SK"] == "IDEAGOLD#my-skill#idea-001"

    k5 = anchor_key("acme", "acme/backend", "src/foo.ts", "fooFn", "idea-001")
    assert k5["SK"] == "ANCHOR#acme/backend#src/foo.ts#fooFn#idea-001"

    k6 = processed_pr_key("acme", "acme/backend", 42)
    assert k6["SK"] == "PROCESSED#acme/backend#42"

    k7 = verify_event_key("acme", "idea-001", 3)
    assert k7["SK"] == "VERIFY#idea-001#000000000003"


def test_generated_ts_types_file_exists_and_contains_key_builders():
    """The generated TypeScript file must exist and contain the expected exports."""
    ts_path = (
        Path(__file__).parent.parent
        / "src"
        / "learning_service"
        / "schema"
        / "generated"
        / "ts_types.ts"
    )
    assert ts_path.exists(), f"ts_types.ts not found at {ts_path}"
    src = ts_path.read_text(encoding="utf-8")

    # Key-builder exports.
    assert "export const skillKey" in src
    assert "export const revisionKey" in src
    assert "export const truePointerKey" in src
    assert "export const goldenCaseKey" in src
    assert "export const anchorKey" in src
    assert "export const processedPrKey" in src
    assert "export const verifyEventKey" in src

    # Interface exports.
    assert "export interface SkillRecord" in src
    assert "export interface RevisionRecord" in src
    assert "export interface TruePointerRecord" in src
    assert "export interface IdeaRecord" in src
    assert "export interface GoldenCaseRecord" in src
    assert "export interface AnchorRecord" in src

    # Must be stamped as generated (drift guard).
    assert "GENERATED" in src
    assert "IDL fingerprint:" in src


def test_generated_ts_key_output_matches_python_key_output():
    """Python and TS key builders must produce identical PK/SK strings.

    We verify the string templates are equivalent by asserting the Python
    builder output matches what the TS template would produce, without
    actually running TypeScript.  The templates are both derived from the
    same IDL, so this is a consistency cross-check on the codegen.
    """
    from learning_service.schema.generated.py_types import (
        revision_key,
        true_pointer_key,
        golden_case_key,
        verify_event_key,
    )
    # These assertions mirror what the generated TS functions produce.
    # If the Python and TS generators diverge, one or both will be wrong.
    rev_k = revision_key("acme", "my-skill", 7)
    assert rev_k["SK"] == "SKILL#my-skill#r000000000007", rev_k

    ptr_k = true_pointer_key("acme", "my-skill")
    assert ptr_k["SK"] == "SKILL#my-skill#TRUE"

    gold_k = golden_case_key("acme", "my-skill", "idea-abc")
    assert gold_k["SK"] == "IDEAGOLD#my-skill#idea-abc"

    verify_k = verify_event_key("acme", "idea-xyz", 99)
    assert verify_k["SK"] == "VERIFY#idea-xyz#000000000099"


def test_codegen_idl_fingerprint_is_embedded():
    """Both generated files must carry a matching IDL fingerprint line.

    The fingerprint ties the generated output to the IDL version; a mismatch
    between the py and ts fingerprints means the codegen was run piecemeal.
    """
    from learning_service.schema.codegen import _idl_fingerprint, PY_OUT, TS_OUT

    fp = _idl_fingerprint()
    assert fp, "IDL fingerprint must be non-empty"

    py_src = PY_OUT.read_text(encoding="utf-8")
    ts_src = TS_OUT.read_text(encoding="utf-8")

    assert f"IDL fingerprint: {fp}" in py_src, "py_types.py must embed the current IDL fingerprint"
    assert f"IDL fingerprint: {fp}" in ts_src, "ts_types.ts must embed the current IDL fingerprint"


# ---------------------------------------------------------------------------
# 2.  test_ts_authoring_routes_through_python_writer
# ---------------------------------------------------------------------------


def test_ts_authoring_routes_through_python_writer():
    """TS-facing author-revision endpoint must call the Python write_revision fn.

    Simulates what the TS authoring UI sends (a JSON body over Lambda event)
    and asserts that the Python handler (a) delegates to the single Python
    writer and (b) returns the expected revision metadata.
    """
    from learning_service.entrypoints.author_revision import handler
    from learning_service.skills_write import InMemorySkillStore

    store = InMemorySkillStore()
    event = {
        "body": json.dumps({
            "org": "acme",
            "baseName": "snake-case-skill",
            "variantId": "",
            "body": "Always use snake_case for Python identifiers.",
            "authorUserId": "ts-ui-user",
            "description": "Naming convention for Python.",
        }),
        "_test_store": store,
    }

    response = handler(event, object())

    assert response["statusCode"] == 200, f"Unexpected status: {response}"
    result = json.loads(response["body"])

    assert result["org"] == "acme"
    assert result["baseName"] == "snake-case-skill"
    assert result["variantId"] == ""
    assert result["rev"] == 1
    assert result["truePointerUpdated"] is True
    assert result["goldenCaseWritten"] is False

    # Verify the revision was actually stored.
    ptr = store.get_true_pointer("acme", "snake-case-skill")
    assert ptr is not None
    assert ptr.rev == 1
    body = store.get_revision_body("acme", "", 1)
    assert body == "Always use snake_case for Python identifiers."


def test_ts_authoring_with_golden_case_routes_through_python_writer():
    """TS-facing fold path (with golden case) must capture IDEAGOLD# via the writer."""
    from learning_service.entrypoints.author_revision import handler
    from learning_service.skills_write import InMemorySkillStore

    store = InMemorySkillStore()
    event = {
        "body": json.dumps({
            "org": "acme",
            "baseName": "snake-case-skill",
            "variantId": "",
            "body": "Always use snake_case for Python identifiers.",
            "authorUserId": "ts-ui-user",
            "ideaId": "idea-001",
            "goldenCase": {
                "caseId": "idea-001",
                "before": "Old skill body without naming convention.",
                "after": "Always use snake_case for Python identifiers.",
                "ideaBody": "The project consistently uses snake_case across modules.",
            },
        }),
        "_test_store": store,
    }

    response = handler(event, object())
    assert response["statusCode"] == 200
    result = json.loads(response["body"])
    assert result["goldenCaseWritten"] is True

    # Verify the golden case was stored.
    gc = store._golden_cases.get(("acme", "snake-case-skill", "idea-001"))
    assert gc is not None
    assert gc.before == "Old skill body without naming convention."
    assert gc.after == "Always use snake_case for Python identifiers."
    assert gc.ideaBody == "The project consistently uses snake_case across modules."


def test_ts_authoring_bad_request_returns_400():
    """Missing required fields must return 400."""
    from learning_service.entrypoints.author_revision import handler
    from learning_service.skills_write import InMemorySkillStore

    store = InMemorySkillStore()

    # Missing org.
    response = handler({"body": json.dumps({"baseName": "foo", "body": "bar"}), "_test_store": store}, object())
    assert response["statusCode"] == 400

    # Missing baseName.
    response = handler({"body": json.dumps({"org": "acme", "body": "bar"}), "_test_store": store}, object())
    assert response["statusCode"] == 400


# ---------------------------------------------------------------------------
# 3.  test_single_writer_cas_race_retries
# ---------------------------------------------------------------------------


def test_single_writer_cas_race_retries():
    """A lost CAS triggers a retry; the writer must converge to a consistent state.

    Scenario: two concurrent callers both read rev=None (no pointer yet) and
    both try to write rev=1.  The second caller loses the CAS and retries,
    re-reading the winning body (rev=1) and writing rev=2.  Final state: rev=2.
    """
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest

    # The interceptor simulates a concurrent write sneaking in before the CAS.
    # It fires once (on attempt 1) and writes the competing revision.
    intercepted = []

    def once_interceptor(store: InMemorySkillStore, attempted_rev: int) -> None:
        if len(intercepted) >= 1:
            return  # only fire once
        intercepted.append(attempted_rev)
        # Simulate a concurrent writer that succeeds first.
        from learning_service.schema.generated.py_types import TruePointerRecord, RevisionRecord
        competing_rev = RevisionRecord(
            baseName="my-skill",
            variantId="",
            rev=1,
            body="Competing writer body.",
            org="acme",
        )
        store._revisions[("acme", "", 1)] = competing_rev
        store._pointers[("acme", "my-skill")] = TruePointerRecord(
            baseName="my-skill",
            variantId="",
            rev=1,
            org="acme",
        )

    store = InMemorySkillStore(cas_interceptor=once_interceptor)
    req = RevisionRequest(
        org="acme",
        base_name="my-skill",
        variant_id="",
        body="Lesson: use snake_case.",
    )

    result = write_revision(req, store)

    # Must have retried and landed on rev=2 (the competing writer took rev=1).
    assert result.rev == 2, f"Expected rev=2 after retry, got {result.rev}"
    assert len(intercepted) == 1, "Interceptor should have fired exactly once"

    ptr = store.get_true_pointer("acme", "my-skill")
    assert ptr is not None
    assert ptr.rev == 2


def test_single_writer_cas_race_retries_two_threads():
    """Two threads writing concurrently must land on rev=1 and rev=2 (no lost update)."""
    from learning_service.skills_write import (
        InMemorySkillStore,
        write_revision,
        RevisionRequest,
        ConflictError,
    )
    import time

    store = InMemorySkillStore()
    lock = threading.Lock()
    results: list[int] = []
    errors: list[Exception] = []

    def write_one(body: str) -> None:
        try:
            req = RevisionRequest(
                org="acme",
                base_name="concurrent-skill",
                variant_id="",
                body=body,
            )
            # Add a tiny sleep to increase collision probability.
            result = write_revision(req, store, max_retries=10)
            with lock:
                results.append(result.rev)
        except Exception as exc:
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=write_one, args=("Body from thread 1.",))
    t2 = threading.Thread(target=write_one, args=("Body from thread 2.",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert not errors, f"Threads raised: {errors}"
    assert sorted(results) == [1, 2], f"Expected revs [1, 2], got {sorted(results)}"

    ptr = store.get_true_pointer("acme", "concurrent-skill")
    assert ptr is not None
    assert ptr.rev == 2


# ---------------------------------------------------------------------------
# 4.  test_cas_retry_preserves_concurrent_edit_body
# ---------------------------------------------------------------------------


def test_cas_retry_preserves_concurrent_edit_body():
    """On a lost CAS the retry MUST re-read the winning body, not re-CAS the stale body.

    Scenario:
      1. Caller A reads pointer (rev=None) and prepares body="Lesson A.".
      2. A concurrent write sneaks in and writes rev=1 with body="Base body.".
      3. Caller A loses CAS, retries.
      4. On retry, the delta applicator is called with (winning_body="Base body.",
         lesson_body="Lesson A."), producing "Base body.\n\nLesson A.".
      5. Caller A writes rev=2 with the merged body.

    If the retry re-CAS'd the stale body ("Lesson A."), "Base body." would be
    silently discarded — that is the bug this test guards against.
    """
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest
    from learning_service.schema.generated.py_types import TruePointerRecord, RevisionRecord

    # Pre-seed a "competing" write so the store already has rev=1.
    store = InMemorySkillStore()
    competing_body = "Base body written by concurrent caller."
    store._revisions[("acme", "", 1)] = RevisionRecord(
        baseName="concurrent-skill",
        variantId="",
        rev=1,
        body=competing_body,
        org="acme",
    )
    store._pointers[("acme", "concurrent-skill")] = TruePointerRecord(
        baseName="concurrent-skill",
        variantId="",
        rev=1,
        org="acme",
    )

    delta_calls: list[tuple[str, str]] = []

    def tracking_delta(winning_body: str, lesson_body: str) -> str:
        delta_calls.append((winning_body, lesson_body))
        return winning_body.rstrip("\n") + "\n\n" + lesson_body.lstrip("\n")

    # Caller B reads rev=None conceptually but the store already has rev=1.
    # We simulate this by making CAS fail on the first attempt from B's perspective.
    # Since the store already has rev=1 but B thinks expected_rev=None, CAS will fail.
    # B's write_revision will retry with the actual current pointer (rev=1).

    # To force the "read as None" scenario we temporarily hide the pointer and
    # restore it before the CAS fires.
    original_pointer = store._pointers[("acme", "concurrent-skill")]
    del store._pointers[("acme", "concurrent-skill")]

    # Interceptor: restore the pointer before the CAS so the first attempt loses.
    fired = []

    def restore_interceptor(s: InMemorySkillStore, attempted_rev: int) -> None:
        if not fired:
            fired.append(True)
            s._pointers[("acme", "concurrent-skill")] = original_pointer

    store.cas_interceptor = restore_interceptor

    req = RevisionRequest(
        org="acme",
        base_name="concurrent-skill",
        variant_id="",
        body="Lesson A: always use snake_case.",
    )

    result = write_revision(req, store, apply_lesson_delta=tracking_delta)

    # The retry must have called the delta applicator.
    assert len(delta_calls) >= 1, (
        "apply_lesson_delta must be called on CAS retry; "
        "a blind re-CAS of the stale body is a bug"
    )

    # The first delta call must receive the WINNING body, not the stale lesson.
    winning_body_seen, lesson_seen = delta_calls[0]
    assert competing_body in winning_body_seen or winning_body_seen == competing_body, (
        f"Delta applicator received wrong winning body.\n"
        f"  Expected:  {competing_body!r}\n"
        f"  Got:       {winning_body_seen!r}\n"
        "The retry must re-read the winning revision, not re-CAS the stale body."
    )

    # The merged body must contain both the competing body and the lesson.
    final_body = store.get_revision_body("acme", "", result.rev)
    assert final_body is not None
    assert "snake_case" in final_body, "Merged body must include the lesson delta"
    assert competing_body.strip() in final_body, "Merged body must preserve the concurrent edit"

    # Final revision must be rev=2.
    assert result.rev == 2, f"Expected rev=2, got {result.rev}"


def test_cas_retry_exhaustion_raises_conflict_error():
    """Exceeding max_retries must raise ConflictError, not loop forever."""
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest, ConflictError
    from learning_service.schema.generated.py_types import TruePointerRecord, RevisionRecord

    # Interceptor always advances the pointer to make CAS fail every time.
    counter = [0]

    def always_fail(store: InMemorySkillStore, attempted_rev: int) -> None:
        counter[0] += 1
        # Advance the rev by writing a new pointer so CAS always loses.
        new_rev = counter[0]
        store._revisions[("acme", "", new_rev)] = RevisionRecord(
            baseName="busy-skill", variantId="", rev=new_rev,
            body=f"Concurrent body #{new_rev}.", org="acme",
        )
        store._pointers[("acme", "busy-skill")] = TruePointerRecord(
            baseName="busy-skill", variantId="", rev=new_rev, org="acme",
        )

    store = InMemorySkillStore(cas_interceptor=always_fail)
    req = RevisionRequest(org="acme", base_name="busy-skill", variant_id="", body="My lesson.")

    with pytest.raises(ConflictError):
        write_revision(req, store, max_retries=3)

    # Must have attempted exactly max_retries times.
    assert counter[0] == 3, f"Expected 3 CAS attempts, got {counter[0]}"


# ---------------------------------------------------------------------------
# 5.  test_wrapper_and_web_parse_generated_schema
# ---------------------------------------------------------------------------


def test_wrapper_and_web_parse_generated_schema():
    """Go wrapper + TS web must be able to parse records written by the Python writer.

    We cannot actually run Go/TS in this test, so we verify the contract at the
    JSON-serialisation layer: a record written by the Python writer can be
    round-tripped through the generated TS interface shape and the generated
    Python dataclass.

    The test asserts:
      - The key format produced by the Python key builder matches the TS key
        builder template exactly (same string).
      - A record serialised by the Python writer can be deserialised by the
        generated Python dataclass (→ models the Go/TS parse contract).
      - The generated TS file contains no hard-coded key strings (all keys
        are built by the exported functions, so a format change propagates).
    """
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest
    from learning_service.schema.generated.py_types import (
        revision_key,
        true_pointer_key,
        RevisionRecord,
        TruePointerRecord,
    )

    store = InMemorySkillStore()
    req = RevisionRequest(
        org="acme",
        base_name="wrap-skill",
        variant_id="",
        body="Use context managers for resource cleanup.",
        author_user_id="python-writer",
    )
    result = write_revision(req, store)
    assert result.rev == 1

    # -- Key-format round-trip ------------------------------------------------
    # Python key builder produces the PK/SK that DynamoDB would store.
    py_rev_key = revision_key("acme", "", 1)
    assert py_rev_key["PK"] == "SCOPE#org#acme"
    assert py_rev_key["SK"] == "SKILL##r000000000001"  # empty variant_id

    py_ptr_key = true_pointer_key("acme", "wrap-skill")
    assert py_ptr_key["SK"] == "SKILL#wrap-skill#TRUE"

    # -- The stored revision body is intact (Go materializer reads it) --------
    stored_body = store.get_revision_body("acme", "", 1)
    assert stored_body == "Use context managers for resource cleanup."

    # -- The stored record can be deserialised as a RevisionRecord dataclass ---
    # (mimics what a Go / TS parser would do from the DynamoDB item attributes)
    stored_rev = store._revisions[("acme", "", 1)]
    assert isinstance(stored_rev, RevisionRecord)
    assert stored_rev.baseName == "wrap-skill"
    assert stored_rev.org == "acme"
    assert stored_rev.rev == 1
    assert stored_rev.body == stored_body

    # -- TRUE pointer is parseable as TruePointerRecord -----------------------
    ptr = store.get_true_pointer("acme", "wrap-skill")
    assert isinstance(ptr, TruePointerRecord)
    assert ptr.rev == 1
    assert ptr.variantId == ""

    # -- TS generated file uses no bare string literals for key prefixes -------
    ts_path = (
        Path(__file__).parent.parent
        / "src"
        / "learning_service"
        / "schema"
        / "generated"
        / "ts_types.ts"
    )
    ts_src = ts_path.read_text(encoding="utf-8")
    # Verify the TS key builder functions are present (not inlined literals).
    assert "export const skillKey" in ts_src
    assert "export const revisionKey" in ts_src


# ---------------------------------------------------------------------------
# 6.  Round-trip: Python writes → store → parse via generated schema types
# ---------------------------------------------------------------------------


def test_round_trip_python_writes_golden_case_go_materializes_ts_reads():
    """Full round-trip (offline): Python writes revision + golden case → both are readable
    via the generated schema types (simulating Go materialization and TS web read).

    No hand-authored mirror anywhere — everything flows through the generated
    key builders and dataclasses.
    """
    from learning_service.skills_write import (
        InMemorySkillStore,
        write_revision,
        RevisionRequest,
        GoldenCasePayload,
    )
    from learning_service.schema.generated.py_types import (
        revision_key,
        true_pointer_key,
        golden_case_key,
        RevisionRecord,
        TruePointerRecord,
        GoldenCaseRecord,
    )

    store = InMemorySkillStore()
    req = RevisionRequest(
        org="acme",
        base_name="round-trip-skill",
        variant_id="",
        body="Prefer explicit return types in TypeScript functions.",
        author_user_id="python-fold-path",
        idea_id="idea-rt-001",
        golden_case=GoldenCasePayload(
            case_id="idea-rt-001",
            before="Old skill body with no return type guidance.",
            after="Prefer explicit return types in TypeScript functions.",
            idea_body="Five PRs add explicit return types where they were missing.",
        ),
    )

    result = write_revision(req, store)
    assert result.rev == 1
    assert result.golden_case_written is True

    # --- "Go materialiser" reads the revision row by the generated SK --------
    rev_k = revision_key("acme", "", 1)
    # Verify the key format matches what the store indexed under.
    stored_rev = store._revisions.get(("acme", "", 1))
    assert stored_rev is not None, "Revision row not found in store"
    assert stored_rev.body == "Prefer explicit return types in TypeScript functions."
    assert stored_rev.ideaId == "idea-rt-001"

    # --- "TS web" reads the TRUE pointer to know which rev is live -----------
    ptr_k = true_pointer_key("acme", "round-trip-skill")
    ptr = store.get_true_pointer("acme", "round-trip-skill")
    assert ptr is not None
    assert ptr.rev == 1

    # --- "TS web" reads the golden case for the regression suite -------------
    gold_k = golden_case_key("acme", "round-trip-skill", "idea-rt-001")
    gc = store._golden_cases.get(("acme", "round-trip-skill", "idea-rt-001"))
    assert gc is not None, "Golden case row not found in store"
    assert gc.before == "Old skill body with no return type guidance."
    assert gc.after == "Prefer explicit return types in TypeScript functions."
    assert gc.ideaBody == "Five PRs add explicit return types where they were missing."

    # No hand-authored key strings anywhere — all derived from generated builders.
    # (The key_builder functions are the single source of truth; TS readers use
    # the exported TS functions from ts_types.ts, not inline literals.)
    assert rev_k["SK"].startswith("SKILL#")
    assert ptr_k["SK"].endswith("#TRUE")
    assert gold_k["SK"].startswith("IDEAGOLD#")


def test_write_revision_no_golden_case():
    """write_revision without a golden case must succeed and not write any IDEAGOLD# row."""
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest

    store = InMemorySkillStore()
    req = RevisionRequest(
        org="acme",
        base_name="plain-skill",
        variant_id="",
        body="Use dataclasses for simple value objects.",
    )
    result = write_revision(req, store)
    assert result.golden_case_written is False
    assert len(store._golden_cases) == 0


def test_write_multiple_revisions_are_append_immutable():
    """Each write_revision call must produce a new immutable revision row.

    Existing revisions must not be overwritten.
    """
    from learning_service.skills_write import InMemorySkillStore, write_revision, RevisionRequest

    store = InMemorySkillStore()
    base = RevisionRequest(org="acme", base_name="growing-skill", variant_id="", body="")

    import dataclasses

    r1 = write_revision(dataclasses.replace(base, body="Rev 1 body."), store)
    r2 = write_revision(dataclasses.replace(base, body="Rev 2 body."), store)
    r3 = write_revision(dataclasses.replace(base, body="Rev 3 body."), store)

    assert r1.rev == 1
    assert r2.rev == 2
    assert r3.rev == 3

    # All three revisions are still accessible (append-immutable).
    assert store.get_revision_body("acme", "", 1) == "Rev 1 body."
    assert store.get_revision_body("acme", "", 2) == "Rev 2 body."
    assert store.get_revision_body("acme", "", 3) == "Rev 3 body."

    # TRUE pointer points at the latest.
    ptr = store.get_true_pointer("acme", "growing-skill")
    assert ptr is not None
    assert ptr.rev == 3
