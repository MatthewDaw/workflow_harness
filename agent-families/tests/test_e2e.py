"""U9 end-to-end acceptance: the nine CLI commands over a curated idea set (R24).

Fully offline: embeddings come from a mapped fake encoder injected through the
``cli._embedding_service`` seam, and every judge call replays a fixture written
by the test for the exact prompt the pipeline builds at that moment.

The fixture-chain property (documented in README.md): placement prompts embed
prior registrations (neighbor lists, skill memberships, statuses), so e2e
fixtures form a CHAIN — each one is computed from the library state left by the
previous step. Whenever a prompt template or the curated idea set changes, the
whole chain re-records as a unit; here that happens automatically because the
test recomputes every prompt from the live library before recording it.

The curated set covers every judge outcome (R24): new_skill (cold-start
taxonomy), append_to_skill, exact duplicate (content hash), near-duplicate
merge_discard (+ the merge-log fast path), contradiction_supersede,
contradiction_flag, lint_reject (target-trivia), rewrite_proposed (+
--accept-rewrite re-entry), and no_placement.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from agent_families import cli, judge
from agent_families.embedding import EmbeddingError, EmbeddingService
from agent_families.judge import write_fixture
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    build_idea_text,
    build_merge_prompt,
    build_placement_prompt,
    build_taxonomy_prompt,
)
from agent_families.rendering import COMPILE_SCHEMA, Renderer
from agent_families.store import Store
from agent_families.vecindex import Neighbor, VecIndex

REPO_THRESHOLDS = Path(__file__).resolve().parent.parent / "thresholds.toml"

DIM = 8
BATCH1 = "curated-1"
BATCH2 = "curated-2"

# Small-dim config so hand-chosen cosines drive the routing; thresholds match
# the shipped template's tunables.
E2E_THRESHOLDS = """\
[embedding]
model = "fake-embedder"
dim = 8
device = "cpu"

[merge]
cosine_threshold = 0.92

[retrieval]
ann_top_k = 10
relevance_floor = 0.5

[judge]
model = "sonnet"
max_retries = 1
bare = false

[lifecycle]
active_cap = 50

[store]
busy_timeout_ms = 5000
"""


def blend(c: float, i: int, j: int) -> list[float]:
    """Unit vector with cosine ``c`` against axis ``i`` (remainder on axis ``j``)."""
    v = [0.0] * DIM
    v[i] = c
    v[j] = math.sqrt(1.0 - c * c)
    return v


# --- the curated idea set (R24) -------------------------------------------------
# Cosines against already-indexed insights pick each idea's route: >= 0.92 hits
# the merge prefilter, >= 0.5 the placement prompt, below it the taxonomy prompt.

IDEA_A = dict(  # cold start -> new_skill (insight 1, skill 1)
    precondition="A new target app needs its requirements gathered",
    action="Walk every visible screen before asking any questions",
    expected_outcome="A feature map exists before elicitation begins",
)
IDEA_B = dict(  # append_to_skill (insight 2)
    precondition="An elicitation interview is underway",
    action="Enumerate the target's user roles before feature questions",
    expected_outcome="Feature questions are scoped per role",
)
IDEA_C = dict(  # near-duplicate of A -> merge_discard (no insight row)
    precondition="A fresh target app needs requirements gathered",
    action="Visit every visible screen before the interview starts",
    expected_outcome="A feature map exists before the interview begins",
)
IDEA_D = dict(  # contradiction_supersede vs A -> new skill 2 (insight 3)
    precondition="A target app is too large to walk exhaustively",
    action="Ask for the priority workflows first and walk only those",
    expected_outcome="Elicitation starts without a full feature map",
)
IDEA_E = dict(  # target trivia -> lint_reject (nothing written)
    precondition="The linkding bookmark form is open",
    action="Click the #save-bookmark button in the navbar",
    expected_outcome="The bookmark persists after reload",
)
IDEA_F = dict(  # names target internals -> rewrite_proposed (nothing written)
    precondition="The target runs Django admin at /admin",
    action="Probe /admin for hidden model pages",
    expected_outcome="Admin-only features surface in the map",
)
REWRITE_F = dict(  # the judge's rewrite, accepted via --accept-rewrite (insight 4)
    precondition="A target exposes a role-gated admin area",
    action="Probe admin areas generically during exploration",
    expected_outcome="Admin-only features surface as explicit requirements",
)
IDEA_G = dict(  # judge declines -> no_placement (nothing written)
    precondition="Coffee is available in the workspace",
    action="Drink coffee before starting exploration",
    expected_outcome="Exploration feels subjectively faster",
)
IDEA_H = dict(  # contradiction_flag vs A, placed in skill 1 (insight 5)
    precondition="A complete feature map already exists",
    action="Re-walk every screen after each delivered increment",
    expected_outcome="Feature drift is caught early",
)
IDEA_I = dict(  # post-promote registration, used by the revert probe (insight 6)
    precondition="A screen contains paginated lists",
    action="Walk every page of each paginated list",
    expected_outcome="Features hidden on later pages enter the map",
)


def text(fields: dict) -> str:
    return build_idea_text(**fields)


def doc_key(fields: dict) -> str:
    return "search_document: " + text(fields)


VECTORS = {
    doc_key(IDEA_A): blend(1.0, 0, 1),
    doc_key(IDEA_B): blend(0.6, 0, 1),
    doc_key(IDEA_C): blend(1.0, 0, 1),  # cosine 1.0 vs A -> merge prefilter
    doc_key(IDEA_D): blend(0.55, 0, 2),
    doc_key(IDEA_E): blend(0.7, 1, 3),  # cosine 0.56 vs B, 0 vs A
    doc_key(IDEA_F): blend(0.7, 0, 4),
    doc_key(REWRITE_F): blend(0.65, 0, 4),
    doc_key(IDEA_G): blend(0.6, 0, 5),
    doc_key(IDEA_H): blend(0.68, 0, 6),
    doc_key(IDEA_I): blend(0.58, 0, 7),
}


# --- offline harness --------------------------------------------------------------


class MappedEncoder:
    """text -> hand-chosen vector; any unexpected embed is a hard test failure."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = dict(mapping)
        self.calls: list[str] = []

    def encode(self, text: str) -> list[float]:
        self.calls.append(text)
        if text not in self.mapping:
            raise AssertionError(f"unexpected embed text: {text!r}")
        return list(self.mapping[text])


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


def out(outcome: str, **extra) -> dict:
    base = {
        "outcome": outcome,
        "scope_tag": {
            "value": "universal",
            "justification": "applies to any web target",
        },
        "lint": {"verdict": "pass"},
        "confidence": 0.9,
    }
    base.update(extra)
    return base


def record(fixtures_dir, prompt: str, output: dict, schema: dict = JUDGE_SCHEMA):
    write_fixture(fixtures_dir, prompt, schema, "sonnet", envelope_for(output))


def neighbor(insight_id: int, status: str = "quarantined") -> Neighbor:
    # Prompt builders use only insight_id and status; distance never appears in
    # prompts (R23), so 0.0 is fine.
    return Neighbor(insight_id=insight_id, distance=0.0, status=status)


def install_fake_embedder(monkeypatch, mapping: dict | None = None) -> MappedEncoder:
    encoder = MappedEncoder(mapping or {})
    monkeypatch.setattr(
        cli,
        "_embedding_service",
        lambda config: EmbeddingService(config.embedding, encoder=encoder),
    )
    return encoder


@pytest.fixture(autouse=True)
def clean_judge_env(monkeypatch):
    monkeypatch.delenv(judge.MODE_ENV, raising=False)
    monkeypatch.delenv(judge.FIXTURES_ENV, raising=False)


def parse_skill_md(path: Path) -> tuple[dict, bytes]:
    """Parse a SKILL.md into (frontmatter dict, body bytes); asserts the layout."""
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    assert text.startswith("---\n")
    end = text.index("\n---\n", 3)
    frontmatter = text[4:end]
    meta = {}
    for line in frontmatter.splitlines():
        key, sep, value = line.partition(": ")
        assert sep, f"malformed frontmatter line: {line!r}"
        meta[key] = value
    meta["description"] = json.loads(meta["description"])
    prefix = f"---\n{frontmatter}\n---\n\n".encode("utf-8")
    assert raw.startswith(prefix)
    return meta, raw[len(prefix):]


# --- init + status (scenario: seeded taxonomy, zero insights) -----------------------


def test_init_then_status_shows_seeded_taxonomy_and_zero_insights(
    tmp_path, monkeypatch, capsys
):
    install_fake_embedder(monkeypatch)
    lib = tmp_path / "lib"
    assert cli.main(["init", "--dir", str(lib)]) == 0
    init_out = capsys.readouterr().out
    assert "wrote" in init_out and "thresholds.toml" in init_out
    assert "pre-fetching embedding model" in init_out  # progress (R19)

    assert cli.main(["status", "--dir", str(lib)]) == 0
    status = capsys.readouterr().out
    assert "snapshot: 0" in status
    assert "embedder: nomic-ai/nomic-embed-text-v1.5 (dim 768)" in status
    for family in ("planner", "worker", "verifier", "context-retriever"):
        assert f"family {family}: 1 agent(s), 0 skill(s)" in status
    assert "insights: total=0 quarantined=0 active=0 dormant=0 retired=0" in status
    assert "pending batches: none" in status
    assert "open contradiction flags: none" in status
    # R11 cut-over: cosine_threshold removed from toml; status shows candidate_floor
    assert "merge.candidate_floor=0.8" in status


def test_second_init_is_safe_noop(tmp_path, monkeypatch, capsys):
    install_fake_embedder(monkeypatch)
    lib = tmp_path / "lib"
    assert cli.main(["init", "--dir", str(lib)]) == 0
    config_bytes = (lib / "thresholds.toml").read_bytes()
    capsys.readouterr()

    assert cli.main(["init", "--dir", str(lib)]) == 0
    second_out = capsys.readouterr().out
    assert "keeping existing" in second_out
    assert "taxonomy already seeded" in second_out
    assert (lib / "thresholds.toml").read_bytes() == config_bytes
    with Store(lib / "library.db") as store:
        n_families = store.conn.execute(
            "SELECT COUNT(*) AS n FROM families"
        ).fetchone()["n"]
        n_agents = store.conn.execute("SELECT COUNT(*) AS n FROM agents").fetchone()[
            "n"
        ]
    assert (n_families, n_agents) == (4, 4)


def test_init_writes_the_committed_thresholds_template(tmp_path, monkeypatch):
    # The CLI's embedded template and the committed agent-families/thresholds.toml
    # are the same file by contract ("written verbatim", R19) — pin them together.
    assert REPO_THRESHOLDS.read_text(encoding="utf-8") == cli.DEFAULT_THRESHOLDS_TOML

    install_fake_embedder(monkeypatch)
    lib = tmp_path / "lib"
    assert cli.main(["init", "--dir", str(lib)]) == 0
    written = (lib / "thresholds.toml").read_bytes()
    assert written == cli.DEFAULT_THRESHOLDS_TOML.encode("utf-8")  # LF pinned


# --- actionable failures before init ------------------------------------------------


def test_status_before_init_fails_with_af_init_hint(tmp_path, capsys):
    assert cli.main(["status", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "af status:" in err
    assert "af init" in err


def test_missing_database_fails_actionably(tmp_path, capsys):
    (tmp_path / "thresholds.toml").write_text(E2E_THRESHOLDS, encoding="utf-8")
    assert cli.main(["status", "--dir", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "no library database" in err
    assert "af init" in err


def test_init_embedding_prefetch_failure_exits_nonzero_with_guidance(
    tmp_path, monkeypatch, capsys
):
    class FailingService:
        def ensure_ready(self):
            raise EmbeddingError(
                "could not load embedding model: cache miss while offline."
                " Unset HF_HUB_OFFLINE and run `af init` once with network access"
                " (~0.5 GB download, honors HF_HOME)."
            )

    monkeypatch.setattr(cli, "_embedding_service", lambda config: FailingService())
    lib = tmp_path / "lib"
    assert cli.main(["init", "--dir", str(lib)]) == 1
    err = capsys.readouterr().err
    assert "af init:" in err and "HF_HOME" in err
    # the schema/seed half completed; a retried init resumes at the prefetch
    assert (lib / "library.db").exists()


# --- the curated end-to-end chain (R24) ------------------------------------------------


def _REMOVED_test_curated_idea_set_end_to_end(tmp_path, monkeypatch, capsysbinary):
    """DEMOTED (plan-008 A-U6 cut-over): add_idea is now the R3 gauntlet; this
    test exercises the legacy placement spine (new_skill/append_to_skill/
    merge_discard) via the CLI and requires fixtures the R3 path never generates.
    The equivalent R3 e2e is tests/test_e2e_r3_ingest.py."""
    lib = tmp_path / "lib"
    lib.mkdir()
    db = lib / "library.db"
    fx = tmp_path / "fixtures"
    exported = tmp_path / "exported"
    # Pre-written small-dim config; init keeps an existing thresholds.toml.
    (lib / "thresholds.toml").write_text(E2E_THRESHOLDS, encoding="utf-8")
    monkeypatch.setenv(judge.FIXTURES_ENV, str(fx))
    encoder = install_fake_embedder(monkeypatch, VECTORS)

    def run(*argv) -> tuple[int, str, str]:
        capsysbinary.readouterr()
        rc = cli.main([str(a) for a in argv])
        cap = capsysbinary.readouterr()
        return rc, cap.out.decode("utf-8"), cap.err.decode("utf-8")

    def run_bytes(*argv) -> tuple[int, bytes, str]:
        capsysbinary.readouterr()
        rc = cli.main([str(a) for a in argv])
        cap = capsysbinary.readouterr()
        return rc, cap.out, cap.err.decode("utf-8")

    def add(fields: dict, batch: str = BATCH1, *extra) -> tuple[int, str, str]:
        return run(
            "add-idea", "--dir", lib,
            "--precondition", fields["precondition"],
            "--action", fields["action"],
            "--expected-outcome", fields["expected_outcome"],
            "--batch", batch, *extra,
        )

    def q(sql: str, *params) -> list[dict]:
        with Store(db) as store:
            return [dict(r) for r in store.conn.execute(sql, params).fetchall()]

    def vec_count() -> int:
        with Store(db) as store:
            return VecIndex(store, DIM).count()

    # --- init -------------------------------------------------------------------
    rc, _, _ = run("init", "--dir", lib)
    assert rc == 0
    with Store(db) as store:
        planner_agent = store.conn.execute(
            "SELECT a.id AS id FROM agents a JOIN families f ON f.id = a.family_id"
            " WHERE f.name = 'planner'"
        ).fetchone()["id"]

    # --- A: cold start -> taxonomy prompt -> new_skill (insight 1, skill 1) -------
    with Store(db) as store:
        prompt = build_taxonomy_prompt(store, text(IDEA_A), None)
    record(fx, prompt, out(
        "new_skill",
        new_skill={
            "agent_id": planner_agent,
            "name": "screen-mapping",
            "description": "Build a feature map by walking the target's UI",
        },
    ))
    rc, stdout, _ = add(IDEA_A)
    assert rc == 0
    assert "registered insight 1" in stdout
    skills = q("SELECT * FROM skills")
    assert len(skills) == 1
    assert skills[0]["name"] == "screen-mapping"
    assert skills[0]["agent_id"] == planner_agent
    assert q("SELECT status FROM insights WHERE id = 1")[0]["status"] == "quarantined"

    # --- B: placement over neighbor A -> append_to_skill (insight 2) --------------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_B), [neighbor(1)], None)
    record(fx, prompt, out("append_to_skill", target_skill_id=1))
    rc, stdout, _ = add(IDEA_B)
    assert rc == 0 and "registered insight 2" in stdout
    with Store(db) as store:
        assert store.skill_members(1) == [1, 2]

    # --- A resubmitted: exact duplicate via content hash, before any embedding ----
    calls_before = len(encoder.calls)
    rc, stdout, _ = add(IDEA_A)
    assert rc == 0
    assert "exact duplicate of insight 1" in stdout
    assert len(encoder.calls) == calls_before  # no encoder call (R5)
    assert q("SELECT COUNT(*) AS n FROM insights")[0]["n"] == 2

    # --- C: near-duplicate (cosine 1.0 vs A) -> judged merge_discard ---------------
    with Store(db) as store:
        prompt = build_merge_prompt(store, text(IDEA_C), [neighbor(1)])
    record(fx, prompt, out("merge_discard", duplicate_of=1))
    rc, stdout, _ = add(IDEA_C)
    assert rc == 0
    assert "judged a duplicate of insight 1" in stdout
    assert q("SELECT COUNT(*) AS n FROM merge_log")[0]["n"] == 1
    assert q("SELECT COUNT(*) AS n FROM insights")[0]["n"] == 2  # discard-new

    # resubmitting the merged idea exits via the merge-log fast path, no embedding
    calls_before = len(encoder.calls)
    rc, stdout, _ = add(IDEA_C)
    assert rc == 0
    assert "previously judged a duplicate of insight 1" in stdout
    assert len(encoder.calls) == calls_before
    assert q("SELECT COUNT(*) AS n FROM merge_log")[0]["n"] == 1

    # --- D: contradiction_supersede vs A, placed in a new skill (insight 3) --------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_D), [neighbor(1)], None)
    record(fx, prompt, out(
        "contradiction_supersede",
        supersedes=1,
        new_skill={
            "agent_id": planner_agent,
            "name": "priority-first-elicitation",
            "description": "Scope the walk by priority workflows",
        },
    ))
    rc, stdout, _ = add(IDEA_D)
    assert rc == 0 and "registered insight 3" in stdout
    row = q("SELECT * FROM insights WHERE id = 3")[0]
    assert row["supersedes"] == 1
    flags = q("SELECT * FROM contradictions WHERE status = 'open'")
    assert [(f["challenger_id"], f["incumbent_id"]) for f in flags] == [(3, 1)]
    assert q("SELECT status FROM insights WHERE id = 1")[0]["status"] == "quarantined"
    assert q("SELECT name FROM skills WHERE id = 2")[0]["name"] == (
        "priority-first-elicitation"
    )

    # --- E: target trivia -> lint_reject, exit non-zero, nothing written -----------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_E), [neighbor(2)], None)
    record(fx, prompt, out(
        "lint_reject",
        lint={"verdict": "reject", "reason": "names target internals: #save-bookmark"},
    ))
    rc, _, stderr = add(IDEA_E)
    assert rc == 1
    assert "names target internals: #save-bookmark" in stderr
    assert q("SELECT COUNT(*) AS n FROM insights")[0]["n"] == 3

    # --- F: rewrite_proposed -> exit non-zero printing the rewrite + the hint ------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_F), [neighbor(1)], None)
    record(fx, prompt, out(
        "rewrite_proposed", lint={"verdict": "rewrite", "rewrite": REWRITE_F}
    ))
    rc, _, stderr = add(IDEA_F)
    assert rc == 1
    assert "Re-run with `--accept-rewrite` to accept" in stderr
    assert REWRITE_F["action"] in stderr  # the rewrite is printed (R10)
    assert q("SELECT COUNT(*) AS n FROM insights")[0]["n"] == 3

    # accepting the rewrite re-enters at the content-hash check and registers
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(REWRITE_F), [neighbor(1)], None)
    record(fx, prompt, out("append_to_skill", target_skill_id=1))
    rc, stdout, _ = add(REWRITE_F, BATCH1, "--accept-rewrite")
    assert rc == 0 and "registered insight 4" in stdout
    with Store(db) as store:
        assert store.skill_members(1) == [1, 2, 4]

    # --- G: no_placement -> exit non-zero, nothing written --------------------------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_G), [neighbor(1)], None)
    record(fx, prompt, out("no_placement"))
    rc, _, stderr = add(IDEA_G)
    assert rc == 1 and "no_placement" in stderr
    assert q("SELECT COUNT(*) AS n FROM insights")[0]["n"] == 4

    # --- H: contradiction_flag vs A, appended to skill 1 (insight 5) ----------------
    with Store(db) as store:
        prompt = build_placement_prompt(store, text(IDEA_H), [neighbor(1)], None)
    record(fx, prompt, out("contradiction_flag", supersedes=1, target_skill_id=1))
    rc, stdout, _ = add(IDEA_H)
    assert rc == 0 and "registered insight 5" in stdout
    assert q("SELECT supersedes FROM insights WHERE id = 5")[0]["supersedes"] is None
    assert len(q("SELECT * FROM contradictions WHERE status = 'open'")) == 2

    # --- status reflects the registration phase -------------------------------------
    rc, status, _ = run("status", "--dir", lib)
    assert rc == 0
    assert "snapshot: 0" in status  # registration never mints snapshots (R3)
    assert "embedder: fake-embedder (dim 8)" in status
    assert "insights: total=5 quarantined=5 active=0 dormant=0 retired=0" in status
    assert f"{BATCH1}: 5 quarantined insight(s)" in status
    assert "open contradiction flags: 2" in status
    assert "  - flag 1: insight 3 contradicts insight 1" in status
    assert "family planner: 1 agent(s), 2 skill(s)" in status

    # --- promote, then render shows the promoted content -----------------------------
    rc, stdout, _ = run("promote", "--dir", lib, "--batch", BATCH1)
    assert rc == 0
    assert "5 insight(s) -> active (snapshot 1)" in stdout

    rc, status, _ = run("status", "--dir", lib)
    assert "snapshot: 1" in status
    assert "insights: total=5 quarantined=0 active=5 dormant=0 retired=0" in status
    assert "pending batches: none" in status

    rc, skill1_promoted, _ = run_bytes("render", "--dir", lib, "--skill", 1)
    assert rc == 0
    rendered = skill1_promoted.decode("utf-8")
    assert "# Skill: screen-mapping" in rendered
    for insight_id in (1, 2, 4, 5):
        assert f"## Insight {insight_id}" in rendered
    assert IDEA_A["action"] in rendered and IDEA_H["action"] in rendered
    assert "## Insight 3" not in rendered  # insight 3 lives in skill 2

    # --- compile skill 2 through the judge seam (render --compile) -------------------
    with Store(db) as store:
        compile_prompt = Renderer(store).compile_prompt(2)
    record(fx, compile_prompt, {
        "sections": [
            {
                "heading": "Prioritise the walk",
                "body": "Ask for priority workflows first; walk only those.",
                "insight_ids": [3],
            }
        ]
    }, schema=COMPILE_SCHEMA)
    rc, compiled_bytes, stderr = run_bytes(
        "render", "--dir", lib, "--skill", 2, "--compile"
    )
    assert rc == 0
    compiled_path = lib / "compiled" / "skill-2-snapshot-1.md"
    assert "compiled document written to" in stderr
    assert compiled_path.read_bytes() == compiled_bytes
    assert b"<!-- insights: 3 -->" in compiled_bytes

    # --- export produces loadable SKILL.md files (R17) --------------------------------
    rc, stdout, _ = run("export", "--dir", lib, "--out", exported)
    assert rc == 0
    assert "[compiled]" in stdout  # skill 2 exported from its compiled doc

    meta1, body1 = parse_skill_md(exported / "screen-mapping" / "SKILL.md")
    assert meta1["name"] == "screen-mapping"
    assert meta1["description"] == "Build a feature map by walking the target's UI"
    assert body1 == skill1_promoted

    meta2, body2 = parse_skill_md(
        exported / "priority-first-elicitation" / "SKILL.md"
    )
    assert meta2["name"] == "priority-first-elicitation"
    assert body2 == compiled_bytes

    # --- grow skill 1, promote, then revert restores the old bytes (R12/R15) ----------
    with Store(db) as store:
        prompt = build_placement_prompt(
            store, text(IDEA_I), [neighbor(1, "active")], None
        )
    record(fx, prompt, out("append_to_skill", target_skill_id=1))
    rc, stdout, _ = add(IDEA_I, BATCH2)
    assert rc == 0 and "registered insight 6" in stdout

    rc, stdout, _ = run("promote", "--dir", lib, "--batch", BATCH2)
    assert rc == 0 and "(snapshot 2)" in stdout

    rc, skill1_grown, _ = run_bytes("render", "--dir", lib, "--skill", 1)
    assert rc == 0
    assert skill1_grown != skill1_promoted
    assert skill1_grown.startswith(skill1_promoted)  # append-only growth (R15)
    assert b"## Insight 6" in skill1_grown

    vec_rows_before = vec_count()
    rc, stdout, _ = run("revert", "--dir", lib, "--batch", BATCH2)
    assert rc == 0
    assert "1 insight(s) -> retired (snapshot 3)" in stdout
    assert vec_count() == vec_rows_before  # lifecycle never touches vec rows (R13)

    rc, skill1_reverted, _ = run_bytes("render", "--dir", lib, "--skill", 1)
    assert rc == 0
    assert skill1_reverted == skill1_promoted  # byte-identical restore

    rc, skill1_at_one, _ = run_bytes(
        "render", "--dir", lib, "--skill", 1, "--snapshot", 1
    )
    assert rc == 0
    assert skill1_at_one == skill1_promoted

    # --- status reflects the full lifecycle --------------------------------------------
    rc, status, _ = run("status", "--dir", lib)
    assert rc == 0
    assert "snapshot: 3" in status
    assert "insights: total=6 quarantined=0 active=5 dormant=0 retired=1" in status
    assert "pending batches: none" in status
    assert "open contradiction flags: 2" in status
    assert "empty skills: none" in status
    assert "skills created by reverted batches: none" in status
