//go:build windows

package daemon

import (
	"bufio"
	"net"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/pty"
)

// winSpawn runs a long-lived child that holds a PTY open without exiting, so the
// daemon's auto-spawned session stays "active" for the lifetime of the test.
// `cmd /k` runs an interactive shell that does not exit on its own (we never
// send `exit`), which is the closest Windows analogue to the unix `cat` fake.
func winSpawn(repoRoot, sessionID string) pty.CmdSpec {
	return pty.CmdSpec{Name: "cmd", Args: []string{"/k"}, Dir: repoRoot}
}

func winWaitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(15 * time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}

func winStartDaemon(t *testing.T) (*Daemon, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()
	d, err := New(repo, winSpawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = d.Serve() }()
	winWaitFor(t, func() bool {
		e, ok, _ := Find(repo)
		return ok && compatible(e.Sock)
	})
	return d, repo
}

func winSock(t *testing.T, repo string) string {
	t.Helper()
	e, ok, err := Find(repo)
	if err != nil || !ok {
		t.Fatalf("Find(%q): ok=%v err=%v", repo, ok, err)
	}
	return e.Sock
}

func winMetaExists(t *testing.T, repo string) bool {
	t.Helper()
	_, ok, err := Find(repo)
	if err != nil {
		t.Fatalf("Find: %v", err)
	}
	return ok
}

// TestWinLifecycleStopCleans proves stop tears the daemon down: the socket stops
// answering and the registry record is removed (cross-platform Stop cleanup).
func TestWinLifecycleStopCleans(t *testing.T) {
	d, repo := winStartDaemon(t)
	sock := winSock(t, repo)

	d.Stop()
	winWaitFor(t, func() bool { return !alive(sock) })
	winWaitFor(t, func() bool { return !winMetaExists(t, repo) })

	entries, err := List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("stopped daemon must not appear in ls, got %+v", entries)
	}
	d.Stop() // idempotent
}

// staleListenerWin simulates an older-build daemon on Windows: it answers ping
// and hello with a different ProtocolVersion.
type staleListenerWin struct {
	ln      net.Listener
	version int
}

func newStaleListenerWin(t *testing.T, version int) *staleListenerWin {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("stale listen: %v", err)
	}
	s := &staleListenerWin{ln: ln, version: version}
	go func() {
		for {
			conn, err := s.ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				f, err := readFrame(bufio.NewReader(c))
				if err != nil {
					return
				}
				switch f.Type {
				case FramePing:
					_ = writeFrame(c, Frame{Type: FramePong, Version: s.version})
				case FrameHello:
					_ = writeFrame(c, Frame{Type: FrameAck, Version: s.version, Err: "protocol mismatch"})
				}
			}(conn)
		}
	}()
	return s
}

func (s *staleListenerWin) addr() string { return s.ln.Addr().String() }
func (s *staleListenerWin) close()       { _ = s.ln.Close() }

// TestWinEnsureDaemonReplacesIncompatible proves the proactive replacement path
// on Windows: EnsureDaemon retires an alive-but-incompatible daemon and spawns a
// fresh, compatible one.
func TestWinEnsureDaemonReplacesIncompatible(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()

	stale := newStaleListenerWin(t, ProtocolVersion+1)
	defer stale.close()
	if err := writeMeta(Entry{
		Repo: repo, Sock: stale.addr(), Version: ProtocolVersion + 1,
		Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta: %v", err)
	}

	var fresh *Daemon
	prev := spawnDaemon
	spawnDaemon = func(rr string) error {
		d, err := New(rr, winSpawn)
		if err != nil {
			return err
		}
		fresh = d
		go func() { _ = d.Serve() }()
		return nil
	}
	t.Cleanup(func() {
		spawnDaemon = prev
		if fresh != nil {
			fresh.Stop()
		}
	})

	e, err := EnsureDaemon(repo)
	if err != nil {
		t.Fatalf("EnsureDaemon should replace an incompatible daemon, got: %v", err)
	}
	if e.Version != ProtocolVersion || !compatible(e.Sock) {
		t.Fatalf("EnsureDaemon returned incompatible record: %+v", e)
	}
}

// TestWinDialAutoRecovery proves the end-to-end recovery on Windows: Dial finds
// an incompatible daemon squatting the registry, retires it, spawns a fresh one,
// and attaches successfully (no manual kill, no error).
func TestWinDialAutoRecovery(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()

	stale := newStaleListenerWin(t, ProtocolVersion+1)
	defer stale.close()
	staleSock := stale.addr()
	if err := writeMeta(Entry{
		Repo: repo, Sock: staleSock, Version: ProtocolVersion + 1,
		Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta: %v", err)
	}

	var fresh *Daemon
	prev := spawnDaemon
	spawnDaemon = func(rr string) error {
		d, err := New(rr, winSpawn)
		if err != nil {
			return err
		}
		fresh = d
		go func() { _ = d.Serve() }()
		return nil
	}
	t.Cleanup(func() {
		spawnDaemon = prev
		if fresh != nil {
			fresh.Stop()
		}
	})

	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("Dial should auto-recover from a protocol mismatch, got: %v", err)
	}
	defer c.Detach()
	go func() { _ = c.Run() }()

	if fresh == nil {
		t.Fatal("auto-recovery should have spawned a fresh daemon")
	}
	winWaitFor(t, func() bool { return fresh.Mux().Count() == 1 })

	e, ok, err := Find(repo)
	if err != nil || !ok {
		t.Fatalf("registry record missing after recovery: ok=%v err=%v", ok, err)
	}
	if e.Sock == staleSock {
		t.Fatal("registry still points at the retired stale daemon")
	}
	if !compatible(e.Sock) {
		t.Fatal("recovered daemon is not compatible")
	}
}

// TestWinListPrunes proves `ls` prunes a dead record and an alive-but-
// incompatible record, keeping only the live compatible daemon.
func TestWinListPrunes(t *testing.T) {
	d, liveRepo := winStartDaemon(t) // sets HOME/USERPROFILE to a temp dir
	defer d.Stop()

	deadRepo := t.TempDir()
	if err := writeMeta(Entry{
		Repo: deadRepo, Sock: "127.0.0.1:1", Version: ProtocolVersion,
		Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta dead: %v", err)
	}

	stale := newStaleListenerWin(t, ProtocolVersion+1)
	defer stale.close()
	staleRepo := t.TempDir()
	if err := writeMeta(Entry{
		Repo: staleRepo, Sock: stale.addr(), Version: ProtocolVersion + 1,
		Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta stale: %v", err)
	}

	entries, err := List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 1 || entries[0].Repo != liveRepo {
		t.Fatalf("ls must prune dead + incompatible, want only %q, got %+v", liveRepo, entries)
	}
	if winMetaExists(t, deadRepo) {
		t.Error("dead record should be pruned")
	}
	if winMetaExists(t, staleRepo) {
		t.Error("incompatible record should be pruned")
	}
}
