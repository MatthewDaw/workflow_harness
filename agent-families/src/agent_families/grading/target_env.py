"""linkding target lifecycle: boot, seed, reset, health (plan-003 U2, R5–R7).

The target is the behavioral oracle every grading instrument measures against,
so its environment is pinned hard:

- the image is pinned **by digest** (R9 keys the FEAT registry and all cached
  apparatus to it); the pin lives both here and in the committed compose file,
  and :meth:`LinkdingTarget.up` refuses setup if they disagree or if the
  pulled image's repo digests do not contain the pin
  (:class:`DigestMismatchError` — the documented hard error);
- data lives in a **named volume**, never a Windows bind mount (SQLite WAL
  corruption risk, R5);
- the superuser is minted headlessly via ``LD_SUPERUSER_*`` and background
  tasks are disabled for determinism (R5);
- readiness = polling ``GET /health`` for ``{"status": "healthy"}`` (R5,
  verified against the v1.45.0 source: ``bookmarks/views/health.py``);
- the API token is minted via a ``manage.py shell`` one-liner against the
  v1.45 ``bookmarks.models.ApiToken`` model (NOT the DRF token), idempotent
  by ``(user, name)`` — ``get_or_create`` on exactly those fields (R6);
- seeding loops ``POST /api/bookmarks/`` from the committed manifest (no rate
  limit on the API); **reset-to-seed = volume drop + re-boot + re-seed** (R6).
  The cold reset path costs tens of seconds, amortized per episode. If reset
  time ever dominates, the documented faster alternative is a ``docker cp``
  DB snapshot: after seeding, ``docker compose cp linkding:/etc/linkding/data
  ./seed-snapshot`` once, then restore with ``docker compose cp
  ./seed-snapshot linkding:/etc/linkding/data`` + container restart instead
  of the volume drop (measure before switching — plan-003 Risks).

Ports come from the **static port-allocation table** (R7): one committed
mapping, linkding and the clone dev server disjoint by construction.
Per-episode container/port namespacing stays the Phase 3 seam — this module
owns exactly one static stack (the compose project name pins it).

Subprocess (``docker``/``docker compose``) and HTTP access go through
injectable seams (``runner``, ``http``) so every decision branch here is
testable offline; the live docker-required suite exercises the real stack.
Behavior tunables (readiness timeout, poll interval) are caller-supplied per
the established U3/U6 precedent — nothing here hardcodes one.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

logger = logging.getLogger(__name__)

# --- pins and static tables --------------------------------------------------

LINKDING_IMAGE_REPO = "sissbruecker/linkding"
LINKDING_IMAGE_TAG = "1.45.0"
# Multi-arch manifest-list digest for v1.45.0, resolved from Docker Hub on
# 2026-06-10. Re-pinning is a deliberate apparatus migration (R9): the FEAT
# registry is keyed to this value and a mismatch at setup is a hard error.
LINKDING_IMAGE_DIGEST = (
    "sha256:61b2eb9eed8e5772a473fb7f1f8923e046cb8cbbeb50e88150afd5ff287d4060"
)
LINKDING_IMAGE_REF = (
    f"{LINKDING_IMAGE_REPO}:{LINKDING_IMAGE_TAG}@{LINKDING_IMAGE_DIGEST}"
)

# Static port-allocation table (R7): committed, disjoint, never computed.
# linkding's container port is fixed upstream at 9090; the clone dev server
# (vite preview) gets 4173. Dual-app settlement (U7) reads both from here.
PORT_TABLE: MappingProxyType = MappingProxyType(
    {
        "linkding": 9090,
        "clone": 4173,
    }
)

# Per-episode container/port namespacing (plan-005 U5, R15): the Phase 1/2 seam
# this module reserved (see the module docstring's "stays the Phase 3 seam"
# note) activates here so N episodes can hold disjoint stacks against one
# snapshot. Each episode gets its own compose PROJECT name (Docker isolates
# container/network names by project) and a private block of HOST ports carved
# from a static base so two episodes can never collide by construction — the
# §15 "per-episode environment isolation" invariant, enforced arithmetically
# rather than discovered at bind time. The container-internal ports stay
# upstream-fixed (linkding 9090, clone preview 4173); only the host side is
# namespaced. Base/stride are caller-overridable per the established tunable
# precedent; the carried defaults record provenance.
EPISODE_PORT_BASE = 20000  # PROVENANCE: well above linkding/clone defaults and
# the OS ephemeral range floor; a static allocation, not a discovered port.
EPISODE_PORT_STRIDE = 100  # host-port block per episode; >> services per stack,
# so episode E's block never overlaps episode E+1's.
EPISODE_COMPOSE_PREFIX = "af-ep"


@dataclass(frozen=True)
class EpisodeNamespace:
    """One episode's isolated stack identity: compose project + host ports.

    ``compose_project`` is passed to ``docker compose -p`` so the episode's
    containers, networks, and volumes live in their own namespace; ``ports``
    maps each service to its private HOST port. Two namespaces minted for
    different episode ids carry disjoint project names AND disjoint port sets
    (R15 — isolation by construction, not by probe).
    """

    episode_id: int
    compose_project: str
    ports: Mapping[str, int]


def episode_namespace(
    episode_id: int,
    *,
    services: Sequence[str] = tuple(PORT_TABLE),
    port_base: int = EPISODE_PORT_BASE,
    port_stride: int = EPISODE_PORT_STRIDE,
) -> EpisodeNamespace:
    """Mint the disjoint stack namespace for ``episode_id`` (R15).

    Episode ids are 1-based; episode E owns the host-port block
    ``[port_base + (E-1)*port_stride, … + len(services))``. The stride must
    exceed the service count so neighbouring episodes' blocks never touch, and
    the top of the highest block must stay a valid TCP port. Service→port
    assignment is by sorted service name so it is deterministic regardless of
    the caller's iteration order.
    """
    if episode_id < 1:
        raise TargetEnvError(
            f"episode_id must be a positive 1-based id, got {episode_id}"
        )
    ordered = sorted(set(services))
    if not ordered:
        raise TargetEnvError("episode_namespace needs at least one service")
    if len(ordered) > port_stride:
        raise TargetEnvError(
            f"port_stride {port_stride} is too small for {len(ordered)} services"
            " — neighbouring episode blocks would overlap (R15 disjointness)"
        )
    block = port_base + (episode_id - 1) * port_stride
    top = block + len(ordered) - 1
    if block < 1 or top > 65535:
        raise TargetEnvError(
            f"episode {episode_id} host-port block [{block}, {top}] falls outside"
            " the valid TCP port range — lower port_base/port_stride or cap the"
            " concurrent-episode count"
        )
    ports = {svc: block + i for i, svc in enumerate(ordered)}
    return EpisodeNamespace(
        episode_id=episode_id,
        compose_project=f"{EPISODE_COMPOSE_PREFIX}{episode_id}",
        ports=MappingProxyType(ports),
    )

# Compose service name and the harness's superuser/token identities. These are
# the committed local-harness contract (they appear verbatim in the compose
# file), not secrets and not tunables.
COMPOSE_SERVICE = "linkding"
SUPERUSER_NAME = "admin"
SUPERUSER_PASSWORD = "agent-families-admin"  # noqa: S105 - committed harness cred
API_TOKEN_NAME = "agent-families-harness"

# Hygiene constants (not behavior tunables — those are caller-supplied).
_HTTP_TIMEOUT_S = 30.0
_COMPOSE_TIMEOUT_S = 600.0  # first boot pulls the image; generous hard cap

# The v1.45 ApiToken model carries (key, user FK, name, created); key is
# auto-generated on save, so get_or_create keyed (user, name) is the
# idempotent mint (R6). Verified against bookmarks/models.py at tag v1.45.0.
_MINT_TOKEN_CODE = """\
from bookmarks.models import ApiToken
from django.contrib.auth import get_user_model
user = get_user_model().objects.get(username={username!r})
token, _ = ApiToken.objects.get_or_create(user=user, name={token_name!r})
print(token.key)
"""

_TOKEN_RE = re.compile(r"^[0-9a-f]{40}$")
_IMAGE_LINE_RE = re.compile(r"^\s*image:\s*['\"]?([^'\"\s]+)['\"]?\s*$", re.MULTILINE)


class TargetEnvError(Exception):
    """The target stack could not boot, seed, reset, or respond (R5/R6)."""


class DigestMismatchError(TargetEnvError):
    """The running/pinned target image digest disagrees with the expected pin.

    This is the documented hard setup error (R9): the FEAT registry and every
    cached apparatus artifact are keyed to the pinned digest, so continuing
    against a different image would silently invalidate all of them.
    """


# --- seams --------------------------------------------------------------------

# runner: argv -> CompletedProcess-shaped result (returncode/stdout/stderr).
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess]
# http: (method, url, headers, payload) -> (status, body). Raises OSError on
# connection failure (the not-up-yet signal the readiness poll absorbs).
Http = Callable[..., tuple[int, str]]


def _run_subprocess(argv: Sequence[str]) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(  # noqa: S603 - argv is module-constructed
            list(argv),
            capture_output=True,
            encoding="utf-8",  # Windows KTD: never the locale codepage
            errors="replace",
            timeout=_COMPOSE_TIMEOUT_S,
        )
    except FileNotFoundError as exc:
        raise TargetEnvError(
            "docker is not installed or not on PATH; the linkding target"
            " requires Docker Desktop (plan-003 U2 verification note)"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise TargetEnvError(
            f"docker command timed out after {_COMPOSE_TIMEOUT_S}s:"
            f" {subprocess.list2cmdline(list(argv))}"
        ) from exc


def _http_request(
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    payload: dict | None = None,
) -> tuple[int, str]:
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


# --- config -------------------------------------------------------------------


@dataclass(frozen=True)
class TargetEnvConfig:
    """One linkding stack's envelope. Timeout/poll are caller-supplied
    tunables; the digest defaults to the module pin and is overridable only
    so mismatch handling itself can be exercised."""

    compose_file: Path
    seed_manifest: Path
    readiness_timeout_s: float
    poll_interval_s: float
    expected_digest: str = LINKDING_IMAGE_DIGEST
    superuser: str = SUPERUSER_NAME
    host: str = "127.0.0.1"
    port: int = PORT_TABLE["linkding"]
    # Per-episode isolation (R15): when set, every ``docker compose`` call is
    # scoped with ``-p <project>`` so parallel episodes never share container
    # state. ``None`` keeps the single static stack (Phase 2 default) — the
    # default-stack argv is unchanged, so the pin/seed/mint contract is intact.
    compose_project: str | None = None

    def __post_init__(self) -> None:
        if self.readiness_timeout_s <= 0:
            raise TargetEnvError(
                f"readiness_timeout_s must be positive, got"
                f" {self.readiness_timeout_s}"
            )
        if self.poll_interval_s <= 0:
            raise TargetEnvError(
                f"poll_interval_s must be positive, got {self.poll_interval_s}"
            )
        if not self.expected_digest.startswith("sha256:"):
            raise TargetEnvError(
                f"expected_digest must be a sha256: digest, got"
                f" {self.expected_digest!r}"
            )

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


# --- seed manifest --------------------------------------------------------------


def load_seed_manifest(path: Path) -> list[dict]:
    """Load and validate the committed seed manifest (R6).

    Returns the bookmark entries; raises :class:`TargetEnvError` on any shape
    problem (the manifest is apparatus, so malformation is loud, not skipped).
    """
    if not path.exists():
        raise TargetEnvError(f"seed manifest not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TargetEnvError(f"seed manifest is not valid JSON: {path}: {exc}") from exc
    bookmarks = data.get("bookmarks") if isinstance(data, dict) else None
    if not isinstance(bookmarks, list) or not bookmarks:
        raise TargetEnvError(
            f"seed manifest must carry a non-empty 'bookmarks' list: {path}"
        )
    seen_urls: set[str] = set()
    for i, entry in enumerate(bookmarks):
        if not isinstance(entry, dict) or not str(entry.get("url", "")).strip():
            raise TargetEnvError(
                f"seed manifest bookmark #{i} must be an object with a"
                f" non-empty 'url': {path}"
            )
        url = entry["url"]
        if url in seen_urls:
            raise TargetEnvError(
                f"seed manifest bookmark URLs must be unique; duplicate: {url}"
            )
        seen_urls.add(url)
        tags = entry.get("tag_names", [])
        if not isinstance(tags, list) or not all(
            isinstance(t, str) and t.strip() for t in tags
        ):
            raise TargetEnvError(
                f"seed manifest bookmark #{i} 'tag_names' must be a list of"
                f" non-empty strings: {path}"
            )
    return bookmarks


def expected_seed_counts(manifest: list[dict]) -> tuple[int, int]:
    """(bookmark count, distinct tag count) the post-seed state must show."""
    tags = {t for entry in manifest for t in entry.get("tag_names", [])}
    return len(manifest), len(tags)


def compose_image_ref(compose_file: Path) -> str:
    """Extract the single pinned ``image:`` ref from the compose file."""
    if not compose_file.exists():
        raise TargetEnvError(f"compose file not found: {compose_file}")
    text = compose_file.read_text(encoding="utf-8")
    refs = _IMAGE_LINE_RE.findall(text)
    if len(refs) != 1:
        raise TargetEnvError(
            f"compose file must pin exactly one image, found {len(refs)}:"
            f" {compose_file}"
        )
    return refs[0]


# --- target -------------------------------------------------------------------


class LinkdingTarget:
    """Lifecycle owner for one linkding stack: up, health, mint, seed, reset.

    One-command bring-up on Windows Docker Desktop (the U2 verification)::

        uv run python -c "from pathlib import Path; \\
            from agent_families.grading.target_env import *; \\
            t = LinkdingTarget(TargetEnvConfig( \\
                compose_file=Path('targets/linkding/docker-compose.yml'), \\
                seed_manifest=Path('targets/linkding/seed_manifest.json'), \\
                readiness_timeout_s=180, poll_interval_s=2)); \\
            t.up(); print(t.seed(t.mint_token()))"
    """

    def __init__(
        self,
        config: TargetEnvConfig,
        *,
        runner: Runner | None = None,
        http: Http | None = None,
    ) -> None:
        self.config = config
        self._run = runner or _run_subprocess
        self._http = http or _http_request

    # --- lifecycle -------------------------------------------------------------

    def up(self) -> None:
        """Digest-check, boot, and block until ``/health`` reports healthy.

        Order: the compose-pin check runs BEFORE any docker call (a drifted
        pin must not even pull), the image-digest verification right after
        boot (belt and braces against a locally-retagged image), readiness
        last.
        """
        self.check_compose_pin()
        result = self._compose("up", "-d")
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose up failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )
        self.verify_image_digest()
        self.wait_healthy()
        logger.info("linkding target healthy at %s", self.config.base_url)

    def down(self, *, drop_volume: bool = False) -> None:
        """Stop the stack; ``drop_volume=True`` also drops the named data
        volume (the reset-to-seed path, R6)."""
        argv = ["down"] + (["--volumes"] if drop_volume else [])
        result = self._compose(*argv)
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker compose down failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )

    def reset_to_seed(self) -> str:
        """Reset the target to the committed post-seed state (R6); returns a
        fresh API token (the volume drop destroyed the old one).

        Runs at episode start and again at settlement start. Cold path: volume
        drop + re-boot + re-seed (~tens of seconds, amortized per episode);
        the ``docker cp`` DB-snapshot fast path is documented in the module
        docstring if this ever dominates.
        """
        self.down(drop_volume=True)
        self.up()
        token = self.mint_token()
        self.seed(token)
        return token

    # --- digest discipline -------------------------------------------------------

    def check_compose_pin(self) -> None:
        """The compose file's image ref must carry exactly the expected digest."""
        ref = compose_image_ref(self.config.compose_file)
        if "@sha256:" not in ref:
            raise DigestMismatchError(
                f"target image is not digest-pinned in"
                f" {self.config.compose_file}: {ref!r} — the FEAT registry is"
                " keyed to the image digest (R9); refusing setup"
            )
        pinned = ref.split("@", 1)[1]
        if pinned != self.config.expected_digest:
            raise DigestMismatchError(
                f"target image digest mismatch at setup: expected"
                f" {self.config.expected_digest}, compose file pins {pinned}"
                f" ({self.config.compose_file}). The registry and all cached"
                " apparatus are keyed to the expected digest (R9) — refusing"
                " setup. If the target was deliberately upgraded, re-pin and"
                " re-run registry pre-research."
            )

    def verify_image_digest(self) -> None:
        """The pulled image's repo digests must contain the expected pin."""
        ref = compose_image_ref(self.config.compose_file)
        result = self._run(
            ["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"]
        )
        if result.returncode != 0:
            raise TargetEnvError(
                f"docker image inspect failed for {ref} (exit"
                f" {result.returncode}): {result.stderr.strip()}"
            )
        try:
            repo_digests = json.loads(result.stdout.strip() or "[]")
        except json.JSONDecodeError as exc:
            raise TargetEnvError(
                f"unparseable docker inspect output: {result.stdout!r}"
            ) from exc
        if not any(
            d.endswith("@" + self.config.expected_digest) for d in repo_digests
        ):
            raise DigestMismatchError(
                f"target image digest mismatch at setup: expected"
                f" {self.config.expected_digest}, running image reports"
                f" {repo_digests}. The registry and all cached apparatus are"
                " keyed to the expected digest (R9) — refusing setup. If the"
                " target was deliberately upgraded, re-pin and re-run registry"
                " pre-research."
            )

    # --- readiness -----------------------------------------------------------

    def wait_healthy(self) -> None:
        """Poll ``GET /health`` until ``{"status": "healthy"}`` (R5)."""
        url = f"{self.config.base_url}/health"
        deadline = time.monotonic() + self.config.readiness_timeout_s
        last: str = "no response yet"
        while time.monotonic() < deadline:
            try:
                status, body = self._http("GET", url)
            except OSError as exc:
                last = f"connection failed: {exc}"
            else:
                if status == 200:
                    try:
                        if json.loads(body).get("status") == "healthy":
                            return
                    except json.JSONDecodeError:
                        pass
                last = f"HTTP {status}: {body[:200]}"
            time.sleep(self.config.poll_interval_s)
        raise TargetEnvError(
            f"linkding did not report healthy at {url} within"
            f" {self.config.readiness_timeout_s}s; last: {last}"
        )

    # --- token + seed ---------------------------------------------------------

    def mint_token(self) -> str:
        """Mint (or fetch) the harness API token headlessly (R6).

        ``manage.py shell`` one-liner against the v1.45 ``ApiToken`` model;
        ``get_or_create(user, name)`` makes repeat mints return the same key.
        """
        code = _MINT_TOKEN_CODE.format(
            username=self.config.superuser, token_name=API_TOKEN_NAME
        )
        result = self._compose(
            "exec", "-T", COMPOSE_SERVICE, "python", "manage.py", "shell", "-c", code
        )
        if result.returncode != 0:
            raise TargetEnvError(
                f"API token mint failed (exit {result.returncode}):"
                f" {result.stderr.strip() or result.stdout.strip()}"
            )
        lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        if not lines or not _TOKEN_RE.fullmatch(lines[-1]):
            raise TargetEnvError(
                f"API token mint produced no parseable key; stdout:"
                f" {result.stdout!r}"
            )
        return lines[-1]

    def seed(self, token: str) -> tuple[int, int]:
        """POST every manifest bookmark to ``/api/bookmarks/`` (R6); returns
        the manifest's expected (bookmarks, tags) counts."""
        manifest = load_seed_manifest(self.config.seed_manifest)
        url = f"{self.config.base_url}/api/bookmarks/"
        for entry in manifest:
            payload = {
                key: entry[key]
                for key in ("url", "title", "description", "tag_names")
                if key in entry
            }
            status, body = self._http(
                "POST", url, headers=self._auth(token), payload=payload
            )
            if status != 201:
                raise TargetEnvError(
                    f"seeding bookmark {entry['url']!r} failed: HTTP {status}:"
                    f" {body[:300]}"
                )
        return expected_seed_counts(manifest)

    def api_counts(self, token: str) -> tuple[int, int]:
        """Live (bookmark, tag) counts from the paginated API ``count`` field."""
        return (
            self._count(f"{self.config.base_url}/api/bookmarks/?limit=1", token),
            self._count(f"{self.config.base_url}/api/tags/?limit=1", token),
        )

    def add_bookmark(self, token: str, url: str, **fields) -> None:
        """Create one extra bookmark (the mutation half of reset testing)."""
        status, body = self._http(
            "POST",
            f"{self.config.base_url}/api/bookmarks/",
            headers=self._auth(token),
            payload={"url": url, **fields},
        )
        if status != 201:
            raise TargetEnvError(
                f"bookmark create failed: HTTP {status}: {body[:300]}"
            )

    # --- plumbing --------------------------------------------------------------

    def _compose(self, *args: str) -> subprocess.CompletedProcess:
        base = ["docker", "compose", "-f", str(self.config.compose_file)]
        if self.config.compose_project:
            # Scope the stack to the episode's namespace (R15). Only added when
            # a project is set, so the static single-stack argv is unchanged.
            base += ["-p", self.config.compose_project]
        return self._run([*base, *args])

    @staticmethod
    def _auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Token {token}"}

    def _count(self, url: str, token: str) -> int:
        status, body = self._http("GET", url, headers=self._auth(token))
        if status != 200:
            raise TargetEnvError(f"GET {url} failed: HTTP {status}: {body[:300]}")
        try:
            return int(json.loads(body)["count"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise TargetEnvError(
                f"GET {url} returned no parseable count: {body[:300]}"
            ) from exc
