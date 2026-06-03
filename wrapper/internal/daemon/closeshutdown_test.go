//go:build !windows

package daemon

import (
	"testing"
)

// TestFrameKillRemovesSessionAndAcks proves the ✕ control frame: a client's
// CloseSession (FrameKill) makes the daemon force-kill that session and reply
// with a fresh session list (FrameSessAck) reflecting the removal. Killing the
// last session leaves the list empty (no auto-spawn on an empty transition).
func TestFrameKillRemovesSessionAndAcks(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	var lastList []SessInfo
	c.OnSessions = func(list []SessInfo) { lastList = list }
	go func() { _ = c.Run() }()

	// The daemon auto-spawns one session on first attach; add a second so we can
	// see a non-empty list shrink before the empty case.
	waitFor(t, func() bool { return d.Mux().Count() == 1 })
	if err := c.NewSession(); err != nil {
		t.Fatalf("NewSession: %v", err)
	}
	waitFor(t, func() bool { return d.Mux().Count() == 2 })

	views := d.Mux().List()
	killID := views[0].ID

	if err := c.CloseSession(killID); err != nil {
		t.Fatalf("CloseSession: %v", err)
	}
	waitFor(t, func() bool { return d.Mux().Count() == 1 })
	// The ack reflects the removal for the requesting client.
	waitFor(t, func() bool {
		for _, s := range lastList {
			if s.ID == killID {
				return false
			}
		}
		return len(lastList) == 1
	})

	// Kill the last one: list goes empty, no auto-spawn.
	if err := c.CloseSession(lastList[0].ID); err != nil {
		t.Fatalf("CloseSession last: %v", err)
	}
	waitFor(t, func() bool { return d.Mux().Count() == 0 })
	waitFor(t, func() bool { return len(lastList) == 0 })
}

// TestFrameShutdownStopsDaemon proves quit terminates everything: a client's
// Shutdown (FrameShutdown) stops the daemon — its registry record is removed and
// the socket stops answering — and its sessions are torn down.
func TestFrameShutdownStopsDaemon(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop() // idempotent; safe even though Shutdown already stopped it

	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	go func() { _ = c.Run() }()
	waitFor(t, func() bool { return d.Mux().Count() == 1 })

	if err := c.Shutdown(); err != nil {
		t.Fatalf("Shutdown: %v", err)
	}

	// The daemon stops: sessions torn down, registry record gone, socket dead.
	waitFor(t, func() bool { return d.Mux().Count() == 0 })
	waitFor(t, func() bool {
		_, ok, _ := Find(repo)
		return !ok
	})
}
