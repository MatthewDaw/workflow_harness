"""plan-003 U9: episode end-to-end — the human reflector's tools, proven.

Fully offline and fake-driven, exactly per the U9 approach: a complete episode
runs through the real :func:`~agent_families.pipeline.episode.run_episode` loop
with scripted explorer/planner/worker/verifier stages and a fixture resolver,
the grader settles it into real SCEN rows + a stored settlement report, and then
**every CLI command the report embeds** is executed against the resulting
library to prove it works. Embeddings come from the injected fake encoder
(``cli._embedding_service``) and the one add-idea judge call replays a fixture —
zero quota, no Docker, no ``claude`` on PATH. The live full episode (real
explorer + grader against seeded linkding) is the documented README procedure;
the docker-required halves live in ``test_target_env.py`` / ``test_settle.py``.

## Conformance

Plan test-scenarios / invariants (003 U9), mapped 1:1:

- e2e fake episode produces a settlement report whose every embedded CLI command
  executes successfully (incl. ``af trace chain <SCEN>`` — the attribution join
  Plan 4 Stage A reuses):
  ``test_episode_e2e_embedded_report_commands_execute``
- ideas registered from the e2e episode carry full provenance (003 R27):
  ``test_episode_e2e_reflected_idea_carries_provenance``
- ``add-idea --episode`` rejects an unknown episode (validated before any judge
  call): ``test_add_idea_unknown_episode_rejected`` (+ the unknown
  ``--scenario``/``--ticket`` arms)
- README documents the manual-reflection workflow (read report → trace queries →
  add_idea) and the frozen-replay hand-verification step:
  ``test_readme_documents_reflection_and_frozen_replay``

The non-embedded R26 trace surface (``spans``/``iterations``/``evidence``/
``transcript``) is exercised against the same settled episode in
``test_episode_e2e_trace_surface``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent_families import cli, judge
from agent_families.embedding import EmbeddingService
from agent_families.grading.registry import FeatureCandidate, mint_feat
from agent_families.grading.scenarios import (
    Observation,
    Resolution,
    ResolutionCache,
    parse_manifest,
)
from agent_families.grading.settle import (
    SettleConfig,
    SettlementPlan,
    run_settlement,
)
from agent_families.judge import write_fixture
from agent_families.pipeline import (
    JUDGE_SCHEMA,
    build_idea_text,
    build_taxonomy_prompt,
)
from agent_families.pipeline.episode import (
    EpisodeConfig,
    EpisodeStages,
    run_episode,
)
from agent_families.pipeline.explorer import write_explorer_msg
from agent_families.pipeline.orchestrator import RunResult
from agent_families.pipeline.planning import plan_meta_key
from agent_families.store import Store

import subprocess

TARGET = "linkding"
DIGEST = "sha256:0123456789abcdef"

REPO_ROOT = Path(__file__).resolve().parent.parent

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


# --- offline harness (the test_e2e seams, reused) ---------------------------------


class MappedEncoder:
    """text -> hand-chosen vector; an unexpected embed is a hard test failure."""

    def __init__(self, mapping: dict[str, list[float]]) -> None:
        self.mapping = dict(mapping)
        self.calls: list[str] = []

    def encode(self, text: str) -> list[float]:
        self.calls.append(text)
        if text not in self.mapping:
            raise AssertionError(f"unexpected embed text: {text!r}")
        return list(self.mapping[text])


def install_fake_embedder(monkeypatch, mapping: dict) -> MappedEncoder:
    encoder = MappedEncoder(mapping)
    monkeypatch.setattr(
        cli,
        "_embedding_service",
        lambda config: EmbeddingService(config.embedding, encoder=encoder),
    )
    return encoder


def envelope_for(output: dict) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "duration_ms": 12,
        "num_turns": 1,
        "result": "ok",
        "total_cost_usd": 0.003,
        "structured_output": output,
    }


def taxonomy_new_skill(agent_id: int) -> dict:
    return {
        "outcome": "new_skill",
        "scope_tag": {"value": "universal", "justification": "any web target"},
        "lint": {"verdict": "pass"},
        "confidence": 0.9,
        "new_skill": {
            "agent_id": agent_id,
            "name": "search-fidelity",
            "description": "Compare clone results to query terms before matching",
        },
    }


# --- a11y trees + scripted grader seams (the test_settle precedents) --------------


def node(role: str, name: str = "", *children: dict) -> dict:
    n: dict = {"role": role, "name": name}
    if children:
        n["children"] = list(children)
    return n


TREE = node(
    "document",
    "app",
    node("main", "", node("button", "Go"), node("list", "")),
)


def click(target: str) -> dict:
    return {"action": "click", "selector": f"text={target}", "args": []}


class FakeDriver:
    def __init__(self, states: list[tuple[dict, str]]):
        self.states = states
        self.idx = 0
        self.executed: list[dict] = []

    def observe(self) -> Observation:
        tree, url = self.states[min(self.idx, len(self.states) - 1)]
        return Observation(a11y_tree=tree, url=url)

    def execute(self, action: dict) -> None:
        self.executed.append(action)
        self.idx = min(self.idx + 1, len(self.states) - 1)

    def screenshot(self) -> bytes:
        return b"\x89PNG-fake"


class ScriptedResolver:
    def __init__(self, script: dict):
        self.script = dict(script)
        self.calls: list[str] = []

    def __call__(self, step_text: str, a11y_tree: dict) -> Resolution:
        self.calls.append(step_text)
        entry = self.script.get(step_text)
        if entry is None:
            return Resolution("resolved", click(step_text), "default resolve")
        if isinstance(entry, list):
            return entry.pop(0)
        return entry


# --- workspace scaffolding (a small real git repo) --------------------------------


def make_ws(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "src").mkdir()
    (root / "src" / "index.ts").write_text(
        "export const answer = 42\n", encoding="utf-8", newline="\n"
    )
    for args in (
        ("init", "--initial-branch=main"),
        ("config", "user.name", "t"),
        ("config", "user.email", "t@localhost"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.autocrlf", "false"),
        ("add", "-A"),
        ("commit", "-m", "init"),
    ):
        subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True
        )
    return root


def make_candidate(key: str) -> FeatureCandidate:
    return FeatureCandidate(
        key=key,
        area="bookmarks",
        behavior=f"behavior {key}",
        route="/bookmarks",
        confirm_steps=({"action": "goto", "selector": "", "args": {}},),
        scenario_steps=(f"exercise {key}",),
        expected_outcome="it works",
        tier="must",
    )


# --- the fake episode stages ------------------------------------------------------


def scripted_request():
    def fn(ctx, increment_index, slice_ids):
        msg_id = f"MSG-e{ctx.episode_id}-inc{increment_index:03d}-open"
        text = f"please build {', '.join(slice_ids)}"
        write_explorer_msg(ctx.store, msg_id, text, slice_ids)
        return SimpleNamespace(msg_id=msg_id, text=text)

    return fn


def scripted_uat():
    def fn(ctx, increment_index, run_id, briefing):
        return SimpleNamespace(verdict="accepted", feedback_msg_ids=())

    return fn


def scripted_increment():
    """A real increment: a run carrying episode FKs, one done TKT + REQ + SPAN per
    slice FEAT (so the attribution chain connects), the canonical plan document
    (so the UAT briefing renders), and a workspace marker."""

    def fn(inc):
        store = inc.store
        run_id = store.create_run(
            f"episode:{inc.episode_id}",
            store.current_snapshot_id(),
            episode_id=inc.episode_id,
            increment_index=inc.increment_index,
        )
        reqs, tkts = [], []
        for i, _fid in enumerate(inc.slice_feat_ids):
            rid = f"REQ-r{run_id}-{i:03d}"
            tid = f"TKT-r{run_id}-{i:03d}"
            reqs.append({"id": rid, "source_msg": inc.request_msg_id})
            tkts.append({"id": tid, "covers": [rid]})
        with store.transaction():
            for r in reqs:
                store.conn.execute(
                    "INSERT INTO trace_req (id, source_msg_id) VALUES (?, ?)",
                    (r["id"], r["source_msg"]),
                )
            for t in tkts:
                store.conn.execute(
                    "INSERT INTO trace_tkt (id, status) VALUES (?, 'pending')",
                    (t["id"],),
                )
                for rid in t["covers"]:
                    store.conn.execute(
                        "INSERT INTO trace_tkt_covers (tkt_id, req_id) VALUES (?, ?)",
                        (t["id"], rid),
                    )
                store.set_ticket_status(t["id"], "done", run_id)
            document = {
                "run_id": run_id,
                "spec_ref": f"run-{run_id}",
                "requirements": [
                    {"id": r["id"], "text": "req", "source_msg": r["source_msg"]}
                    for r in reqs
                ],
                "tickets": [
                    {
                        "id": t["id"],
                        "title": f"ticket {t['id']}",
                        "kind": "feature",
                        "covers": t["covers"],
                    }
                    for t in tkts
                ],
                "assumptions": [],
                "warnings": [],
            }
            store.set_meta(
                plan_meta_key(run_id),
                json.dumps(document, sort_keys=True, ensure_ascii=False),
            )
        for t in tkts:
            span_id = f"SPAN-r{run_id}-{t['id'].rsplit('-', 1)[-1]}"
            store.insert_span(
                span_id,
                run_id=run_id,
                family="worker",
                agent="generalist",
                ticket_id=t["id"],
                ralph_iteration=1,
                model_version="sonnet",
                prompt_set_version="v1",
            )
            store.finalize_span(
                span_id,
                "completed",
                num_turns=1,
                duration_ms=10,
                cost_usd=0.01,
                input_tokens=10,
                output_tokens=5,
                files_json=json.dumps(["src/index.ts"]),
                artifact_refs_json=json.dumps([f"transcripts/{span_id}.jsonl"]),
            )
        Path(inc.workspace.root, f"inc-{inc.increment_index}.txt").write_text(
            "delivered\n", encoding="utf-8", newline="\n"
        )
        store.set_run_status(run_id, "success")
        return RunResult(
            run_id=run_id,
            status="success",
            ticket_statuses={t["id"]: "done" for t in tkts},
            detail="",
        )

    return fn


def settle_stage(feat_ids: list[str], tmp_path: Path):
    """A real settlement: FEAT #1 passes deterministically (identical trees),
    FEAT #2 is feature_absent on the clone — so failed_scenarios (and its embedded
    `af trace chain`) is non-empty. Writes SCEN rows + stores the report."""
    f1, f2 = feat_ids
    m1 = parse_manifest(
        {
            "scenario_id": f"{f1}/scn",
            "feat_id": f1,
            "title": "list works",
            "steps": [f"open {f1}"],
            "expected_outcome": "ok",
            "tier": "must",
        }
    )
    m2 = parse_manifest(
        {
            "scenario_id": f"{f2}/scn",
            "feat_id": f2,
            "title": "search works",
            "steps": [f"open {f2}"],
            "expected_outcome": "ok",
            "tier": "must",
        }
    )
    resolver = ScriptedResolver(
        {
            f"open {f2}": [
                Resolution("resolved", click("x"), "target has it"),
                Resolution("element_absent", None, "clone lacks it"),
            ],
        }
    )

    def fn(ctx):
        drivers = {
            "target": FakeDriver([(TREE, "http://target/bookmarks")]),
            "clone": FakeDriver([(TREE, "http://clone/bookmarks")]),
        }
        plan = SettlementPlan(
            episode_id=ctx.episode_id,
            snapshot_id=0,
            target=ctx.target,
            manifests=(m1, m2),
        )
        return run_settlement(
            plan,
            store=ctx.store,
            config=SettleConfig(
                model="sonnet",
                max_retries=0,
                panel_size=3,
                mode="replay",
                fixtures_dir=tmp_path / "settle-fixtures",
            ),
            reset_target=ctx.reset_target,
            drivers=drivers,
            cache=ResolutionCache(),
            resolve=resolver,
        )

    return fn


def make_cfg(ws_root: Path) -> EpisodeConfig:
    return EpisodeConfig(
        target=TARGET,
        digest=DIGEST,
        workspace_dir=ws_root,
        max_increments=5,
        cost_ceiling_usd=100.0,
        slice_size=2,
    )


def run_full_episode(store: Store, tmp_path: Path) -> tuple[int, list[str]]:
    """Mint two FEATs, run one full fake-driven episode to settlement. Returns the
    episode id and its FEAT ids."""
    feat_ids = [
        mint_feat(
            store, make_candidate(key), f"evidence/{key}.json",
            target=TARGET, digest=DIGEST,
        )
        for key in ("list", "search")
    ]
    ws = make_ws(tmp_path / "engagement")
    stages = EpisodeStages(
        request_fn=scripted_request(),
        increment_fn=scripted_increment(),
        uat_fn=scripted_uat(),
        reset_target_fn=lambda: None,
        settle_fn=settle_stage(feat_ids, tmp_path),
    )
    result = run_episode(store, make_cfg(ws), stages)
    assert result.status == "frontier_exhausted", result
    return result.episode_id, feat_ids


# --- the test bodies --------------------------------------------------------------


def init_library(tmp_path, monkeypatch, mapping: dict) -> Path:
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "thresholds.toml").write_text(E2E_THRESHOLDS, encoding="utf-8")
    install_fake_embedder(monkeypatch, mapping)
    assert cli.main(["init", "--dir", str(lib)]) == 0
    return lib


def capture(capsysbinary, *argv) -> tuple[int, str, str]:
    capsysbinary.readouterr()
    rc = cli.main([str(a) for a in argv])
    cap = capsysbinary.readouterr()
    return rc, cap.out.decode("utf-8"), cap.err.decode("utf-8")


def test_episode_e2e_embedded_report_commands_execute(
    tmp_path, monkeypatch, capsysbinary
):
    lib = init_library(tmp_path, monkeypatch, {})
    with Store(lib / "library.db") as store:
        episode_id, feat_ids = run_full_episode(store, tmp_path)

    # The stored report is the human reflector's entry point; collect EVERY CLI
    # command it embeds and prove each one runs (003 U9 acceptance test).
    rc, report_out, _ = capture(capsysbinary, "episode", "report", episode_id, "--dir", lib)
    assert rc == 0
    report = json.loads(report_out)
    assert report["episode_id"] == episode_id
    # FEAT #2 failed feature_absent; FEAT #1 passed → embedded commands exist.
    assert report["score"]["scoreable"] == 2
    assert report["score"]["passed"] == 1
    assert len(report["failed_scenarios"]) == 1

    embedded = [report["commands"]["report"]]
    embedded += [row["trace_command"] for row in report["scenarios"]]
    assert any(c.startswith("af trace chain ") for c in embedded), embedded
    assert f"af episode report {episode_id}" in embedded

    for command in embedded:
        assert command.startswith("af "), command
        argv = command.split()[1:] + ["--dir", str(lib)]
        rc, out, err = capture(capsysbinary, *argv)
        assert rc == 0, f"embedded command failed: {command!r}\n{err}"
        assert out.strip(), f"embedded command produced nothing: {command!r}"

    # The load-bearing one: `af trace chain <SCEN>` walks the attribution join the
    # report only embeds — SCEN -> FEAT -> requests -> tickets -> spans.
    failed = report["failed_scenarios"][0]
    rc, chain_out, _ = capture(
        capsysbinary, "trace", "chain", failed["scen_id"], "--dir", lib
    )
    assert rc == 0
    assert failed["scen_id"] in chain_out
    assert failed["feat_id"] in chain_out
    assert "tickets: TKT-" in chain_out
    assert "spans: SPAN-" in chain_out
    assert "requests (MSG mentions): MSG-" in chain_out


def test_episode_e2e_trace_surface(tmp_path, monkeypatch, capsysbinary):
    """The non-embedded R26 trace commands over the same settled episode."""
    lib = init_library(tmp_path, monkeypatch, {})
    with Store(lib / "library.db") as store:
        episode_id, _ = run_full_episode(store, tmp_path)
        ticket = store.conn.execute(
            "SELECT s.id AS span, s.ticket_id AS tkt FROM trace_span s ORDER BY s.id"
        ).fetchone()
        scen = store.conn.execute(
            "SELECT id FROM trace_scen WHERE result = 'fail'"
        ).fetchone()["id"]
    tkt, span = ticket["tkt"], ticket["span"]

    rc, out, _ = capture(capsysbinary, "episode", "status", episode_id, "--dir", lib)
    assert rc == 0
    assert "status: frontier_exhausted" in out
    assert "settlement score:" in out

    rc, out, _ = capture(capsysbinary, "trace", "spans", "--ticket", tkt, "--dir", lib)
    assert rc == 0 and span in out

    rc, out, _ = capture(
        capsysbinary, "trace", "spans", "--episode", episode_id, "--dir", lib
    )
    assert rc == 0 and span in out

    rc, out, _ = capture(
        capsysbinary, "trace", "spans", "--episode", episode_id,
        "--increment", 1, "--dir", lib,
    )
    assert rc == 0 and span in out

    rc, out, _ = capture(capsysbinary, "trace", "iterations", tkt, "--dir", lib)
    assert rc == 0 and "src/index.ts" in out

    rc, out, _ = capture(capsysbinary, "trace", "evidence", scen, "--dir", lib)
    assert rc == 0 and "judge_input" in out

    rc, out, _ = capture(capsysbinary, "trace", "transcript", span, "--dir", lib)
    assert rc == 0 and "transcripts/" in out


# --- idea provenance (003 R27) ----------------------------------------------------

IDEA = dict(
    precondition="A search returns bookmarks the user did not ask to filter",
    action="Compare the clone result set to the query terms before matching",
    expected_outcome="Only bookmarks matching the query are listed",
)


def doc_key() -> str:
    return "search_document: " + build_idea_text(**IDEA)


def test_episode_e2e_reflected_idea_carries_provenance(
    tmp_path, monkeypatch, capsysbinary
):
    fx = tmp_path / "judge-fixtures"
    lib = init_library(tmp_path, monkeypatch, {doc_key(): [1.0, 0, 0, 0, 0, 0, 0, 0]})
    db = lib / "library.db"
    with Store(db) as store:
        episode_id, feat_ids = run_full_episode(store, tmp_path)
        scen = store.conn.execute(
            "SELECT id FROM trace_scen WHERE result = 'fail'"
        ).fetchone()["id"]
        tkt = store.conn.execute(
            "SELECT id FROM trace_tkt ORDER BY id"
        ).fetchone()["id"]
        planner_agent = store.conn.execute(
            "SELECT a.id AS id FROM agents a JOIN families f ON f.id = a.family_id"
            " WHERE f.name = 'planner'"
        ).fetchone()["id"]
        # Cold start (zero insights) → the taxonomy prompt; record its fixture.
        prompt = build_taxonomy_prompt(store, build_idea_text(**IDEA), None)
    write_fixture(
        fx, prompt, JUDGE_SCHEMA, "sonnet",
        envelope_for(taxonomy_new_skill(planner_agent)),
    )
    monkeypatch.setenv(judge.FIXTURES_ENV, str(fx))

    rc, out, err = capture(
        capsysbinary,
        "add-idea", "--dir", lib, "--batch", "reflect-1",
        "--precondition", IDEA["precondition"],
        "--action", IDEA["action"],
        "--expected-outcome", IDEA["expected_outcome"],
        "--episode", episode_id, "--scenario", scen, "--ticket", tkt,
    )
    assert rc == 0, err
    assert "registered insight 1" in out
    assert f"provenance: episode={episode_id}" in out

    with Store(db) as store:
        row = store.get_insight(1)
    assert row["episode_id"] == episode_id
    assert row["evidence_scenario_id"] == scen
    assert row["evidence_ticket_id"] == tkt


def test_add_idea_unknown_episode_rejected(tmp_path, monkeypatch, capsysbinary):
    # No judge/embedder is spent: the ref is validated up front, so an unknown
    # episode (or scenario/ticket) is a fast, actionable non-zero exit (003 R27).
    lib = init_library(tmp_path, monkeypatch, {})
    base = [
        "add-idea", "--dir", lib, "--batch", "b",
        "--precondition", IDEA["precondition"],
        "--action", IDEA["action"],
        "--expected-outcome", IDEA["expected_outcome"],
    ]
    rc, _, err = capture(capsysbinary, *base, "--episode", 999)
    assert rc == 1
    assert "unknown episode 999" in err

    with Store(lib / "library.db") as store:
        run_full_episode(store, tmp_path)  # episode 1 now exists
    rc, _, err = capture(capsysbinary, *base, "--episode", 1, "--scenario", "SCEN-nope")
    assert rc == 1
    assert "unknown scenario" in err
    rc, _, err = capture(capsysbinary, *base, "--episode", 1, "--ticket", "TKT-nope")
    assert rc == 1
    assert "unknown ticket" in err
    # Nothing was written on any rejected path.
    with Store(lib / "library.db") as store:
        assert store.conn.execute(
            "SELECT COUNT(*) AS n FROM insights"
        ).fetchone()["n"] == 0


def test_readme_documents_reflection_and_frozen_replay():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    # The manual-reflection workflow: read report -> trace queries -> add_idea.
    assert "af episode report" in readme
    assert "af trace chain" in readme
    assert "af add-idea --episode" in readme
    assert "reflection" in readme.lower()
    # The frozen-replay hand-verification step (a Phase 2 exit criterion).
    assert "frozen replay set" in readme.lower()
    assert "hand-verif" in readme.lower()
    assert "non-empty" in readme.lower()
