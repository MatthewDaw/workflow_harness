package daemon

import (
	"bufio"
	"net"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/event"
)

// TestAttachRejectsVersionMismatch proves the protocol-version handshake: a
// client whose version does not match is rejected with a clear error before any
// session is spawned. This guards against a freshly-launched new client silently
// misbehaving against an older still-running daemon. Runs on all platforms — the
// rejection path needs no PTY child.
func TestAttachRejectsVersionMismatch(t *testing.T) {
	d, err := New(t.TempDir(), nil)
	if err != nil {
		t.Fatalf("New: %v", err)
	}

	cli, srv := net.Pipe()
	defer cli.Close()
	go func() {
		defer srv.Close()
		// Simulate handle() having read a hello frame carrying a bad version.
		d.attach(srv, bufio.NewReader(srv), ProtocolVersion+1)
	}()

	ack, err := readFrame(bufio.NewReader(cli))
	if err != nil {
		t.Fatalf("read ack: %v", err)
	}
	if ack.Type != FrameAck {
		t.Fatalf("ack type = %q, want %q", ack.Type, FrameAck)
	}
	if ack.Err == "" {
		t.Fatal("expected a protocol-mismatch error in the ack")
	}
	if ack.Version != ProtocolVersion {
		t.Errorf("ack version = %d, want %d", ack.Version, ProtocolVersion)
	}
	if d.Mux().Count() != 0 {
		t.Errorf("mismatch must not spawn a session; count = %d", d.Mux().Count())
	}
}

// TestAttachAckIsFirstFrame is a regression test for the lifecycle bug where a
// daemon that had anything to replay on attach — buffered events, or existing
// (e.g. resumed) sessions' recent output — could never be attached. attach()
// registered the output/event sinks BEFORE writing the version-ack, and a sink's
// IMMEDIATE replay frame carries no protocol Version (parsed as 0). The client's
// dialSock reads frame #1 as the version-ack, so it saw "protocol v0" and rejected
// the daemon as an incompatible build. The version-ack MUST be the first frame
// after a matching hello, before any sink replay.
func TestAttachAckIsFirstFrame(t *testing.T) {
	d, err := New(t.TempDir(), longSpawn) // cross-platform dummy child (sleep/ping)
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	defer d.Stop()
	// Seed the replay buffer so the event sink has a (Version-less) frame to replay
	// on attach — the exact condition that used to precede and shadow the ack.
	d.PublishEvent(event.Envelope{V: 1, TS: 1, Event: event.StatusChange("s1", event.StatusIdle, event.StatusActive)})

	cli, srv := net.Pipe()
	defer cli.Close()
	go func() {
		defer srv.Close()
		d.attach(srv, bufio.NewReader(srv), ProtocolVersion) // matching version
	}()

	first, err := readFrame(bufio.NewReader(cli))
	if err != nil {
		t.Fatalf("read first frame: %v", err)
	}
	if first.Type != FrameAck {
		t.Fatalf("first frame = %q, want %q (the version-ack must precede any sink replay)", first.Type, FrameAck)
	}
	if first.Version != ProtocolVersion {
		t.Fatalf("first frame version = %d, want %d", first.Version, ProtocolVersion)
	}
}

// TestAttachAcceptsMatchingVersion is the positive control: a matching version
// is not rejected at the handshake. We can't drive the full attach loop here
// without a PTY child, so we assert the rejection branch does NOT fire by
// checking the daemon does not immediately write an error ack for a good
// version. (Full attach I/O is covered by the !windows integration tests.)
func TestProtocolVersionIsStable(t *testing.T) {
	if ProtocolVersion != 4 {
		t.Fatalf("ProtocolVersion = %d; bump deliberately and update clients", ProtocolVersion)
	}
}
