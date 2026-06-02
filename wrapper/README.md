# claude+ wrapper (Go)

The `claude+` terminal wrapper: a per-repo daemon that hosts the real `claude`
CLI over a PTY, multiplexes auto-named sessions, captures events, and streams
them to Command HQ over an outbound WebSocket. Built to run over SSH on
arbitrary remote hosts (single static binary, no inbound ports).

See `docs/plans/2026-06-01-001-feat-claude-plus-command-hq-plan.md`, units
U12–U18.

## Status

Module scaffold only. Implementation begins at U12.

## Prerequisites

- Go 1.23+ (`go version`) — **not yet installed in this environment**; install
  before building the wrapper.

## Layout (planned)

```
cmd/claude-plus/      CLI entrypoint (ls, attach, run)
internal/daemon/      per-repo daemon, attach protocol
internal/pty/         PTY multiplexing, session lifecycle, auto-name
internal/capture/     JSONL transcript tail + hooks + cost -> events
internal/transport/   outbound WS client, offline buffer, control receiver
internal/tui/         Bubble Tea tabs + status line
internal/config/      ~/.claude sync
```
