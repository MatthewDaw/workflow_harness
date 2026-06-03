package shell

import (
	"bytes"
	"strings"
	"testing"
)

// TestClickSwitchesTab proves a mouse click on the tab bar switches tabs, a
// click on a session sub-tab reports the session to focus, a click on its ✕
// reports a close, and a click on "+ new" reports a new-session request — the
// wiring behind clickable tabs.
func TestClickSwitchesTab(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)
	c.Render() // populates the click spans

	// Tab bar: " Session "(0..8) gap " Agents "(9..16) ...
	if r := c.Click(12, rowTabBar); !r.Changed { // inside "Agents"
		t.Fatal("clicking the Agents tab should change the active tab")
	}
	if c.active != 1 {
		t.Fatalf("active tab = %d, want 1 (Agents)", c.active)
	}

	// Clicking a sub-tab (on its name) reports its session id to focus.
	c.SetTab(0)
	c.Render()
	if r := c.Click(c.subSpans[0].lo, rowSubTabs); r.FocusSessID != "a" {
		t.Fatalf("sub-tab click FocusSessID = %q, want \"a\"", r.FocusSessID)
	}

	// Clicking the ✕ reports a close for that session, not a focus.
	if len(c.closeSpans) != 1 {
		t.Fatalf("closeSpans = %d, want 1", len(c.closeSpans))
	}
	if r := c.Click(c.closeSpans[0].lo, rowSubTabs); r.CloseSessID != "a" {
		t.Fatalf("✕ click CloseSessID = %q, want \"a\"", r.CloseSessID)
	}

	// Clicking "+ new" requests a new session. It sits just past the sub-tab.
	mid := (c.newSpan.lo + c.newSpan.hi) / 2
	if r := c.Click(mid, rowSubTabs); !r.NewSession {
		t.Fatalf("clicking + new (x=%d, span=%v) should request a new session", mid, c.newSpan)
	}

	// A click in the body region is inert.
	if r := c.Click(40, bodyTop+3); (r != ClickResult{}) {
		t.Fatalf("body click should be inert, got %+v", r)
	}
}

// TestRenderShowsClosePerSession asserts an always-visible ✕ cell is drawn for
// each session sub-tab.
func TestRenderShowsClosePerSession(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{
		{ID: "a", Name: "sess-a", Status: "active"},
		{ID: "b", Name: "sess-b", Status: "idle"},
	}, 0)
	c.Render()

	if len(c.closeSpans) != 2 {
		t.Fatalf("closeSpans = %d, want 2 (one ✕ per session)", len(c.closeSpans))
	}
	for i, sp := range c.closeSpans {
		if got := s.cur[rowSubTabs][sp.lo].Ch; got != '✕' {
			t.Fatalf("session %d: cell at ✕ span lo=%d = %q, want '✕'", i, sp.lo, got)
		}
	}
}

// TestRenameEditState exercises the inline rename draft lifecycle: begin seeds
// from the current name, input edits it, commit returns the trimmed result, and
// cancel discards.
func TestRenameEditState(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "old", Status: "active"}}, 0)

	if c.Editing() {
		t.Fatal("should not be editing before BeginRename")
	}
	c.BeginRename("a")
	if !c.Editing() {
		t.Fatal("BeginRename should open a draft")
	}
	if c.draft != "old" {
		t.Fatalf("draft seeded = %q, want \"old\"", c.draft)
	}

	// Backspace twice removes "ld", then type "k " -> "ok ".
	c.RenameInput(0x7f)
	c.RenameInput(0x08)
	c.RenameInput('k')
	c.RenameInput(' ')
	if c.draft != "ok " {
		t.Fatalf("draft after edits = %q, want \"ok \"", c.draft)
	}

	// While editing, the ✕ for that row is hidden.
	c.Render()
	if len(c.closeSpans) != 0 {
		t.Fatalf("closeSpans while editing = %d, want 0 (✕ hidden)", len(c.closeSpans))
	}

	id, name, ok := c.CommitRename()
	if !ok || id != "a" || name != "ok" { // trailing space trimmed
		t.Fatalf("CommitRename = (%q, %q, %v), want (\"a\", \"ok\", true)", id, name, ok)
	}
	if c.Editing() {
		t.Fatal("CommitRename should clear editing")
	}

	// An empty (whitespace-only) draft commits as not-ok and clears editing.
	c.BeginRename("a")
	for i := 0; i < len("old"); i++ {
		c.RenameInput(0x7f)
	}
	if _, _, ok := c.CommitRename(); ok {
		t.Fatal("committing an empty draft should return ok=false")
	}

	// CancelRename discards the draft and clears editing.
	c.BeginRename("a")
	c.RenameInput('z')
	c.CancelRename()
	if c.Editing() {
		t.Fatal("CancelRename should clear editing")
	}
}

// TestScrollbackViewport feeds enough lines to evict some into history, then
// asserts Pane.History() is non-empty and that ScrollUp changes the rendered
// top row of the body.
func TestScrollbackViewport(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(40, 12, &buf) // small body so output scrolls quickly
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)

	// Feed many numbered lines so the live grid scrolls and evicts history.
	var out strings.Builder
	for i := 0; i < 60; i++ {
		out.WriteString("line")
		out.WriteString(itoa(i))
		out.WriteString("\r\n")
	}
	c.FeedOutput("a", []byte(out.String()))

	p := c.EnsurePane("a")
	if len(p.History()) == 0 {
		t.Fatal("expected evicted lines in Pane.History() after scrolling output")
	}

	// Capture the body's top row at live, then after scrolling up.
	c.Render()
	liveTop := rowString(s, bodyTop)

	c.ScrollUp(5)
	if c.scrollOff == 0 {
		t.Fatal("ScrollUp should move the viewport off the live bottom")
	}
	c.Render()
	scrolledTop := rowString(s, bodyTop)
	if scrolledTop == liveTop {
		t.Fatalf("ScrollUp should change the top body row; both = %q", scrolledTop)
	}

	// ScrollToBottom returns to live.
	c.ScrollToBottom()
	if c.scrollOff != 0 {
		t.Fatalf("ScrollToBottom should pin to live, scrollOff = %d", c.scrollOff)
	}
	c.Render()
	if got := rowString(s, bodyTop); got != liveTop {
		t.Fatalf("after ScrollToBottom top = %q, want live %q", got, liveTop)
	}
}

// rowString reads back a rendered screen row as a trimmed string.
func rowString(s *Screen, y int) string {
	var b strings.Builder
	for x := 0; x < s.w; x++ {
		b.WriteRune(s.cur[y][x].Ch)
	}
	return strings.TrimRight(b.String(), " ")
}

// itoa is a tiny base-10 formatter to avoid importing strconv in the test.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var d []byte
	for n > 0 {
		d = append([]byte{byte('0' + n%10)}, d...)
		n /= 10
	}
	return string(d)
}
