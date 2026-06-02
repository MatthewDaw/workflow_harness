//go:build !windows

package daemon

import (
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/pty"
)

// fakeSpawn runs `cat` as a stand-in for `claude` so tests need no claude
// install: `cat` echoes PTY stdin back to stdout, which lets us assert I/O.
func fakeSpawn(repoRoot, sessionID string) pty.CmdSpec {
	return pty.CmdSpec{Name: "cat", Dir: repoRoot}
}

// startTestDaemon starts an in-process daemon on a temp HOME so it doesn't touch
// the developer's real ~/.claude-plus.
func startTestDaemon(t *testing.T) (*Daemon, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	repo := t.TempDir()

	d, err := New(repo, fakeSpawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = d.Serve() }()

	// Wait for the daemon to register its loopback address and answer a ping.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if e, ok, _ := Find(repo); ok && alive(e.Sock) {
			return d, repo
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("daemon never came up")
	return nil, ""
}

// TestAttachDetachReattach is the SSH-disconnect contract: a client attaches,
// detaches, and a second client re-attaches the same surviving daemon and
// sessions. (U12 execution note: start with this failing test.)
func TestAttachDetachReattach(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	c1, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial 1: %v", err)
	}
	go func() { _ = c1.Run() }()
	// The daemon auto-spawns a session on first attach.
	waitFor(t, func() bool { return d.Mux().Count() == 1 })

	if err := c1.Detach(); err != nil {
		t.Fatalf("detach: %v", err)
	}

	// Daemon and its session survive the detach.
	if d.Mux().Count() != 1 {
		t.Fatalf("session should survive detach, count=%d", d.Mux().Count())
	}

	c2, err := Dial(repo)
	if err != nil {
		t.Fatalf("re-attach: %v", err)
	}
	defer c2.Detach()
	go func() { _ = c2.Run() }()
	// Re-attach must not spawn a duplicate; still exactly one session.
	if d.Mux().Count() != 1 {
		t.Fatalf("re-attach should reuse session, count=%d", d.Mux().Count())
	}
}

// TestLsListsDaemon verifies the registry surfaces a running daemon for `ls`.
func TestLsListsDaemon(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	entries, err := List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 1 {
		t.Fatalf("want 1 daemon, got %d", len(entries))
	}
	e := entries[0]
	if e.Repo != repo || e.State != StateRunning {
		t.Errorf("unexpected entry: %+v", e)
	}
	if e.Index != 0 {
		t.Errorf("first entry index should be 0, got %d", e.Index)
	}
}

// TestByIndexOutOfRange verifies a clear error for `--session=N` overflow.
func TestByIndexOutOfRange(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	if _, err := ByIndex(7); err == nil {
		t.Error("expected out-of-range error")
	}
}

var _ pty.SpawnFunc = fakeSpawn

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}
