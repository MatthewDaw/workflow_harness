package transport

import (
	"fmt"
	"testing"

	"github.com/gorilla/websocket"
)

// TestIsAuthRejection distinguishes a permanent token rejection (401/403 on the
// WS handshake) from transient network/server errors. Only the former means
// retrying will never succeed until the credentials change.
func TestIsAuthRejection(t *testing.T) {
	if !IsAuthRejection(&HandshakeError{Status: 403}) {
		t.Fatal("403 must be an auth rejection")
	}
	if !IsAuthRejection(&HandshakeError{Status: 401}) {
		t.Fatal("401 must be an auth rejection")
	}
	if IsAuthRejection(&HandshakeError{Status: 502}) {
		t.Fatal("502 is a transient server error, not an auth rejection")
	}
	if IsAuthRejection(fmt.Errorf("dial tcp: i/o timeout")) {
		t.Fatal("a plain network error is not an auth rejection")
	}
}

// TestRunSurfacesAuthRejection proves the connection manager no longer fails
// silently on a rejected device token: a 403 handshake is reported through the
// onDialError hook (the seam the default logger hangs on) instead of being
// swallowed by the backoff loop.
func TestRunSurfacesAuthRejection(t *testing.T) {
	buf := newBuf(t)
	c := NewClient("wss://hq.example", "stale-token", "inst", buf, nil)

	got := make(chan error, 1)
	c.onDialError = func(err error) {
		select {
		case got <- err:
		default:
		}
		c.Stop() // break the retry loop so Run returns
	}
	c.dial = func(string, string, string) (*websocket.Conn, error) {
		return nil, &HandshakeError{Status: 403}
	}

	c.Run()

	select {
	case err := <-got:
		if !IsAuthRejection(err) {
			t.Fatalf("expected an auth rejection to be surfaced, got %v", err)
		}
	default:
		t.Fatal("Run swallowed the 403 dial error instead of surfacing it")
	}
}
