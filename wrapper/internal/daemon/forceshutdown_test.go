package daemon

import (
	"runtime"
	"testing"
	"time"

	"github.com/workflow-harness/claude-plus/internal/event"
	"github.com/workflow-harness/claude-plus/internal/pty"
	"github.com/workflow-harness/claude-plus/internal/transport"
)

// longLivedSpawn launches a child that stays alive for a minute so it is still
// running when the force-shutdown control frame arrives — the prerequisite for
// proving the terminate actually kills a live process.
func longLivedSpawn(repoRoot, sessionID string) pty.CmdSpec {
	if runtime.GOOS == "windows" {
		return pty.CmdSpec{Name: "ping", Args: []string{"-n", "60", "127.0.0.1"}, Dir: repoRoot}
	}
	return pty.CmdSpec{Name: "sleep", Args: []string{"60"}, Dir: repoRoot}
}

// newForceReceiver builds a control receiver wired exactly like the daemon
// runtime does: its Terminated hook emits a terminal status.change -> done so HQ
// retires the live row. The emitted envelopes are captured via the daemon's
// event sink (the same local bus HQ delivery is teed off). This is the SAME
// inbound path the HQ WebSocket uses (Receiver.Handle).
func newForceReceiver(d *Daemon) *transport.Receiver {
	recv := d.NewControlReceiver()
	recv.Terminated = func(sessionID string) {
		d.PublishEvent(event.Envelope{
			V: 1, TS: time.Now().UnixMilli(),
			Event: event.StatusChange(sessionID, event.StatusActive, event.StatusDone),
		})
	}
	return recv
}

// collectDone subscribes to the daemon event bus and returns a func reporting
// whether a status.change -> done has been seen for sessID.
func collectDone(d *Daemon, sinkID string) func(sessID string) bool {
	seen := map[string]bool{}
	d.AddEventSink(sinkID, func(env event.Envelope) {
		e := env.Event
		if e.Kind == event.KindStatusChange && e.To == event.StatusDone {
			seen[e.SessionID] = true
		}
	})
	return func(sessID string) bool { return seen[sessID] }
}

// TestForceShutdownKnownSessionTerminatesAndNotifiesHQ is the end-to-end proof
// for a KNOWN session: a real long-lived child is shut down via the HQ inbound
// control path, and we assert BOTH (a) the child PROCESS is gone AND (b) a
// terminal `done` event was emitted to the event sink so HQ drops the live row.
//
// Without the fix this FAILS on (b): the old receiver applied Shutdown() but
// emitted nothing, so HQ never learned the session ended — the user's "nothing
// happened" symptom.
func TestForceShutdownKnownSessionTerminatesAndNotifiesHQ(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()

	d, err := New(repo, longLivedSpawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer d.Stop()

	s, err := d.Mux().Spawn("victim")
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	pid := s.PID()
	if pid == 0 {
		t.Fatal("spawned session has no child PID")
	}
	// Give the child a moment to be schedulable, then confirm it is genuinely
	// running before we try to kill it (so a later "dead" assertion is meaningful).
	alive := false
	for i := 0; i < 100; i++ {
		if pidRunning(pid) {
			alive = true
			break
		}
		time.Sleep(20 * time.Millisecond)
	}
	if !alive {
		t.Fatalf("child %d never observed alive after spawn", pid)
	}

	doneSeen := collectDone(d, "force-known")
	recv := newForceReceiver(d)

	// Drive the SAME entrypoint the HQ WebSocket read loop calls.
	recv.Handle(transport.ControlFrame{SessionID: s.ID, Action: transport.ActionShutdown})

	// (a) The child process actually dies.
	deadline := time.Now().Add(8 * time.Second)
	for pidRunning(pid) && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	if pidRunning(pid) {
		t.Fatalf("child %d still alive after force shutdown — process did not die", pid)
	}

	// (b) HQ was told the session is done so the live row clears.
	if !doneSeen(s.ID) {
		t.Fatalf("no status.change -> done emitted for known session %q; HQ would keep the live row", s.ID)
	}
}

// TestForceShutdownGhostSessionStillNotifiesHQ proves the ghost case: a shutdown
// for a sessionId this daemon never hosted (stable per-device instanceId routes
// a dead daemon's sessions here) must STILL emit `done` so the orphaned live row
// the user clicked disappears. Without the fix the receiver NACKed silently and
// the row stuck forever.
func TestForceShutdownGhostSessionStillNotifiesHQ(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	repo := t.TempDir()

	d, err := New(repo, longLivedSpawn)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer d.Stop()

	doneSeen := collectDone(d, "force-ghost")
	recv := newForceReceiver(d)

	ghost := "ghost-session-from-a-dead-daemon"
	recv.Handle(transport.ControlFrame{SessionID: ghost, Action: transport.ActionShutdown})

	if !doneSeen(ghost) {
		t.Fatalf("no status.change -> done emitted for ghost session %q; the orphan row would never clear", ghost)
	}
}
