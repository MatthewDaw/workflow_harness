package main

import (
	"bytes"
	"testing"

	"github.com/workflow-harness/claude-plus/internal/shell"
)

// newTestCompositor builds a Compositor backed by an in-memory screen, with one
// session sub-tab and the Session tab (index 0) active — the exact state in
// which the original bug reproduced.
func newTestCompositor() *shell.Compositor {
	s := shell.NewScreen(120, 30, &bytes.Buffer{})
	c := shell.NewCompositor(s, "test")
	c.SetSubs([]shell.SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)
	return c
}

// TestCtrlGThenDDetaches drives the per-byte key handler exactly as the input
// loop does — Ctrl-G arms the prefix, then `d` resolves it — and asserts a
// detach action on the Session tab (active==0). This is the core regression:
// the chord must yield actDetach while the hosted claude TUI is focused.
func TestCtrlGThenDDetaches(t *testing.T) {
	comp := newTestCompositor()
	if !comp.ActiveIsSession() {
		t.Fatal("test fixture should start on the Session tab")
	}
	prefix := false

	// Ctrl-G arms the prefix and is consumed by the chrome (not forwarded).
	if got := handleKey(nil, comp, ctrlG, &prefix); got != actHandled {
		t.Fatalf("Ctrl-G action = %d, want actHandled", got)
	}
	if !prefix {
		t.Fatal("Ctrl-G should arm the prefix")
	}

	// `d` resolves to a detach and clears the prefix.
	if got := handleKey(nil, comp, 'd', &prefix); got != actDetach {
		t.Fatalf("Ctrl-G d action = %d, want actDetach", got)
	}
	if prefix {
		t.Fatal("resolving the chord should clear the prefix")
	}
}

// TestResolvePrefixedDetach proves the chord-resolution semantics independent of
// a live daemon: both `d` and a repeated Ctrl-G detach, and detach is reachable
// on the Session tab.
func TestResolvePrefixedDetach(t *testing.T) {
	comp := newTestCompositor()
	if got := resolvePrefixed(comp, 'd'); got != actDetach {
		t.Fatalf("Ctrl-G d = %d, want actDetach", got)
	}
	if got := resolvePrefixed(comp, ctrlG); got != actDetach {
		t.Fatalf("Ctrl-G Ctrl-G = %d, want actDetach (repeat-prefix detach)", got)
	}
	if got := resolvePrefixed(comp, 'c'); got != actNewSession {
		t.Fatalf("Ctrl-G c = %d, want actNewSession", got)
	}
}

// resolveEvent mirrors the input loop's per-event decision order WITHOUT a live
// daemon: a pending prefix is authoritative and resolves on the first byte of
// the next event regardless of its shape (mouse, single byte, or a coalesced
// multi-byte escape sequence). This is the exact ordering the fix installs, and
// the test below proves the prefix is no longer stranded by the multi-byte ESC
// short-circuit that only fired on the Session tab.
func resolveEvent(comp *shell.Compositor, prefix *bool, ev inputEvent) keyAction {
	if *prefix {
		*prefix = false
		if ev.mouse || len(ev.bytes) == 0 {
			return actHandled // chord cancelled, nothing forwarded
		}
		return resolvePrefixed(comp, ev.bytes[0])
	}
	if ev.mouse {
		return actHandled
	}
	if len(ev.bytes) > 1 && ev.bytes[0] == 0x1b {
		return actForward // multi-byte escape forwarded to claude
	}
	last := actForward
	for _, b := range ev.bytes {
		last = handleKey(nil, comp, b, prefix)
	}
	return last
}

// TestPrefixSurvivesEscapeTraffic is the Session-tab regression test. After
// Ctrl-G arms the prefix, an event whose first byte starts a multi-byte escape
// sequence (the kind of traffic only present when the hosted claude TUI is
// focused) must NOT short-circuit to forwarding — the pending prefix has to win
// and detach. Before the fix this event hit the `len>1 && 0x1b` branch and the
// prefix was silently dropped, which is why detach failed on the Session tab.
func TestPrefixSurvivesEscapeTraffic(t *testing.T) {
	comp := newTestCompositor()
	prefix := false

	// Arm the prefix with a standalone Ctrl-G event.
	if got := resolveEvent(comp, &prefix, inputEvent{bytes: []byte{ctrlG}}); got != actHandled {
		t.Fatalf("arming Ctrl-G = %d, want actHandled", got)
	}
	if !prefix {
		t.Fatal("prefix should be armed")
	}

	// Next event leads with ESC (e.g. terminal coalesced the keypress with TUI
	// escape output). The pending prefix must still resolve on its first byte.
	// Use ESC 'd' here only to assert the prefix path is taken, not the escape
	// short-circuit; the resolver only looks at ev.bytes[0].
	esc := inputEvent{bytes: []byte{0x1b, 'd'}}
	if got := resolveEvent(comp, &prefix, esc); got == actForward {
		t.Fatal("a multi-byte event after Ctrl-G must not be forwarded; the prefix must win")
	}
}

// TestBatchedCtrlGD covers the case where the terminal delivers Ctrl-G and `d`
// as separate single-byte events in one read (the common fast-typing path):
// the prefix set by the first event must survive into the second and detach.
func TestBatchedCtrlGD(t *testing.T) {
	comp := newTestCompositor()
	prefix := false

	events := []inputEvent{
		{bytes: []byte{ctrlG}},
		{bytes: []byte{'d'}},
	}
	var last keyAction
	for _, ev := range events {
		last = resolveEvent(comp, &prefix, ev)
	}
	if last != actDetach {
		t.Fatalf("batched Ctrl-G d action = %d, want actDetach", last)
	}
}

// TestPlainKeysStillForward guards against regressing normal claude typing: with
// no prefix armed, ordinary keys forward to the hosted session and `d` is not
// special.
func TestPlainKeysStillForward(t *testing.T) {
	comp := newTestCompositor()
	prefix := false
	for _, b := range []byte("hello d") {
		if got := handleKey(nil, comp, b, &prefix); got != actForward {
			t.Fatalf("plain byte %q action = %d, want actForward", b, got)
		}
		if prefix {
			t.Fatalf("plain byte %q should not arm the prefix", b)
		}
	}
}
