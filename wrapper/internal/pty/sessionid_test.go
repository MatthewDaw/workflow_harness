package pty

import (
	"runtime"
	"testing"

	"github.com/google/uuid"
)

// hasFlagValue reports whether args contains flag immediately followed by val.
func hasFlagValue(args []string, flag, val string) bool {
	for i := 0; i+1 < len(args); i++ {
		if args[i] == flag && args[i+1] == val {
			return true
		}
	}
	return false
}

// TestDefaultSpawnPassesSessionID proves the wrapper makes itself authoritative
// over the session id: it passes our id to `claude --session-id` so Claude Code
// names its transcript <id>.jsonl. Without this, claude generates its own UUID
// and the transcript tailer (which keys on our id) reads a file that never
// exists — emitting zero tool/message/cost events.
func TestDefaultSpawnPassesSessionID(t *testing.T) {
	const id = "11111111-2222-3333-4444-555555555555"
	spec := DefaultSpawn("/repo", id)
	if !hasFlagValue(spec.Args, "--session-id", id) {
		t.Fatalf("DefaultSpawn must pass --session-id %s; args were %v", id, spec.Args)
	}
}

// fakeEcho is a trivial, cross-platform child so Spawn can start a real PTY
// without a claude install.
func fakeEcho(repoRoot, sessionID string) CmdSpec {
	if runtime.GOOS == "windows" {
		return CmdSpec{Name: "cmd", Args: []string{"/c", "echo hi"}}
	}
	return CmdSpec{Name: "sh", Args: []string{"-c", "echo hi"}}
}

// TestSpawnSessionIDIsValidUUID proves the mux session id is a full UUID, not a
// truncated prefix. Claude Code's transcript filename is its session UUID, so a
// truncated id (the historical uuid[:8]) can never match the transcript path.
func TestSpawnSessionIDIsValidUUID(t *testing.T) {
	m := NewMux("", 80, 24, fakeEcho)
	s, err := m.Spawn("")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	defer m.CloseAll()
	if _, err := uuid.Parse(s.ID); err != nil {
		t.Fatalf("session id %q must be a valid UUID (to match the transcript filename): %v", s.ID, err)
	}
}
