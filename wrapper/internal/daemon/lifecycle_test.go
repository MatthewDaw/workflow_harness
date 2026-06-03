//go:build !windows

package daemon

import (
	"bufio"
	"net"
	"testing"
	"time"
)

// metaExists reports whether the registry json for repo is present on disk.
func metaExists(t *testing.T, repo string) bool {
	t.Helper()
	_, ok, err := Find(repo)
	if err != nil {
		t.Fatalf("Find: %v", err)
	}
	return ok
}

// TestLifecycleStartAttachSession proves the create half of attach-or-create:
// a fresh daemon comes up, an attach client connects, and the daemon
// auto-spawns exactly one session that the client can see.
func TestLifecycleStartAttachSession(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	if !metaExists(t, repo) {
		t.Fatal("registry record should exist for a running daemon")
	}

	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	defer c.Detach()
	go func() { _ = c.Run() }()

	waitFor(t, func() bool { return d.Mux().Count() == 1 })
	if len(c.Sessions) != 1 {
		t.Fatalf("attach ack should seed exactly 1 session, got %d", len(c.Sessions))
	}
}

// TestLifecycleDetachSurvivesReattach is the detach contract: after a clean
// detach the daemon and its session survive, and a second client re-attaches
// the same daemon without spawning a duplicate session.
func TestLifecycleDetachSurvivesReattach(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	c1, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial 1: %v", err)
	}
	go func() { _ = c1.Run() }()
	waitFor(t, func() bool { return d.Mux().Count() == 1 })

	if err := c1.Detach(); err != nil {
		t.Fatalf("detach: %v", err)
	}

	// Daemon + session + registry record all survive the detach.
	if d.Mux().Count() != 1 {
		t.Fatalf("session must survive detach, count=%d", d.Mux().Count())
	}
	if !metaExists(t, repo) {
		t.Fatal("registry record must survive a client detach")
	}
	if !compatible(mustSock(t, repo)) {
		t.Fatal("daemon must still be reachable + compatible after detach")
	}

	c2, err := Dial(repo)
	if err != nil {
		t.Fatalf("re-attach: %v", err)
	}
	defer c2.Detach()
	go func() { _ = c2.Run() }()
	if d.Mux().Count() != 1 {
		t.Fatalf("re-attach must reuse the session, count=%d", d.Mux().Count())
	}
}

// TestLifecycleStopCleansRegistryAndSocket proves stop tears the daemon down
// fully: the socket stops answering and the registry record is removed, so the
// daemon never lingers as a stale `ls` row.
func TestLifecycleStopCleansRegistryAndSocket(t *testing.T) {
	d, repo := startTestDaemon(t)
	sock := mustSock(t, repo)

	d.Stop()

	// The listener closes and the registry record is removed.
	waitFor(t, func() bool { return !alive(sock) })
	waitFor(t, func() bool { return !metaExists(t, repo) })

	entries, err := List()
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(entries) != 0 {
		t.Fatalf("stopped daemon must not appear in ls, got %+v", entries)
	}

	// Stop is idempotent — a second call must not panic.
	d.Stop()
}

// TestLifecycleRestart proves a stop-then-start cycle yields a clean fresh
// daemon (new socket) that attaches without error — the rebuild flow.
func TestLifecycleRestart(t *testing.T) {
	d1, repo := startTestDaemon(t)
	sock1 := mustSock(t, repo)
	d1.Stop()
	waitFor(t, func() bool { return !alive(sock1) })

	// Start a second daemon for the same repo (fresh process/socket).
	d2, err := New(repo, fakeSpawn)
	if err != nil {
		t.Fatalf("New 2: %v", err)
	}
	defer d2.Stop()
	go func() { _ = d2.Serve() }()
	waitFor(t, func() bool {
		e, ok, _ := Find(repo)
		return ok && compatible(e.Sock)
	})

	sock2 := mustSock(t, repo)
	if sock2 == sock1 {
		// Extremely unlikely to reuse the exact port, but assert a genuinely fresh
		// daemon is reachable rather than the dead one.
		if !compatible(sock2) {
			t.Fatal("restarted daemon socket is not reachable")
		}
	}

	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("attach after restart: %v", err)
	}
	defer c.Detach()
	go func() { _ = c.Run() }()
	waitFor(t, func() bool { return d2.Mux().Count() == 1 })
}

// staleListener is a tiny fake daemon that simulates an OLDER build: it answers
// pings and the hello handshake with a DIFFERENT ProtocolVersion, exactly as a
// lingering pre-rebuild daemon would. It records its loopback address so a test
// can register it, and lets the test assert it gets retired.
type staleListener struct {
	ln      net.Listener
	version int
}

func newStaleListener(t *testing.T, version int) *staleListener {
	t.Helper()
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("stale listen: %v", err)
	}
	s := &staleListener{ln: ln, version: version}
	go s.serve()
	return s
}

func (s *staleListener) addr() string { return s.ln.Addr().String() }

func (s *staleListener) serve() {
	for {
		conn, err := s.ln.Accept()
		if err != nil {
			return
		}
		go func(c net.Conn) {
			defer c.Close()
			r := bufio.NewReader(c)
			f, err := readFrame(r)
			if err != nil {
				return
			}
			switch f.Type {
			case FramePing:
				// Answer the liveness probe with the WRONG version: alive, but
				// incompatible — the proactive-detection path.
				_ = writeFrame(c, Frame{Type: FramePong, Version: s.version})
			case FrameHello:
				// Reject the handshake with a version-tagged ack: the
				// attach-time detection path (the historical dead-end).
				_ = writeFrame(c, Frame{Type: FrameAck, Version: s.version, Err: "protocol mismatch"})
			}
		}(conn)
	}
}

func (s *staleListener) close() { _ = s.ln.Close() }

// TestProtocolMismatchAutoRecovery is the headline lifecycle bug fix: a daemon
// from an older build is squatting the repo's registry record and answers the
// handshake with a different ProtocolVersion. Dial must NOT dead-end with a
// "restart it yourself" error — it must automatically retire the stale daemon,
// spawn a fresh compatible one, and attach successfully.
func TestProtocolMismatchAutoRecovery(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	repo := t.TempDir()

	// Stand up a fake "old build" daemon and register it under the repo, exactly
	// as a lingering pre-rebuild daemon would appear in the registry.
	stale := newStaleListener(t, ProtocolVersion+1)
	defer stale.close()
	staleSock := stale.addr()
	if err := writeMeta(Entry{
		Repo:    repo,
		Sock:    staleSock,
		PID:     0, // no real OS process to kill in this in-process test
		Version: ProtocolVersion + 1,
		Started: time.Now(),
		State:   StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta: %v", err)
	}

	// Substitute the exec-based spawner with an in-process compatible daemon so
	// the test exercises the recovery wiring without re-execing the test binary.
	var fresh *Daemon
	prev := spawnDaemon
	spawnDaemon = func(rr string) error {
		d, err := New(rr, fakeSpawn)
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

	// Dial must auto-recover: detect the mismatch, retire the stale record,
	// spawn the fresh daemon, and attach to it — no manual kill, no error.
	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("Dial should auto-recover from a protocol mismatch, got: %v", err)
	}
	defer c.Detach()
	go func() { _ = c.Run() }()

	if fresh == nil {
		t.Fatal("auto-recovery should have spawned a fresh daemon")
	}
	waitFor(t, func() bool { return fresh.Mux().Count() == 1 })

	// The new registry record points at the fresh, compatible daemon — not the
	// stale one — so subsequent attaches go straight through.
	e, ok, err := Find(repo)
	if err != nil || !ok {
		t.Fatalf("registry record missing after recovery: ok=%v err=%v", ok, err)
	}
	if e.Sock == staleSock {
		t.Fatal("registry still points at the retired stale daemon")
	}
	if e.Version != ProtocolVersion {
		t.Fatalf("recovered record version = %d, want %d", e.Version, ProtocolVersion)
	}
	if !compatible(e.Sock) {
		t.Fatal("recovered daemon is not compatible")
	}
}

// TestEnsureDaemonReplacesIncompatible proves the proactive (pre-attach) half:
// EnsureDaemon must treat an alive-but-incompatible daemon as stale and spawn a
// fresh one rather than handing back the incompatible record.
func TestEnsureDaemonReplacesIncompatible(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	repo := t.TempDir()

	stale := newStaleListener(t, ProtocolVersion+1)
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
		d, err := New(rr, fakeSpawn)
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
	if e.Version != ProtocolVersion {
		t.Fatalf("EnsureDaemon returned an incompatible record: v%d", e.Version)
	}
	if !compatible(e.Sock) {
		t.Fatal("EnsureDaemon returned an unreachable/incompatible daemon")
	}
}

// TestListPrunesIncompatibleAndStale proves `ls` is robust to leftover records:
// a dead socket and an alive-but-incompatible daemon are both pruned, leaving
// only the live compatible daemon.
func TestListPrunesIncompatibleAndStale(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)

	// A live, compatible daemon (kept).
	live, liveRepo := startTestDaemonOn(t, home)
	defer live.Stop()

	// A dead record: a socket that nobody is listening on (pruned).
	deadRepo := t.TempDir()
	if err := writeMeta(Entry{
		Repo: deadRepo, Sock: "127.0.0.1:1", Version: ProtocolVersion,
		Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta dead: %v", err)
	}

	// An alive-but-incompatible record (pruned).
	stale := newStaleListener(t, ProtocolVersion+1)
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
	if len(entries) != 1 {
		t.Fatalf("ls must prune dead + incompatible, want 1 entry, got %d: %+v", len(entries), entries)
	}
	if entries[0].Repo != liveRepo {
		t.Fatalf("surviving entry should be the live daemon, got %q", entries[0].Repo)
	}
	// The pruned records' json files are gone.
	if metaExists(t, deadRepo) {
		t.Error("dead record should be pruned from the registry")
	}
	if metaExists(t, staleRepo) {
		t.Error("incompatible record should be pruned from the registry")
	}
}

// startTestDaemonOn starts an in-process daemon using the already-set HOME (so a
// test can stand up multiple daemons sharing one registry dir).
func startTestDaemonOn(t *testing.T, _ string) (*Daemon, string) {
	t.Helper()
	repo := t.TempDir()
	d, err := New(repo, fakeSpawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = d.Serve() }()
	waitFor(t, func() bool {
		e, ok, _ := Find(repo)
		return ok && compatible(e.Sock)
	})
	return d, repo
}

// mustSock returns the registered socket for repo, failing the test if absent.
func mustSock(t *testing.T, repo string) string {
	t.Helper()
	e, ok, err := Find(repo)
	if err != nil || !ok {
		t.Fatalf("Find(%q): ok=%v err=%v", repo, ok, err)
	}
	return e.Sock
}
