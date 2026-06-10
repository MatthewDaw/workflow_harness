package pty

import (
	"runtime"
	"testing"
)

// killSpawn returns a cross-platform long-running child so a spawned session is
// still alive when Kill closes it (and Session.Close has a real PTY/cmd to tear
// down). Mirrors session_shutdown_test's helper.
func killSpawn(repoRoot, sessionID string) CmdSpec {
	if runtime.GOOS == "windows" {
		return CmdSpec{Name: "ping", Args: []string{"-n", "60", "127.0.0.1"}}
	}
	return CmdSpec{Name: "sleep", Args: []string{"60"}}
}

// TestMuxKillRemovesAndRefocuses proves Kill removes the target session from the
// sub-tab list and re-focuses a surviving neighbor (the same bookkeeping
// onSessionExit performs), and that killing the last session leaves the list
// empty (no auto-spawn on an empty transition).
func TestMuxKillRemovesAndRefocuses(t *testing.T) {
	m := NewMux("", 80, 24, killSpawn)

	s1, err := m.Spawn("")
	if err != nil {
		t.Fatalf("Spawn s1: %v", err)
	}
	s2, err := m.Spawn("")
	if err != nil {
		t.Fatalf("Spawn s2: %v", err)
	}
	if m.Count() != 2 {
		t.Fatalf("count = %d, want 2", m.Count())
	}

	// A client focused on s1 must be re-pointed to a survivor when s1 is killed.
	m.RegisterClient("A", 80, 24)
	if err := m.SetClientFocus("A", s1.ID); err != nil {
		t.Fatalf("SetClientFocus: %v", err)
	}

	m.Kill(s1.ID)

	if m.Count() != 1 {
		t.Fatalf("after kill count = %d, want 1", m.Count())
	}
	if m.Get(s1.ID) != nil {
		t.Error("killed session s1 still present")
	}
	if m.Get(s2.ID) == nil {
		t.Error("survivor s2 missing")
	}
	if got := focusedID(m.ListFor("A")); got != s2.ID {
		t.Errorf("client focus = %q, want survivor %q", got, s2.ID)
	}

	// Killing the last session leaves the list empty (no auto-spawn).
	m.Kill(s2.ID)
	if m.Count() != 0 {
		t.Fatalf("after killing last, count = %d, want 0", m.Count())
	}
	m.mu.RLock()
	fi := m.focusIdx
	m.mu.RUnlock()
	if fi != -1 {
		t.Errorf("focusIdx = %d, want -1 when no sessions remain", fi)
	}
}

// TestMuxKillIdempotentWithOnSessionExit proves Kill is safe against the pump's
// own later EOF-driven onSessionExit for the same id: the second removal is a
// harmless no-op (no panic, no double-remove) because the id is already gone.
func TestMuxKillIdempotentWithOnSessionExit(t *testing.T) {
	m := NewMux("", 80, 24, killSpawn)
	a, err := m.Spawn("")
	if err != nil {
		t.Fatalf("Spawn a: %v", err)
	}
	b, err := m.Spawn("")
	if err != nil {
		t.Fatalf("Spawn b: %v", err)
	}

	m.Kill(a.ID)
	if m.Count() != 1 {
		t.Fatalf("after kill count = %d, want 1", m.Count())
	}

	// The pump goroutine for the killed session will later observe EOF and call
	// onSessionExit(a.ID). Simulate that here: it must be a no-op.
	m.onSessionExit(a.ID)
	if m.Count() != 1 {
		t.Fatalf("onSessionExit after Kill changed count to %d, want 1", m.Count())
	}
	if m.Get(b.ID) == nil {
		t.Error("survivor b removed by double onSessionExit")
	}

	// Killing an unknown id is a no-op too.
	m.Kill("does-not-exist")
	if m.Count() != 1 {
		t.Fatalf("Kill unknown changed count to %d, want 1", m.Count())
	}

	m.CloseAll()
}
