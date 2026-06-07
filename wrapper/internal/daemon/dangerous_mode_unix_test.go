//go:build !windows

package daemon

import (
	"testing"
	"time"
)

// TestEnsureDaemonUpgradesToDangerous proves the permission-posture half of the
// reuse contract: a compatible daemon that was started WITHOUT dangerous mode is
// retired and replaced when a launch requests dangerous mode
// (CLAUDE_PLUS_DANGEROUS set, from `claude+ --dangerously-skip-permissions`), so
// the fresh daemon — and every claude child it spawns — actually runs dangerous.
//
// It mirrors TestEnsureDaemonReplacesIncompatible: the existing daemon is a
// listener answering the CURRENT ProtocolVersion (so it is compatible, not a
// version mismatch) with PID 0 (no real OS process to kill in-process), and the
// only reason to replace it is the dangerous-mode upgrade.
func TestEnsureDaemonUpgradesToDangerous(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	repo := t.TempDir()

	// A live, COMPATIBLE, NON-dangerous daemon already registered for the repo.
	live := newStaleListener(t, ProtocolVersion) // answers the current version → compatible
	defer live.close()
	liveSock := live.addr()
	if err := writeMeta(Entry{
		Repo: repo, Sock: liveSock, PID: 0, Version: ProtocolVersion,
		Dangerous: false, Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta: %v", err)
	}

	// This launch requests dangerous mode. The substituted spawner stands up an
	// in-process daemon, which reads CLAUDE_PLUS_DANGEROUS at registration and so
	// records Dangerous=true.
	t.Setenv("CLAUDE_PLUS_DANGEROUS", "1")
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
		t.Fatalf("EnsureDaemon should upgrade a non-dangerous daemon, got: %v", err)
	}
	if fresh == nil {
		t.Fatal("EnsureDaemon should have spawned a fresh daemon for the dangerous upgrade")
	}
	if e.Sock == liveSock {
		t.Fatal("registry still points at the retired non-dangerous daemon")
	}
	if !e.Dangerous {
		t.Fatalf("replacement daemon must record Dangerous=true, got %+v", e)
	}
	if !compatible(e.Sock) {
		t.Fatal("replacement daemon is not reachable/compatible")
	}
}

// TestEnsureDaemonReusesMatchingDangerous proves the no-needless-restart side: a
// dangerous launch finding an already-dangerous compatible daemon reuses it
// rather than churning a fresh process (and tearing down live sessions).
func TestEnsureDaemonReusesMatchingDangerous(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	repo := t.TempDir()

	live := newStaleListener(t, ProtocolVersion)
	defer live.close()
	liveSock := live.addr()
	if err := writeMeta(Entry{
		Repo: repo, Sock: liveSock, PID: 0, Version: ProtocolVersion,
		Dangerous: true, Started: time.Now(), State: StateRunning,
	}); err != nil {
		t.Fatalf("writeMeta: %v", err)
	}

	t.Setenv("CLAUDE_PLUS_DANGEROUS", "1")
	prev := spawnDaemon
	spawnDaemon = func(string) error {
		t.Fatal("EnsureDaemon must reuse a matching dangerous daemon, not respawn")
		return nil
	}
	t.Cleanup(func() { spawnDaemon = prev })

	e, err := EnsureDaemon(repo)
	if err != nil {
		t.Fatalf("EnsureDaemon: %v", err)
	}
	if e.Sock != liveSock {
		t.Fatalf("EnsureDaemon should reuse the dangerous daemon at %s, got %s", liveSock, e.Sock)
	}
}
