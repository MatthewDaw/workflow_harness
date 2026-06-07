package pty

import (
	"runtime"
	"testing"
	"time"
)

// TestSessionShutdownTerminates launches a long-running child and shuts it down,
// proving Shutdown actually terminates the session (gracefully via SIGTERM on
// Unix, force-fallback on Windows where SIGTERM isn't deliverable), marks it
// done, and returns well inside the grace window. It also asserts a second
// terminate is an idempotent no-op.
func TestSessionShutdownTerminates(t *testing.T) {
	spawn := func(repoRoot, sessionID string) CmdSpec {
		if runtime.GOOS == "windows" {
			// A child that stays alive long enough to still be running at Shutdown.
			return CmdSpec{Name: "ping", Args: []string{"-n", "60", "127.0.0.1"}}
		}
		return CmdSpec{Name: "sleep", Args: []string{"60"}}
	}

	s, err := newSession("sd1", "sd1", "", 80, 24, spawn, false, nil)
	if err != nil {
		t.Fatalf("newSession: %v", err)
	}

	start := time.Now()
	if err := s.Shutdown(5 * time.Second); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}
	if d := time.Since(start); d >= 5*time.Second {
		t.Errorf("Shutdown took %v; it should not have waited out the full grace window", d)
	}
	if got := s.Status(); got != StatusDone {
		t.Errorf("status after shutdown = %q, want %q", got, StatusDone)
	}

	// Idempotent: a second terminate (either path) is a no-op and never errors.
	if err := s.Shutdown(time.Second); err != nil {
		t.Errorf("second Shutdown should be a no-op, got %v", err)
	}
	if err := s.Close(); err != nil {
		t.Errorf("Close after Shutdown should be a no-op, got %v", err)
	}
}
