package main

import (
	"testing"
	"time"
)

// subTabClick returns a left-click inputEvent landing on the first session
// sub-tab's name. renderSubTabs lays out " ●␣" (3 cols) before the name, so
// x=4, y=rowSubTabs(1) reliably hits the "sess-a" label.
func subTabClick(press bool) inputEvent {
	return inputEvent{mouse: true, x: 4, y: 1, button: 0, press: press}
}

// TestDoubleClickOpensDraftAndSurvivesButtonRelease is the regression test for
// the rename-flow bug: in xterm mouse mode a double-click is delivered as
// press,release,press,release. The second press opens the inline rename draft;
// the trailing button RELEASE must NOT commit (and thereby close) that draft, or
// the user never gets a chance to type. Before the fix, handleMouse committed on
// every mouse event, so the draft vanished on that release.
func TestDoubleClickOpensDraftAndSurvivesButtonRelease(t *testing.T) {
	comp := newTestCompositor()
	comp.Render() // populate the click spans so the hit-test resolves
	var ct clickTracker
	base := time.Unix(100, 0)

	// First click: press focuses the sub-tab, release is inert. No draft yet.
	if eff := ct.handleMouse(comp, subTabClick(true), base); eff.focusID != "a" {
		t.Fatalf("first press focusID = %q, want \"a\"", eff.focusID)
	}
	ct.handleMouse(comp, subTabClick(false), base.Add(1*time.Millisecond))
	if comp.Editing() {
		t.Fatal("a single click should not open a rename draft")
	}

	// Second click within the window: press opens the draft instead of focusing.
	if eff := ct.handleMouse(comp, subTabClick(true), base.Add(120*time.Millisecond)); eff.focusID != "" {
		t.Fatalf("second press should open a draft, not re-focus (focusID=%q)", eff.focusID)
	}
	if !comp.Editing() {
		t.Fatal("double-click should open the rename draft")
	}

	// The RELEASE of that same second click must not commit the draft.
	if eff := ct.handleMouse(comp, subTabClick(false), base.Add(121*time.Millisecond)); eff.renameID != "" {
		t.Fatalf("button-release of the opening double-click must not commit (got rename id %q)", eff.renameID)
	}
	if !comp.Editing() {
		t.Fatal("the draft must stay open after the double-click's button release — this is the bug")
	}
}

// TestRenameDraftTakesTypingThenCommitsOnClickElsewhere proves the full path
// after a double-click opens the draft: typed bytes edit the draft, and a later
// genuine click elsewhere (a press) blurs and commits the edited name — the
// terminal analogue of the desktop <input>'s onBlur.
func TestRenameDraftTakesTypingThenCommitsOnClickElsewhere(t *testing.T) {
	comp := newTestCompositor()
	comp.Render()
	var ct clickTracker
	base := time.Unix(200, 0)

	// Double-click to open the draft (seeded with "sess-a").
	ct.handleMouse(comp, subTabClick(true), base)
	ct.handleMouse(comp, subTabClick(false), base.Add(1*time.Millisecond))
	ct.handleMouse(comp, subTabClick(true), base.Add(100*time.Millisecond))
	ct.handleMouse(comp, subTabClick(false), base.Add(101*time.Millisecond))
	if !comp.Editing() {
		t.Fatal("setup: double-click should have opened the draft")
	}

	// Type a fresh name: clear "sess-a" then enter "renamed".
	for i := 0; i < len("sess-a"); i++ {
		comp.RenameInput(0x7f) // Backspace
	}
	for _, b := range []byte("renamed") {
		comp.RenameInput(b)
	}

	// A click elsewhere (the "+ new" affordance is far to the right) is a press,
	// so it blurs the draft and commits the edited name.
	elsewhere := inputEvent{mouse: true, x: 60, y: 1, button: 0, press: true}
	eff := ct.handleMouse(comp, elsewhere, base.Add(2*time.Second))
	if eff.renameID != "a" || eff.renameName != "renamed" {
		t.Fatalf("blur-commit = (%q, %q), want (\"a\", \"renamed\")", eff.renameID, eff.renameName)
	}
	if comp.Editing() {
		t.Fatal("committing on click-elsewhere should close the draft")
	}
}

// TestTwoSlowClicksJustFocus guards the non-double-click path: two left-clicks
// on the same sub-tab spaced beyond the window must both focus and never open a
// rename draft.
func TestTwoSlowClicksJustFocus(t *testing.T) {
	comp := newTestCompositor()
	comp.Render()
	var ct clickTracker
	base := time.Unix(300, 0)

	if eff := ct.handleMouse(comp, subTabClick(true), base); eff.focusID != "a" {
		t.Fatalf("first click focusID = %q, want \"a\"", eff.focusID)
	}
	// Second press well past dblClickWindow -> still a focus, not a rename.
	if eff := ct.handleMouse(comp, subTabClick(true), base.Add(2*dblClickWindow)); eff.focusID != "a" {
		t.Fatalf("slow second click focusID = %q, want \"a\" (not a rename)", eff.focusID)
	}
	if comp.Editing() {
		t.Fatal("two slow clicks should never open a rename draft")
	}
}
