package daemon

import (
	"strings"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/pty"
)

// TestDaemonLoopbackUp is the cross-platform daemon proof (runs on Windows too,
// unlike the unix-socket/cat test): a daemon binds a loopback TCP port, records
// its address in the registry, answers a ping, and shows up in `ls` as running.
// This exercises the IPC path that replaced Unix sockets so the daemon works on
// Windows + macOS + Linux.
func TestDaemonLoopbackUp(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)        // Unix home
	t.Setenv("USERPROFILE", home) // Windows home (os.UserHomeDir)
	repo := t.TempDir()

	// No client attaches, so the spawn func is never invoked.
	d, err := New(repo, func(string, string) pty.CmdSpec { return pty.CmdSpec{Name: "noop"} })
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = d.Serve() }()
	defer d.Stop()

	var addr string
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if e, ok, _ := Find(repo); ok && alive(e.Sock) {
			addr = e.Sock
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if addr == "" {
		t.Fatal("daemon never came up on loopback")
	}
	if !strings.HasPrefix(addr, "127.0.0.1:") {
		t.Fatalf("expected a 127.0.0.1 loopback address, got %q", addr)
	}

	entries, err := List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 1 || entries[0].State != StateRunning {
		t.Fatalf("ls: want exactly 1 running daemon, got %+v", entries)
	}
}
