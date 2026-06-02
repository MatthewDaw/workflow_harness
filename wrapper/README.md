# claude+ wrapper (Go)

The `claude+` terminal wrapper: a per-repo daemon that hosts the real `claude`
CLI over a PTY, multiplexes auto-named sessions, captures events, and streams
them to Command HQ over an outbound WebSocket. Built to run over SSH on
arbitrary remote hosts (single static binary, no inbound ports).

See `docs/plans/2026-06-01-001-feat-claude-plus-command-hq-plan.md`, units
U12–U18.

## Status

Units U12–U18 authored. **Unverified: Go is not installed in this environment**,
so `go build`/`go test`/`go mod tidy` have not been run. Before building:

1. Install Go 1.23+.
2. From `wrapper/`, run `go mod tidy` to generate `go.sum` (the module pins are
   in `go.mod`; `go.sum` is intentionally not hand-written).
3. `go build ./...` and `go test ./...`.

## Prerequisites

- Go 1.23+ (`go version`).

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
