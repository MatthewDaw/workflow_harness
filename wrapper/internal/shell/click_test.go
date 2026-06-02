package shell

import (
	"bytes"
	"testing"
)

// TestClickSwitchesTab proves a mouse click on the tab bar switches tabs and a
// click on the sub-tab row reports the session to focus — the wiring behind
// clickable tabs.
func TestClickSwitchesTab(t *testing.T) {
	var buf bytes.Buffer
	s := NewScreen(120, 30, &buf)
	c := NewCompositor(s, "test")
	c.SetSubs([]SubTab{{ID: "a", Name: "sess-a", Status: "active"}}, 0)
	c.Render() // populates the click spans

	// Tab bar: " Session "(0..8) gap " Tickets "(10..18) ...
	changed, _ := c.Click(12, rowTabBar) // inside "Tickets"
	if !changed {
		t.Fatal("clicking the Tickets tab should change the active tab")
	}
	if c.active != 1 {
		t.Fatalf("active tab = %d, want 1 (Tickets)", c.active)
	}

	// Clicking a sub-tab reports its session id for the caller to focus.
	c.SetTab(0)
	c.Render()
	if _, sess := c.Click(2, rowSubTabs); sess != "a" {
		t.Fatalf("sub-tab click session = %q, want \"a\"", sess)
	}

	// A click in the body region is not a chrome action.
	if changed, sess := c.Click(40, bodyTop+3); changed || sess != "" {
		t.Fatalf("body click should be inert, got changed=%v sess=%q", changed, sess)
	}
}
