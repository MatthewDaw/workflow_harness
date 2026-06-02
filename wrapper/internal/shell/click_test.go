package shell

import (
	"bytes"
	"testing"
)

// TestClickSwitchesTab proves a mouse click on the tab bar switches tabs, a
// click on a session sub-tab reports the session to focus, and a click on
// "+ new" reports a new-session request — the wiring behind clickable tabs.
func TestClickSwitchesTab(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)
	c.Render() // populates the click spans

	// Tab bar: " Session "(0..8) gap " Tickets "(10..18) ...
	changed, _, _ := c.Click(12, rowTabBar) // inside "Tickets"
	if !changed {
		t.Fatal("clicking the Tickets tab should change the active tab")
	}
	if c.active != 1 {
		t.Fatalf("active tab = %d, want 1 (Tickets)", c.active)
	}

	// Clicking a sub-tab reports its session id for the caller to focus.
	c.SetTab(0)
	c.Render()
	if _, sess, _ := c.Click(2, rowSubTabs); sess != "a" {
		t.Fatalf("sub-tab click session = %q, want \"a\"", sess)
	}

	// Clicking "+ new" requests a new session. It sits just past the sub-tab.
	mid := (c.newSpan.lo + c.newSpan.hi) / 2
	if _, _, newSess := c.Click(mid, rowSubTabs); !newSess {
		t.Fatalf("clicking + new (x=%d, span=%v) should request a new session", mid, c.newSpan)
	}

	// A click in the body region is inert.
	if changed, sess, newSess := c.Click(40, bodyTop+3); changed || sess != "" || newSess {
		t.Fatalf("body click should be inert, got changed=%v sess=%q new=%v", changed, sess, newSess)
	}
}
