# claude+ wrapper (Go)

The `claude+` terminal wrapper: a per-repo daemon that hosts the real `claude`
CLI over a PTY, multiplexes auto-named sessions, captures events, and streams
them to Command HQ over an outbound WebSocket. Built to run over SSH on
arbitrary remote hosts (single static binary, no inbound ports).

See `docs/plans/2026-06-01-001-feat-claude-plus-command-hq-plan.md`, units
U12–U18.

## Status

Units U12–U18 implemented and **cross-platform**: builds and tests pass on
Windows, macOS, and Linux. The PTY layer uses [`go-pty`](https://github.com/aymanbagabas/go-pty)
(ConPTY on Windows, native pseudo-terminals on macOS/Linux), and the daemon
attach IPC is loopback TCP (`127.0.0.1:<port>`) rather than Unix sockets, so the
daemon runs on every platform. Verified on Windows: ConPTY hosts a real child
process, and the daemon binds loopback + appears in `claude+ ls`.

Build:

```
cd wrapper
go build ./...     # or: GOOS=windows|darwin|linux GOARCH=amd64|arm64 go build ./cmd/claude-plus
go test ./...
```

## Prerequisites

- Go 1.23+ (`go version`).
- The real `claude` CLI on PATH (the wrapper hosts it).

## Layout

```
cmd/claude-plus/      CLI: claude+, claude+ ls, claude+ --session=N  (U12)
internal/event/       shared event envelope, golden-fixture parity   (U2/U14)
internal/daemon/      per-repo daemon, attach protocol, registry      (U12)
internal/pty/         PTY multiplexing, lifecycle, auto-name          (U13)
internal/capture/     JSONL transcript tail + settings.json hooks     (U14)
internal/transport/   outbound WS client, on-disk ring buffer,
                      control receiver (inject/pause/interrupt)        (U14/U15)
internal/tui/         Bubble Tea tabs + session sub-tabs + status line (U16)
internal/config/      ~/.claude agents+skills sync, drift detection    (U17)
```

## Distribution (U18)

- `.goreleaser.yaml` — cross-compiles darwin/linux × amd64/arm64.
- `../npm/claude-plus` — npm wrapper: per-platform prebuilt binaries via
  `optionalDependencies` + a launcher shim (`npm i -g claude-plus`).
- `../scripts/install.sh` — `curl | sh` installer for bare hosts.
- `../.github/workflows/release.yml` — tag-triggered release + npm publish +
  install smoke job.

## CLI

```
claude+              attach-or-create the per-repo daemon for cwd
claude+ ls           list running daemons (index, repo, host, sessions, state, uptime)
claude+ --session=N  attach to the daemon at registry index N
claude+ --version    print version
```

The daemon survives client disconnect (tmux model): detaching leaves sessions
running and re-attachable from a second terminal — the SSH-disconnect contract,
covered by `internal/daemon/daemon_test.go` (attach → detach → re-attach).
