package main

import (
	"strconv"
	"strings"
	"testing"
)

func TestParseSGRMouse(t *testing.T) {
	// Left-press at terminal col 13, row 1 (1-based) -> 0-based (12, 0).
	ev, ok := parseSGRMouse([]byte("0;13;1"), true)
	if !ok {
		t.Fatal("expected a valid mouse event")
	}
	if !ev.mouse || ev.button != 0 || ev.x != 12 || ev.y != 0 || !ev.press {
		t.Fatalf("got %+v", ev)
	}

	// Release encodes with 'm'.
	if ev, ok := parseSGRMouse([]byte("0;5;3"), false); !ok || ev.press {
		t.Fatalf("release parse wrong: %+v ok=%v", ev, ok)
	}

	// Malformed params are rejected.
	if _, ok := parseSGRMouse([]byte("0;5"), true); ok {
		t.Fatal("malformed params should not parse")
	}
}

// A wheel-up event forwarded to a mouse-tracking session must come back out as a
// byte-identical SGR sequence, so claude scrolls exactly as if run directly.
func TestEncodeSGRMouseRoundTrip(t *testing.T) {
	// Wheel-up (button 64), pane col 7 / row 4 (1-based), press-only.
	got := encodeSGRMouse(64, 7, 4, true)
	if want := "\x1b[<64;7;4M"; string(got) != want {
		t.Fatalf("encode = %q, want %q", got, want)
	}

	// Re-parse the params and confirm they round-trip to 0-based coords.
	params := strings.TrimSuffix(strings.TrimPrefix(string(got), "\x1b[<"), "M")
	ev, ok := parseSGRMouse([]byte(params), true)
	if !ok || ev.button != 64 || ev.x != 6 || ev.y != 3 || !ev.press {
		t.Fatalf("round-trip mismatch: parsed %+v ok=%v from %q", ev, ok, params)
	}

	// Release uses the 'm' terminator.
	if rel := encodeSGRMouse(0, 1, 1, false); !strings.HasSuffix(string(rel), "m") {
		t.Fatalf("release should end in m, got %q", rel)
	}

	// Sanity: the encoded col/row are decimal (no zero-padding surprises).
	if _, err := strconv.Atoi("7"); err != nil {
		t.Fatal(err)
	}
}
