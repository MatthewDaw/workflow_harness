"""Orchestrator-managed dev server for browser checks (plan-002 U6, R15).

The verifier only DRIVES the browser; the server lifecycle is real
orchestration work owned here (KTD Q9): start on a single configurable port
(the command is launched strict — e.g. vite ``--strictPort`` — so a busy port
fails loudly instead of silently drifting), readiness probe, teardown, hard
timeout. A bind failure retries with a new port inside the readiness loop:

- a pre-flight TCP probe skips ports something else already serves (the
  deterministic strict-port conflict), and
- a child that exits during the readiness window (how ``--strictPort``
  actually reports a lost bind race) also advances to the next port.

A child that stays alive but never becomes ready is HUNG: it is killed at the
readiness timeout and raised — never retried (the port was not the problem).

Readiness is a TCP connect probe: framework-agnostic, and sufficient because a
strict-port server owns its port once bound. The tiny race where a foreign
process grabs the port between pre-flight and the child's bind is closed by
requiring the child to still be alive when the probe succeeds.

Dynamic per-episode port-namespacing is a marked Phase 3 seam (§15 invariant 4
concerns parallel-episode isolation); Phase 1 is a single configurable port
with bounded linear retry. All behavior tunables (port, attempts, readiness
timeout) are caller-supplied per the U3 precedent — nothing here hardcodes one.

Hold-open semantics (plan-003 U7, R7): dual-app settlement needs the clone dev
server to survive across explorer UAT and the settlement rubric, while nested
users (UAT session contexts, ``with`` blocks) keep calling ``stop()`` on their
way out. ``hold_open()`` marks the server held: every ``stop()`` while held is
DEFERRED (recorded, not executed) so only the orchestrator — the lifecycle
owner — actually tears it down via ``release()``, which executes a deferred
stop if one arrived during the hold. Ownership stays exactly where R7 puts it.

:class:`DevServerError` subclasses ``WorkspaceError`` deliberately: a server
that cannot start is infrastructure failing under the run, and that routes to
the orchestrator's checkpoint-and-``aborted_error`` arm (U4) instead of
escaping the state machine.
"""

from __future__ import annotations

import logging
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agent_families.pipeline.workspace import WorkspaceError

logger = logging.getLogger(__name__)

# Substituted into every command element that carries it.
PORT_TOKEN = "{port}"

# Process/probe hygiene constants (not behavior tunables — the behavior
# tunable is the caller-supplied readiness timeout).
_PROBE_CONNECT_TIMEOUT_S = 0.25
_PROBE_INTERVAL_S = 0.05
_KILL_REAP_S = 10.0


class DevServerError(WorkspaceError):
    """The dev server could not start, bind, or become ready (R15)."""


@dataclass(frozen=True)
class DevServerConfig:
    """One server's launch envelope. ``command`` must carry ``{port}`` in at
    least one element (strict-port discipline: the server is TOLD its port)."""

    command: tuple[str, ...]
    port: int
    port_attempts: int
    readiness_timeout_s: float
    cwd: Path | None = None
    host: str = "127.0.0.1"
    log_path: Path | None = None

    def __post_init__(self) -> None:
        if not self.command or not all(str(c).strip() for c in self.command):
            raise DevServerError(
                "DevServerConfig.command must be a non-empty tuple of"
                " non-empty strings"
            )
        if not any(PORT_TOKEN in element for element in self.command):
            raise DevServerError(
                f"DevServerConfig.command must carry the {PORT_TOKEN} token in"
                " at least one element (the orchestrator allocates the port,"
                " R15) — got: " + " ".join(self.command)
            )
        if not 1 <= self.port <= 65535:
            raise DevServerError(f"port must be in 1..65535, got {self.port}")
        if self.port_attempts < 1:
            raise DevServerError(
                f"port_attempts must be >= 1, got {self.port_attempts}"
            )
        if self.readiness_timeout_s <= 0:
            raise DevServerError(
                f"readiness_timeout_s must be positive, got"
                f" {self.readiness_timeout_s}"
            )

    def argv_for(self, port: int) -> list[str]:
        return [element.replace(PORT_TOKEN, str(port)) for element in self.command]


def _port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), _PROBE_CONNECT_TIMEOUT_S):
            return True
    except OSError:
        return False


class DevServer:
    """One managed server process: start (ready-gated), url, stop. Reusable as
    a context manager; ``stop()`` is idempotent."""

    def __init__(self, config: DevServerConfig) -> None:
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._log_handle = None
        self.port: int | None = None
        self._held = False
        self._deferred_stop = False

    @property
    def held(self) -> bool:
        """True while the orchestrator holds this server open (003 R7)."""
        return self._held

    @property
    def url(self) -> str:
        if self.port is None:
            raise DevServerError("dev server is not running; call start() first")
        return f"http://{self.config.host}:{self.port}"

    def __enter__(self) -> DevServer:
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()

    # --- lifecycle -----------------------------------------------------------

    def start(self) -> int:
        """Spawn and block until the readiness probe passes; return the port.

        Tries ``port .. port + port_attempts - 1``: busy ports (pre-flight) and
        bind-failure exits (child dies in the readiness window) advance to the
        next candidate; a hung child is killed at ``readiness_timeout_s`` and
        raised without retry (R15).
        """
        if self._proc is not None:
            raise DevServerError("dev server already started; stop() it first")
        cfg = self.config
        attempts: list[str] = []
        for candidate in range(cfg.port, cfg.port + cfg.port_attempts):
            if candidate > 65535:
                break
            if _port_open(cfg.host, candidate):
                attempts.append(f"port {candidate}: already in use (pre-flight)")
                continue
            proc = self._spawn(candidate)
            deadline = time.monotonic() + cfg.readiness_timeout_s
            exited = False
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    exited = True
                    break
                # ready = probe connects AND the child still owns the port
                if _port_open(cfg.host, candidate) and proc.poll() is None:
                    self._proc = proc
                    self.port = candidate
                    logger.info(
                        "dev server ready on %s:%d (pid %d)",
                        cfg.host,
                        candidate,
                        proc.pid,
                    )
                    return candidate
                time.sleep(_PROBE_INTERVAL_S)
            if exited:
                # strict-port bind failure (or crash): retry with a new port
                attempts.append(
                    f"port {candidate}: server exited {proc.returncode} before"
                    f" becoming ready{self._log_tail()}"
                )
                self._close_log()
                continue
            # alive but never ready: hung — kill at the hard timeout, no retry
            self._kill(proc)
            self._close_log()
            raise DevServerError(
                f"dev server did not become ready on {cfg.host}:{candidate}"
                f" within {cfg.readiness_timeout_s}s and was killed (hung"
                f" server, R15){self._log_tail()}"
            )
        self._close_log()
        raise DevServerError(
            "dev server could not bind any candidate port"
            f" ({cfg.port}..{cfg.port + cfg.port_attempts - 1}):\n  "
            + "\n  ".join(attempts)
        )

    def hold_open(self) -> None:
        """Mark this server held across UAT and settlement (003 R7).

        While held, ``stop()`` calls are deferred instead of executed — the
        orchestrator alone ends the hold with :meth:`release`. Idempotent;
        requires a running server (holding nothing open is a caller bug).
        """
        if self._proc is None:
            raise DevServerError(
                "cannot hold open a dev server that is not running (003 R7):"
                " start() it first"
            )
        self._held = True

    def release(self) -> None:
        """End the hold (003 R7); a stop() deferred during the hold runs now.

        Idempotent — safe in ``finally`` blocks whether or not a hold exists.
        """
        self._held = False
        if self._deferred_stop:
            self._deferred_stop = False
            self.stop()

    def stop(self) -> None:
        """Teardown (R15): kill the child and reap it. Safe to call twice.

        While held open (003 R7) the stop is deferred: recorded and executed
        by :meth:`release`, never here — the orchestrator owns the lifecycle
        across UAT and settlement.
        """
        if self._held:
            self._deferred_stop = True
            logger.info(
                "dev server stop() deferred: held open across UAT/settlement"
                " (003 R7); release() will execute it"
            )
            return
        proc, self._proc = self._proc, None
        self.port = None
        if proc is not None and proc.poll() is None:
            self._kill(proc)
        self._close_log()

    # --- plumbing --------------------------------------------------------------

    def _spawn(self, port: int) -> subprocess.Popen:
        cfg = self.config
        if cfg.log_path is not None:
            cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_handle = cfg.log_path.open(
                "a", encoding="utf-8", newline="\n"
            )
            out = self._log_handle
        else:
            out = subprocess.DEVNULL
        argv = cfg.argv_for(port)
        try:
            return subprocess.Popen(
                argv,
                cwd=str(cfg.cwd) if cfg.cwd is not None else None,
                stdout=out,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self._close_log()
            raise DevServerError(
                f"dev server command could not be spawned"
                f" ({subprocess.list2cmdline(argv)}): {exc}"
            ) from exc

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            if sys.platform == "win32":
                # Kill the TREE: dev-server commands routinely run behind
                # launcher shims (npm.cmd, uv-venv python trampolines) whose
                # children would otherwise keep the port bound (Windows KTD).
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            proc.kill()  # no-op if taskkill already got it; the kill elsewhere
            proc.wait(timeout=_KILL_REAP_S)
        except Exception:  # noqa: BLE001 - reap is best-effort post-kill
            logger.warning("dev server pid %d did not reap cleanly", proc.pid)

    def _close_log(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            finally:
                self._log_handle = None

    def _log_tail(self) -> str:
        """A short diagnostic tail from the server log, when one exists."""
        path = self.config.log_path
        if path is None or not path.exists():
            return ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            return ""
        if not text:
            return ""
        return "; server log tail: " + text[-500:]
