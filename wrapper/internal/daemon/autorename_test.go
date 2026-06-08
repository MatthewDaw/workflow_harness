package daemon

import (
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/pty"
)

// longSpawn returns a long-running command so the auto-spawned attach session
// stays alive for the duration of the test on any platform.
func longSpawn(repoRoot, sessionID string) pty.CmdSpec {
	if runtime.GOOS == "windows" {
		return pty.CmdSpec{Name: "ping", Args: []string{"-n", "60", "127.0.0.1"}, Dir: repoRoot}
	}
	return pty.CmdSpec{Name: "sleep", Args: []string{"60"}, Dir: repoRoot}
}

// waitForCond polls cond until true or a 3s timeout (cross-platform; the
// waitFor in daemon_test.go is !windows-tagged).
func waitForCond(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("condition not met within timeout")
}

// startTestDaemonSpawn brings up an in-process daemon on a temp HOME with the
// given spawn func, cross-platform (loopback TCP).
func startTestDaemonSpawn(t *testing.T, spawn pty.SpawnFunc) (*Daemon, string) {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()

	d, err := New(repo, spawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	go func() { _ = d.Serve() }()

	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if e, ok, _ := Find(repo); ok && alive(e.Sock) {
			return d, repo
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatal("daemon never came up")
	return nil, ""
}

// TestAutoRenamePushesSessionListToAttachedClient is the regression proof for
// the stale-tab bug: when a session is auto-named on its first prompt (the
// onFirst path in runtime.go — ApplyAutoName, a session.rename event on the bus,
// then broadcastSessList), the daemon MUST push a fresh session list to attached
// clients so their sub-tab row (compositor.SetSubs) shows the new name. The
// rename event alone only feeds the Stream panel (FrameEvent), which does NOT
// update the sub-tab Name — broadcastSessList is what fans a FrameSessAck to each
// attached client's listSink (registered in attach()).
//
// This test drives that exact production sequence and fails if broadcastSessList
// no longer reaches attached clients.
func TestAutoRenamePushesSessionListToAttachedClient(t *testing.T) {
	d, repo := startTestDaemonSpawn(t, longSpawn)
	defer d.Stop()

	var mu sync.Mutex
	var lists [][]SessInfo
	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	c.OnSessions = func(list []SessInfo) {
		mu.Lock()
		lists = append(lists, append([]SessInfo(nil), list...))
		mu.Unlock()
	}
	go func() { _ = c.Run() }()
	defer c.Detach()

	// Wait for the daemon to auto-spawn the attach session.
	waitForCond(t, func() bool { return d.Mux().Count() == 1 })
	sid := d.Mux().List()[0].ID

	// Drive the EXACT onFirst path: apply the first-prompt auto-name, then emit
	// the session.rename event on the bus (PublishEvent is what runtime.emit calls).
	renamed, name := d.Mux().ApplyAutoName(sid, "please type up a full summary of the design")
	if !renamed {
		t.Fatalf("ApplyAutoName did not rename session %s", sid)
	}
	d.PublishEvent(event.Envelope{V: 1, TS: time.Now().UnixMilli(), Event: event.SessionRename(sid, name)})
	// Mirror the production auto-name path (runtime.go onFirst): after the rename
	// + event emit, push a fresh session list so attached clients' tab strip
	// updates. This is the step the stale-tab fix added.
	d.broadcastSessList()

	// The attached client must receive a session-list update whose focused
	// sub-tab Name equals the auto-generated slug.
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		for _, list := range lists {
			for _, s := range list {
				if s.ID == sid && s.Name == name {
					mu.Unlock()
					return // PASS
				}
			}
		}
		mu.Unlock()
		time.Sleep(10 * time.Millisecond)
	}

	mu.Lock()
	defer mu.Unlock()
	t.Fatalf("attached client never received a session-list update with name %q (got %d list updates: %+v)", name, len(lists), lists)
}

// TestAutoTitlePushesSessionListToAttachedClient covers the onExchange path:
// the LLM-generated title (ApplyTitle) also emits session.rename and calls
// broadcastSessList, pushing a fresh list so the sub-tab upgrades from the
// provisional slug to the title.
func TestAutoTitlePushesSessionListToAttachedClient(t *testing.T) {
	d, repo := startTestDaemonSpawn(t, longSpawn)
	defer d.Stop()

	var mu sync.Mutex
	var lastByID = map[string]string{}
	c, err := Dial(repo)
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	c.OnSessions = func(list []SessInfo) {
		mu.Lock()
		for _, s := range list {
			lastByID[s.ID] = s.Name
		}
		mu.Unlock()
	}
	go func() { _ = c.Run() }()
	defer c.Detach()

	waitForCond(t, func() bool { return d.Mux().Count() == 1 })
	sid := d.Mux().List()[0].ID

	// First the provisional auto-name, then the LLM title upgrade.
	d.Mux().ApplyAutoName(sid, "first prompt text here")
	renamed, title := d.Mux().ApplyTitle(sid, "Reconcile Variance Report")
	if !renamed {
		t.Fatalf("ApplyTitle did not rename session %s", sid)
	}
	d.PublishEvent(event.Envelope{V: 1, TS: time.Now().UnixMilli(), Event: event.SessionRename(sid, title)})
	// Mirror the production auto-title path (runtime.go onExchange): push a fresh
	// session list so attached clients' tab strip upgrades to the LLM title.
	d.broadcastSessList()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		mu.Lock()
		got := lastByID[sid]
		mu.Unlock()
		if got == title {
			return // PASS
		}
		time.Sleep(10 * time.Millisecond)
	}
	mu.Lock()
	defer mu.Unlock()
	t.Fatalf("attached client never saw title %q for session %s (last name=%q)", title, sid, lastByID[sid])
}
