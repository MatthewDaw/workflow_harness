//go:build !windows

package daemon

import (
	"sync"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// collectEvents subscribes to the daemon's event bus and returns a snapshot
// accessor plus the sink id (so callers can assert on published envelopes).
func collectEvents(d *Daemon, id string) func() []event.Envelope {
	var mu sync.Mutex
	var got []event.Envelope
	d.AddEventSink(id, func(env event.Envelope) {
		mu.Lock()
		got = append(got, env)
		mu.Unlock()
	})
	return func() []event.Envelope {
		mu.Lock()
		defer mu.Unlock()
		return append([]event.Envelope(nil), got...)
	}
}

// TestHookEventReachesDaemon is the U18 happy path: a Notification hook posted
// to the daemon socket surfaces as a status.change → needs_input on the local
// event bus, the same stream the transcript tailer feeds.
func TestHookEventReachesDaemon(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	s, err := d.Mux().Spawn("")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	snapshot := collectEvents(d, "hooktest")

	raw := `{"hook_event_name":"Notification","session_id":"` + s.ID + `","message":"need a decision"}`
	if err := SendHook(repo, []byte(raw)); err != nil {
		t.Fatalf("SendHook: %v", err)
	}

	waitFor(t, func() bool {
		for _, env := range snapshot() {
			if env.Event.Kind == event.KindStatusChange &&
				env.Event.SessionID == s.ID &&
				env.Event.To == event.StatusNeedsInput {
				return true
			}
		}
		return false
	})
}

// TestUserPromptSubmitHookRenamesSession proves a UserPromptSubmit hook carrying
// the user's first prompt triggers ApplyAutoName and surfaces a session.rename on
// the local bus that carries BOTH a derived slug name and the raw first prompt as
// the summary. Only the first prompt renames (ApplyAutoName is idempotent).
func TestUserPromptSubmitHookRenamesSession(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	s, err := d.Mux().Spawn("")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	snapshot := collectEvents(d, "hooktest")

	prompt := "fix the login bug in the cursor flow"
	raw := `{"hook_event_name":"UserPromptSubmit","session_id":"` + s.ID + `","prompt":"` + prompt + `"}`
	if err := SendHook(repo, []byte(raw)); err != nil {
		t.Fatalf("SendHook: %v", err)
	}

	waitFor(t, func() bool {
		for _, env := range snapshot() {
			e := env.Event
			if e.Kind == event.KindSessionRename && e.SessionID == s.ID &&
				e.Name != "" && e.Summary == prompt {
				return true
			}
		}
		return false
	})
}

// TestHookMalformedPayloadDropped is the U18 edge case: a malformed payload (and
// a no-op hook kind) produce no event and leave the daemon stable.
func TestHookMalformedPayloadDropped(t *testing.T) {
	d, repo := startTestDaemon(t)
	defer d.Stop()

	s, err := d.Mux().Spawn("")
	if err != nil {
		t.Fatalf("spawn: %v", err)
	}
	snapshot := collectEvents(d, "hooktest")

	// Malformed JSON, then a PostToolUse (maps to no status change).
	if err := SendHook(repo, []byte("{not json")); err != nil {
		t.Fatalf("SendHook malformed: %v", err)
	}
	postUse := `{"hook_event_name":"PostToolUse","session_id":"` + s.ID + `"}`
	if err := SendHook(repo, []byte(postUse)); err != nil {
		t.Fatalf("SendHook PostToolUse: %v", err)
	}

	// A subsequent valid hook must still arrive, proving the daemon stayed up and
	// the earlier drops were silent (no panic, no spurious event).
	good := `{"hook_event_name":"Stop","session_id":"` + s.ID + `"}`
	if err := SendHook(repo, []byte(good)); err != nil {
		t.Fatalf("SendHook good: %v", err)
	}
	waitFor(t, func() bool {
		var status int
		for _, env := range snapshot() {
			if env.Event.Kind == event.KindStatusChange {
				status++
			}
		}
		return status == 1 // only the Stop hook produced an event
	})
}
