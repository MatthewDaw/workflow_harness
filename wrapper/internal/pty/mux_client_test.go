package pty

import "testing"

// newTestMux builds a mux with bare sessions (no real PTY) for exercising the
// per-client focus + sizing logic. Session.Resize is nil-PTY-safe, so recompute
// records cols/rows on each Session without touching a pseudo-terminal.
func newTestMux(ids ...string) (*Mux, map[string]*Session) {
	m := NewMux("", 80, 24, nil)
	byID := map[string]*Session{}
	for _, id := range ids {
		s := &Session{ID: id, Name: id}
		m.sessions = append(m.sessions, s)
		byID[id] = s
	}
	return m, byID
}

func focusedID(views []SessionView) string {
	for _, v := range views {
		if v.Focused {
			return v.ID
		}
	}
	return ""
}

func TestPerClientFocusIsIndependent(t *testing.T) {
	m, _ := newTestMux("s1", "s2")
	m.RegisterClient("A", 100, 50)
	m.RegisterClient("B", 80, 24)

	if err := m.SetClientFocus("A", "s1"); err != nil {
		t.Fatal(err)
	}
	if err := m.SetClientFocus("B", "s2"); err != nil {
		t.Fatal(err)
	}

	if got := focusedID(m.ListFor("A")); got != "s1" {
		t.Errorf("A focus = %q, want s1", got)
	}
	if got := focusedID(m.ListFor("B")); got != "s2" {
		t.Errorf("B focus = %q, want s2", got)
	}
}

func TestDifferentSessionsKeepFullSize(t *testing.T) {
	m, byID := newTestMux("s1", "s2")
	m.RegisterClient("A", 100, 50)
	m.RegisterClient("B", 80, 24)
	_ = m.SetClientFocus("A", "s1")
	_ = m.SetClientFocus("B", "s2")

	if byID["s1"].cols != 100 || byID["s1"].rows != 50 {
		t.Errorf("s1 size = %dx%d, want 100x50", byID["s1"].cols, byID["s1"].rows)
	}
	if byID["s2"].cols != 80 || byID["s2"].rows != 24 {
		t.Errorf("s2 size = %dx%d, want 80x24", byID["s2"].cols, byID["s2"].rows)
	}
}

func TestSharedSessionUsesSmaller(t *testing.T) {
	m, byID := newTestMux("s1", "s2")
	m.RegisterClient("A", 100, 50)
	m.RegisterClient("B", 80, 24)
	_ = m.SetClientFocus("A", "s1")
	_ = m.SetClientFocus("B", "s1") // both now share s1

	if byID["s1"].cols != 80 || byID["s1"].rows != 24 {
		t.Errorf("shared s1 size = %dx%d, want smaller 80x24", byID["s1"].cols, byID["s1"].rows)
	}
}

func TestUnregisterRegrowsSession(t *testing.T) {
	m, byID := newTestMux("s1")
	m.RegisterClient("A", 100, 50)
	m.RegisterClient("B", 80, 24)
	_ = m.SetClientFocus("A", "s1")
	_ = m.SetClientFocus("B", "s1") // shared -> 80x24
	if byID["s1"].cols != 80 {
		t.Fatalf("precondition: shared s1 cols = %d, want 80", byID["s1"].cols)
	}

	m.UnregisterClient("B") // only A (100x50) remains on s1
	if byID["s1"].cols != 100 || byID["s1"].rows != 50 {
		t.Errorf("after B left, s1 size = %dx%d, want 100x50", byID["s1"].cols, byID["s1"].rows)
	}
}

func TestSetClientSizeReshrinksFocusedSession(t *testing.T) {
	m, byID := newTestMux("s1")
	m.RegisterClient("A", 100, 50)
	_ = m.SetClientFocus("A", "s1")
	if byID["s1"].cols != 100 {
		t.Fatalf("precondition: s1 cols = %d, want 100", byID["s1"].cols)
	}
	m.SetClientSize("A", 60, 20)
	if byID["s1"].cols != 60 || byID["s1"].rows != 20 {
		t.Errorf("after resize, s1 = %dx%d, want 60x20", byID["s1"].cols, byID["s1"].rows)
	}
}

func TestSessionExitRepointsClientFocus(t *testing.T) {
	m, _ := newTestMux("s1", "s2")
	m.RegisterClient("A", 80, 24)
	_ = m.SetClientFocus("A", "s2")

	m.onSessionExit("s2") // s2's child died

	if got := m.clients["A"].focus; got != "s1" {
		t.Errorf("after s2 exit, A focus = %q, want s1 (neighbor)", got)
	}
}

func TestWriteForClientNoFocusErrors(t *testing.T) {
	m, _ := newTestMux()
	m.RegisterClient("A", 80, 24) // no sessions, so focus is ""
	if _, err := m.WriteForClient("A", []byte("x")); err == nil {
		t.Error("expected error writing with no focused session")
	}
}
