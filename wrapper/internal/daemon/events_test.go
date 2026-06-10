package daemon

import (
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

func envWith(sid string, e event.Event) event.Envelope {
	return event.Envelope{V: 1, InstanceID: "i", Host: "h", TS: 1, Seq: 0, Event: e}
}

func TestEventBusReplaysBufferThenFansOutLive(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}

	// Publish before any subscriber: lands in the replay buffer.
	d.PublishEvent(envWith("s1", event.SessionRename("s1", "alpha")))

	var got []event.Envelope
	d.AddEventSink("c", func(env event.Envelope) { got = append(got, env) })
	if len(got) != 1 || got[0].Event.Name != "alpha" {
		t.Fatalf("replay = %+v, want one 'alpha' event", got)
	}

	// Live publish reaches the subscriber.
	d.PublishEvent(envWith("s1", event.SessionRename("s1", "beta")))
	if len(got) != 2 || got[1].Event.Name != "beta" {
		t.Fatalf("after live publish = %+v, want 'beta' second", got)
	}

	// After removal, no further delivery.
	d.RemoveEventSink("c")
	d.PublishEvent(envWith("s1", event.SessionRename("s1", "gamma")))
	if len(got) != 2 {
		t.Fatalf("got %d events after RemoveEventSink, want 2", len(got))
	}
}

func TestEventBufferBounded(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatal(err)
	}
	for i := 0; i < maxRecentEvents+50; i++ {
		d.PublishEvent(envWith("s", event.UserMsgText("s", int64(i), "")))
	}
	got := 0
	d.AddEventSink("c", func(env event.Envelope) { got++ })
	if got != maxRecentEvents {
		t.Fatalf("replay delivered %d, want capped at %d", got, maxRecentEvents)
	}
}
