package transport

import (
	"fmt"
	"path/filepath"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

func env(seq int64) event.Envelope {
	return event.Envelope{
		V: 1, InstanceID: "inst-0", Host: "matt@mbp", TS: 1717200000000 + seq, Seq: seq,
		Event: event.UserMsg("a91f", 10),
	}
}

// TestRingBufferOfflineReplay verifies events buffer to disk and replay in order
// with no gaps/dupes, and that acking compacts the buffer (U14 offline path).
func TestRingBufferOfflineReplay(t *testing.T) {
	p := filepath.Join(t.TempDir(), "buf.jsonl")
	rb, err := OpenRingBuffer(p)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	for i := int64(0); i < 5; i++ {
		if err := rb.Append(env(i)); err != nil {
			t.Fatalf("append %d: %v", i, err)
		}
	}
	pending, err := rb.Pending()
	if err != nil {
		t.Fatalf("pending: %v", err)
	}
	if len(pending) != 5 {
		t.Fatalf("want 5 pending, got %d", len(pending))
	}
	for i, e := range pending {
		if e.Seq != int64(i) {
			t.Errorf("out of order: pos %d has seq %d", i, e.Seq)
		}
	}

	// Reopen to simulate a daemon restart — pending must persist.
	rb2, err := OpenRingBuffer(p)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	pending2, _ := rb2.Pending()
	if len(pending2) != 5 {
		t.Fatalf("buffer did not persist across restart: %d", len(pending2))
	}

	// Ack all and confirm compaction (no dupes on next pending).
	if err := rb2.Ack(5); err != nil {
		t.Fatalf("ack: %v", err)
	}
	after, _ := rb2.Pending()
	if len(after) != 0 {
		t.Errorf("acked buffer should be empty, got %d", len(after))
	}
}

// TestPartialAck verifies acking N leaves the remainder pending in order.
func TestPartialAck(t *testing.T) {
	p := filepath.Join(t.TempDir(), "buf.jsonl")
	rb, _ := OpenRingBuffer(p)
	for i := int64(0); i < 4; i++ {
		_ = rb.Append(env(i))
	}
	_ = rb.Ack(2)
	pending, _ := rb.Pending()
	if len(pending) != 2 || pending[0].Seq != 2 {
		t.Fatalf("want seq 2,3 remaining, got %+v", pending)
	}
}

func TestSeqMonotonic(t *testing.T) {
	s := NewSeq()
	if s.Next("a") != 0 || s.Next("a") != 1 || s.Next("a") != 2 {
		t.Error("per-session seq not monotonic")
	}
	if s.Next("b") != 0 {
		t.Error("seq should be independent per session")
	}
}

// fakeMux implements SessionWriter for control tests, recording writes per id.
type fakeMux struct{ writes map[string][]byte }

func newFakeMux() *fakeMux { return &fakeMux{writes: map[string][]byte{}} }
func (f *fakeMux) WriteTo(id string, p []byte) (int, error) {
	if id == "missing" {
		return 0, fmt.Errorf("no session %q", id)
	}
	f.writes[id] = append(f.writes[id], p...)
	return len(p), nil
}
func (f *fakeMux) Get(string) PTYSession { return nil }

// TestControlInjectTargetsSession verifies inject reaches the addressed session,
// not the focused one (U15 targeting), and appends a submit newline.
func TestControlInjectTargetsSession(t *testing.T) {
	mux := newFakeMux()
	r := NewReceiver(mux)
	r.Handle(ControlFrame{SessionID: "bg", Action: ActionInject, Payload: "answer the prompt"})
	if got := string(mux.writes["bg"]); got != "answer the prompt\n" {
		t.Errorf("inject = %q", got)
	}
	if _, ok := mux.writes["focused"]; ok {
		t.Error("inject leaked to a non-target session")
	}
}

func TestControlInterrupt(t *testing.T) {
	mux := newFakeMux()
	NewReceiver(mux).Handle(ControlFrame{SessionID: "s", Action: ActionInterrupt})
	if string(mux.writes["s"]) != "\x03" {
		t.Errorf("interrupt should send Ctrl-C, got %q", mux.writes["s"])
	}
}

// TestControlUnknownSessionNacks verifies a frame for a missing session is
// dropped with a NACK and does not panic.
func TestControlUnknownSessionNacks(t *testing.T) {
	mux := newFakeMux()
	r := NewReceiver(mux)
	var nacked bool
	r.Nack = func(ControlFrame, error) { nacked = true }
	r.Handle(ControlFrame{SessionID: "missing", Action: ActionInject, Payload: "x"})
	if !nacked {
		t.Error("expected NACK for unknown session")
	}
}
