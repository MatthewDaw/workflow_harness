"""plan-002 U6: orchestrator-managed dev server (R15).

Fully offline: the "dev server" is a tiny strict-bind python TCP script — it
exits nonzero when its port is taken (exactly vite ``--strictPort``'s
behavior), optionally delays before binding, and can be told to refuse a
specific port (simulating a bind race the pre-flight probe cannot see).

## Conformance

Scenario / invariant -> test mapping (plan-002 U6, dev-server slice of R15):

- readiness probe gates the verifier start (start() returns only once the
  server actually serves): ``test_start_returns_only_after_server_is_ready``
- ``--strictPort`` bind failure retries with a new port inside the readiness
  loop — both the pre-flight-visible conflict and the child-exit path:
  ``test_busy_port_retries_with_a_new_port``,
  ``test_child_bind_failure_retries_with_a_new_port``
- teardown on verdict: ``test_stop_tears_the_server_down`` (+ the context
  manager variant)
- hung server killed at the hard timeout, never retried:
  ``test_hung_server_is_killed_at_readiness_timeout``
- exhausted port attempts fail actionably:
  ``test_exhausted_port_attempts_is_actionable``
- config validation (port token required, positive budgets):
  ``test_config_validation``
"""

from __future__ import annotations

import socket
import sys
import time

import pytest

from agent_families.pipeline.devserver import (
    DevServer,
    DevServerConfig,
    DevServerError,
)
from agent_families.pipeline.workspace import WorkspaceError

# argv: port [delay_s] [fail_port] [marker_file]
SERVER_PY = """\
import socket, sys, time

port = int(sys.argv[1])
delay = float(sys.argv[2]) if len(sys.argv) > 2 else 0.0
fail_port = int(sys.argv[3]) if len(sys.argv) > 3 else -1
marker = sys.argv[4] if len(sys.argv) > 4 else ""
if port == fail_port:
    sys.stderr.write("Port %d is in use (strictPort)\\n" % port)
    sys.exit(1)
time.sleep(delay)
server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    server.bind(("127.0.0.1", port))
except OSError:
    sys.stderr.write("bind failed: port %d in use\\n" % port)
    sys.exit(1)
server.listen(5)
if marker:
    with open(marker, "w") as fh:
        fh.write("started\\n")
while True:
    conn, _ = server.accept()
    conn.close()
"""


@pytest.fixture
def server_script(tmp_path):
    script = tmp_path / "fake-dev-server.py"
    script.write_text(SERVER_PY, encoding="utf-8", newline="\n")
    return script


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def connectable(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), 0.25):
            return True
    except OSError:
        return False


def config(script, port, *extra, log=None, attempts=3, readiness=10.0, **kw):
    return DevServerConfig(
        command=(sys.executable, str(script), "{port}", *map(str, extra)),
        port=port,
        port_attempts=attempts,
        readiness_timeout_s=readiness,
        log_path=log,
        **kw,
    )


# --- readiness gates the verifier start (R15) ---------------------------------------


def test_start_returns_only_after_server_is_ready(server_script, tmp_path):
    port = free_port()
    server = DevServer(config(server_script, port, 0.4))  # binds after 0.4s
    started = time.monotonic()
    bound = server.start()
    try:
        elapsed = time.monotonic() - started
        # start() blocked through the pre-bind delay: the probe gated it
        assert elapsed >= 0.35
        assert bound == port
        assert server.url == f"http://127.0.0.1:{port}"
        assert connectable(port)
    finally:
        server.stop()


# --- strict-port bind failure retries with a new port ---------------------------------


def test_busy_port_retries_with_a_new_port(server_script):
    port = free_port()
    with socket.socket() as occupier:
        occupier.bind(("127.0.0.1", port))
        occupier.listen(1)
        server = DevServer(config(server_script, port))
        bound = server.start()
        try:
            # the occupied port was skipped; the next candidate served
            assert bound == port + 1
            assert connectable(bound)
        finally:
            server.stop()


def test_child_bind_failure_retries_with_a_new_port(server_script, tmp_path):
    # the child itself refuses the first port (a bind race the pre-flight
    # probe cannot see): its exit during the readiness window must advance
    # the loop to the next candidate
    port = free_port()
    log = tmp_path / "devserver.log"
    server = DevServer(config(server_script, port, 0, port, log=log))
    bound = server.start()
    try:
        assert bound == port + 1
        assert connectable(bound)
    finally:
        server.stop()


def test_exhausted_port_attempts_is_actionable(server_script):
    port = free_port()
    # every candidate refuses (fail_port matching is moot: use two occupiers)
    with socket.socket() as one, socket.socket() as two:
        one.bind(("127.0.0.1", port))
        one.listen(1)
        two.bind(("127.0.0.1", port + 1))
        two.listen(1)
        server = DevServer(config(server_script, port, attempts=2))
        with pytest.raises(DevServerError, match="could not bind"):
            server.start()


# --- teardown (R15) -----------------------------------------------------------------


def test_stop_tears_the_server_down(server_script):
    server = DevServer(config(server_script, free_port()))
    bound = server.start()
    assert connectable(bound)
    proc = server._proc
    server.stop()
    assert proc.poll() is not None  # child reaped
    assert not connectable(bound)
    assert server.port is None
    server.stop()  # idempotent


def test_context_manager_stops_on_exit(server_script):
    with DevServer(config(server_script, free_port())) as server:
        bound = server.port
        assert connectable(bound)
    assert not connectable(bound)


# --- hung server killed at the hard timeout (R15) --------------------------------------


def test_hung_server_is_killed_at_readiness_timeout(server_script):
    port = free_port()
    # delay 600s: alive but never binds -> hung, killed at the timeout, no retry
    server = DevServer(config(server_script, port, 600, readiness=0.5))
    started = time.monotonic()
    with pytest.raises(DevServerError, match="did not become ready"):
        server.start()
    assert time.monotonic() - started < 30
    assert not connectable(port)
    assert server._proc is None or server._proc.poll() is not None


# --- config validation -------------------------------------------------------------------


def test_config_validation(server_script):
    good = dict(
        command=(sys.executable, str(server_script), "{port}"),
        port=4000,
        port_attempts=3,
        readiness_timeout_s=5.0,
    )
    with pytest.raises(DevServerError, match="\\{port\\}"):
        DevServerConfig(**{**good, "command": (sys.executable, str(server_script))})
    with pytest.raises(DevServerError, match="port_attempts"):
        DevServerConfig(**{**good, "port_attempts": 0})
    with pytest.raises(DevServerError, match="readiness_timeout_s"):
        DevServerConfig(**{**good, "readiness_timeout_s": 0})
    with pytest.raises(DevServerError, match="port"):
        DevServerConfig(**{**good, "port": 0})
    # DevServerError routes to the orchestrator's aborted_error arm (U4)
    assert issubclass(DevServerError, WorkspaceError)


def test_url_requires_running_server(server_script):
    server = DevServer(config(server_script, free_port()))
    with pytest.raises(DevServerError, match="not running"):
        _ = server.url
