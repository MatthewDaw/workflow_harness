package main

import "testing"

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
