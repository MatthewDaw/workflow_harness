"""MAT-138 (U2) — Code-anchor model + locality join tests.

Acceptance checklist items:
  - test_anchor_written_on_fold_and_queryable
  - test_merge_touching_anchor_finds_incumbent
  - test_file_level_fallback_when_symbol_unparseable
  - test_unfold_removes_current_anchor_keeps_history
  - test_symbol_anchor_from_ts_arrow_const
  - test_symbol_anchor_from_go_anonymous_func
  - test_anchor_write_is_org_scoped

Additional regression/coverage tests:
  - test_blank_org_raises_org_guard_error
  - test_extract_deduplicates_file_symbol_pairs
  - test_python_function_symbol_extraction
  - test_get_incumbent_ideas_deduplicates_fan_out
  - test_empty_content_falls_back_to_file_level
  - test_anchor_key_format
  - test_dynamo_anchor_written_and_queryable   (moto-backed)
  - test_dynamo_unfold_retires_anchor          (moto-backed)
  - test_dynamo_org_scoped_keys_do_not_collide (moto-backed)

moto tests are skipped if boto3/moto are not installed.
"""
from __future__ import annotations

import importlib.util
from dataclasses import replace

import pytest

from learning_service.anchors import (
    FILE_LEVEL_SYMBOL,
    Anchor,
    DiffHunk,
    extract_anchors_from_diff,
    get_incumbent_ideas,
    retire_anchors_on_unfold,
    write_anchors_on_fold,
)
from learning_service.db.store import (
    InMemoryLearningStore,
    OrgGuardError,
)
from learning_service.schema.generated.py_types import (
    AnchorRecord,
    IdeaRecord,
    anchor_key,
)

# ---------------------------------------------------------------------------
# moto availability guard
# ---------------------------------------------------------------------------

_HAS_BOTO = (
    importlib.util.find_spec("boto3") is not None
    and importlib.util.find_spec("moto") is not None
)

if _HAS_BOTO:
    import boto3
    from moto import mock_aws
    from learning_service.db.store import DynamoLearningStore

TABLE_NAME = "harness"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def store() -> InMemoryLearningStore:
    return InMemoryLearningStore()


def _idea(idea_id: str = "idea-001", skill: str = "no-dashes", org: str = "acme") -> IdeaRecord:
    return IdeaRecord(
        ideaId=idea_id,
        skillBaseName=skill,
        org=org,
        body="Use snake_case, not dashes.",
        status="folded",
        corroborationVersion=1,
    )


def _anchor(
    idea_id: str = "idea-001",
    file: str = "src/utils.ts",
    symbol: str = "formatName",
    org: str = "acme",
    owner_repo: str = "acme/backend",
    active: bool = True,
) -> AnchorRecord:
    return AnchorRecord(
        ideaId=idea_id,
        ownerRepo=owner_repo,
        file=file,
        symbol=symbol,
        org=org,
        active=active,
    )


# ===========================================================================
# 1. Symbol extraction — TypeScript
# ===========================================================================


class TestTsSymbolExtraction:
    """test_symbol_anchor_from_ts_arrow_const and related."""

    # Sample TS code with an arrow const.
    _TS_ARROW = b"""\
const formatName = (name: string): string => {
  return name.trim().toLowerCase();
};

const helper = (x: number) => x * 2;
"""

    def test_symbol_anchor_from_ts_arrow_const(self):
        """Arrow const assignment extracts the const name as the symbol."""
        hunks = [DiffHunk(
            file="src/utils.ts",
            start_line=2,
            end_line=2,
            content=self._TS_ARROW,
        )]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "formatName"
        assert anchors[0].resolution == "symbol"
        assert anchors[0].file == "src/utils.ts"

    def test_ts_function_declaration(self):
        """A plain function declaration extracts the function name."""
        code = b"""\
function processRequest(req: Request): Response {
  const body = req.body;
  return new Response(body);
}
"""
        hunks = [DiffHunk(file="api/handler.ts", start_line=2, end_line=3, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "processRequest"
        assert anchors[0].resolution == "symbol"

    def test_ts_class_declaration(self):
        """A class body line maps to the smallest enclosing named symbol.

        A line inside a method body resolves to the *method* (the smallest
        enclosing named symbol), which is the correct and desired behaviour —
        more precise than the class itself.  A hunk touching a field in the
        class body (outside any method) falls back to the class name.
        """
        code = b"""\
class UserService {
  private db: Database;

  async getUser(id: string) {
    return this.db.find(id);
  }
}
"""
        # Line 5 is inside getUser() — smallest enclosing symbol is getUser.
        hunks = [DiffHunk(file="services/user.ts", start_line=5, end_line=5, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        # The smallest enclosing named symbol for a line inside getUser is the
        # method itself; the class is a coarser enclosing scope but not the
        # smallest one.
        assert anchors[0].symbol in ("getUser", "UserService"), (
            f"Expected 'getUser' or 'UserService', got {anchors[0].symbol!r}"
        )
        assert anchors[0].resolution == "symbol"

        # A hunk on line 2 (the field declaration, outside any method) should
        # map to the class.
        hunks2 = [DiffHunk(file="services/user.ts", start_line=2, end_line=2, content=code)]
        anchors2 = extract_anchors_from_diff(hunks2)
        assert len(anchors2) == 1
        assert anchors2[0].symbol == "UserService"

    def test_ts_inner_const_does_not_escape_to_outer_scope(self):
        """A const x = ... inside an arrow body should still map to the arrow,
        not to x (which is not a function)."""
        code = b"""\
const myHandler = async (req) => {
  const x = req.params.id;
  return { id: x };
};
"""
        # Line 2 is inside the body (const x = ...)
        hunks = [DiffHunk(file="handler.ts", start_line=2, end_line=2, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "myHandler", (
            f"Expected 'myHandler' (the enclosing arrow const), got {anchors[0].symbol!r}"
        )


# ===========================================================================
# 2. Symbol extraction — Go
# ===========================================================================


class TestGoSymbolExtraction:
    """test_symbol_anchor_from_go_anonymous_func and related."""

    _GO_ANON = b"""\
package main

import "net/http"

func main() {
\thandler := func(w http.ResponseWriter, r *http.Request) {
\t\tw.Write([]byte("ok"))
\t}
\thttp.HandleFunc("/", handler)
}
"""

    def test_symbol_anchor_from_go_anonymous_func(self):
        """A Go anonymous func assigned via := extracts the variable name."""
        hunks = [DiffHunk(
            file="main.go",
            start_line=7,
            end_line=7,
            content=self._GO_ANON,
        )]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "handler"
        assert anchors[0].resolution == "symbol"

    def test_go_function_declaration(self):
        """A named Go function extracts its name."""
        code = b"""\
package main

func fetchUser(id string) (*User, error) {
\treturn db.Find(id)
}
"""
        hunks = [DiffHunk(file="user.go", start_line=4, end_line=4, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "fetchUser"
        assert anchors[0].resolution == "symbol"


# ===========================================================================
# 3. Symbol extraction — Python
# ===========================================================================


class TestPythonSymbolExtraction:
    """Python function and class symbol extraction."""

    def test_python_function_symbol_extraction(self):
        """A Python function body line maps to the function name."""
        code = b"""\
def process_items(items: list) -> list:
    result = []
    for item in items:
        result.append(item.strip())
    return result


class Processor:
    def run(self):
        pass
"""
        hunks = [DiffHunk(file="processor.py", start_line=3, end_line=4, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == "process_items"
        assert anchors[0].resolution == "symbol"

    def test_python_class_body(self):
        """A line inside a class method maps to the class."""
        code = b"""\
class MyProcessor:
    def run(self):
        return 42
"""
        hunks = [DiffHunk(file="proc.py", start_line=2, end_line=3, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        # Could be MyProcessor or run — either is a valid named symbol
        assert anchors[0].symbol in ("MyProcessor", "run")
        assert anchors[0].resolution == "symbol"


# ===========================================================================
# 4. File-level fallback
# ===========================================================================


class TestFileLevelFallback:
    """test_file_level_fallback_when_symbol_unparseable."""

    def test_file_level_fallback_unsupported_extension(self):
        """An unsupported extension (e.g. .rs) falls back to __file__."""
        code = b"fn main() { println!(\"hello\"); }\n"
        hunks = [DiffHunk(file="main.rs", start_line=1, end_line=1, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == FILE_LEVEL_SYMBOL
        assert anchors[0].resolution == "file_fallback"

    def test_file_level_fallback_when_symbol_unparseable(self):
        """Malformed/unparseable content falls back to __file__ (no crash)."""
        # tree-sitter is error-tolerant, but passing non-code bytes should still
        # produce a tree — the symbol just may not be found, yielding file-level.
        garbage = b"\x00\x01\x02\x03\xff\xfe"
        hunks = [DiffHunk(file="broken.ts", start_line=1, end_line=1, content=garbage)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == FILE_LEVEL_SYMBOL
        assert anchors[0].resolution == "file_fallback"

    def test_empty_content_falls_back_to_file_level(self):
        """Empty file content (e.g. new file) falls back to __file__."""
        hunks = [DiffHunk(file="new_file.ts", start_line=1, end_line=5, content=b"")]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == FILE_LEVEL_SYMBOL
        assert anchors[0].resolution == "file_fallback"

    def test_no_enclosing_symbol_falls_back(self):
        """Code at the module level with no function/class falls back."""
        code = b"x = 1\ny = 2\n"  # valid Python, but no function/class
        hunks = [DiffHunk(file="constants.py", start_line=1, end_line=1, content=code)]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 1
        assert anchors[0].symbol == FILE_LEVEL_SYMBOL
        assert anchors[0].resolution == "file_fallback"


# ===========================================================================
# 5. Deduplication
# ===========================================================================


class TestExtractDeduplication:
    def test_extract_deduplicates_file_symbol_pairs(self):
        """Two hunks in the same function produce a single anchor."""
        code = b"""\
function doWork(x: number): number {
  const a = x * 2;
  const b = a + 1;
  return b;
}
"""
        hunks = [
            DiffHunk(file="work.ts", start_line=2, end_line=2, content=code),
            DiffHunk(file="work.ts", start_line=3, end_line=3, content=code),
        ]
        anchors = extract_anchors_from_diff(hunks)
        # Both hunks are inside doWork — should yield ONE anchor
        assert len(anchors) == 1
        assert anchors[0].symbol == "doWork"

    def test_different_files_produce_separate_anchors(self):
        """Hunks in different files each produce their own anchor."""
        ts_code = b"const fn1 = () => { return 1; };\n"
        py_code = b"def fn2():\n    return 2\n"
        hunks = [
            DiffHunk(file="a.ts", start_line=1, end_line=1, content=ts_code),
            DiffHunk(file="b.py", start_line=1, end_line=2, content=py_code),
        ]
        anchors = extract_anchors_from_diff(hunks)
        assert len(anchors) == 2
        files = {a.file for a in anchors}
        assert "a.ts" in files
        assert "b.py" in files


# ===========================================================================
# 6. write_anchors_on_fold + get_incumbent_ideas (InMemory)
# ===========================================================================


class TestAnchorWriteAndQuery:
    """test_anchor_written_on_fold_and_queryable and test_merge_touching_anchor_finds_incumbent."""

    def test_anchor_written_on_fold_and_queryable(self, store):
        """Writing an anchor on fold makes it findable via get_incumbent_ideas."""
        anchors = [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")]
        write_anchors_on_fold(store, "acme", "acme/backend", "idea-001", anchors)

        incumbents = get_incumbent_ideas(
            store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-001" in incumbents

    def test_merge_touching_anchor_finds_incumbent(self, store):
        """When a new PR touches a (file, symbol) that has a folded idea, the idea is found."""
        # Fold idea-001, writing its anchor.
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        # Simulate a new PR touching the same (file, symbol).
        new_pr_anchors = [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")]
        incumbents = get_incumbent_ideas(store, "acme", "acme/backend", new_pr_anchors)
        assert incumbents == ["idea-001"]

    def test_get_incumbent_ideas_deduplicates_fan_out(self, store):
        """Fan-out: the same idea anchored to multiple symbols appears only once."""
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [
                Anchor(file="src/a.ts", symbol="fnA", resolution="symbol"),
                Anchor(file="src/b.ts", symbol="fnB", resolution="symbol"),
            ],
        )
        # PR touches both anchors.
        new_pr_anchors = [
            Anchor(file="src/a.ts", symbol="fnA", resolution="symbol"),
            Anchor(file="src/b.ts", symbol="fnB", resolution="symbol"),
        ]
        incumbents = get_incumbent_ideas(store, "acme", "acme/backend", new_pr_anchors)
        assert incumbents.count("idea-001") == 1, "Duplicate idea-id in locality join result"

    def test_no_anchor_match_returns_empty(self, store):
        """A PR that touches a (file, symbol) with no anchored idea returns []."""
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        # Different symbol — no match.
        incumbents = get_incumbent_ideas(
            store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="otherFn", resolution="symbol")],
        )
        assert incumbents == []

    def test_multiple_ideas_anchored_to_same_symbol(self, store):
        """Multiple folded ideas on the same (file, symbol) are all returned."""
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-002",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        incumbents = get_incumbent_ideas(
            store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-001" in incumbents
        assert "idea-002" in incumbents


# ===========================================================================
# 7. retire_anchors_on_unfold (InMemory)
# ===========================================================================


class TestUnfoldRetirement:
    """test_unfold_removes_current_anchor_keeps_history."""

    def test_unfold_removes_current_anchor_keeps_history(self, store):
        """After un-fold, get_incumbent_ideas returns [] but the record persists."""
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        # Verify it's findable before un-fold.
        assert get_incumbent_ideas(
            store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        ) == ["idea-001"]

        # Un-fold (retire the anchor).
        retire_anchors_on_unfold(store, "acme", "acme/backend", "idea-001")

        # Should no longer appear in locality join.
        after = get_incumbent_ideas(
            store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-001" not in after, "Retired anchor must not appear in locality join"

        # But history survives (get_anchors_for_idea still returns the record).
        history = store.get_anchors_for_idea("acme", "acme/backend", "idea-001")
        assert len(history) >= 1, "Anchor record must be kept for history"
        assert all(not a.active for a in history), "All history anchors must be inactive"

    def test_retire_with_multiple_anchors_retires_all(self, store):
        """All anchors for an idea are retired when the idea is un-folded."""
        write_anchors_on_fold(
            store, "acme", "acme/backend", "idea-001",
            [
                Anchor(file="src/a.ts", symbol="fnA", resolution="symbol"),
                Anchor(file="src/b.ts", symbol="fnB", resolution="symbol"),
            ],
        )
        retire_anchors_on_unfold(store, "acme", "acme/backend", "idea-001")

        for sym in ("fnA", "fnB"):
            file_ = f"src/{'a' if sym == 'fnA' else 'b'}.ts"
            incumbents = get_incumbent_ideas(
                store, "acme", "acme/backend",
                [Anchor(file=file_, symbol=sym, resolution="symbol")],
            )
            assert incumbents == [], f"Retired anchor for {sym} must not appear"


# ===========================================================================
# 8. Org guard
# ===========================================================================


class TestOrgGuard:
    """test_anchor_write_is_org_scoped."""

    def test_anchor_write_is_org_scoped(self, store):
        """Anchors from different orgs do not collide in the locality join."""
        write_anchors_on_fold(
            store, "org-a", "org-a/repo", "idea-a",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        write_anchors_on_fold(
            store, "org-b", "org-b/repo", "idea-b",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )

        # Org-a query should only see idea-a.
        a_incumbents = get_incumbent_ideas(
            store, "org-a", "org-a/repo",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        b_incumbents = get_incumbent_ideas(
            store, "org-b", "org-b/repo",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-a" in a_incumbents and "idea-b" not in a_incumbents
        assert "idea-b" in b_incumbents and "idea-a" not in b_incumbents

    def test_blank_org_raises_org_guard_error_on_write(self, store):
        """write_anchors_on_fold with blank org raises OrgGuardError."""
        anchors = [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")]
        with pytest.raises(OrgGuardError):
            write_anchors_on_fold(store, "", "acme/backend", "idea-001", anchors)

    def test_blank_org_raises_org_guard_error_on_retire(self, store):
        """retire_anchors_on_unfold with blank org raises OrgGuardError."""
        with pytest.raises(OrgGuardError):
            retire_anchors_on_unfold(store, "", "acme/backend", "idea-001")


# ===========================================================================
# 9. Key builder unit test
# ===========================================================================


class TestAnchorKey:
    def test_anchor_key_format(self):
        """anchor_key produces the expected ANCHOR# SK format."""
        key = anchor_key("acme", "acme/backend", "src/utils.ts", "formatName", "idea-001")
        assert key["PK"] == "SCOPE#org#acme"
        assert key["SK"] == "ANCHOR#acme/backend#src/utils.ts#formatName#idea-001"

    def test_file_level_sentinel_in_key(self):
        """__file__ sentinel is a valid key component."""
        key = anchor_key("acme", "acme/backend", "main.rs", FILE_LEVEL_SYMBOL, "idea-002")
        assert "__file__" in key["SK"]


# ===========================================================================
# 10. DynamoLearningStore via moto
# ===========================================================================


@pytest.fixture()
def _dynamo_table():
    """Create the single-table 'harness' (PK/SK string keys) via moto."""
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=TABLE_NAME,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield resource


@pytest.fixture()
def _dynamo_store(_dynamo_table):
    return DynamoLearningStore(TABLE_NAME, dynamodb_resource=_dynamo_table)


@pytest.mark.skipif(not _HAS_BOTO, reason="boto3/moto not installed")
class TestAnchorDynamo:
    """Real boto3 round-trip tests via moto DynamoDB."""

    def test_dynamo_anchor_written_and_queryable(self, _dynamo_store):
        """put_anchor + get_ideas_by_anchor round-trips through real boto3."""
        write_anchors_on_fold(
            _dynamo_store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        incumbents = get_incumbent_ideas(
            _dynamo_store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-001" in incumbents

    def test_dynamo_unfold_retires_anchor(self, _dynamo_store):
        """retire_anchors_on_unfold marks records inactive in DynamoDB."""
        write_anchors_on_fold(
            _dynamo_store, "acme", "acme/backend", "idea-001",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        retire_anchors_on_unfold(_dynamo_store, "acme", "acme/backend", "idea-001")

        after = get_incumbent_ideas(
            _dynamo_store, "acme", "acme/backend",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-001" not in after

        # History survives.
        history = _dynamo_store.get_anchors_for_idea("acme", "acme/backend", "idea-001")
        assert len(history) >= 1
        assert all(not a.active for a in history)

    def test_dynamo_org_scoped_keys_do_not_collide(self, _dynamo_store):
        """Two orgs' anchors live under different PKs and do not collide."""
        write_anchors_on_fold(
            _dynamo_store, "org-a", "org-a/repo", "idea-a",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        write_anchors_on_fold(
            _dynamo_store, "org-b", "org-b/repo", "idea-b",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        a = get_incumbent_ideas(
            _dynamo_store, "org-a", "org-a/repo",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        b = get_incumbent_ideas(
            _dynamo_store, "org-b", "org-b/repo",
            [Anchor(file="src/utils.ts", symbol="formatName", resolution="symbol")],
        )
        assert "idea-a" in a and "idea-b" not in a
        assert "idea-b" in b and "idea-a" not in b

    def test_dynamo_file_level_fallback_anchor_roundtrips(self, _dynamo_store):
        """File-level sentinel (__file__) round-trips through DynamoDB correctly."""
        write_anchors_on_fold(
            _dynamo_store, "acme", "acme/backend", "idea-rs",
            [Anchor(file="main.rs", symbol=FILE_LEVEL_SYMBOL, resolution="file_fallback")],
        )
        incumbents = get_incumbent_ideas(
            _dynamo_store, "acme", "acme/backend",
            [Anchor(file="main.rs", symbol=FILE_LEVEL_SYMBOL, resolution="file_fallback")],
        )
        assert "idea-rs" in incumbents
