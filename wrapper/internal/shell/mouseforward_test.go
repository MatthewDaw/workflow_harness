package shell

import (
	"bytes"
	"testing"
)

// TestBodyMouseMapsRegion proves chrome rows (tab bar, sub-tab row, status line)
// are excluded from the body while body rows map to 1-based, pane-relative
// coordinates — the gate that decides whether a mouse event is forwarded to the
// hosted session or handled by the chrome.
func TestBodyMouseMapsRegion(t *testing.T) {
	var buf bytes.Buffer
	c := NewCompositor(NewScreen(120, 30, &buf), "test")

	// Tab bar and sub-tab row are chrome, not body.
	if _, _, in := c.BodyMouse(5, rowTabBar); in {
		t.Fatal("tab bar should not be in the body")
	}
	if _, _, in := c.BodyMouse(5, rowSubTabs); in {
		t.Fatal("sub-tab row should not be in the body")
	}
	// Last row is the status line.
	if _, _, in := c.BodyMouse(5, 29); in {
		t.Fatal("status line should not be in the body")
	}
	// First body row: screen col passes through (+1), row shifts up by bodyTop.
	col, row, in := c.BodyMouse(0, bodyTop)
	if !in || col != 1 || row != 1 {
		t.Fatalf("body origin mapped to col=%d row=%d in=%v, want 1,1,true", col, row, in)
	}
	col, row, in = c.BodyMouse(9, bodyTop+3)
	if !in || col != 10 || row != 4 {
		t.Fatalf("body (9, bodyTop+3) -> col=%d row=%d in=%v, want 10,4,true", col, row, in)
	}
}

// TestFocusedMouseTracking proves the chrome only forwards mouse events once the
// hosted session has actually enabled an xterm mouse mode (as claude does on
// startup) — before that, the wheel falls back to the chrome's own handling.
func TestFocusedMouseTracking(t *testing.T) {
	var buf bytes.Buffer
	c := NewCompositor(NewScreen(120, 30, &buf), "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)
	p := c.EnsurePane("a")

	if c.FocusedMouseTracking() {
		t.Fatal("a fresh session has no mouse tracking yet")
	}
	// claude enables SGR mouse tracking; the chrome must now forward.
	p.Write([]byte("\x1b[?1000h\x1b[?1006h"))
	if !c.FocusedMouseTracking() {
		t.Fatal("expected mouse tracking after the session enabled it")
	}
}
