"""plan-003 U2: linkding target lifecycle (R5, R6, R7 port-table half).

Two layers, per the plan's test-tier decision:

- **Offline** (the default suite): every decision branch via the injectable
  ``runner``/``http`` seams plus byte-level checks on the committed compose
  file and seed manifest. No docker, no network.
- **Docker-required** (``-m docker``, auto-skipped when docker is absent):
  the five live scenarios against the real pinned stack.

## Conformance

Unit test scenarios (plan-003 U2, marked docker-required) -> tests:

- boot reaches healthy: ``test_boot_reaches_healthy`` (live);
  poll mechanics offline in ``test_wait_healthy_polls_until_healthy`` /
  ``test_wait_healthy_times_out``
- seed produces expected bookmark/tag counts via API:
  ``test_seed_produces_expected_counts`` (live); request construction offline
  in ``test_seed_posts_each_bookmark_with_token_auth``
- reset returns API counts to seed state after mutations:
  ``test_reset_returns_to_seed_after_mutation`` (live); sequencing offline in
  ``test_reset_to_seed_sequences_drop_boot_mint_seed``
- token mint idempotent: ``test_token_mint_idempotent`` (live); the
  get_or_create-by-(user, name) one-liner offline in
  ``test_mint_token_command_and_parse``
- digest mismatch detection fails setup with the documented error:
  ``test_digest_mismatch_in_compose_fails_before_boot`` and
  ``test_post_boot_digest_verification`` — both run offline by construction
  (the pin check is a pure file/inspect-output comparison), so this scenario
  needs no docker at all; ``test_unpinned_compose_image_rejected`` covers the
  dropped-pin variant

R7 (port-table half): ``test_port_table_is_static_and_disjoint`` +
``test_compose_matches_port_table_and_env``. R5 environment contract
(named volume, superuser env, background tasks off, digest pin):
``test_compose_pins_image_by_digest``,
``test_compose_uses_named_volume_not_bind_mount``,
``test_compose_matches_port_table_and_env``.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_families.grading.target_env import (
    API_TOKEN_NAME,
    COMPOSE_SERVICE,
    LINKDING_IMAGE_DIGEST,
    LINKDING_IMAGE_REF,
    PORT_TABLE,
    SUPERUSER_NAME,
    DigestMismatchError,
    LinkdingTarget,
    TargetEnvConfig,
    TargetEnvError,
    compose_image_ref,
    expected_seed_counts,
    load_seed_manifest,
)

TARGET_DIR = Path(__file__).resolve().parent.parent / "targets" / "linkding"
COMPOSE_FILE = TARGET_DIR / "docker-compose.yml"
SEED_MANIFEST = TARGET_DIR / "seed_manifest.json"

FAKE_KEY = "deadbeef" * 5  # 40 hex chars, the ApiToken key shape


def make_config(**kw) -> TargetEnvConfig:
    defaults = dict(
        compose_file=COMPOSE_FILE,
        seed_manifest=SEED_MANIFEST,
        readiness_timeout_s=5.0,
        poll_interval_s=0.01,
    )
    return TargetEnvConfig(**{**defaults, **kw})


class FakeRunner:
    """Records argv lists and answers via a handler."""

    def __init__(self, handler):
        self.calls: list[list[str]] = []
        self._handler = handler

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        return self._handler(argv)


def proc(argv, returncode=0, stdout="", stderr="") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


class FakeHttp:
    """Records (method, url, headers, payload) and answers via a handler."""

    def __init__(self, handler):
        self.calls: list[tuple[str, str, dict, dict | None]] = []
        self._handler = handler

    def __call__(self, method, url, *, headers=None, payload=None):
        self.calls.append((method, url, dict(headers or {}), payload))
        return self._handler(method, url)


def healthy_http(method, url):
    if url.endswith("/health"):
        return 200, json.dumps({"version": "1.45.0", "status": "healthy"})
    if method == "POST":
        return 201, "{}"
    return 200, json.dumps({"count": 0, "results": []})


def good_runner_handler(argv):
    if argv[:3] == ["docker", "image", "inspect"]:
        return proc(argv, stdout=json.dumps(
            [f"sissbruecker/linkding@{LINKDING_IMAGE_DIGEST}"]
        ))
    if "shell" in argv:
        return proc(argv, stdout=FAKE_KEY + "\n")
    return proc(argv)


# --- static contract: port table, compose file, seed manifest (R5/R6/R7) -----


def test_port_table_is_static_and_disjoint():
    ports = list(PORT_TABLE.values())
    assert len(ports) == len(set(ports)), "port table entries must be disjoint"
    assert all(1 <= p <= 65535 for p in ports)
    assert {"linkding", "clone"} <= set(PORT_TABLE)
    with pytest.raises(TypeError):
        PORT_TABLE["linkding"] = 1  # static means immutable


def test_compose_pins_image_by_digest():
    ref = compose_image_ref(COMPOSE_FILE)
    assert "@sha256:" in ref, "tag-only pins drift silently (R9)"
    assert ref == LINKDING_IMAGE_REF, (
        "compose file and target_env.py disagree on the image pin"
    )


def test_compose_uses_named_volume_not_bind_mount():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    mounts = re.findall(r"^\s*-\s*['\"]?([^:'\"\s]+):(/[^\s'\"]+)", text, re.MULTILINE)
    data_mounts = [(src, dst) for src, dst in mounts if "linkding/data" in dst]
    assert data_mounts, "compose must mount the linkding data dir"
    for src, _dst in data_mounts:
        # a named volume reference carries no path separators or drive letter
        assert "/" not in src and "\\" not in src and "." not in src, (
            f"data must live in a named volume, not a bind mount (WAL"
            f" corruption risk, R5); got source {src!r}"
        )
        assert re.search(
            rf"^volumes:\s*\n\s+{re.escape(src)}:", text, re.MULTILINE
        ), f"named volume {src!r} must be declared top-level"


def test_compose_matches_port_table_and_env():
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    port = PORT_TABLE["linkding"]
    assert f'"{port}:9090"' in text, "host port must come from the static table (R7)"
    assert f"LD_SUPERUSER_NAME: {SUPERUSER_NAME}" in text
    assert "LD_SUPERUSER_PASSWORD:" in text
    assert 'LD_DISABLE_BACKGROUND_TASKS: "True"' in text, (
        "background tasks must be disabled for determinism (R5)"
    )


def test_seed_manifest_loads_and_counts():
    manifest = load_seed_manifest(SEED_MANIFEST)
    bookmarks, tags = expected_seed_counts(manifest)
    assert bookmarks == len(manifest) >= 10, "seed must be non-trivial"
    assert tags >= 5
    assert all(entry["url"].startswith("https://") for entry in manifest)


def test_seed_manifest_validation_rejects_malformed(tmp_path):
    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(TargetEnvError, match="not valid JSON"):
        load_seed_manifest(bad_json)

    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"bookmarks": []}), encoding="utf-8")
    with pytest.raises(TargetEnvError, match="non-empty 'bookmarks'"):
        load_seed_manifest(empty)

    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps({"bookmarks": [{"url": "https://a"}, {"url": "https://a"}]}),
        encoding="utf-8",
    )
    with pytest.raises(TargetEnvError, match="unique"):
        load_seed_manifest(dup)

    bad_tags = tmp_path / "tags.json"
    bad_tags.write_text(
        json.dumps({"bookmarks": [{"url": "https://a", "tag_names": [1]}]}),
        encoding="utf-8",
    )
    with pytest.raises(TargetEnvError, match="tag_names"):
        load_seed_manifest(bad_tags)

    with pytest.raises(TargetEnvError, match="not found"):
        load_seed_manifest(tmp_path / "missing.json")


def test_config_validates_tunables():
    with pytest.raises(TargetEnvError, match="readiness_timeout_s"):
        make_config(readiness_timeout_s=0)
    with pytest.raises(TargetEnvError, match="poll_interval_s"):
        make_config(poll_interval_s=-1)
    with pytest.raises(TargetEnvError, match="sha256"):
        make_config(expected_digest="latest")
    assert make_config().port == PORT_TABLE["linkding"]
    assert make_config().base_url == f"http://127.0.0.1:{PORT_TABLE['linkding']}"


# --- digest discipline (R9's documented hard setup error) ---------------------


def test_digest_mismatch_in_compose_fails_before_boot():
    wrong = "sha256:" + "0" * 64
    runner = FakeRunner(lambda argv: pytest.fail("docker must not be invoked"))
    target = LinkdingTarget(make_config(expected_digest=wrong), runner=runner)
    with pytest.raises(DigestMismatchError) as exc_info:
        target.up()
    message = str(exc_info.value)
    assert wrong in message and LINKDING_IMAGE_DIGEST in message
    assert "refusing" in message and "re-pin" in message.lower()
    assert runner.calls == [], "the pin check must precede any docker call"


def test_unpinned_compose_image_rejected(tmp_path):
    unpinned = tmp_path / "docker-compose.yml"
    unpinned.write_text(
        COMPOSE_FILE.read_text(encoding="utf-8").replace(
            f"@{LINKDING_IMAGE_DIGEST}", ""
        ),
        encoding="utf-8",
        newline="\n",
    )
    target = LinkdingTarget(
        make_config(compose_file=unpinned),
        runner=FakeRunner(lambda argv: pytest.fail("docker must not be invoked")),
    )
    with pytest.raises(DigestMismatchError, match="not digest-pinned"):
        target.up()


def test_post_boot_digest_verification():
    # a locally-retagged image whose RepoDigests miss the pin is refused
    def retagged(argv):
        if argv[:3] == ["docker", "image", "inspect"]:
            return proc(argv, stdout=json.dumps(
                ["sissbruecker/linkding@sha256:" + "f" * 64]
            ))
        return proc(argv)

    target = LinkdingTarget(make_config(), runner=FakeRunner(retagged))
    with pytest.raises(DigestMismatchError, match="digest mismatch"):
        target.up()

    # the matching digest passes straight through to readiness
    target = LinkdingTarget(
        make_config(),
        runner=FakeRunner(good_runner_handler),
        http=FakeHttp(healthy_http),
    )
    target.up()  # no raise


def test_compose_up_failure_is_loud():
    def boom(argv):
        if "up" in argv:
            return proc(argv, returncode=1, stderr="no space left on device")
        return proc(argv)

    target = LinkdingTarget(make_config(), runner=FakeRunner(boom))
    with pytest.raises(TargetEnvError, match="no space left"):
        target.up()


# --- readiness (R5) -----------------------------------------------------------


def test_wait_healthy_polls_until_healthy():
    responses = iter(
        [
            ConnectionRefusedError("not up yet"),
            (500, json.dumps({"status": "unhealthy"})),
            (200, json.dumps({"version": "1.45.0", "status": "healthy"})),
        ]
    )

    def scripted(method, url):
        item = next(responses)
        if isinstance(item, Exception):
            raise item
        return item

    http = FakeHttp(scripted)
    target = LinkdingTarget(make_config(), runner=FakeRunner(proc), http=http)
    target.wait_healthy()
    # connection-refused and unhealthy were both absorbed, then success
    assert len(http.calls) == 3


def test_wait_healthy_times_out():
    def refused(method, url):
        raise ConnectionRefusedError("nope")

    target = LinkdingTarget(
        make_config(readiness_timeout_s=0.05, poll_interval_s=0.01),
        runner=FakeRunner(proc),
        http=FakeHttp(refused),
    )
    with pytest.raises(TargetEnvError, match=r"/health"):
        target.wait_healthy()


# --- token mint + seeding (R6) -------------------------------------------------


def test_mint_token_command_and_parse():
    runner = FakeRunner(good_runner_handler)
    target = LinkdingTarget(make_config(), runner=runner)
    key = target.mint_token()
    assert key == FAKE_KEY
    assert target.mint_token() == key, "repeat mint returns the same key"

    argv = runner.calls[0]
    assert argv[:4] == ["docker", "compose", "-f", str(COMPOSE_FILE)]
    assert argv[4:6] == ["exec", "-T"], "-T: no TTY allocation headlessly"
    assert argv[6] == COMPOSE_SERVICE
    assert argv[7:11] == ["python", "manage.py", "shell", "-c"]
    code = argv[11]
    # the v1.45 ApiToken model, idempotent by (user, name) — not the DRF token
    assert "from bookmarks.models import ApiToken" in code
    assert "get_or_create" in code
    assert repr(API_TOKEN_NAME) in code
    assert repr(SUPERUSER_NAME) in code


def test_mint_token_garbage_output_is_loud():
    runner = FakeRunner(lambda argv: proc(argv, stdout="System check passed\n"))
    target = LinkdingTarget(make_config(), runner=runner)
    with pytest.raises(TargetEnvError, match="no parseable key"):
        target.mint_token()


def test_seed_posts_each_bookmark_with_token_auth():
    http = FakeHttp(healthy_http)
    target = LinkdingTarget(make_config(), runner=FakeRunner(proc), http=http)
    counts = target.seed("tok123")

    manifest = load_seed_manifest(SEED_MANIFEST)
    assert counts == expected_seed_counts(manifest)
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == len(manifest), "exactly one POST per manifest entry"
    for (_m, url, headers, payload), entry in zip(posts, manifest):
        assert url.endswith("/api/bookmarks/")
        assert headers["Authorization"] == "Token tok123"
        assert payload["url"] == entry["url"]
        assert payload["tag_names"] == entry["tag_names"]


def test_seed_failure_is_loud():
    def reject(method, url):
        return 400, json.dumps({"url": ["Enter a valid URL."]})

    target = LinkdingTarget(
        make_config(), runner=FakeRunner(proc), http=FakeHttp(reject)
    )
    with pytest.raises(TargetEnvError, match="HTTP 400"):
        target.seed("tok123")


def test_reset_to_seed_sequences_drop_boot_mint_seed():
    runner = FakeRunner(good_runner_handler)
    http = FakeHttp(healthy_http)
    target = LinkdingTarget(make_config(), runner=runner, http=http)
    token = target.reset_to_seed()
    assert token == FAKE_KEY, "reset must hand back the freshly-minted token"

    ops = []
    for argv in runner.calls:
        if "down" in argv:
            ops.append("down" + ("+volumes" if "--volumes" in argv else ""))
        elif "up" in argv:
            ops.append("up")
        elif argv[:3] == ["docker", "image", "inspect"]:
            ops.append("inspect")
        elif "shell" in argv:
            ops.append("mint")
    assert ops == ["down+volumes", "up", "inspect", "mint"], (
        "reset = volume drop, re-boot (digest-checked), re-mint, re-seed (R6)"
    )
    posts = [c for c in http.calls if c[0] == "POST"]
    assert len(posts) == len(load_seed_manifest(SEED_MANIFEST))


# --- live docker-required suite (the unit's five scenarios, R5/R6) -------------


docker_required = [
    pytest.mark.docker,
    pytest.mark.skipif(
        shutil.which("docker") is None,
        reason="requires Docker Desktop (docker not on PATH)",
    ),
]


@pytest.fixture(scope="module")
def live_target():
    config = TargetEnvConfig(
        compose_file=COMPOSE_FILE,
        seed_manifest=SEED_MANIFEST,
        readiness_timeout_s=300.0,  # first boot pulls the image
        poll_interval_s=2.0,
    )
    target = LinkdingTarget(config)
    target.up()
    yield target
    target.down(drop_volume=True)


class TestLiveLinkding:
    """Ordered live scenarios: boot -> mint -> seed -> mutate+reset."""

    pytestmark = docker_required

    def test_boot_reaches_healthy(self, live_target):
        live_target.wait_healthy()  # already gated in up(); re-assert explicitly

    def test_token_mint_idempotent(self, live_target):
        first = live_target.mint_token()
        second = live_target.mint_token()
        assert first == second
        assert re.fullmatch(r"[0-9a-f]{40}", first)

    def test_seed_produces_expected_counts(self, live_target):
        token = live_target.mint_token()
        expected = live_target.seed(token)
        assert live_target.api_counts(token) == expected

    def test_reset_returns_to_seed_after_mutation(self, live_target):
        token = live_target.mint_token()
        expected = expected_seed_counts(load_seed_manifest(SEED_MANIFEST))
        live_target.add_bookmark(
            token, "https://example.org/mutation/extra", title="extra"
        )
        assert live_target.api_counts(token)[0] == expected[0] + 1
        fresh_token = live_target.reset_to_seed()
        assert fresh_token != "", "volume drop destroys the old token; re-mint"
        assert live_target.api_counts(fresh_token) == expected
